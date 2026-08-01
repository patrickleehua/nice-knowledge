"""agent/service.py 专测:历史重建、续跑拼接与四个扩展点。

不碰真实 DB / LLM:`_build_tool_snapshot` 与两个上下文派生函数都只需要一个
能回答 `execute()` 的替身会话。按 A10 纪律,替身一律 monkeypatch 到**包对象
属性**上(`monkeypatch.setattr(service, "org_session", ...)`),不动 sys.modules。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.agent import service
from nicekit.agent.compression import (
    SUMMARY_HISTORY_HEADER,
    rows_after_summary,
    summary_user_message,
)
from nicekit.agent.mcp_manager import McpToolSpec
from nicekit.agent.service import (
    CapabilityOutcome,
    ContextBlock,
    ResolvedAgentCard,
    business_facts_for,
    default_card_name,
    register_capability_probe,
    register_context_provider,
    reset_capability_probes,
    reset_context_providers,
    rows_to_history,
    set_business_facts_provider,
    set_default_card_name,
    splice_pending_bundle,
    splice_pending_call,
)
from nicekit.agent.sub_agents import DelegatableAgent, build_sub_agent_tools
from nicekit.agent.tools import (
    ToolContext,
    ToolDef,
    ToolRegistry,
    tool_permission,
)
from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolReversibility,
    ToolRisk,
)
from nicekit.models.chat import (
    ChatMessage,
    ChatSession,
    ChatSessionOriginMode,
    ChatSessionSummary,
)
from nicekit.models.mcp import McpServer

ORG_ID = uuid4()
USER_ID = uuid4()
SESSION_ID = uuid4()


# ---------------------------------------------------------------------------
# 历史重建(搬自 TF tests/test_agent_history.py)
# ---------------------------------------------------------------------------


def message(sequence: int, role: str, **values) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        sequence=sequence,
        role=role,
        **values,
    )


def test_multiple_tool_rows_attach_to_one_assistant_turn() -> None:
    history = rows_to_history(
        [
            message(1, "user", content="查资料"),
            message(2, "assistant", content=None),
            message(
                3,
                "tool",
                content='{"one": true}',
                tool_name="one",
                tool_call_id="c1",
                tool_input={"a": 1},
            ),
            message(
                4,
                "tool",
                content='{"two": true}',
                tool_name="two",
                tool_call_id="c2",
                tool_input={"b": 2},
            ),
            message(5, "assistant", content="完成"),
        ]
    )
    assert [item["role"] for item in history] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert history[1]["tool_calls"] == [
        {"id": "c1", "name": "one", "arguments": {"a": 1}},
        {"id": "c2", "name": "two", "arguments": {"b": 2}},
    ]


def test_orphan_empty_assistant_is_not_sent_to_provider() -> None:
    history = rows_to_history(
        [
            message(1, "user", content="你好"),
            message(2, "assistant", content=None),
            message(3, "user", content="继续"),
            message(4, "assistant", content="可以"),
        ]
    )
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "可以", "tool_calls": []},
    ]


def test_orphan_tool_row_gets_a_legal_tool_use_parent() -> None:
    history = rows_to_history(
        [
            message(1, "user", content="继续"),
            message(
                2,
                "tool",
                content='{"ok": true}',
                tool_name="image_generate",
                tool_call_id="img-1",
                tool_input={"prompt": "海报"},
            ),
        ]
    )
    assert history[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "img-1", "name": "image_generate", "arguments": {"prompt": "海报"}}
        ],
    }
    assert history[2]["role"] == "tool"


def test_compressed_history_replaces_covered_rows_with_one_summary() -> None:
    """A9 待补项:compression 与 rows_to_history 的接缝。

    被摘要覆盖的行不进模型历史,摘要以 user 角色顶在最前;未覆盖的尾部原样重建
    (含 assistant→tool 的回挂关系)。
    """
    rows = [
        message(1, "user", content="第一轮"),
        message(2, "assistant", content="回答一"),
        message(3, "user", content="第二轮"),
        message(4, "assistant", content=None),
        message(
            5,
            "tool",
            content='{"ok": true}',
            tool_name="kb_search",
            tool_call_id="c1",
            tool_input={"q": "x"},
        ),
        message(6, "assistant", content="回答二"),
    ]
    summary = ChatSessionSummary(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        content="早前对话讲了甲和乙",
        covered_until_sequence=3,
        status="success",
    )
    kept = rows_after_summary(rows, summary)
    assert [row.sequence for row in kept] == [4, 5, 6]

    history = rows_to_history(kept)
    history.insert(0, summary_user_message(summary))
    assert history[0] == {
        "role": "user",
        "content": SUMMARY_HISTORY_HEADER + "早前对话讲了甲和乙",
    }
    assert history[1]["tool_calls"] == [
        {"id": "c1", "name": "kb_search", "arguments": {"q": "x"}}
    ]
    assert history[2]["role"] == "tool"
    assert history[-1]["content"] == "回答二"


def test_rows_after_summary_without_summary_keeps_everything() -> None:
    rows = [message(1, "user", content="a"), message(2, "assistant", content="b")]
    assert rows_after_summary(rows, None) == rows


# ---------------------------------------------------------------------------
# 续跑拼接
# ---------------------------------------------------------------------------


def test_splice_pending_call_closes_the_conversation() -> None:
    history = [{"role": "user", "content": "删一下"}]
    persisted = splice_pending_call(
        history,
        tool_call_id="c1",
        name="danger",
        args={"id": "1"},
        result={"ok": True},
    )
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"] == [
        {"id": "c1", "name": "danger", "arguments": {"id": "1"}}
    ]
    assert history[2]["role"] == "tool"
    assert persisted == [history[2]]


def test_splice_pending_bundle_preserves_item_order() -> None:
    history = [{"role": "user", "content": "批量执行"}]
    items = [
        {"order": 0, "tool_call_id": "c1", "name": "a", "input": {"x": 1}},
        {"order": 1, "tool_call_id": "c2", "name": "b", "input": {"x": 2}},
    ]
    persisted = splice_pending_bundle(
        history, items=items, results=[{"ok": True}, {"ok": False}]
    )
    assistant = history[1]
    assert [call["id"] for call in assistant["tool_calls"]] == ["c1", "c2"]
    assert [msg["tool_call_id"] for msg in persisted] == ["c1", "c2"]


def test_splice_pending_bundle_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        splice_pending_bundle(
            [], items=[{"order": 0, "tool_call_id": "c1", "name": "a"}], results=[]
        )


# ---------------------------------------------------------------------------
# 扩展点:缺省卡名
# ---------------------------------------------------------------------------


def test_default_card_name_is_assistant_and_configurable() -> None:
    assert default_card_name() == "assistant"
    try:
        set_default_card_name("workbench")
        assert default_card_name() == "workbench"
        set_default_card_name("  ")  # 空白回落默认
        assert default_card_name() == "assistant"
    finally:
        set_default_card_name(None)
    assert default_card_name() == "assistant"


# ---------------------------------------------------------------------------
# 扩展点:business_facts
# ---------------------------------------------------------------------------


def _chat_session(**values) -> ChatSession:
    defaults = {
        "id": SESSION_ID,
        "org_id": ORG_ID,
        "user_id": USER_ID,
        "agent_card_id": uuid4(),
        "title": "新对话",
    }
    return ChatSession(**{**defaults, **values})


def test_default_business_facts_expose_mode_and_bound_scope() -> None:
    scope_id = uuid4()
    chat = _chat_session(
        scope_type="ticket",
        scope_id=scope_id,
        origin_mode=ChatSessionOriginMode.SCOPED,
    )
    assert business_facts_for(chat) == {
        "conversation_mode": "scoped",
        "bound_scope": {"scope_type": "ticket", "scope_id": str(scope_id)},
    }


def test_business_facts_provider_can_be_injected_and_fails_soft() -> None:
    chat = _chat_session()
    try:
        set_business_facts_provider(lambda session: {"tenant_tier": "gold"})
        assert business_facts_for(chat) == {"tenant_tier": "gold"}

        def _boom(_chat):
            raise RuntimeError("宿主派生炸了")

        set_business_facts_provider(_boom)
        # 宿主实现异常不打断审批评审,降级回 SDK 默认事实
        assert business_facts_for(chat)["conversation_mode"] == "general"
    finally:
        set_business_facts_provider(None)
    assert business_facts_for(chat)["conversation_mode"] == "general"


# ---------------------------------------------------------------------------
# 扩展点:ContextProvider
# ---------------------------------------------------------------------------


class _FakeSession:
    """只回答 execute/rollback/close 的替身会话。"""

    def __init__(self, results: list | None = None) -> None:
        self._results = list(results or [])
        self.rollbacks = 0
        self.closed = False

    async def execute(self, _statement):
        return self._results.pop(0) if self._results else _FakeResult([])

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_context_providers()
    reset_capability_probes()
    yield
    reset_context_providers()
    reset_capability_probes()


class _Provider:
    def __init__(self, name, block, *, budget=2500, timeout=2.0):
        self.name = name
        self.budget_chars = budget
        self.timeout_seconds = timeout
        self._block = block
        self.calls = 0

    async def derive(self, session, ctx):
        self.calls += 1
        if callable(self._block):
            return await self._block(session, ctx)
        return self._block


async def test_context_providers_inject_in_registration_order() -> None:
    register_context_provider(_Provider("first", ContextBlock("scope_state", "【当前作用域】开着")))
    register_context_provider(
        _Provider("second", ContextBlock("sla", "【SLA】4 小时", notes=("sla_partial",)))
    )
    session = _FakeSession()

    sections, notes = await service._resolve_context_blocks(
        session,
        org_id=ORG_ID,
        user_id=USER_ID,
        chat=_chat_session(),
        query_text="怎么办",
    )
    assert sections == [("scope_state", "【当前作用域】开着"), ("sla", "【SLA】4 小时")]
    assert notes == ["sla_partial"]


async def test_context_provider_over_budget_is_dropped_whole() -> None:
    register_context_provider(
        _Provider("fat", ContextBlock("scope_state", "x" * 50), budget=10)
    )
    sections, notes = await service._resolve_context_blocks(
        _FakeSession(), org_id=ORG_ID, user_id=USER_ID, chat=_chat_session(), query_text=""
    )
    assert sections == []
    assert notes and notes[0].startswith("fat_skipped:")


async def test_context_provider_timeout_rolls_back_and_notes() -> None:
    async def _slow(_session, _ctx):
        await asyncio.sleep(1)
        return ContextBlock("scope_state", "太慢了")

    register_context_provider(_Provider("slow", _slow, timeout=0.01))
    session = _FakeSession()
    sections, notes = await service._resolve_context_blocks(
        session, org_id=ORG_ID, user_id=USER_ID, chat=_chat_session(), query_text=""
    )
    assert sections == []
    assert notes and "超出" in notes[0]
    assert session.rollbacks == 1


async def test_context_provider_failure_never_breaks_the_turn() -> None:
    async def _boom(_session, _ctx):
        raise RuntimeError("派生失败")

    register_context_provider(_Provider("broken", _boom))
    register_context_provider(_Provider("ok", ContextBlock("sla", "【SLA】仍然注入")))
    session = _FakeSession()
    sections, notes = await service._resolve_context_blocks(
        session, org_id=ORG_ID, user_id=USER_ID, chat=_chat_session(), query_text=""
    )
    assert sections == [("sla", "【SLA】仍然注入")]
    assert notes == ["broken_failed:上下文派生失败,本轮未注入"]
    assert session.rollbacks == 1


async def test_no_registered_provider_means_no_extra_sections() -> None:
    sections, notes = await service._resolve_context_blocks(
        _FakeSession(), org_id=ORG_ID, user_id=USER_ID, chat=_chat_session(), query_text=""
    )
    assert sections == []
    assert notes == []


# ---------------------------------------------------------------------------
# 扩展点:CapabilityProbe
# ---------------------------------------------------------------------------


def test_sdk_registers_websearch_and_imagegen_probes_by_default() -> None:
    names = [probe.name for probe in service.registered_capability_probes()]
    assert names == ["websearch", "imagegen"]


async def test_probe_is_skipped_when_none_of_its_tools_are_in_the_snapshot() -> None:
    class _Probe:
        name = "never"
        tools = frozenset({"absent_tool"})

        def __init__(self) -> None:
            self.calls = 0

        async def probe(self, request):
            self.calls += 1
            return CapabilityOutcome(notes=("不该被调用",))

    reset_capability_probes()
    probe = _Probe()
    service._capability_probes.clear()
    register_capability_probe(probe)
    drop, notes, runtime = await service._run_capability_probes(
        _FakeSession(), {"kb_search"}, web_search_choice=None
    )
    assert probe.calls == 0
    assert (drop, notes) == (set(), [])
    assert runtime == {"provider": None, "builtin": None}


async def test_probe_failure_is_fail_closed(monkeypatch) -> None:
    class _Probe:
        name = "flaky"
        tools = frozenset({"web_search"})

        async def probe(self, request):
            raise RuntimeError("配置读不出来")

    service._capability_probes.clear()
    register_capability_probe(_Probe())
    session = _FakeSession()
    drop, notes, _runtime = await service._run_capability_probes(
        session, {"web_search"}, web_search_choice=None
    )
    assert drop == {"web_search"}
    assert notes and "flaky" in notes[0]
    assert session.rollbacks == 1


def _websearch_readiness(*, ready=True, allows=True):
    return SimpleNamespace(
        ready=ready,
        builtin={"max_uses": 3},
        allows=lambda choice: allows,
    )


async def test_websearch_probe_off_drops_both_tools(monkeypatch) -> None:
    from nicekit.capabilities.websearch import service as websearch_service

    async def _readiness(_session):
        return _websearch_readiness()

    monkeypatch.setattr(websearch_service, "resolve_websearch_readiness", _readiness)
    drop, notes, runtime = await service._run_capability_probes(
        _FakeSession(), {"web_search", "web_fetch"}, web_search_choice="off"
    )
    assert drop == {"web_search", "web_fetch"}
    assert notes and "已被用户关闭" in notes[0]
    assert runtime == {"provider": None, "builtin": None}


async def test_websearch_probe_builtin_keeps_fetch_and_sets_runtime(monkeypatch) -> None:
    from nicekit.capabilities.websearch import service as websearch_service

    async def _readiness(_session):
        return _websearch_readiness()

    monkeypatch.setattr(websearch_service, "resolve_websearch_readiness", _readiness)
    drop, notes, runtime = await service._run_capability_probes(
        _FakeSession(), {"web_search", "web_fetch"}, web_search_choice="builtin"
    )
    assert drop == {"web_search"}
    assert runtime["builtin"] == {"max_uses": 3}
    assert notes and "模型内置联网搜索" in notes[0]


async def test_websearch_probe_unavailable_choice_degrades(monkeypatch) -> None:
    from nicekit.capabilities.websearch import service as websearch_service

    async def _readiness(_session):
        return _websearch_readiness(ready=False, allows=False)

    monkeypatch.setattr(websearch_service, "resolve_websearch_readiness", _readiness)
    drop, notes, runtime = await service._run_capability_probes(
        _FakeSession(), {"web_search"}, web_search_choice="tavily"
    )
    assert drop == {"web_search", "web_fetch"}
    assert notes and "未提供联网搜索" in notes[0]
    assert runtime["provider"] is None


async def test_websearch_probe_passes_provider_override(monkeypatch) -> None:
    from nicekit.capabilities.websearch import service as websearch_service

    async def _readiness(_session):
        return _websearch_readiness()

    monkeypatch.setattr(websearch_service, "resolve_websearch_readiness", _readiness)
    drop, notes, runtime = await service._run_capability_probes(
        _FakeSession(), {"web_search"}, web_search_choice="bocha"
    )
    assert drop == set()
    assert notes == []
    assert runtime["provider"] == "bocha"


async def test_imagegen_probe_drops_tool_when_not_ready(monkeypatch) -> None:
    from nicekit.capabilities.imagegen import service as imagegen_service

    async def _readiness(_session):
        return SimpleNamespace(ready=False)

    monkeypatch.setattr(imagegen_service, "resolve_image_service_readiness", _readiness)
    drop, notes, _runtime = await service._run_capability_probes(
        _FakeSession(), {"image_generate"}, web_search_choice=None
    )
    assert drop == {"image_generate"}
    assert notes and "图片生成" in notes[0]


# ---------------------------------------------------------------------------
# 工具快照
# ---------------------------------------------------------------------------

_READ_PERMISSION = tool_permission(
    ToolEffect.READ,
    ToolRisk.ROUTINE,
    {ToolCategory.LOCAL_DATA},
    ToolReversibility.REVERSIBLE,
    ToolDelegation.AUTOMATIC,
    PermissionScope.SESSION,
)
_WRITE_PERMISSION = tool_permission(
    ToolEffect.WRITE,
    ToolRisk.SENSITIVE,
    {ToolCategory.LOCAL_DATA},
    ToolReversibility.REVERSIBLE,
    ToolDelegation.REVIEWABLE,
    PermissionScope.SESSION,
)
_EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _registry() -> ToolRegistry:
    registry = ToolRegistry("test")

    async def _noop(_ctx, _args):
        return {"ok": True}

    registry.add(
        ToolDef(
            name="reader",
            description="读",
            schema=_EMPTY_SCHEMA,
            executor=_noop,
            permission=_READ_PERMISSION,
            label="读工具",
        )
    )
    registry.add(
        ToolDef(
            name="writer",
            description="写",
            schema=_EMPTY_SCHEMA,
            executor=_noop,
            permission=_WRITE_PERMISSION,
            label="写工具",
            stage="delivery",
            # REVIEWABLE 档经 registry.register() 会派生 confirm=True;这里直接
            # 构造 ToolDef,显式给上以复现生产口径
            confirm=True,
        )
    )
    registry.add(
        ToolDef(
            name="skills_list",
            description="技能",
            schema=_EMPTY_SCHEMA,
            executor=_noop,
            permission=_READ_PERMISSION,
            runtime_grantable=False,
        )
    )
    registry.add(
        ToolDef(
            name="skill_view",
            description="技能文件",
            schema=_EMPTY_SCHEMA,
            executor=_noop,
            permission=_READ_PERMISSION,
            runtime_grantable=False,
        )
    )
    registry.add(
        ToolDef(
            name="spare_reader",
            description="备用读",
            schema=_EMPTY_SCHEMA,
            executor=_noop,
            permission=_READ_PERMISSION,
        )
    )
    return registry


def _card(**values) -> ResolvedAgentCard:
    defaults = {
        "card_id": uuid4(),
        "version_id": uuid4(),
        "name": "assistant",
        "system_prompt": "角色",
        "model_task": "agent.default",
        "max_turns": 8,
        "timeout_seconds": 300,
        "tools": ("reader", "writer"),
        "mcp_bindings": (),
        "skills": (),
    }
    return ResolvedAgentCard(**{**defaults, **values})


async def _noop_emit(_event: dict) -> None:
    return None


async def _snapshot(card, session, **kwargs):
    return await service._build_tool_snapshot(
        session,
        card,
        _noop_emit,
        session_factory=object(),
        org_id=ORG_ID,
        user_id=USER_ID,
        role="member",
        chat_session_id=SESSION_ID,
        registry=_registry(),
        **kwargs,
    )


async def test_snapshot_takes_card_whitelist_and_hides_skill_tools() -> None:
    service._capability_probes.clear()
    tools, allowed, capability, runtime, decisions = await _snapshot(
        _card(tools=("reader", "writer", "skills_list")), _FakeSession()
    )
    assert set(tools) == {"reader", "writer"}  # 卡没绑技能包 → 技能工具不出现
    assert set(allowed) == {"reader", "writer"}
    assert capability is None
    assert runtime == {"provider": None, "builtin": None}
    assert decisions == []


async def test_snapshot_adds_skill_tools_when_card_binds_skills() -> None:
    service._capability_probes.clear()
    tools, _allowed, _cap, _rt, _dec = await _snapshot(
        _card(tools=("reader",), skills=("demo",)), _FakeSession()
    )
    assert {"skills_list", "skill_view"} <= set(tools)


async def test_snapshot_merges_runtime_tool_overrides_and_reports_rejections() -> None:
    """A9 待补项:runtime_tools 与工具快照的接缝(只读放行 / 写工具拒绝)。"""
    service._capability_probes.clear()
    tools, allowed, _cap, _rt, decisions = await _snapshot(
        _card(tools=("reader",)),
        _FakeSession(),
        runtime_overrides=["spare_reader", "writer", "ghost"],
    )
    assert "spare_reader" in tools and "spare_reader" in allowed
    assert "writer" not in tools  # 写工具不能临时开(权限提升通道被堵死)
    by_name = {decision.tool_name: decision for decision in decisions}
    assert by_name["spare_reader"].accepted is True
    assert by_name["writer"].accepted is False
    assert by_name["ghost"].reject_reason == "unknown_tool"


async def test_snapshot_drops_tools_that_a_probe_reports_unavailable() -> None:
    class _Probe:
        name = "writer_gate"
        tools = frozenset({"writer"})

        async def probe(self, request):
            return CapabilityOutcome(drop_tools=("writer",), notes=("写能力暂不可用",))

    service._capability_probes.clear()
    register_capability_probe(_Probe())
    tools, allowed, capability, _rt, _dec = await _snapshot(_card(), _FakeSession())
    assert set(tools) == {"reader"}
    assert "writer" not in allowed
    assert capability == "写能力暂不可用"


async def test_snapshot_preserves_tool_flags_through_isolation_wrapper() -> None:
    service._capability_probes.clear()
    tools, _allowed, _cap, _rt, _dec = await _snapshot(_card(), _FakeSession())
    # stage / runtime_grantable / emit_start_progress 都必须原样透过隔离包装,
    # 否则 stage.update 与临时工具判定会在运行期悄悄失效
    assert tools["writer"].stage == "delivery"
    assert tools["writer"].side_effect == "write"
    assert tools["reader"].runtime_grantable is True
    assert tools["reader"].label == "读工具"


async def test_snapshot_registers_mcp_tools_with_derived_permission(monkeypatch) -> None:
    """A4:MCP 工具权限走 mcp_tool_permission,不再一律按 write 处理。"""
    service._capability_probes.clear()
    server = McpServer(
        id=uuid4(),
        org_id=ORG_ID,
        name="files",
        transport="streamable_http",
        endpoint_url="https://example.invalid/mcp",
    )
    specs = [
        McpToolSpec(
            name="read_file",
            description="读文件",
            schema=_EMPTY_SCHEMA,
            read_only_hint=True,
        ),
        McpToolSpec(name="write_file", description="写文件", schema=_EMPTY_SCHEMA),
        McpToolSpec(name="hidden", description="被服务端白名单挡住", schema=_EMPTY_SCHEMA),
    ]
    server.enabled_tools = ["read_file", "write_file"]

    async def _list_tools(_server):
        return specs

    monkeypatch.setattr(service.mcp_client_manager, "list_tools", _list_tools)
    session = _FakeSession([_FakeResult([server])])
    card = _card(
        mcp_bindings=({"server_id": str(server.id), "enabled_tools": ["read_file", "write_file"]},)
    )
    tools, allowed, _cap, _rt, _dec = await _snapshot(card, session)

    assert "mcp__files__read_file" in tools
    assert "mcp__files__write_file" in tools
    assert "mcp__files__hidden" not in tools  # 双层白名单求交
    assert tools["mcp__files__read_file"].permission.effect is ToolEffect.READ
    assert tools["mcp__files__write_file"].permission.effect is ToolEffect.WRITE
    assert allowed[-2:] == ["mcp__files__read_file", "mcp__files__write_file"]


async def test_snapshot_isolated_executor_runs_on_its_own_session(monkeypatch) -> None:
    """每次工具调用一条独立连接;引用游标与取消登记簿必须沿用同一实例。"""
    service._capability_probes.clear()
    registry = ToolRegistry("iso")
    seen: dict = {}

    async def _capture(ctx: ToolContext, args: dict) -> dict:
        seen["session"] = ctx.session
        seen["citations"] = ctx.citations
        seen["cancellable"] = ctx.cancellable_resources
        seen["role"] = ctx.role
        return {"ok": True}

    registry.add(
        ToolDef(
            name="reader",
            description="读",
            schema=_EMPTY_SCHEMA,
            executor=_capture,
            permission=_READ_PERMISSION,
        )
    )

    chat = _chat_session()
    call_session = _FakeSession([_FakeResult([chat])])
    monkeypatch.setattr(service, "org_session", lambda _factory, _org: call_session)

    tools, _allowed, _cap, _rt, _dec = await service._build_tool_snapshot(
        _FakeSession(),
        _card(tools=("reader",)),
        _noop_emit,
        session_factory=object(),
        org_id=ORG_ID,
        user_id=USER_ID,
        role="member",
        chat_session_id=SESSION_ID,
        registry=registry,
    )
    outer = _FakeSession()
    ctx = ToolContext(
        session=outer,
        org_id=ORG_ID,
        user_id=USER_ID,
        role="member",
        chat_session=chat,
    )
    ctx.cancellable_resources.add("job-1")
    assert await tools["reader"].executor(ctx, {}) == {"ok": True}
    assert seen["session"] is call_session
    assert seen["session"] is not outer
    assert seen["citations"] is ctx.citations
    assert seen["cancellable"] is ctx.cancellable_resources
    assert seen["role"] == "member"
    assert call_session.closed is True


async def test_sub_agent_tools_derive_from_the_run_snapshot() -> None:
    """A9 待补项:专员可用面只能是主轮快照的子集,且剔掉需确认工具。"""
    service._capability_probes.clear()
    tools, _allowed, _cap, _rt, _dec = await _snapshot(
        _card(tools=("reader", "writer")), _FakeSession()
    )
    specialist = DelegatableAgent(
        card_id=uuid4(),
        name="研究员",
        description="查资料",
        system_prompt="只查资料",
        model_task="agent.default",
        max_turns=6,
        timeout_seconds=300,
        tools=("reader", "writer", "Agent", "ask_user", "not_in_snapshot"),
    )
    sub_tools = build_sub_agent_tools(specialist, tools)
    # writer 是 REVIEWABLE → confirm=True → 子 agent 内没有人能批准,整体剔除
    assert set(sub_tools) == {"reader"}


# ---------------------------------------------------------------------------
# stage.update 契约与业务残留
# ---------------------------------------------------------------------------

_SERVICE_SOURCE = Path(service.__file__).read_text(encoding="utf-8")


def test_stage_event_reads_tool_declaration_not_tool_name_prefix() -> None:
    assert '"type": "stage.update"' in _SERVICE_SOURCE
    assert '"stage": tool.stage' in _SERVICE_SOURCE
    # TF 的按工具名前缀推导旅游阶段必须彻底消失
    for residue in ("startswith((\"render", "quote", "itinerary", "demand"):
        assert residue not in _SERVICE_SOURCE


def test_tool_def_stage_defaults_to_none() -> None:
    async def _noop(_ctx, _args):
        return {}

    tool = ToolDef(
        name="plain",
        description="",
        schema=_EMPTY_SCHEMA,
        executor=_noop,
        permission=_READ_PERMISSION,
    )
    assert tool.stage is None


@pytest.mark.parametrize(
    "word", ("project_id", "customer_id", "itinerary", "quote", "旅游", "旅行社")
)
def test_service_has_no_business_residue(word: str) -> None:
    assert word not in _SERVICE_SOURCE
