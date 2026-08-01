"""Embedding 适配层:OpenAI 兼容 /v1/embeddings,provider 可换(D9)。

标签 = "{provider}:{model}" 存入 kb_chunks.embedding_model;换模型必须全量重建索引。
"""

import hashlib
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID

import openai
from openai import AsyncOpenAI

from nicekit.core.config import get_settings
from nicekit.kb.metrics import (
    KB_EMBEDDING_CALLS,
    KB_EMBEDDING_FAILURES,
    KB_EMBEDDING_OVERSIZE_RETRIES,
)
from nicekit.llm.capability_routes import (
    EMBEDDING_ROUTE_TASK,
    ModelEndpoint,
    capability_route_endpoints,
    provider_model_endpoint,
)
from nicekit.llm.runtime_config import runtime_overrides
from nicekit.llm.service import LLMService, get_llm_service
from nicekit.models.kb import EMBEDDING_DIM

logger = logging.getLogger(__name__)

# 单输入超模型上下文时折半重试的最大次数；多输入批次先二分。
_OVERSIZE_MAX_RETRIES = 3
_OVERSIZE_MARKERS = ("too long", "maximum context", "context length")
_DEFAULT_TASK = "kb.embedding"


@dataclass(frozen=True)
class _EmbeddingBatchResult:
    vectors: list[list[float]]
    prompt_tokens: int


@dataclass(frozen=True)
class _EmbeddingAttempt:
    provider: str
    model: str
    client: AsyncOpenAI


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_identifier(value: object, *, field: str, casefold: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"embedding {field} must be a string")
    normalized = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    if not normalized:
        raise ValueError(f"embedding {field} must not be empty")
    return normalized.casefold() if casefold else normalized


@dataclass(frozen=True)
class EmbeddingFingerprint:
    provider: str
    model: str
    dim: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "EmbeddingFingerprint":
        dim = config.get("dim", EMBEDDING_DIM)
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError("embedding dim must be a positive integer")
        return cls(
            provider=_normalize_identifier(config.get("provider"), field="provider", casefold=True),
            model=_normalize_identifier(config.get("model"), field="model", casefold=False),
            dim=dim,
        )

    def as_dict(self) -> dict[str, str | int]:
        return {"provider": self.provider, "model": self.model, "dim": self.dim}

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def normalize_embedding_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe config with a canonical fingerprint tuple."""
    fingerprint = EmbeddingFingerprint.from_config(config)
    normalized = dict(config)
    normalized.update(fingerprint.as_dict())
    for key in ("api_key", "base_url"):
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"embedding {key} must be a string")
        if isinstance(value, str):
            normalized[key] = unicodedata.normalize("NFKC", value).strip()
    return normalized


def embedding_content_hash(text: str) -> str:
    """Hash the exact UTF-8 text sent to the embedding provider."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_embedding_text(
    content: str,
    heading_path: str | None = None,
    context: str | None = None,
) -> str:
    """嵌入文本口径(全链路唯一来源):context\\nheading_path\\ncontent。

    context 为 Contextual Retrieval 的定位上下文(meta.context_text);
    嵌入文本变则 embedding_content_hash 随之变,内容寻址复用不会串。
    """
    parts = [part for part in (context, heading_path) if part]
    parts.append(content)
    return "\n".join(parts)


def chunk_context_text(meta: Mapping[str, Any] | None) -> str | None:
    """从 chunk meta 提取定位上下文;缺失/脏数据一律视为无 context。"""
    if not isinstance(meta, Mapping):
        return None
    value = meta.get("context_text")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _is_oversize_error(exc: Exception) -> bool:
    """输入超长类报错:HTTP 413,或报文含 too long / maximum context 等字样。"""
    if getattr(exc, "status_code", None) == 413:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _OVERSIZE_MARKERS)


def _estimate_tokens(text: str) -> int:
    """Cheap multilingual estimate used only for request shaping and budget preflight."""
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    utf8_quarters = (len(text.encode("utf-8")) + 3) // 4
    multilingual_estimate = non_ascii_chars + (ascii_chars + 3) // 4
    return max(utf8_quarters, multilingual_estimate)


def _split_batches(
    texts: list[str], *, max_items: int, max_estimated_tokens: int
) -> list[tuple[list[str], int]]:
    batches: list[tuple[list[str], int]] = []
    current: list[str] = []
    current_tokens = 0

    for text in texts:
        estimated_tokens = _estimate_tokens(text)
        exceeds_limit = current and (
            len(current) >= max_items or current_tokens + estimated_tokens > max_estimated_tokens
        )
        if exceeds_limit:
            batches.append((current, current_tokens))
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += estimated_tokens

    if current:
        batches.append((current, current_tokens))
    return batches


class EmbeddingUnavailableError(Exception):
    pass


class EmbeddingService:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        dim: int = EMBEDDING_DIM,
        llm_service: LLMService | None = None,
        max_batch_items: int | None = None,
        max_batch_estimated_tokens: int | None = None,
        fallback_endpoints: Sequence[ModelEndpoint] | None = None,
    ):
        # 模型指纹仍由 active service_config 控制；提供商凭证与可用降级跳
        # 统一从模型提供商/系统路由读取。显式参数用于迁移与快照重建。
        s = get_settings()
        o = runtime_overrides("embedding")
        explicit_selection = provider is not None or model is not None
        self._provider = _normalize_identifier(
            provider or o.get("provider") or s.embedding_provider,
            field="provider",
            casefold=True,
        )
        self._model = _normalize_identifier(
            model or o.get("model") or s.embedding_model,
            field="model",
            casefold=False,
        )
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError("embedding dim must be a positive integer")
        self._dim = dim
        self._llm_service = llm_service or get_llm_service()
        self._max_batch_items = max_batch_items or s.embedding_batch_max_items
        self._max_batch_estimated_tokens = (
            max_batch_estimated_tokens or s.embedding_batch_max_estimated_tokens
        )
        if self._max_batch_items <= 0 or self._max_batch_estimated_tokens <= 0:
            raise ValueError("embedding batch limits must be positive")
        provider_endpoint = provider_model_endpoint(
            self._provider,
            self._model,
            capability="embedding",
        )
        resolved_api_key = (
            api_key
            or (provider_endpoint.api_key if provider_endpoint is not None else None)
        )
        resolved_base_url = (
            base_url
            or (provider_endpoint.base_url if provider_endpoint is not None else None)
        )
        if not resolved_api_key:
            raise EmbeddingUnavailableError(
                "embedding 模型未配置可用的提供商凭证"
            )
        self._client = AsyncOpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url or None,
        )
        if fallback_endpoints is None and not explicit_selection and api_key is None:
            route_endpoints = capability_route_endpoints(
                EMBEDDING_ROUTE_TASK,
                "embedding",
                active_primary=(self._provider, self._model),
            )
            fallback_endpoints = route_endpoints[1:] if route_endpoints else ()
        self._fallback_attempts = [
            _EmbeddingAttempt(
                provider=endpoint.provider,
                model=endpoint.model,
                client=AsyncOpenAI(
                    api_key=endpoint.api_key,
                    base_url=endpoint.base_url or None,
                ),
            )
            for endpoint in (fallback_endpoints or ())
            if (endpoint.provider, endpoint.model) != (self._provider, self._model)
        ]

    @property
    def label(self) -> str:
        return f"{self._provider}:{self._model}"

    @property
    def fingerprint(self) -> EmbeddingFingerprint:
        return EmbeddingFingerprint(self._provider, self._model, self._dim)

    async def close(self) -> None:
        """Release the underlying async HTTP connection pool."""
        await self._client.close()
        for attempt in self._fallback_attempts:
            await attempt.client.close()

    async def embed(
        self, texts: list[str], *, org_id: UUID, task: str | None = None
    ) -> list[list[float]]:
        """Embed texts in governed item/token batches while preserving input order."""
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("embedding texts must not be empty or whitespace-only")

        vectors: list[list[float]] = []
        for batch, estimated_tokens in _split_batches(
            texts,
            max_items=self._max_batch_items,
            max_estimated_tokens=self._max_batch_estimated_tokens,
        ):
            vectors.extend(
                await self._embed_batch(
                    batch,
                    org_id=org_id,
                    task=task or _DEFAULT_TASK,
                    estimated_tokens=estimated_tokens,
                )
            )
        return vectors

    async def _embed_batch(
        self,
        texts: list[str],
        *,
        org_id: UUID,
        task: str,
        estimated_tokens: int,
    ) -> list[list[float]]:
        attempts = [
            _EmbeddingAttempt(self._provider, self._model, self._client),
            *self._fallback_attempts,
        ]
        failures: list[str] = []
        fallback_from: str | None = None
        for route_attempt, endpoint in enumerate(attempts, start=1):
            try:
                return await self._embed_batch_with_endpoint(
                    texts,
                    org_id=org_id,
                    task=task,
                    estimated_tokens=estimated_tokens,
                    endpoint=endpoint,
                    route_attempt=route_attempt,
                    fallback_from=fallback_from,
                )
            except EmbeddingUnavailableError as exc:
                failures.append(str(exc))
                fallback_from = f"{endpoint.provider}:{endpoint.model}"
        raise EmbeddingUnavailableError(f"embedding 降级链全部失败:{'; '.join(failures)}")

    async def _embed_batch_with_endpoint(
        self,
        texts: list[str],
        *,
        org_id: UUID,
        task: str,
        estimated_tokens: int,
        endpoint: _EmbeddingAttempt,
        route_attempt: int,
        fallback_from: str | None,
    ) -> list[list[float]]:
        """Split oversized batches, then halve only an oversized single input."""
        current = list(texts)
        for attempt in range(_OVERSIZE_MAX_RETRIES + 1):
            try:
                current_estimate = (
                    estimated_tokens if attempt == 0 else sum(map(_estimate_tokens, current))
                )

                KB_EMBEDDING_CALLS.inc()
                trace_route = (
                    {
                        "attempt": route_attempt,
                        "fallback_from": fallback_from,
                    }
                    if route_attempt > 1 or fallback_from is not None
                    else {}
                )
                result = await self._llm_service.run_governed_call(
                    org_id=org_id,
                    task=task,
                    provider=endpoint.provider,
                    model=endpoint.model,
                    estimated_tokens=current_estimate,
                    quantity=len(current),
                    call=partial(
                        self._request_batch,
                        endpoint.client,
                        endpoint.model,
                        list(current),
                    ),
                    token_usage=lambda parsed: (parsed.prompt_tokens, 0),
                    **trace_route,
                )
            except (openai.APIError, openai.APIConnectionError) as exc:
                if attempt < _OVERSIZE_MAX_RETRIES and _is_oversize_error(exc):
                    KB_EMBEDDING_OVERSIZE_RETRIES.inc()
                    if len(current) > 1:
                        midpoint = len(current) // 2
                        left = current[:midpoint]
                        right = current[midpoint:]
                        logger.warning(
                            "embedding 批次输入超长,二分为 %s + %s 条重试: %s",
                            len(left),
                            len(right),
                            exc,
                        )
                        return await self._embed_batch_with_endpoint(
                            left,
                            org_id=org_id,
                            task=task,
                            estimated_tokens=sum(map(_estimate_tokens, left)),
                            endpoint=endpoint,
                            route_attempt=route_attempt,
                            fallback_from=fallback_from,
                        ) + await self._embed_batch_with_endpoint(
                            right,
                            org_id=org_id,
                            task=task,
                            estimated_tokens=sum(map(_estimate_tokens, right)),
                            endpoint=endpoint,
                            route_attempt=route_attempt,
                            fallback_from=fallback_from,
                        )
                    current = [t[: max(len(t) // 2, 1)] for t in current]
                    logger.warning(
                        "embedding 输入超长,截半重试(第 %s/%s 次): %s",
                        attempt + 1,
                        _OVERSIZE_MAX_RETRIES,
                        exc,
                    )
                    continue
                KB_EMBEDDING_FAILURES.inc()
                raise EmbeddingUnavailableError(f"embedding 服务不可用: {exc}") from exc
            if attempt:
                truncated = sum(
                    1 for orig, cur in zip(texts, current, strict=True) if len(cur) < len(orig)
                )
                logger.warning(
                    "embedding 截半 %s 次后成功,%s/%s 段文本按截断后内容入向量",
                    attempt,
                    truncated,
                    len(texts),
                )
            return result.vectors
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request_batch(
        self,
        client: AsyncOpenAI,
        model: str,
        texts: list[str],
    ) -> _EmbeddingBatchResult:
        # bge-m3 等固定维度模型会拒绝 dimensions 参数。
        response = await client.embeddings.create(model=model, input=texts)
        return self._parse_response(response, expected_count=len(texts))

    def _parse_response(self, response: object, *, expected_count: int) -> _EmbeddingBatchResult:
        try:
            data = list(response.data)  # type: ignore[attr-defined]
        except (AttributeError, TypeError) as exc:
            raise EmbeddingUnavailableError("embedding 响应缺少 data") from exc
        if len(data) != expected_count:
            raise EmbeddingUnavailableError(
                f"embedding 返回数量不符:期望 {expected_count},实际 {len(data)}"
            )

        indices = [getattr(item, "index", None) for item in data]
        if any(
            not isinstance(index, int) or isinstance(index, bool) for index in indices
        ) or sorted(indices) != list(range(expected_count)):
            raise EmbeddingUnavailableError("embedding 返回 index 无效或不完整")

        ordered = sorted(data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]
        for index, vector in enumerate(vectors):
            if len(vector) != self._dim:
                raise EmbeddingUnavailableError(
                    f"embedding 维度不符:index={index},期望 {self._dim},实际 {len(vector)}"
                )

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        if (
            not isinstance(prompt_tokens, int)
            or isinstance(prompt_tokens, bool)
            or prompt_tokens < 0
        ):
            raise EmbeddingUnavailableError("embedding 响应缺少有效 usage.prompt_tokens")
        return _EmbeddingBatchResult(vectors=vectors, prompt_tokens=prompt_tokens)
