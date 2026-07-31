"""LLMService 单元测试:路由/降级/trace 逻辑,全部 mock,不碰数据库与网络。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

import nicekit.llm.service as service_module
from nicekit.llm.providers import ProviderError, ProviderResult, ToolLoopResult
from nicekit.llm.registry import ResolvedRoute
from nicekit.llm.service import AllProvidersFailedError, LLMService


class FakeOutput(BaseModel):
    answer: str


class FakePrompt:
    version = 3
    content = "你是测试助手"


class FakeProvider:
    def __init__(self, name: str, *, fail: Exception | None = None):
        self.name = name
        self.fail = fail
        self.calls: list[dict] = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return ProviderResult(
            parsed=FakeOutput(answer=f"from-{self.name}"), tokens_in=10, tokens_out=5
        )

    async def generate_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return ToolLoopResult(
            text=f"from-{self.name}",
            tool_calls=[],
            stop_reason="end_turn",
            tokens_in=10,
            tokens_out=5,
        )


class LeakyProviderError(ProviderError):
    """Simulates a custom provider bypassing ProviderError initialization."""

    def __init__(self, *, retryable: bool) -> None:
        Exception.__init__(self, "SECRET_API_KEY raw provider body")
        self.code = "SECRET_API_KEY raw provider body"
        self.retryable = retryable
        self.status_code = 503
        self.request_id = "req_safe_123"


@pytest.fixture(autouse=True)
def clear_cooldowns():
    """SDK 化后冷却收进 LLMService 实例属性,直接实例化的 svc 天然隔离;
    这里只清 get_llm_service() 默认路径注入的进程级共享容器,避免顺序耦合。"""
    service_module._shared_provider_cooldowns.clear()
    yield
    service_module._shared_provider_cooldowns.clear()


@pytest.fixture
def records() -> list[dict]:
    return []


@pytest.fixture
def make_service(monkeypatch, records):
    """构造 LLMService,mock 掉 registry 与 trace 落库。"""

    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: SimpleNamespace(
            llm_provider_cooldown_seconds=60,
            llm_org_daily_token_budget=0,
            llm_org_max_concurrency=0,
        ),
    )

    def _make(providers: dict, fallback_chain: list[dict]) -> LLMService:
        async def fake_prompt(session, task):
            return FakePrompt()

        async def fake_route(session, task, org_id):
            return ResolvedRoute(
                primary_provider="p1",
                primary_model="m1",
                fallback_chain=fallback_chain,
                max_tokens=1024,
                timeout_seconds=30,
            )

        monkeypatch.setattr(service_module, "get_active_prompt", fake_prompt)
        monkeypatch.setattr(service_module, "resolve_route", fake_route)

        class FakeSessionCtx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                return False

        svc = LLMService(providers, session_factory=lambda: FakeSessionCtx())

        async def fake_record(**kwargs):
            records.append(kwargs)

        monkeypatch.setattr(svc, "_record", fake_record)
        return svc

    return _make


async def test_primary_success_no_fallback(make_service, records):
    p1 = FakeProvider("p1")
    svc = make_service({"p1": p1}, fallback_chain=[])
    result = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    assert result.answer == "from-p1"
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert records[0]["fallback_from"] is None
    assert records[0]["prompt_version"] == 3


async def test_retryable_error_falls_back(make_service, records):
    p1 = FakeProvider("p1", fail=ProviderError("timeout", retryable=True))
    p2 = FakeProvider("p2")
    svc = make_service({"p1": p1, "p2": p2}, fallback_chain=[{"provider": "p2", "model": "m2"}])
    result = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    assert result.answer == "from-p2"
    assert [r["status"] for r in records] == ["error", "success"]
    assert records[1]["fallback_from"] == "p1:m1"
    assert p2.calls[0]["model"] == "m2"


async def test_structured_metadata_reports_actual_fallback_route(make_service, records):
    p1 = FakeProvider("p1", fail=ProviderError("timeout", retryable=True))
    p2 = FakeProvider("p2")
    svc = make_service(
        {"p1": p1, "p2": p2},
        fallback_chain=[{"provider": "p2", "model": "m2"}],
    )

    generation = await svc.generate_structured_with_metadata(
        task="t",
        messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput,
        org_id=uuid4(),
    )

    assert generation.parsed.answer == "from-p2"
    assert generation.provider == "p2"
    assert generation.model == "m2"
    assert generation.prompt_version == 3


async def test_non_retryable_error_raises_immediately(make_service, records):
    p1 = FakeProvider(
        "p1",
        fail=ProviderError("http_error", retryable=False, status_code=400),
    )
    p2 = FakeProvider("p2")
    svc = make_service({"p1": p1, "p2": p2}, fallback_chain=[{"provider": "p2", "model": "m2"}])
    with pytest.raises(ProviderError):
        await svc.generate_structured(
            task="t", messages=[{"role": "user", "content": "hi"}],
            output_model=FakeOutput, org_id=uuid4(),
        )
    assert p2.calls == []  # 不降级
    assert records[0]["status"] == "error"


async def test_structured_trace_resanitizes_custom_provider_error(
    make_service,
    records,
) -> None:
    provider = FakeProvider("p1", fail=LeakyProviderError(retryable=False))
    svc = make_service({"p1": provider}, fallback_chain=[])

    with pytest.raises(ProviderError) as caught:
        await svc.generate_structured(
            task="t",
            messages=[{"role": "user", "content": "hi"}],
            output_model=FakeOutput,
            org_id=uuid4(),
        )

    assert caught.value.diagnostic == (
        "provider_error;status=503;request_id=req_safe_123"
    )
    assert str(caught.value) == caught.value.diagnostic
    assert records[0]["error"] == caught.value.diagnostic
    assert "SECRET_API_KEY" not in str(caught.value)
    assert "SECRET_API_KEY" not in records[0]["error"]
    assert caught.value.__cause__ is None


async def test_tool_trace_resanitizes_custom_provider_error(
    make_service,
    records,
) -> None:
    provider = FakeProvider("p1", fail=LeakyProviderError(retryable=True))
    svc = make_service({"p1": provider}, fallback_chain=[])

    with pytest.raises(AllProvidersFailedError) as caught:
        await svc.generate_with_tools(
            task="t",
            system="test",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            org_id=uuid4(),
        )

    assert caught.value.errors == [
        "provider_error;status=503;request_id=req_safe_123"
    ]
    assert records[0]["error"] == caught.value.errors[0]
    assert "SECRET_API_KEY" not in str(caught.value)
    assert "SECRET_API_KEY" not in records[0]["error"]


def test_all_providers_error_rejects_untrusted_error_strings() -> None:
    error = AllProvidersFailedError(
        "kb.caption.image",
        ["SECRET_API_KEY raw provider body", "rate_limit;status=429"],
    )

    assert error.errors == ["provider_error", "rate_limit;status=429"]
    assert "SECRET_API_KEY" not in str(error)


async def test_all_fail_raises_aggregate(make_service, records):
    p1 = FakeProvider("p1", fail=ProviderError("timeout", retryable=True))
    p2 = FakeProvider(
        "p2",
        fail=ProviderError("http_error", retryable=True, status_code=503),
    )
    svc = make_service({"p1": p1, "p2": p2}, fallback_chain=[{"provider": "p2", "model": "m2"}])
    with pytest.raises(AllProvidersFailedError):
        await svc.generate_structured(
            task="t", messages=[{"role": "user", "content": "hi"}],
            output_model=FakeOutput, org_id=uuid4(),
        )
    assert [r["status"] for r in records] == ["error", "error"]


async def test_cooldown_skips_recently_failed_provider(make_service, records):
    """retryable 失败进入冷却:窗口内的下一次调用不再白等主线,直接走降级位。"""
    p1 = FakeProvider(
        "p1",
        fail=ProviderError("http_error", retryable=True, status_code=502),
    )
    p2 = FakeProvider("p2")
    svc = make_service({"p1": p1, "p2": p2}, fallback_chain=[{"provider": "p2", "model": "m2"}])
    args = dict(
        task="t", messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    await svc.generate_structured(**args)  # 第一次:p1 失败 → 降级 p2
    assert len(p1.calls) == 1
    await svc.generate_structured(**args)  # 第二次:p1 冷却中,直接 p2
    assert len(p1.calls) == 1
    assert len(p2.calls) == 2
    assert records[-1]["status"] == "success"
    assert records[-1]["fallback_from"] is None  # 跳过不算降级,不是失败后切换


async def test_cooldown_all_cooled_still_attempts(make_service, records):
    """全链都在冷却时按原链尝试,不能无路可走;成功即解除该线冷却。"""
    p1 = FakeProvider("p1")
    svc = make_service({"p1": p1}, fallback_chain=[])
    # SDK 化改造:冷却是 LLMService 实例属性(默认工厂注入进程级共享容器)
    svc._provider_cooldowns["p1:m1"] = service_module.time.monotonic() + 999
    result = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    assert result.answer == "from-p1"
    assert "p1:m1" not in svc._provider_cooldowns


async def test_structured_model_override_uses_exact_selected_model(make_service, records):
    """kb.caption.image 等能力敏感任务:锁定 provider/model,不进任务路由。"""
    p1 = FakeProvider("p1")
    p2 = FakeProvider("p2")
    svc = make_service(
        {"p1": p1, "p2": p2},
        fallback_chain=[{"provider": "p2", "model": "route-fallback"}],
    )

    result = await svc.generate_structured(
        task="t",
        messages=[{"role": "user", "content": "hi"}],
        output_model=FakeOutput,
        org_id=uuid4(),
        model_override={"provider": "p2", "model": "vision-model"},
    )

    assert result.answer == "from-p2"
    assert p1.calls == []
    assert p2.calls[0]["model"] == "vision-model"
    assert records[0]["provider"] == "p2"
    assert records[0]["model"] == "vision-model"


async def test_structured_model_override_does_not_silently_fallback(make_service, records):
    p1 = FakeProvider(
        "p1",
        fail=ProviderError("http_error", retryable=True, status_code=503),
    )
    p2 = FakeProvider("p2")
    svc = make_service(
        {"p1": p1, "p2": p2},
        fallback_chain=[{"provider": "p2", "model": "route-fallback"}],
    )

    with pytest.raises(AllProvidersFailedError):
        await svc.generate_structured(
            task="t",
            messages=[{"role": "user", "content": "hi"}],
            output_model=FakeOutput,
            org_id=uuid4(),
            model_override={"provider": "p1", "model": "vision-model"},
        )

    assert len(p1.calls) == 1
    assert p2.calls == []


async def test_tool_model_override_uses_exact_selected_model(make_service, records):
    p1 = FakeProvider("p1")
    p2 = FakeProvider("p2")
    svc = make_service(
        {"p1": p1, "p2": p2},
        fallback_chain=[{"provider": "p2", "model": "route-fallback"}],
    )

    result = await svc.generate_with_tools(
        task="t",
        system="test",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        org_id=uuid4(),
        model_override={"provider": "p2", "model": "explicit-model"},
    )

    assert result.text == "from-p2"
    assert p1.calls == []
    assert p2.calls[0]["model"] == "explicit-model"
    assert records[0]["provider"] == "p2"
    assert records[0]["model"] == "explicit-model"


async def test_tool_model_override_does_not_silently_fallback(make_service, records):
    p1 = FakeProvider(
        "p1",
        fail=ProviderError("http_error", retryable=True, status_code=503),
    )
    p2 = FakeProvider("p2")
    svc = make_service(
        {"p1": p1, "p2": p2},
        fallback_chain=[{"provider": "p2", "model": "route-fallback"}],
    )

    with pytest.raises(AllProvidersFailedError):
        await svc.generate_with_tools(
            task="t",
            system="test",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            org_id=uuid4(),
            model_override={"provider": "p1", "model": "explicit-model"},
        )

    assert len(p1.calls) == 1
    assert p2.calls == []
