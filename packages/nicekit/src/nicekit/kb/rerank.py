"""Governed cross-encoder reranking through provider model routes."""

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol
from uuid import UUID

import httpx

from nicekit.core.config import get_settings
from nicekit.kb.metrics import KB_RERANK_CALLS, KB_RERANK_LATENCY
from nicekit.llm.capability_routes import (
    RERANK_ROUTE_TASK,
    capability_route_endpoints,
    capability_route_timeout,
)
from nicekit.llm.service import LLMService, get_llm_service

_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_TASK = RERANK_ROUTE_TASK
RERANK_TOP_N = 50

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """A safe, provider-agnostic rerank failure for retrieval fallback."""


class Reranker(Protocol):
    async def rerank(
        self,
        *,
        org_id: UUID,
        query: str,
        documents: Sequence[str],
    ) -> list[float]:
        """Return one relevance score per document, aligned to input order."""


@dataclass(frozen=True)
class _RerankResponse:
    scores: list[float]
    input_tokens: int
    output_tokens: int


def _estimate_tokens(text: str) -> int:
    """Conservative multilingual estimate for governance preflight and fallback usage."""
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    utf8_quarters = (len(text.encode("utf-8")) + 3) // 4
    multilingual_estimate = non_ascii_chars + (ascii_chars + 3) // 4
    return max(utf8_quarters, multilingual_estimate, 1)


def _usage_value(value: object, *, minimum: int, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return fallback


class RerankService:
    """OpenAI-compatible rerank adapter with shared governance guardrails."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        provider: str = "siliconflow",
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout_seconds: float = 3.0,
        llm_service: LLMService | None = None,
        client: httpx.AsyncClient | None = None,
        attempt: int = 1,
        fallback_from: str | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("rerank api_key must not be empty")
        if not base_url.strip():
            raise ValueError("rerank base_url must not be empty")
        if not provider.strip() or not model.strip():
            raise ValueError("rerank provider and model must not be empty")
        normalized_provider = provider.strip().casefold()
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("rerank timeout_seconds must be positive and finite")
        if attempt < 1:
            raise ValueError("rerank attempt must be positive")

        self._api_key = api_key.strip()
        self._endpoint = f"{base_url.strip().rstrip('/')}/rerank"
        self._provider = normalized_provider
        self._model = model.strip()
        self._timeout_seconds = timeout_seconds
        self._llm_service = llm_service or get_llm_service()
        self._client = client
        self._attempt = attempt
        self._fallback_from = fallback_from

    async def rerank(
        self,
        *,
        org_id: UUID,
        query: str,
        documents: Sequence[str],
    ) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise RerankError("rerank query must not be empty")
        items = list(documents)
        if not items:
            return []
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise RerankError("rerank documents must be non-empty strings")

        estimated_input = len(items) * _estimate_tokens(query) + sum(
            _estimate_tokens(item) for item in items
        )
        estimated_output = len(items)
        # 每次调用记录 provider/model/候选数/耗时与结局(ok|timeout|error),
        # 供在线降级与评测门禁定位 reranker 慢点(R4 根治项)。
        started = time.perf_counter()

        def _latency_ms() -> float:
            return (time.perf_counter() - started) * 1000

        def _observe(outcome: str) -> None:
            # 指标与既有日志并行,只做可见性,不影响调用结局
            KB_RERANK_CALLS.labels(outcome=outcome).inc()
            KB_RERANK_LATENCY.observe(time.perf_counter() - started)

        try:
            async with asyncio.timeout(self._timeout_seconds):
                trace_route = (
                    {
                        "attempt": self._attempt,
                        "fallback_from": self._fallback_from,
                    }
                    if self._attempt > 1 or self._fallback_from is not None
                    else {}
                )
                response = await self._llm_service.run_governed_call(
                    org_id=org_id,
                    task=_DEFAULT_TASK,
                    provider=self._provider,
                    model=self._model,
                    estimated_tokens=estimated_input + estimated_output,
                    quantity=len(items),
                    call=partial(
                        self._request,
                        query,
                        items,
                        estimated_input=estimated_input,
                        estimated_output=estimated_output,
                    ),
                    token_usage=lambda result: (result.input_tokens, result.output_tokens),
                    **trace_route,
                )
        except RerankError:
            _observe("error")
            logger.warning(
                "rerank_call outcome=error provider=%s model=%s candidates=%s latency_ms=%.0f",
                self._provider,
                self._model,
                len(items),
                _latency_ms(),
            )
            raise
        except TimeoutError:
            _observe("timeout")
            logger.warning(
                "rerank_call outcome=timeout provider=%s model=%s candidates=%s "
                "latency_ms=%.0f timeout_seconds=%s",
                self._provider,
                self._model,
                len(items),
                _latency_ms(),
                self._timeout_seconds,
            )
            raise RerankError("rerank request timed out") from None
        except Exception:
            _observe("error")
            logger.warning(
                "rerank_call outcome=error provider=%s model=%s candidates=%s latency_ms=%.0f",
                self._provider,
                self._model,
                len(items),
                _latency_ms(),
            )
            raise RerankError("rerank governance or provider call failed") from None
        _observe("ok")
        logger.info(
            "rerank_call outcome=ok provider=%s model=%s candidates=%s latency_ms=%.0f",
            self._provider,
            self._model,
            len(items),
            _latency_ms(),
        )
        return response.scores

    async def _request(
        self,
        query: str,
        documents: list[str],
        *,
        estimated_input: int,
        estimated_output: int,
    ) -> _RerankResponse:
        request = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                if self._client is None:
                    async with httpx.AsyncClient(timeout=None) as client:
                        response = await client.post(self._endpoint, headers=headers, json=request)
                else:
                    response = await self._client.post(
                        self._endpoint, headers=headers, json=request
                    )
                response.raise_for_status()
                payload = response.json()
        except TimeoutError:
            raise RerankError("rerank request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise RerankError(f"rerank service returned HTTP {exc.response.status_code}") from None
        except httpx.HTTPError:
            raise RerankError("rerank service is unavailable") from None
        except ValueError:
            raise RerankError("rerank service returned invalid JSON") from None

        return self._parse_response(
            payload,
            expected_count=len(documents),
            estimated_input=estimated_input,
            estimated_output=estimated_output,
        )

    @staticmethod
    def _parse_response(
        payload: object,
        *,
        expected_count: int,
        estimated_input: int,
        estimated_output: int,
    ) -> _RerankResponse:
        if not isinstance(payload, Mapping):
            raise RerankError("rerank response must be an object")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != expected_count:
            raise RerankError("rerank response has incomplete results")

        scores_by_index: dict[int, float] = {}
        for result in results:
            if not isinstance(result, Mapping):
                raise RerankError("rerank result must be an object")
            index = result.get("index")
            score = result.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < expected_count
                or index in scores_by_index
            ):
                raise RerankError("rerank response has invalid or duplicate index")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
                or not 0 <= score <= 1
            ):
                raise RerankError("rerank response has invalid relevance score")
            scores_by_index[index] = float(score)

        if set(scores_by_index) != set(range(expected_count)):
            raise RerankError("rerank response has incomplete indices")

        meta = payload.get("meta")
        tokens: Any = meta.get("tokens") if isinstance(meta, Mapping) else None
        input_tokens = _usage_value(
            tokens.get("input_tokens") if isinstance(tokens, Mapping) else None,
            minimum=0,
            fallback=estimated_input,
        )
        output_tokens = _usage_value(
            tokens.get("output_tokens") if isinstance(tokens, Mapping) else None,
            minimum=0,
            fallback=estimated_output,
        )
        if not isinstance(tokens, Mapping):
            logger.warning(
                "rerank_usage_missing candidate_count=%s estimated_input_tokens=%s "
                "estimated_output_tokens=%s",
                expected_count,
                estimated_input,
                estimated_output,
            )
        return _RerankResponse(
            scores=[scores_by_index[index] for index in range(expected_count)],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class RerankChain:
    """Try configured rerank endpoints in route order."""

    def __init__(self, services: Sequence[RerankService]) -> None:
        if not services:
            raise ValueError("rerank chain must not be empty")
        self._services = tuple(services)
        self._provider = services[0]._provider
        self._model = services[0]._model

    async def rerank(
        self,
        *,
        org_id: UUID,
        query: str,
        documents: Sequence[str],
    ) -> list[float]:
        failures: list[str] = []
        for service in self._services:
            try:
                return await service.rerank(
                    org_id=org_id,
                    query=query,
                    documents=documents,
                )
            except RerankError as exc:
                failures.append(str(exc))
        raise RerankError(f"rerank 降级链全部失败:{'; '.join(failures)}")


def get_rerank_service(
    *,
    llm_service: LLMService | None = None,
    client: httpx.AsyncClient | None = None,
) -> Reranker | None:
    """Build the routed reranker; unavailable routes fall back to RRF."""
    return get_rerank_execution(llm_service=llm_service, client=client)[0]


def get_rerank_execution(
    *,
    llm_service: LLMService | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Reranker | None, dict[str, Any]]:
    """Resolve one immutable reranker instance and its secret-free manifest."""
    settings = get_settings()
    if not settings.rerank_enabled:
        return None, {"schema_version": "rerank-v2", "enabled": False}

    timeout_seconds = (
        capability_route_timeout(RERANK_ROUTE_TASK)
        or settings.rerank_timeout_seconds
    )
    route_endpoints = capability_route_endpoints(
        RERANK_ROUTE_TASK,
        "rerank",
    )
    if not route_endpoints:
        return (
            None,
            {
                "schema_version": "rerank-v2",
                "enabled": True,
                "available": False,
                "route": [],
                "timeout_seconds": timeout_seconds,
                "top_n": RERANK_TOP_N,
            },
        )

    services: list[RerankService] = []
    fallback_from: str | None = None
    for attempt, endpoint in enumerate(route_endpoints, start=1):
        services.append(
            RerankService(
                api_key=endpoint.api_key,
                base_url=endpoint.base_url,
                provider=endpoint.provider,
                model=endpoint.model,
                timeout_seconds=timeout_seconds,
                llm_service=llm_service,
                client=client,
                attempt=attempt,
                fallback_from=fallback_from,
            )
        )
        fallback_from = f"{endpoint.provider}:{endpoint.model}"
    endpoint_hashes = [
        hashlib.sha256(
            f"{endpoint.base_url.strip().rstrip('/')}/rerank".encode()
        ).hexdigest()
        for endpoint in route_endpoints
    ]
    return (
        RerankChain(services),
        {
            "schema_version": "rerank-v2",
            "enabled": True,
            "available": True,
            "provider": route_endpoints[0].provider,
            "model": route_endpoints[0].model,
            "route": [
                {"provider": endpoint.provider, "model": endpoint.model}
                for endpoint in route_endpoints
            ],
            "endpoint_sha256": endpoint_hashes[0],
            "endpoint_sha256_chain": endpoint_hashes,
            "timeout_seconds": timeout_seconds,
            "top_n": RERANK_TOP_N,
        },
    )
