"""TraceSink/UsageSink 协议单测(SDK 化新增):
LLMService._record 经注入 sink 落账、org 上下文绑定、默认 Sql 实现的行为。
全部 mock,不碰数据库与网络。
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

from pydantic import BaseModel

import nicekit.llm.service as service_module
from nicekit.llm.providers import ProviderResult
from nicekit.llm.registry import ResolvedRoute
from nicekit.llm.service import LLMService
from nicekit.llm.sinks import SqlTraceSink, SqlUsageSink
from nicekit.models.llm import LlmTrace


class FakeOutput(BaseModel):
    answer: str


class FakePrompt:
    version = 1
    content = "sys"


class FakeProvider:
    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail

    async def generate_structured(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        return ProviderResult(parsed=FakeOutput(answer="ok"), tokens_in=7, tokens_out=3)


class RecordingSession:
    """记录 execute/add/flush/commit 的假 AsyncSession(可当 context manager)。"""

    def __init__(self):
        self.executed: list = []
        self.added: list = []
        self.flushed = 0
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        return SimpleNamespace(scalar_one=lambda: 0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.commits += 1


class MockTraceSink:
    def __init__(self):
        self.payloads: list[dict] = []
        self.trace_id = uuid4()

    async def record_trace(self, session, trace_payload) -> UUID:
        self.payloads.append(trace_payload)
        return self.trace_id


class MockUsageSink:
    def __init__(self):
        self.calls: list[dict] = []

    async def record_usage(self, session, **usage_fields) -> None:
        self.calls.append(usage_fields)


def _patch(monkeypatch):
    async def fake_prompt(session, task):
        return FakePrompt()

    async def fake_route(session, task, org_id):
        return ResolvedRoute(
            primary_provider="p1", primary_model="m1",
            fallback_chain=[], max_tokens=128, timeout_seconds=30,
        )

    monkeypatch.setattr(service_module, "get_active_prompt", fake_prompt)
    monkeypatch.setattr(service_module, "resolve_route", fake_route)
    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: SimpleNamespace(
            llm_provider_cooldown_seconds=0,
            llm_org_daily_token_budget=0,
            llm_org_max_concurrency=0,
        ),
    )


async def test_success_records_via_both_sinks_with_org_context(monkeypatch) -> None:
    _patch(monkeypatch)
    session = RecordingSession()
    trace_sink, usage_sink = MockTraceSink(), MockUsageSink()
    svc = LLMService(
        {"p1": FakeProvider()},
        session_factory=lambda: session,
        trace_sink=trace_sink,
        usage_sink=usage_sink,
    )
    org_id = uuid4()

    out = await svc.generate_structured(
        task="t", messages=[{"role": "user", "content": "x"}],
        output_model=FakeOutput, org_id=org_id,
    )

    assert out.answer == "ok"
    payload = trace_sink.payloads[0]
    assert payload["org_id"] == org_id
    assert payload["status"] == "success"
    assert payload["tokens_in"] == 7 and payload["tokens_out"] == 3
    assert payload["prompt_version"] == 1
    assert usage_sink.calls[0]["tokens_in"] == 7
    assert usage_sink.calls[0]["calls"] == 1
    # RLS 正确性:自建 session 必须先绑定 org 上下文(set_config)
    set_configs = [
        params for stmt, params in session.executed
        if params is not None and str(params.get("org")) == str(org_id)
    ]
    assert set_configs, "落账前必须 set_config('app.current_org_id')"
    assert session.commits == 1


async def test_error_records_trace_but_not_usage(monkeypatch) -> None:
    _patch(monkeypatch)
    session = RecordingSession()
    trace_sink, usage_sink = MockTraceSink(), MockUsageSink()
    svc = LLMService(
        {"p1": FakeProvider()},
        session_factory=lambda: session,
        trace_sink=trace_sink,
        usage_sink=usage_sink,
    )
    await svc._record(
        org_id=uuid4(), task="t", provider="p1", model="m1",
        prompt_version=None, attempt=1, fallback_from=None,
        status="error", error="provider_error",
        tokens_in=0, tokens_out=0, latency_ms=5,
    )
    assert trace_sink.payloads[0]["status"] == "error"
    assert usage_sink.calls == []


async def test_tool_turn_carries_trace_id_from_sink(monkeypatch) -> None:
    _patch(monkeypatch)

    class ToolProvider:
        async def generate_with_tools(self, **kwargs):
            from nicekit.llm.providers import ToolLoopResult

            return ToolLoopResult(
                text="hi", tool_calls=[], stop_reason="end_turn",
                tokens_in=1, tokens_out=1,
            )

    trace_sink = MockTraceSink()
    svc = LLMService(
        {"p1": ToolProvider()},
        session_factory=RecordingSession,
        trace_sink=trace_sink,
        usage_sink=MockUsageSink(),
    )
    turn = await svc.generate_with_tools(
        task="t", system="s", messages=[{"role": "user", "content": "x"}],
        tools=[], org_id=uuid4(),
    )
    assert turn.trace_id == trace_sink.trace_id


async def test_sql_trace_sink_adds_llm_trace_row() -> None:
    session = RecordingSession()
    payload = {
        "org_id": uuid4(), "task": "t", "provider": "p1", "model": "m1",
        "prompt_version": 2, "attempt": 1, "fallback_from": None,
        "status": "success", "error": None, "tokens_in": 7, "tokens_out": 3,
        "latency_ms": 12, "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    await SqlTraceSink().record_trace(session, dict(payload))
    assert len(session.added) == 1
    trace = session.added[0]
    assert isinstance(trace, LlmTrace)
    assert trace.task == "t" and trace.tokens_in == 7 and trace.status == "success"
    # id 由列默认值在 flush 时生成,sink 必须 flush 才能把 id 交还调用方
    assert session.flushed == 1


async def test_sql_usage_sink_executes_upsert_stmt(monkeypatch) -> None:
    import nicekit.tenancy.usage as usage_module

    captured: list[dict] = []
    sentinel = object()

    def fake_stmt(**fields):
        captured.append(fields)
        return sentinel

    # SqlUsageSink 在调用期才 import usage_upsert_stmt,monkeypatch 模块属性即可生效
    monkeypatch.setattr(usage_module, "usage_upsert_stmt", fake_stmt)
    session = RecordingSession()
    await SqlUsageSink().record_usage(
        session, org_id=uuid4(), task="t", provider="p1", model="m1",
        tokens_in=7, tokens_out=3, calls=1,
        cache_read_tokens=0, cache_write_tokens=0, quantity=0,
    )
    assert captured[0]["tokens_in"] == 7
    assert session.executed[0][0] is sentinel
