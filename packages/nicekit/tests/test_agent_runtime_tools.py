"""运行时临时工具开关(nicekit/agent/runtime_tools.py)。

迁移自 TF tests/test_runtime_tools.py。SDK 化后的差异:
- 不再有模块级 TOOLS,一律用 `default_registry` 的快照;
- 排除名单从 `NOT_RUNTIME_GRANTABLE` 常量改成 `ToolDef.runtime_grantable` 标志位;
- 显示名从 TOOL_META 改成 ToolDef.label。

覆盖面:
- 安全底线——写工具 / 需确认工具 / 非 automatic 委派档一律拒绝,拒绝码正确;
- 未知工具被拒、已在卡白名单内只记审计不算覆盖;
- 排队输入携带覆盖(来源标 queued_input);
- 判定结果(含被拒项)进运行时上下文快照。

依赖 agent/service.py 的工具快照接线(_build_tool_snapshot)与 chat API 请求体
校验两段随 P2c/P4 交付,届时把 TF 里对应的 8 个用例搬回来。
"""

from uuid import uuid4

import nicekit.agent.builtin_tools  # noqa: F401  (触发内置工具注册)
from nicekit.agent.run_inputs import queue_event_payload
from nicekit.agent.runtime_snapshot import build_runtime_context_snapshot
from nicekit.agent.runtime_tools import (
    OUTCOME_ALREADY_ALLOWED,
    OUTCOME_GRANTED,
    REJECT_NOT_GRANTABLE,
    REJECT_UNKNOWN_TOOL,
    REJECT_WRITE_NOT_ALLOWED,
    SOURCE_QUEUED_INPUT,
    SOURCE_USER_INPUT,
    RuntimeToolOverride,
    available_runtime_tools,
    is_runtime_grantable,
    request_payloads,
    resolve_overrides,
)
from nicekit.agent.sub_agents import AGENT_TOOL_NAME
from nicekit.agent.tools import default_registry
from nicekit.models.chat import ChatRunInput

ALL_TOOLS = default_registry.as_dict()
CARD_TOOLS = ("kb_search", "memory_write")


def _codes(decisions: list[RuntimeToolOverride]) -> dict[str, str]:
    return {item.tool_name: item.reason for item in decisions}


# ---------------------------------------------------------------------------
# 判定规则
# ---------------------------------------------------------------------------


def test_read_only_tool_is_granted_for_this_turn() -> None:
    granted, decisions = resolve_overrides(["web_search"], CARD_TOOLS, ALL_TOOLS)
    assert granted == ["web_search"]
    decision = decisions[0]
    assert decision.accepted is True
    assert decision.reason == OUTCOME_GRANTED
    assert decision.reject_reason is None
    assert decision.source == SOURCE_USER_INPUT


def test_write_tool_is_rejected_with_reason() -> None:
    """安全底线:临时开关不得成为拿写权限的通道。"""
    granted, decisions = resolve_overrides(["plan_update"], CARD_TOOLS, ALL_TOOLS)
    assert granted == []
    assert decisions[0].accepted is False
    assert decisions[0].reject_reason == REJECT_WRITE_NOT_ALLOWED


def test_confirm_tool_is_rejected() -> None:
    """需要人点头的工具(confirm/reviewable 档)同样不能被临时开关跳过。"""
    confirm_tools = [name for name, tool in ALL_TOOLS.items() if tool.confirm]
    assert confirm_tools, "注册表里应当存在需确认的工具,否则这条底线无从验证"
    granted, decisions = resolve_overrides(confirm_tools, (), ALL_TOOLS)
    assert granted == []
    assert {item.reject_reason for item in decisions} == {REJECT_WRITE_NOT_ALLOWED}


def test_every_write_tool_in_registry_is_rejected() -> None:
    """全注册表扫描:任何 side_effect=write 的工具都开不出来(防新增工具漏网)。"""
    write_tools = [
        name for name, tool in ALL_TOOLS.items() if tool.side_effect == "write"
    ]
    granted, decisions = resolve_overrides(write_tools, (), ALL_TOOLS)
    assert granted == []
    assert all(item.reject_reason == REJECT_WRITE_NOT_ALLOWED for item in decisions)


def test_unknown_tool_is_rejected() -> None:
    granted, decisions = resolve_overrides(["totally_made_up"], CARD_TOOLS, ALL_TOOLS)
    assert granted == []
    assert decisions[0].reject_reason == REJECT_UNKNOWN_TOOL


def test_agent_delegation_tool_cannot_be_runtime_enabled() -> None:
    """Agent 委派工具由 sub_agents 按组织专员卡运行时注入,不在注册表里,
    因此走 unknown_tool 分支——用户无法用本轮开关自己点开委派。"""
    granted, decisions = resolve_overrides([AGENT_TOOL_NAME], CARD_TOOLS, ALL_TOOLS)
    assert granted == []
    assert decisions[0].reject_reason == REJECT_UNKNOWN_TOOL


def test_card_bound_readonly_tools_are_not_runtime_grantable() -> None:
    """技能/交互类工具的开关在卡片配置上,临时放开只会拿到空技能表或改掉自治设计。

    名单改由 ToolDef.runtime_grantable 自声明(TF 是模块级硬编码 frozenset)。
    """
    not_grantable = sorted(
        name for name, tool in ALL_TOOLS.items() if not tool.runtime_grantable
    )
    assert not_grantable == ["ask_user", "skill_view", "skills_list"]
    granted, decisions = resolve_overrides(not_grantable, (), ALL_TOOLS)
    assert granted == []
    assert all(item.reject_reason == REJECT_NOT_GRANTABLE for item in decisions)


def test_tool_already_in_card_allowlist_is_audited_but_not_an_override() -> None:
    granted, decisions = resolve_overrides(["kb_search"], CARD_TOOLS, ALL_TOOLS)
    assert granted == []  # 本来就能用,不重复并入,免得快照口径失真
    assert decisions[0].accepted is True
    assert decisions[0].reason == OUTCOME_ALREADY_ALLOWED
    assert decisions[0].reject_reason is None


def test_duplicates_and_blanks_are_ignored() -> None:
    granted, decisions = resolve_overrides(
        ["web_search", " web_search ", "", "  "], CARD_TOOLS, ALL_TOOLS
    )
    assert granted == ["web_search"]
    assert len(decisions) == 1  # 重复项不产生审计噪音


def test_queued_input_source_is_recorded() -> None:
    _granted, decisions = resolve_overrides(
        ["web_search"], CARD_TOOLS, ALL_TOOLS, source=SOURCE_QUEUED_INPUT
    )
    assert decisions[0].source == SOURCE_QUEUED_INPUT
    assert decisions[0].to_payload()["source"] == SOURCE_QUEUED_INPUT


def test_payload_carries_label_from_tool_def() -> None:
    _granted, decisions = resolve_overrides(["web_search"], (), ALL_TOOLS)
    payload = decisions[0].to_payload()
    assert payload["label"] == "联网搜索"
    assert "本轮" in payload["hint"]


def test_unknown_tool_payload_falls_back_to_name() -> None:
    _granted, decisions = resolve_overrides(["totally_made_up"], (), ALL_TOOLS)
    assert decisions[0].to_payload()["label"] == "totally_made_up"


# ---------------------------------------------------------------------------
# 候选列表:只读且不在卡白名单里
# ---------------------------------------------------------------------------


def test_available_candidates_are_read_only_and_outside_card_allowlist() -> None:
    candidates = available_runtime_tools(CARD_TOOLS, ALL_TOOLS)
    names = {item["name"] for item in candidates}
    assert "web_search" in names
    assert "kb_search" not in names  # 已在卡白名单里
    assert not names & {
        name for name, t in ALL_TOOLS.items() if t.side_effect == "write"
    }
    assert not names & {
        name for name, t in ALL_TOOLS.items() if not t.runtime_grantable
    }
    assert all(item["side_effect"] == "read" for item in candidates)
    assert all(item["label"] and item["category"] for item in candidates)


def test_candidates_match_grantability_rule() -> None:
    """候选列表与硬判定必须同源:列出来的每一个都要真能开出来。"""
    candidates = [item["name"] for item in available_runtime_tools((), ALL_TOOLS)]
    granted, _decisions = resolve_overrides(candidates, (), ALL_TOOLS)
    assert granted == candidates
    assert all(is_runtime_grantable(ALL_TOOLS[name]) for name in candidates)


# ---------------------------------------------------------------------------
# 排队输入携带覆盖
# ---------------------------------------------------------------------------


def _queued_item(runtime_tools: list[str] | None = None) -> ChatRunInput:
    return ChatRunInput(
        id=uuid4(),
        org_id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        user_id=uuid4(),
        content="顺便帮我上网查一下",
        position=1,
        runtime_tools=runtime_tools if runtime_tools is not None else [],
    )


def test_queued_input_defaults_to_no_runtime_tools() -> None:
    assert _queued_item().runtime_tools == []


def test_queue_event_payload_exposes_runtime_tools() -> None:
    payload = queue_event_payload("input.queued", _queued_item(["web_search"]))
    assert payload["runtime_tools"] == ["web_search"]


def test_queued_runtime_tools_follow_the_same_rules() -> None:
    item = _queued_item(["web_search", "memory_forget"])
    granted, decisions = resolve_overrides(
        item.runtime_tools, ("kb_search",), ALL_TOOLS, source=SOURCE_QUEUED_INPUT
    )
    assert granted == ["web_search"]
    assert _codes(decisions)["memory_forget"] == REJECT_WRITE_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 运行时上下文快照:判定结果(含被拒项)必须留痕
# ---------------------------------------------------------------------------


def test_snapshot_records_runtime_tool_requests() -> None:
    _granted, decisions = resolve_overrides(
        ["web_search", "plan_update", "kb_search"], ("kb_search",), ALL_TOOLS
    )
    snapshot = build_runtime_context_snapshot(
        card=None,
        thinking_level=None,
        model_override=None,
        system_prompt="p",
        capability_context=None,
        history=[],
        run_tools={},
        allowed_tools=[],
        policy_snapshot={},
        runtime_tool_requests=request_payloads(decisions),
    )
    recorded = {item["name"]: item for item in snapshot.runtime_tool_requests}
    assert recorded["web_search"]["accepted"] is True
    assert recorded["plan_update"]["accepted"] is False
    assert recorded["plan_update"]["reject_reason"] == REJECT_WRITE_NOT_ALLOWED
    assert recorded["kb_search"]["reason"] == OUTCOME_ALREADY_ALLOWED
    # to_event_payload 必须可 JSON 序列化(随 chat_events 落库)
    import json

    json.dumps(snapshot.to_event_payload(), ensure_ascii=False)


def test_snapshot_without_runtime_requests_stays_empty() -> None:
    snapshot = build_runtime_context_snapshot(
        card=None,
        thinking_level=None,
        model_override=None,
        system_prompt="p",
        capability_context=None,
        history=[],
        run_tools={},
        allowed_tools=[],
        policy_snapshot={},
    )
    assert snapshot.runtime_tool_requests == []
