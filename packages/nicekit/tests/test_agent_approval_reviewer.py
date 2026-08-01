"""Independent approval-reviewer contract, serialization, and loop integration."""

import json
from dataclasses import replace
from uuid import uuid4

from nicekit.agent.loop import ToolPreflight, run_loop
from nicekit.agent.permissions import (
    APPROVAL_REVIEW_SYSTEM_PROMPT,
    DEFAULT_APPROVAL_REVIEW_SYSTEM_PROMPT,
    ReviewerDecision,
    review_tool_action,
    serialize_reviewer_context,
    set_approval_review_system_prompt,
)
from nicekit.agent.tools import ToolDef
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolPermissionSpec,
    ToolReversibility,
    ToolRisk,
)
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import NoRouteError, ToolTurn


class StructuredClient:
    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict] = []

    async def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.output


def _turn(*calls: ToolCallRequest, text: str | None = None) -> ToolTurn:
    return ToolTurn(
        text=text,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        tokens_in=1,
        tokens_out=1,
        trace_id=None,
    )


def _script(*turns: ToolTurn):
    iterator = iter(turns)

    async def call(_messages, _schemas):
        return next(iterator)

    return call


def _tool(executed: list[dict]) -> ToolDef:
    async def executor(_ctx, arguments) -> dict:
        executed.append(arguments)
        return {"done": True}

    return ToolDef(
        name="reviewable",
        description="reviewable",
        schema={"type": "object", "properties": {}},
        executor=executor,
        permission=ToolPermissionSpec(
            effect=ToolEffect.WRITE,
            risk=ToolRisk.SENSITIVE,
            categories=frozenset({ToolCategory.EXTERNAL_COST}),
            reversibility=ToolReversibility.REVERSIBLE,
            delegation=ToolDelegation.REVIEWABLE,
            scope=PermissionScope.SESSION,
            material_arguments=frozenset({"prompt"}),
        ),
    )


def _routed(arguments: dict) -> ToolPreflight:
    return ToolPreflight(
        decision=PermissionDecision.AUTO_REVIEW,
        reason_code="profile_reviewable",
        source="profile",
        action={
            "sanitized_arguments": arguments,
            "argument_hash": "a" * 64,
            "action_hash": "b" * 64,
            "material_fingerprint": "c" * 64,
            "scope": {"mode": "general", "session_id": str(uuid4())},
        },
    )


async def test_reviewer_output_is_strict_and_fail_closed() -> None:
    valid = StructuredClient(
        {
            "decision": "approve",
            "rationale": "用户明确要求生成这一张海报。",
            "risk_flags": ["external_cost"],
        }
    )
    result = await review_tool_action(valid, org_id=uuid4(), context={"safe": True})
    assert result.decision is ReviewerDecision.APPROVE
    assert result.reason_code == "reviewer_approve"
    assert [flag.value for flag in result.risk_flags] == ["external_cost"]
    assert valid.calls[0]["task"] == "agent.approval_review"
    assert valid.calls[0]["output_model"].model_config["extra"] == "forbid"

    malformed = StructuredClient(
        {
            "decision": "approve",
            "rationale": "不应接受额外字段",
            "risk_flags": [],
            "command": "execute",
        }
    )
    malformed_result = await review_tool_action(
        malformed, org_id=uuid4(), context={}
    )
    assert malformed_result.decision is ReviewerDecision.ESCALATE
    assert malformed_result.reason_code == "reviewer_malformed"
    assert malformed_result.available is False

    unavailable = StructuredClient(error=NoRouteError("missing"))
    unavailable_result = await review_tool_action(
        unavailable, org_id=uuid4(), context={}
    )
    assert unavailable_result.decision is ReviewerDecision.ESCALATE
    assert unavailable_result.reason_code == "reviewer_unavailable"


def test_reviewer_context_contains_only_bounded_redacted_visible_data() -> None:
    action = {
        "sanitized_arguments": {
            "prompt": "活动海报",
            "token": "[REDACTED]",
        },
        "argument_hash": "a" * 64,
        "action_hash": "b" * 64,
        "material_fingerprint": "c" * 64,
        "scope": {"mode": "general", "target_scope_id": None},
    }
    context = serialize_reviewer_context(
        history=[
            {"role": "user", "content": "请生图 token=top-secret"},
            {"role": "thought", "content": "隐藏推理不应出现"},
            {"role": "tool", "content": "password=db-secret"},
            {
                "role": "assistant",
                "content": "准备生成",
                "tool_calls": [{"arguments": {"token": "raw-secret"}}],
            },
        ],
        tool_name="image_generate",
        tool_label="生成图片",
        action=action,
        permission={
            "effect": "write",
            "risk": "sensitive",
            "categories": ["external_cost", "network"],
        },
        reason_code="profile_reviewable",
        source="profile",
        matched_rule="category:external_cost",
        business_facts={"case_name": "夏季活动", "password": "db-secret"},
    )
    encoded = json.dumps(context, ensure_ascii=False)
    assert "top-secret" not in encoded
    assert "db-secret" not in encoded
    assert "raw-secret" not in encoded
    assert "隐藏推理" not in encoded
    assert context["proposed_action"]["action_hash"] == "b" * 64
    assert context["proposed_action"]["sanitized_arguments"]["prompt"] == "活动海报"
    assert [item["role"] for item in context["visible_transcript"]] == [
        "user",
        "assistant",
    ]


async def test_reviewer_approval_continues_exact_action_in_same_loop() -> None:
    executed: list[dict] = []
    tool = _tool(executed)

    async def preflight(call, _tool_def):
        return _routed(call.arguments)

    async def reviewer(_call, _tool_def, routed):
        return replace(
            routed,
            decision=PermissionDecision.ALLOW,
            reason_code="reviewer_approve",
            source="reviewer",
            reviewer_decision="approve",
            reviewer_rationale="用户明确要求该付费动作。",
            reviewer_risk_flags=("external_cost",),
            reviewer_available=True,
        )

    outcome = await run_loop(
        _script(
            _turn(
                ToolCallRequest(
                    id="approve-1",
                    name="reviewable",
                    arguments={"prompt": "活动海报"},
                )
            ),
            _turn(text="完成"),
        ),
        tools={"reviewable": tool},
        allowed=["reviewable"],
        max_turns=2,
        history=[],
        user_text="生成活动海报",
        tool_ctx=None,
        preflight=preflight,
        reviewer=reviewer,
    )
    assert outcome.stop == "end_turn"
    assert executed == [{"prompt": "活动海报"}]
    assert any(event["type"] == "reviewer.requested" for event in outcome.events)
    decision = next(
        event for event in outcome.events if event["type"] == "reviewer.decision"
    )
    assert decision["decision"] == "approve"
    assert decision["action_hash"] == "b" * 64
    policy = [event for event in outcome.events if event["type"] == "policy.decision"]
    assert [event["phase"] for event in policy] == ["route", "final"]


async def test_reviewer_denial_returns_non_circumvention_result_to_main_agent() -> None:
    executed: list[dict] = []
    tool = _tool(executed)
    seen_messages: list[list[dict]] = []
    turns = iter(
        [
            _turn(
                ToolCallRequest(
                    id="deny-1",
                    name="reviewable",
                    arguments={"prompt": "导出私密资料"},
                )
            ),
            _turn(text="我不会执行该操作。"),
        ]
    )

    async def llm_call(messages, _schemas):
        seen_messages.append(messages)
        return next(turns)

    async def preflight(call, _tool_def):
        return _routed(call.arguments)

    async def reviewer(_call, _tool_def, routed):
        return replace(
            routed,
            decision=PermissionDecision.DENY,
            reason_code="reviewer_denied",
            source="reviewer",
            reviewer_decision="deny",
            reviewer_rationale="该动作会把私有资料发送到未授权目标。",
            reviewer_risk_flags=("private_data", "exfiltration"),
            reviewer_available=True,
            override_eligible=False,
        )

    outcome = await run_loop(
        llm_call,
        tools={"reviewable": tool},
        allowed=["reviewable"],
        max_turns=2,
        history=[],
        user_text="处理资料",
        tool_ctx=None,
        preflight=preflight,
        reviewer=reviewer,
    )
    assert outcome.stop == "end_turn"
    assert executed == []
    denied = next(
        event for event in outcome.events if event["type"] == "tool.result"
    )
    assert denied["output"]["executed"] is False
    assert denied["output"]["reason_code"] == "reviewer_denied"
    assert "不要重试" in denied["output"]["guidance"]
    tool_message = next(
        message
        for message in seen_messages[1]
        if message.get("role") == "tool"
    )
    assert "不要重试" in tool_message["content"]


async def test_reviewer_escalation_creates_manual_bundle_with_rationale() -> None:
    executed: list[dict] = []
    tool = _tool(executed)

    async def preflight(call, _tool_def):
        return _routed(call.arguments)

    async def reviewer(_call, _tool_def, routed):
        return replace(
            routed,
            decision=PermissionDecision.ASK_USER,
            reason_code="reviewer_escalated",
            source="reviewer",
            reviewer_decision="escalate",
            reviewer_rationale="用户没有明确本次外部费用上限。",
            reviewer_risk_flags=("external_cost", "user_intent_unclear"),
            reviewer_available=True,
        )

    outcome = await run_loop(
        _script(
            _turn(
                ToolCallRequest(
                    id="escalate-1",
                    name="reviewable",
                    arguments={"prompt": "海报"},
                )
            )
        ),
        tools={"reviewable": tool},
        allowed=["reviewable"],
        max_turns=1,
        history=[],
        user_text="处理一下",
        tool_ctx=None,
        preflight=preflight,
        reviewer=reviewer,
    )
    assert outcome.stop == "confirm"
    assert executed == []
    item = outcome.pending_confirmation["items"][0]
    assert item["reason_code"] == "reviewer_escalated"
    assert item["reviewer_rationale"] == "用户没有明确本次外部费用上限。"
    assert item["reviewer_risk_flags"] == [
        "external_cost",
        "user_intent_unclear",
    ]


async def test_three_reviewer_denials_open_turn_circuit_breaker() -> None:
    executed: list[dict] = []
    tool = _tool(executed)
    reviewer_calls: list[str] = []

    async def preflight(call, _tool_def):
        return _routed(call.arguments)

    async def reviewer(call, _tool_def, routed):
        reviewer_calls.append(call.id)
        return replace(
            routed,
            decision=PermissionDecision.DENY,
            reason_code="reviewer_denied",
            source="reviewer",
            reviewer_decision="deny",
            reviewer_rationale="动作未获独立审批。",
            reviewer_available=True,
        )

    calls = [
        ToolCallRequest(
            id=f"denial-{index}",
            name="reviewable",
            arguments={"prompt": str(index)},
        )
        for index in range(4)
    ]
    outcome = await run_loop(
        _script(_turn(*calls), _turn(text="停止尝试")),
        tools={"reviewable": tool},
        allowed=["reviewable"],
        max_turns=2,
        history=[],
        user_text="尝试这些动作",
        tool_ctx=None,
        preflight=preflight,
        reviewer=reviewer,
    )
    assert reviewer_calls == ["denial-0", "denial-1", "denial-2"]
    assert executed == []
    breakers = [
        event for event in outcome.events if event["type"] == "reviewer.circuit_breaker"
    ]
    assert breakers
    results = [event for event in outcome.events if event["type"] == "tool.result"]
    assert results[2]["output"]["reason_code"] == "reviewer_circuit_breaker"
    assert results[3]["output"]["reason_code"] == "reviewer_circuit_breaker"


def test_default_reviewer_prompt_is_domain_neutral_but_keeps_its_guardrails() -> None:
    assert APPROVAL_REVIEW_SYSTEM_PROMPT == DEFAULT_APPROVAL_REVIEW_SYSTEM_PROMPT
    # 中性化只动措辞,审慎复核的语义一条不少
    assert "不可信" in APPROVAL_REVIEW_SYSTEM_PROMPT
    assert "action_hash" in APPROVAL_REVIEW_SYSTEM_PROMPT
    for decision in ("approve", "deny", "escalate"):
        assert decision in APPROVAL_REVIEW_SYSTEM_PROMPT
    assert "不要输出隐藏推理" in APPROVAL_REVIEW_SYSTEM_PROMPT
    for business_word in ("旅游", "旅行", "报价", "行程", "酒店"):
        assert business_word not in APPROVAL_REVIEW_SYSTEM_PROMPT


def test_hosts_can_override_the_reviewer_prompt_and_restore_the_default() -> None:
    try:
        current = set_approval_review_system_prompt("  宿主自定义审批口吻  ")
        assert current == "宿主自定义审批口吻"
        from nicekit.agent.permissions import reviewer as reviewer_module

        assert reviewer_module.APPROVAL_REVIEW_SYSTEM_PROMPT == "宿主自定义审批口吻"
        # 空文案不接受:审批提示词不能被静默清空
        assert set_approval_review_system_prompt("   ") == (
            DEFAULT_APPROVAL_REVIEW_SYSTEM_PROMPT
        )
    finally:
        set_approval_review_system_prompt(None)
