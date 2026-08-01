"""Operator-triggered provider and model connectivity checks.

Two levels, because they fail independently:

- **instance**: can we reach the gateway with these credentials at all
  (``/v1/models``). Answers "is the key/base_url right".
- **model**: does this specific model actually answer. A compatible gateway
  routinely lists models it cannot serve, so listing the catalog proves
  nothing about any single ID.

The model check deliberately calls the SAME endpoint the production path uses
(Responses for the OpenAI line, Messages for Anthropic, ``/rerank`` for
rerankers). Probing ``/v1/chat/completions`` instead would pass on a gateway
that never implements ``/v1/responses`` and then fail in real traffic.

Every failure is reported as an allowlisted, secret-free diagnostic reused from
``providers.classify_provider_exception``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from nicekit.core.config import get_settings
from nicekit.core.secretbox import get_secret_box
from nicekit.domain.model_catalog import ModelCapability
from nicekit.llm.model_catalog import provider_metadata_for_read
from nicekit.llm.providers import classify_provider_exception
from nicekit.models.llm_provider import LlmProvider

# Long enough to survive a cold upstream, short enough that an operator waiting
# on the button does not think the page hung.
INSTANCE_TIMEOUT_SECONDS = 15.0
MODEL_TIMEOUT_SECONDS = 30.0

_PROBE_TEXT = "TravelFlow connectivity check"


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    ok: bool
    latency_ms: float | None
    # ``None`` on success; otherwise an allowlisted diagnostic such as
    # ``http_error;status=404`` or a local precondition code.
    error_code: str | None
    # Instance level only: how many models the gateway listed.
    model_count: int | None = None
    # Model level only: which capability decided the probe shape.
    probed_capability: ModelCapability | None = None


def _credentials(
    row: LlmProvider,
    env: tuple[str, str, str] | None,
) -> tuple[str, str]:
    """Row credentials with the built-in ``.env`` line as fallback.

    ``env`` is the ``(protocol, api_key, base_url)`` triple that
    ``service.env_provider_credentials()`` returns, taken verbatim so a caller
    cannot mis-slice it.
    """
    api_key = get_secret_box().decrypt(row.api_key) if row.api_key else (env[1] if env else "")
    base_url = row.base_url or (env[2] if env else "")
    return api_key, base_url


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _close(client: object) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        await close()


async def check_instance(
    row: LlmProvider,
    *,
    env: tuple[str, str, str] | None = None,
    timeout_seconds: float = INSTANCE_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    """List the remote catalog to prove the credentials and network path work."""
    api_key, base_url = _credentials(row, env)
    if not api_key:
        return ConnectivityResult(
            ok=False,
            latency_ms=None,
            error_code="api_key_missing",
        )

    client: object | None = None
    started = time.perf_counter()
    try:
        if row.protocol == "anthropic":
            client = AsyncAnthropic(api_key=api_key, base_url=base_url or None)
        else:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        page = await client.models.list(timeout=timeout_seconds)  # type: ignore[union-attr]
        return ConnectivityResult(
            ok=True,
            latency_ms=_elapsed_ms(started),
            error_code=None,
            model_count=len(list(page.data)),
        )
    except Exception as exc:  # noqa: BLE001 - classified into a safe code below
        return ConnectivityResult(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code=_annotate_base_url_hint(
                classify_provider_exception(exc).diagnostic, base_url
            ),
        )
    finally:
        if client is not None:
            await _close(client)


def _annotate_base_url_hint(diagnostic: str, base_url: str) -> str:
    """404 且 base_url 看着没带版本路径时,把这条猜测附在诊断码后面。

    OpenAI SDK 把 base_url 当作完整前缀直接拼 `/models`,所以填
    `https://api.example.com` 而不是 `.../v1` 时,请求打到 `/models` 必然 404。
    这是配置面最高频的一次性错误,而 "上游返回错误(HTTP 404)" 本身
    对定位毫无帮助——运维只能去翻网关文档或抓包。诊断码是安全白名单里的
    值,这里只追加固定后缀,不带入任何上游原文。
    """
    if "status=404" not in diagnostic:
        return diagnostic
    tail = base_url.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    # v1 / v1beta / openai/v1 之类都算"带了版本段";空 base_url 走官方端点不提示
    if not base_url.strip() or tail.startswith("v") and any(c.isdigit() for c in tail):
        return diagnostic
    return f"{diagnostic};hint=base_url_missing_version_path"


def _probe_capability(row: LlmProvider, model: str) -> ModelCapability | None:
    """Pick the capability whose call shape we should use.

    Order matters: a purpose-exclusive capability wins, because an embedding or
    rerank endpoint cannot answer a chat request at all.
    """
    metadata = provider_metadata_for_read(row).get(model)
    if metadata is None:
        return None
    capabilities = set(metadata.capabilities)
    for capability in ("rerank", "embedding", "generation"):
        if capability in capabilities:
            return capability  # type: ignore[return-value]
    return None


async def _probe_embedding(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
) -> None:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    try:
        await client.embeddings.create(
            model=model,
            input=[_PROBE_TEXT],
            timeout=timeout_seconds,
        )
    finally:
        await client.close()


async def _probe_rerank(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
) -> None:
    endpoint = f"{base_url.strip().rstrip('/')}/rerank"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "query": _PROBE_TEXT,
                "documents": [_PROBE_TEXT],
                "top_n": 1,
                "return_documents": False,
            },
        )
        response.raise_for_status()


async def _probe_generation(
    *,
    protocol: str,
    instance_name: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
) -> None:
    if protocol == "anthropic":
        anthropic_client = AsyncAnthropic(api_key=api_key, base_url=base_url or None)
        try:
            await anthropic_client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": _PROBE_TEXT}],
                timeout=timeout_seconds,
            )
        finally:
            await anthropic_client.close()
        return

    # Responses, not chat.completions — this is the endpoint production uses.
    openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    try:
        request = {
            "model": model,
            "instructions": "You are a connectivity checker.",
            "input": _PROBE_TEXT,
            "timeout": timeout_seconds,
        }
        if instance_name not in get_settings().llm_openai_omit_max_output_tokens:
            request["max_output_tokens"] = 16
        await openai_client.responses.create(**request)
    finally:
        await openai_client.close()


async def check_model(
    row: LlmProvider,
    model: str,
    *,
    env: tuple[str, str, str] | None = None,
    timeout_seconds: float = MODEL_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    """Send one minimal real request shaped by the model's declared capability."""
    if model not in set(row.models or []):
        return ConnectivityResult(
            ok=False,
            latency_ms=None,
            error_code="model_not_in_inventory",
        )
    capability = _probe_capability(row, model)
    if capability is None:
        # An unclassified model has no known call shape. Guessing chat would
        # report a bogus failure for an embedding endpoint, so ask for a label.
        return ConnectivityResult(
            ok=False,
            latency_ms=None,
            error_code="capability_unclassified",
        )

    api_key, base_url = _credentials(row, env)
    if not api_key:
        return ConnectivityResult(
            ok=False,
            latency_ms=None,
            error_code="api_key_missing",
            probed_capability=capability,
        )
    if capability == "rerank" and not base_url:
        # Rerank has no official SDK default host to fall back on.
        return ConnectivityResult(
            ok=False,
            latency_ms=None,
            error_code="base_url_missing",
            probed_capability=capability,
        )

    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            if capability == "embedding":
                await _probe_embedding(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout_seconds=timeout_seconds,
                )
            elif capability == "rerank":
                await _probe_rerank(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout_seconds=timeout_seconds,
                )
            else:
                await _probe_generation(
                    protocol=row.protocol,
                    instance_name=row.name,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout_seconds=timeout_seconds,
                )
    except TimeoutError:
        return ConnectivityResult(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code="timeout",
            probed_capability=capability,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return ConnectivityResult(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code=f"http_error;status={status}" if 100 <= status <= 599 else "http_error",
            probed_capability=capability,
        )
    except httpx.HTTPError:
        return ConnectivityResult(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code="connection_error",
            probed_capability=capability,
        )
    except Exception as exc:  # noqa: BLE001 - classified into a safe code below
        return ConnectivityResult(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error_code=classify_provider_exception(exc).diagnostic,
            probed_capability=capability,
        )

    return ConnectivityResult(
        ok=True,
        latency_ms=_elapsed_ms(started),
        error_code=None,
        probed_capability=capability,
    )


__all__ = [
    "INSTANCE_TIMEOUT_SECONDS",
    "MODEL_TIMEOUT_SECONDS",
    "ConnectivityResult",
    "check_instance",
    "check_model",
]
