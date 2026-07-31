"""Connectivity checks; no request ever leaves the process."""

from uuid import uuid4

import httpx
import openai
import pytest

import nicekit.llm.connectivity as connectivity
from nicekit.llm.connectivity import check_instance, check_model
from nicekit.models.llm_provider import LlmProvider


def _provider(
    *,
    protocol: str = "openai",
    api_key: str = "secret",
    base_url: str = "https://gateway.test/v1",
    capabilities: list[str] | None = None,
    model: str = "gpt-4o",
) -> LlmProvider:
    metadata = {}
    if capabilities is not None:
        metadata[model] = {
            "capabilities": capabilities,
            "input_modalities": ["image"] if "vision" in capabilities else ["text"],
            "capability_source": "manual",
            "registry_revision": None,
            "provider_metadata": {},
        }
    return LlmProvider(
        id=uuid4(),
        name="catalog-gateway",
        protocol=protocol,
        api_key=api_key,
        base_url=base_url,
        models=[model],
        model_metadata=metadata,
        enabled=True,
        is_builtin=False,
    )


class _Page:
    def __init__(self, count: int) -> None:
        self.data = [object() for _ in range(count)]


# ── instance level ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instance_check_reports_catalog_size_and_latency(monkeypatch) -> None:
    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.models = self

        async def list(self, **_kwargs) -> _Page:
            return _Page(7)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    result = await check_instance(_provider())

    assert result.ok is True
    assert result.model_count == 7
    assert result.error_code is None
    assert result.latency_ms is not None and result.latency_ms >= 0


@pytest.mark.asyncio
async def test_instance_check_fails_locally_without_a_key() -> None:
    # No client is constructed at all — nothing to leak, nothing to wait for.
    result = await check_instance(_provider(api_key=""))

    assert result.ok is False
    assert result.error_code == "api_key_missing"
    assert result.latency_ms is None


@pytest.mark.asyncio
async def test_instance_check_maps_upstream_status_to_a_safe_diagnostic(
    monkeypatch,
) -> None:
    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.models = self

        async def list(self, **_kwargs):
            raise openai.APIStatusError(
                "unauthorized: sk-live-should-never-surface",
                response=httpx.Response(
                    401,
                    request=httpx.Request("GET", "https://gateway.test/v1/models"),
                ),
                body=None,
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    result = await check_instance(_provider())

    assert result.ok is False
    # The upstream message is never echoed — only the allowlisted code shape.
    assert result.error_code == "http_error;status=401"


# ── model level ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_check_probes_generation_through_the_responses_endpoint(
    monkeypatch,
) -> None:
    # Production uses Responses; probing chat.completions would pass on a
    # gateway that never implements /v1/responses.
    calls: list[dict] = []

    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.responses = self

        async def create(self, **kwargs) -> object:
            calls.append(kwargs)
            return object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    result = await check_model(
        _provider(capabilities=["generation", "vision"]),
        "gpt-4o",
    )

    assert result.ok is True
    assert result.probed_capability == "generation"
    assert calls and calls[0]["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_model_check_probes_the_anthropic_messages_endpoint(monkeypatch) -> None:
    calls: list[dict] = []

    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.messages = self

        async def create(self, **kwargs) -> object:
            calls.append(kwargs)
            return object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncAnthropic", _Client)

    result = await check_model(
        _provider(
            protocol="anthropic",
            capabilities=["generation"],
            model="claude-sonnet-4-5",
        ),
        "claude-sonnet-4-5",
    )

    assert result.ok is True
    assert calls and calls[0]["max_tokens"] == 1


@pytest.mark.asyncio
async def test_model_check_uses_the_embeddings_endpoint_for_embedding_models(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.embeddings = self

        async def create(self, **kwargs) -> object:
            calls.append(kwargs)
            return object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    result = await check_model(
        _provider(capabilities=["embedding"], model="bge-m3"),
        "bge-m3",
    )

    assert result.ok is True
    assert result.probed_capability == "embedding"
    assert calls and calls[0]["input"] == [connectivity._PROBE_TEXT]


@pytest.mark.asyncio
async def test_model_check_posts_to_the_rerank_endpoint(monkeypatch) -> None:
    seen: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _AsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def post(self, url: str, **kwargs) -> _Response:
            seen["url"] = url
            seen["json"] = kwargs["json"]
            return _Response()

    monkeypatch.setattr(connectivity.httpx, "AsyncClient", _AsyncClient)

    result = await check_model(
        _provider(capabilities=["rerank"], model="bge-reranker-v2-m3"),
        "bge-reranker-v2-m3",
    )

    assert result.ok is True
    assert result.probed_capability == "rerank"
    # Same endpoint shape RerankService builds in production: `{base_url}/rerank`.
    assert seen["url"] == "https://gateway.test/v1/rerank"
    assert seen["json"]["model"] == "bge-reranker-v2-m3"


@pytest.mark.asyncio
async def test_rerank_capability_wins_over_generation_when_both_are_declared(
    monkeypatch,
) -> None:
    # A rerank endpoint cannot answer a chat request, so the purpose-exclusive
    # capability has to decide the probe shape.
    class _AsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def post(self, _url: str, **_kwargs):
            class _Response:
                def raise_for_status(self) -> None:
                    return None

            return _Response()

    monkeypatch.setattr(connectivity.httpx, "AsyncClient", _AsyncClient)

    result = await check_model(
        _provider(capabilities=["generation", "rerank"], model="odd-model"),
        "odd-model",
    )

    assert result.probed_capability == "rerank"


@pytest.mark.asyncio
async def test_model_check_refuses_an_unclassified_model_instead_of_guessing() -> None:
    # Guessing chat would report a bogus failure for an embedding endpoint.
    result = await check_model(_provider(capabilities=[], model="opaque-model"), "opaque-model")

    assert result.ok is False
    assert result.error_code == "capability_unclassified"
    assert result.latency_ms is None


@pytest.mark.asyncio
async def test_model_check_rejects_a_model_outside_the_inventory() -> None:
    result = await check_model(_provider(capabilities=["generation"]), "never-imported")

    assert result.ok is False
    assert result.error_code == "model_not_in_inventory"


@pytest.mark.asyncio
async def test_rerank_probe_needs_a_base_url_because_there_is_no_sdk_default() -> None:
    result = await check_model(
        _provider(capabilities=["rerank"], model="bge-reranker-v2-m3", base_url=""),
        "bge-reranker-v2-m3",
    )

    assert result.ok is False
    assert result.error_code == "base_url_missing"


@pytest.mark.asyncio
async def test_model_check_reports_an_http_status_without_the_response_body(
    monkeypatch,
) -> None:
    class _AsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def post(self, _url: str, **_kwargs):
            request = httpx.Request("POST", "https://gateway.test/rerank")
            raise httpx.HTTPStatusError(
                "model not found: internal-detail",
                request=request,
                response=httpx.Response(404, request=request),
            )

    monkeypatch.setattr(connectivity.httpx, "AsyncClient", _AsyncClient)

    result = await check_model(
        _provider(capabilities=["rerank"], model="bge-reranker-v2-m3"),
        "bge-reranker-v2-m3",
    )

    assert result.ok is False
    assert result.error_code == "http_error;status=404"


@pytest.mark.asyncio
async def test_model_check_times_out_instead_of_hanging(monkeypatch) -> None:
    class _Client:
        def __init__(self, **_kwargs) -> None:
            self.responses = self

        async def create(self, **_kwargs):
            raise TimeoutError

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    result = await check_model(
        _provider(capabilities=["generation"]),
        "gpt-4o",
        timeout_seconds=0.05,
    )

    assert result.ok is False
    assert result.error_code == "timeout"


@pytest.mark.asyncio
async def test_env_credentials_fill_in_for_a_row_without_its_own_key(monkeypatch) -> None:
    seen: dict = {}

    class _Client:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)
            self.models = self

        async def list(self, **_kwargs) -> _Page:
            return _Page(1)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(connectivity, "AsyncOpenAI", _Client)

    # The triple is `(protocol, api_key, base_url)` exactly as
    # `env_provider_credentials()` returns it.
    result = await check_instance(
        _provider(api_key="", base_url=""),
        env=("openai", "env-key", "https://env.test/v1"),
    )

    assert result.ok is True
    assert seen["api_key"] == "env-key"
    assert seen["base_url"] == "https://env.test/v1"
