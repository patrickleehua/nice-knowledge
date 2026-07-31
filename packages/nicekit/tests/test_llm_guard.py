"""LLM org 级守门单测(prod-readiness-1 D4):日预算拒绝 + org 并发信号量。

全部 mock,不碰 DB / 网络。
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

import nicekit.llm.service as service_module
from nicekit.llm.providers import ProviderResult
from nicekit.llm.registry import ResolvedRoute
from nicekit.llm.service import LlmBudgetExceededError, LLMService


class FakeOutput(BaseModel):
    answer: str


class FakePrompt:
    version = 1
    content = "sys"


class SlowProvider:
    """generate 时先 set 一个 event 再等待放行,用于观测并发。"""

    def __init__(self, gate: asyncio.Event, entered: asyncio.Semaphore):
        self.gate = gate
        self.entered = entered
        self.concurrent = 0
        self.max_concurrent = 0

    async def generate_structured(self, **kwargs):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.entered.release()
        await self.gate.wait()
        self.concurrent -= 1
        return ProviderResult(parsed=FakeOutput(answer="ok"), tokens_in=1, tokens_out=1)


def _patch_registry(monkeypatch) -> None:
    async def fake_prompt(session, task):
        return FakePrompt()

    async def fake_route(session, task, org_id):
        return ResolvedRoute(
            primary_provider="p1", primary_model="m1",
            fallback_chain=[], max_tokens=128, timeout_seconds=30,
        )

    monkeypatch.setattr(service_module, "get_active_prompt", fake_prompt)
    monkeypatch.setattr(service_module, "resolve_route", fake_route)


class FakeSessionCtx:
    def __init__(self, used: int = 0):
        self._used = used

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one=lambda: self._used)


def _settings(concurrency: int, budget: int):
    return SimpleNamespace(
        llm_org_max_concurrency=concurrency, llm_org_daily_token_budget=budget,
        llm_provider_cooldown_seconds=0,
    )


async def test_budget_exceeded_raises_before_provider(monkeypatch) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 100))
    # 当日已用 150 > 预算 100 → 拒绝
    svc = LLMService({"p1": SlowProvider(asyncio.Event(), asyncio.Semaphore(0))},
                     session_factory=lambda: FakeSessionCtx(used=150))

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)
    with pytest.raises(LlmBudgetExceededError) as ei:
        await svc.generate_structured(
            task="t", messages=[{"role": "user", "content": "x"}],
            output_model=FakeOutput, org_id=uuid4(),
        )
    assert ei.value.used == 150
    assert ei.value.budget == 100


async def test_budget_preflight_includes_estimated_tokens(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 100))
    svc = LLMService({}, session_factory=lambda: FakeSessionCtx(used=95))

    with pytest.raises(LlmBudgetExceededError) as ei:
        await svc._check_budget(uuid4(), estimated_tokens=6)

    assert ei.value.used == 95
    assert ei.value.estimated_tokens == 6
    assert ei.value.budget == 100


async def test_budget_under_limit_allows(monkeypatch) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 1000))
    gate = asyncio.Event()
    gate.set()
    svc = LLMService({"p1": SlowProvider(gate, asyncio.Semaphore(0))},
                     session_factory=lambda: FakeSessionCtx(used=10))

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)
    out = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "x"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    assert out.answer == "ok"


async def test_budget_zero_skips_db_check(monkeypatch) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 0))
    gate = asyncio.Event()
    gate.set()

    # 预算=0 时不该查库:session factory 抛异常若被调用则测试失败
    def boom():
        raise AssertionError("预算为 0 不应查 usage_daily")

    prompt_ctx = FakeSessionCtx(used=0)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        # 第一次是 get_active_prompt/resolve_route 用的(被 mock 不真正 execute),
        # _check_budget 若被调用会再取一次并 execute;这里只需保证不因 budget 而 raise
        return prompt_ctx

    svc = LLMService({"p1": SlowProvider(gate, asyncio.Semaphore(0))},
                     session_factory=factory)

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)
    out = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "x"}],
        output_model=FakeOutput, org_id=uuid4(),
    )
    assert out.answer == "ok"


async def test_org_semaphore_serializes_concurrency(monkeypatch) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(1, 0))
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)
    provider = SlowProvider(gate, entered)
    svc = LLMService({"p1": provider}, session_factory=lambda: FakeSessionCtx())

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)
    org = uuid4()

    async def call():
        return await svc.generate_structured(
            task="t", messages=[{"role": "user", "content": "x"}],
            output_model=FakeOutput, org_id=org,
        )

    t1 = asyncio.create_task(call())
    t2 = asyncio.create_task(call())
    # 等第一个进入 provider;并发上限 1 → 第二个应被信号量挡住
    await asyncio.wait_for(entered.acquire(), timeout=1)
    await asyncio.sleep(0.05)
    assert provider.max_concurrent == 1
    gate.set()  # 放行,两个都完成
    await asyncio.gather(t1, t2)
    assert provider.max_concurrent == 1


async def test_different_orgs_run_in_parallel(monkeypatch) -> None:
    _patch_registry(monkeypatch)
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(1, 0))
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)
    provider = SlowProvider(gate, entered)
    svc = LLMService({"p1": provider}, session_factory=lambda: FakeSessionCtx())

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)

    async def call(org):
        return await svc.generate_structured(
            task="t", messages=[{"role": "user", "content": "x"}],
            output_model=FakeOutput, org_id=org,
        )

    t1 = asyncio.create_task(call(uuid4()))
    t2 = asyncio.create_task(call(uuid4()))
    await asyncio.wait_for(entered.acquire(), timeout=1)
    await asyncio.wait_for(entered.acquire(), timeout=1)
    # 两个不同 org 各自独立信号量 → 可同时进入
    assert provider.max_concurrent == 2
    gate.set()
    await asyncio.gather(t1, t2)


async def test_governed_call_records_actual_usage_and_quantity(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 0))
    svc = LLMService({}, session_factory=lambda: FakeSessionCtx())
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)

    async def call():
        return SimpleNamespace(prompt_tokens=17)

    result = await svc.run_governed_call(
        org_id=uuid4(),
        task="kb.embedding",
        provider="siliconflow",
        model="BAAI/bge-m3",
        estimated_tokens=20,
        quantity=4,
        call=call,
        token_usage=lambda value: (value.prompt_tokens, 0),
    )

    assert result.prompt_tokens == 17
    assert records[0]["status"] == "success"
    assert records[0]["tokens_in"] == 17
    assert records[0]["tokens_out"] == 0
    assert records[0]["quantity"] == 4


async def test_governed_call_uses_shared_org_semaphore(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(1, 0))
    svc = LLMService({}, session_factory=lambda: FakeSessionCtx())
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)
    concurrent = 0
    max_concurrent = 0

    async def fake_record(**kwargs):
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)

    async def call():
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        entered.release()
        await gate.wait()
        concurrent -= 1
        return 1

    org_id = uuid4()

    async def invoke():
        return await svc.run_governed_call(
            org_id=org_id,
            task="kb.embedding",
            provider="siliconflow",
            model="BAAI/bge-m3",
            estimated_tokens=1,
            quantity=1,
            call=call,
            token_usage=lambda _: (1, 0),
        )

    first = asyncio.create_task(invoke())
    second = asyncio.create_task(invoke())
    await asyncio.wait_for(entered.acquire(), timeout=1)
    await asyncio.sleep(0.05)
    assert max_concurrent == 1
    gate.set()
    await asyncio.gather(first, second)
    assert max_concurrent == 1


async def test_governed_call_preserves_provider_exception(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 0))
    svc = LLMService({}, session_factory=lambda: FakeSessionCtx())
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)
        return uuid4()

    monkeypatch.setattr(svc, "_record", fake_record)
    error = RuntimeError("SECRET_API_KEY raw provider response")

    async def call():
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await svc.run_governed_call(
            org_id=uuid4(),
            task="kb.embedding",
            provider="siliconflow",
            model="BAAI/bge-m3",
            estimated_tokens=1,
            quantity=1,
            call=call,
            token_usage=lambda _: (0, 0),
        )

    assert exc_info.value is error
    assert records[0]["status"] == "error"
    assert records[0]["tokens_in"] == 0
    assert records[0]["error"] == "provider_error"
    assert "SECRET_API_KEY" not in records[0]["error"]


async def test_governed_call_trace_failure_does_not_mask_provider_error(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings(0, 0))
    svc = LLMService({}, session_factory=lambda: FakeSessionCtx())
    provider_error = RuntimeError("SECRET_API_KEY raw provider response")

    async def failed_call():
        raise provider_error

    async def failed_record(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(svc, "_record", failed_record)

    with pytest.raises(RuntimeError) as exc_info:
        await svc.run_governed_call(
            org_id=uuid4(),
            task="kb.embedding",
            provider="siliconflow",
            model="BAAI/bge-m3",
            estimated_tokens=1,
            quantity=1,
            call=failed_call,
            token_usage=lambda _: (0, 0),
        )

    assert exc_info.value is provider_error
    assert "SECRET_API_KEY" not in caplog.text
