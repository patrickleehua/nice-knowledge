"""Cross-encoder rerank adapter tests; all provider calls are mocked."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import nicekit.kb.rerank as rerank_module
from nicekit.kb.rerank import (
    RerankError,
    RerankService,
    get_rerank_execution,
    get_rerank_service,
)
from nicekit.llm.capability_routes import ModelEndpoint
from nicekit.llm.service import LLMService


class _FakeGovernance:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_governed_call(self, **kwargs):
        result = await kwargs["call"]()
        tokens_in, tokens_out = kwargs["token_usage"](result)
        self.calls.append(
            {
                key: value
                for key, value in kwargs.items()
                if key not in {"call", "token_usage"}
            }
            | {"tokens_in": tokens_in, "tokens_out": tokens_out}
        )
        return result


def _success_payload(*, meta: object = None) -> dict:
    payload = {
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.2},
        ]
    }
    if meta is not None:
        payload["meta"] = meta
    return payload


def _service(
    handler,
    *,
    governance: _FakeGovernance | None = None,
    timeout_seconds: float = 1.0,
) -> tuple[RerankService, _FakeGovernance, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    guard = governance or _FakeGovernance()
    return (
        RerankService(
            api_key="provider-secret",
            base_url="https://example.test/v1/",
            llm_service=guard,  # type: ignore[arg-type]
            client=client,
            timeout_seconds=timeout_seconds,
        ),
        guard,
        client,
    )


async def test_request_contract_reorders_scores_and_records_governance() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_success_payload(
                meta={"tokens": {"input_tokens": 17, "output_tokens": 2}}
            ),
        )

    service, governance, client = _service(handler)
    org_id = uuid4()
    try:
        scores = await service.rerank(
            org_id=org_id,
            query="清迈 酒店",
            documents=["第一个候选", "第二个候选"],
        )
    finally:
        await client.aclose()

    assert scores == [0.2, 0.9]
    assert seen == {
        "url": "https://example.test/v1/rerank",
        "authorization": "Bearer provider-secret",
        "body": {
            "model": "BAAI/bge-reranker-v2-m3",
            "query": "清迈 酒店",
            "documents": ["第一个候选", "第二个候选"],
            "top_n": 2,
            "return_documents": False,
        },
    }
    assert governance.calls == [
        {
            "org_id": org_id,
            "task": "kb.search.rerank",
            "provider": "siliconflow",
            "model": "BAAI/bge-reranker-v2-m3",
            "estimated_tokens": 22,
            "quantity": 2,
            "tokens_in": 17,
            "tokens_out": 2,
        }
    ]


async def test_missing_usage_uses_nonzero_conservative_estimate(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload())

    service, governance, client = _service(handler)
    try:
        await service.rerank(
            org_id=uuid4(), query="query", documents=["first", "second"]
        )
    finally:
        await client.aclose()

    call = governance.calls[0]
    assert call["tokens_in"] > 0
    assert call["tokens_out"] == 2
    assert call["estimated_tokens"] == call["tokens_in"] + call["tokens_out"]
    assert "rerank_usage_missing" in caplog.text
    assert "query" not in caplog.text
    assert "first" not in caplog.text
    assert "provider-secret" not in caplog.text


async def test_real_zero_usage_values_are_preserved(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_success_payload(
                meta={"tokens": {"input_tokens": 0, "output_tokens": 0}}
            ),
        )

    service, governance, client = _service(handler)
    try:
        await service.rerank(
            org_id=uuid4(), query="query", documents=["first", "second"]
        )
    finally:
        await client.aclose()

    assert governance.calls[0]["tokens_in"] == 0
    assert governance.calls[0]["tokens_out"] == 0
    assert "rerank_usage_missing" not in caplog.text


@pytest.mark.parametrize(
    "results,error",
    [
        ([{"index": 0, "relevance_score": 0.5}], "incomplete results"),
        (
            [
                {"index": 0, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.4},
            ],
            "duplicate index",
        ),
        (
            [
                {"index": 0, "relevance_score": float("nan")},
                {"index": 1, "relevance_score": 0.4},
            ],
            "relevance score",
        ),
        (
            [
                {"index": 0, "relevance_score": 1.01},
                {"index": 1, "relevance_score": 0.4},
            ],
            "relevance score",
        ),
        (
            [
                {"index": 0, "relevance_score": 0.5},
                {"index": 2, "relevance_score": 0.4},
            ],
            "index",
        ),
    ],
)
async def test_invalid_results_are_rejected(results: list[dict], error: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"results": results}, allow_nan=True).encode(),
            headers={"Content-Type": "application/json"},
        )

    service, _, client = _service(handler)
    try:
        with pytest.raises(RerankError, match=error):
            await service.rerank(
                org_id=uuid4(), query="query", documents=["first", "second"]
            )
    finally:
        await client.aclose()


async def test_http_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "provider-secret and upstream-private-detail"},
        )

    service, _, client = _service(handler)
    try:
        with pytest.raises(RerankError) as error:
            await service.rerank(
                org_id=uuid4(), query="query", documents=["candidate"]
            )
    finally:
        await client.aclose()

    assert str(error.value) == "rerank service returned HTTP 401"
    assert "provider-secret" not in str(error.value)
    assert "upstream-private-detail" not in str(error.value)


async def test_total_request_timeout_is_mapped_to_rerank_error(caplog) -> None:
    class SlowClient:
        async def post(self, *args, **kwargs):
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

    service = RerankService(
        api_key="secret",
        timeout_seconds=0.01,
        llm_service=_FakeGovernance(),  # type: ignore[arg-type]
        client=SlowClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(RerankError, match="timed out"):
        await service.rerank(
            org_id=uuid4(), query="query", documents=["candidate"]
        )
    # 每次调用必须留下结局/延迟/候选数记录,且不得泄漏文档内容
    assert "rerank_call outcome=timeout" in caplog.text
    assert "latency_ms=" in caplog.text
    assert "candidates=1" in caplog.text
    assert "timeout_seconds=0.01" in caplog.text


async def test_total_timeout_includes_governance_wait_before_provider_call() -> None:
    provider_called = False

    class SlowGovernance:
        async def run_governed_call(self, **kwargs):
            await asyncio.sleep(1)
            return await kwargs["call"]()

    class MustNotRunClient:
        async def post(self, *args, **kwargs):
            nonlocal provider_called
            provider_called = True
            raise AssertionError("provider call must remain outside the exhausted budget")

    service = RerankService(
        api_key="secret",
        timeout_seconds=0.01,
        llm_service=SlowGovernance(),  # type: ignore[arg-type]
        client=MustNotRunClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(RerankError, match="timed out"):
        await service.rerank(
            org_id=uuid4(), query="query", documents=["candidate"]
        )
    assert provider_called is False


async def test_timeout_releases_real_governance_semaphore_for_next_call(
    monkeypatch,
) -> None:
    semaphore = asyncio.Semaphore(1)
    governance = LLMService(providers={})
    # SDK 化后 `_org_semaphore` 是 LLMService 实例方法(TF 是模块级全局)
    monkeypatch.setattr(governance, "_org_semaphore", lambda _org_id: semaphore)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(governance, "_check_budget", no_op)
    monkeypatch.setattr(governance, "_record", no_op)

    class FirstSlowThenFastClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
                raise AssertionError("canceled provider call resumed")
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"results": [{"index": 0, "relevance_score": 0.8}]},
            )

    client = FirstSlowThenFastClient()
    service = RerankService(
        api_key="secret",
        timeout_seconds=0.01,
        llm_service=governance,
        client=client,  # type: ignore[arg-type]
    )
    org_id = uuid4()

    with pytest.raises(RerankError, match="timed out"):
        await service.rerank(org_id=org_id, query="query", documents=["first"])
    assert await service.rerank(
        org_id=org_id, query="query", documents=["second"]
    ) == [0.8]
    await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    semaphore.release()


async def test_empty_documents_skip_governance_and_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be called")

    service, governance, client = _service(handler)
    try:
        assert await service.rerank(org_id=uuid4(), query="query", documents=[]) == []
    finally:
        await client.aclose()
    assert governance.calls == []


async def test_route_chain_falls_back_to_second_rerank_endpoint(monkeypatch) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.host == "primary.test":
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json=_success_payload())

    settings = SimpleNamespace(
        rerank_enabled=True,
        rerank_timeout_seconds=3.0,
    )
    endpoints = [
        ModelEndpoint(
            provider="primary",
            model="primary-reranker",
            api_key="primary-key",
            base_url="https://primary.test/v1",
        ),
        ModelEndpoint(
            provider="backup",
            model="backup-reranker",
            api_key="backup-key",
            base_url="https://backup.test/v1",
        ),
    ]
    monkeypatch.setattr(rerank_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        rerank_module,
        "capability_route_timeout",
        lambda _task: 2.5,
    )
    monkeypatch.setattr(
        rerank_module,
        "capability_route_endpoints",
        lambda _task, _capability: endpoints,
    )
    governance = _FakeGovernance()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        service, manifest = get_rerank_execution(
            llm_service=governance,  # type: ignore[arg-type]
            client=client,
        )
        assert service is not None
        scores = await service.rerank(
            org_id=uuid4(),
            query="query",
            documents=["first", "second"],
        )
    finally:
        await client.aclose()

    assert scores == [0.2, 0.9]
    assert seen_urls == [
        "https://primary.test/v1/rerank",
        "https://backup.test/v1/rerank",
    ]
    assert governance.calls[0]["provider"] == "backup"
    assert governance.calls[0]["model"] == "backup-reranker"
    assert governance.calls[0]["attempt"] == 2
    assert governance.calls[0]["fallback_from"] == "primary:primary-reranker"
    assert manifest["route"] == [
        {"provider": "primary", "model": "primary-reranker"},
        {"provider": "backup", "model": "backup-reranker"},
    ]
    assert manifest["timeout_seconds"] == 2.5


def test_factory_returns_none_when_rerank_is_disabled(monkeypatch) -> None:
    settings = SimpleNamespace(
        rerank_enabled=False,
        rerank_timeout_seconds=3.0,
    )
    monkeypatch.setattr(rerank_module, "get_settings", lambda: settings)

    def must_not_resolve_route(*_args, **_kwargs):
        raise AssertionError("disabled rerank must not resolve routes")

    monkeypatch.setattr(
        rerank_module,
        "capability_route_endpoints",
        must_not_resolve_route,
    )
    assert get_rerank_service(llm_service=_FakeGovernance()) is None  # type: ignore[arg-type]


def test_factory_requires_a_rerank_system_route(monkeypatch) -> None:
    settings = SimpleNamespace(
        rerank_enabled=True,
        rerank_timeout_seconds=3.0,
    )
    monkeypatch.setattr(rerank_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        rerank_module,
        "capability_route_endpoints",
        lambda _task, _capability: [],
    )
    monkeypatch.setattr(
        rerank_module,
        "capability_route_timeout",
        lambda _task: None,
    )

    service, manifest = get_rerank_execution(
        llm_service=_FakeGovernance(),  # type: ignore[arg-type]
    )
    assert service is None
    assert manifest["available"] is False
    assert manifest["route"] == []
