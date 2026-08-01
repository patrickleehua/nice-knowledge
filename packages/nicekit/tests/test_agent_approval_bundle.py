from nicekit.agent.loop import ToolPreflight, run_loop
from nicekit.agent.tools import ToolDef
from nicekit.domain.agent_permission import PermissionDecision
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import ToolTurn


def _turn(*calls: ToolCallRequest, text: str | None = None) -> ToolTurn:
    return ToolTurn(
        text=text,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        tokens_in=1,
        tokens_out=1,
        trace_id=None,
    )


def _call(name: str, call_id: str, **arguments) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def _script(*turns: ToolTurn):
    iterator = iter(turns)

    async def llm_call(_messages, _schemas):
        return next(iterator)

    return llm_call


def _tool(name: str, executed: list[str], *, side_effect: str = "write") -> ToolDef:
    async def executor(_ctx, _arguments) -> dict:
        executed.append(name)
        return {"done": name}

    return ToolDef(
        name=name,
        description=name,
        schema={"type": "object", "properties": {}},
        executor=executor,
        side_effect=side_effect,
    )


def _preflight(
    decision: PermissionDecision,
    *,
    sanitized_arguments: dict | None = None,
    reason: str = "test_policy",
) -> ToolPreflight:
    return ToolPreflight(
        decision=decision,
        reason_code=reason,
        source="profile",
        action={
            "sanitized_arguments": sanitized_arguments or {},
            "argument_hash": "a" * 64,
            "action_hash": "b" * 64,
            "material_fingerprint": "c" * 64,
            "scope": {"target_scope_id": None},
        },
    )


async def test_mixed_safe_and_risky_hop_preflights_all_and_executes_none() -> None:
    executed: list[str] = []
    preflighted: list[str] = []
    tools = {
        "safe": _tool("safe", executed, side_effect="read"),
        "risky": _tool("risky", executed),
    }

    async def preflight(call, _tool_def):
        preflighted.append(call.name)
        return _preflight(
            PermissionDecision.ASK_USER
            if call.name == "risky"
            else PermissionDecision.ALLOW
        )

    outcome = await run_loop(
        _script(_turn(_call("safe", "c1"), _call("risky", "c2"))),
        tools=tools,
        allowed=list(tools),
        max_turns=2,
        history=[],
        user_text="执行",
        tool_ctx=None,
        preflight=preflight,
        run_id="run-1",
        policy_snapshot={"policy_version": 3},
    )

    assert preflighted == ["safe", "risky"]
    assert executed == []
    assert outcome.stop == "confirm"
    bundle = outcome.pending_confirmation
    assert bundle["kind"] == "approval_bundle"
    assert [item["status"] for item in bundle["items"]] == ["allowed", "pending"]
    assert [item["order"] for item in bundle["items"]] == [0, 1]
    assert len([event for event in outcome.events if event["type"] == "policy.decision"]) == 2
    assert len(
        [event for event in outcome.events if event["type"] == "approval.bundle.requested"]
    ) == 1


async def test_shadow_policy_event_records_configured_outcome_but_enforces_legacy() -> None:
    executed: list[str] = []
    tool = _tool("shadowed", executed, side_effect="read")

    async def preflight(_call, _tool_def):
        return ToolPreflight(
            decision=PermissionDecision.ALLOW,
            reason_code="compatibility_allow",
            source="profile",
            action={
                "sanitized_arguments": {"token": "[REDACTED]"},
                "argument_hash": "a" * 64,
                "action_hash": "b" * 64,
                "scope": {"target_scope_id": None},
            },
            shadow_decision=PermissionDecision.ASK_USER,
            shadow_reason_code="profile_request_approval",
            shadow_source="profile",
        )

    outcome = await run_loop(
        _script(
            _turn(_call("shadowed", "shadow-call", token="raw-secret")),
            _turn(text="完成"),
        ),
        tools={"shadowed": tool},
        allowed=["shadowed"],
        max_turns=2,
        history=[],
        user_text="执行",
        tool_ctx=None,
        preflight=preflight,
        policy_snapshot={
            "policy_id": "policy-1",
            "policy_version": 4,
            "profile": "request_approval",
            "scope": "session",
        },
    )

    shadow = next(
        event
        for event in outcome.events
        if event["type"] == "policy.shadow_evaluation"
    )
    assert shadow["enforced_decision"] == "allow"
    assert shadow["shadow_decision"] == "ask_user"
    assert shadow["differs"] is True
    assert shadow["action_hash"] == "b" * 64
    assert "raw-secret" not in str(shadow)
    assert executed == ["shadowed"]


async def test_preflight_finishes_before_allowed_execution_and_denial_is_explicit() -> None:
    timeline: list[str] = []

    async def allowed_executor(_ctx, _arguments) -> dict:
        timeline.append("execute:allowed")
        return {"done": True}

    async def denied_executor(_ctx, _arguments) -> dict:
        timeline.append("execute:denied")
        return {"done": True}

    tools = {
        "allowed": ToolDef(
            name="allowed",
            description="allowed",
            schema={"type": "object", "properties": {}},
            executor=allowed_executor,
            side_effect="write",
        ),
        "denied": ToolDef(
            name="denied",
            description="denied",
            schema={"type": "object", "properties": {}},
            executor=denied_executor,
            side_effect="write",
        ),
    }

    async def preflight(call, _tool_def):
        timeline.append(f"preflight:{call.name}")
        return _preflight(
            PermissionDecision.DENY
            if call.name == "denied"
            else PermissionDecision.ALLOW,
            reason="organization_denied" if call.name == "denied" else "full_access",
        )

    outcome = await run_loop(
        _script(
            _turn(_call("allowed", "c1"), _call("denied", "c2")),
            _turn(text="完成"),
        ),
        tools=tools,
        allowed=list(tools),
        max_turns=2,
        history=[],
        user_text="执行",
        tool_ctx=None,
        preflight=preflight,
    )

    assert timeline == ["preflight:allowed", "preflight:denied", "execute:allowed"]
    denied = next(
        event
        for event in outcome.events
        if event["type"] == "tool.result" and event["tool_call_id"] == "c2"
    )
    assert denied["ok"] is False
    assert denied["output"]["executed"] is False
    assert denied["output"]["reason_code"] == "organization_denied"


async def test_bundle_event_uses_sanitized_arguments_and_groups_multiple_requests() -> None:
    executed: list[str] = []
    tools = {name: _tool(name, executed) for name in ("one", "two")}

    async def preflight(call, _tool_def):
        return _preflight(
            PermissionDecision.ASK_USER,
            sanitized_arguments={"token": "[REDACTED]", "name": call.name},
        )

    outcome = await run_loop(
        _script(
            _turn(
                _call("one", "c1", token="secret-1"),
                _call("two", "c2", token="secret-2"),
            )
        ),
        tools=tools,
        allowed=list(tools),
        max_turns=1,
        history=[],
        user_text="执行",
        tool_ctx=None,
        preflight=preflight,
    )

    request = next(
        event for event in outcome.events if event["type"] == "approval.bundle.requested"
    )
    assert len(request["bundle"]["items"]) == 2
    assert all(item["input"]["token"] == "[REDACTED]" for item in request["bundle"]["items"])
    assert outcome.pending_confirmation["items"][0]["input"]["token"] == "secret-1"
    assert not any(event["type"] == "tool.confirm" for event in outcome.events)


async def test_unhandled_auto_review_fails_closed_into_manual_bundle() -> None:
    executed: list[str] = []
    tool = _tool("review", executed)

    async def preflight(_call, _tool_def):
        return _preflight(PermissionDecision.AUTO_REVIEW)

    outcome = await run_loop(
        _script(_turn(_call("review", "c1"))),
        tools={"review": tool},
        allowed=["review"],
        max_turns=1,
        history=[],
        user_text="执行",
        tool_ctx=None,
        preflight=preflight,
    )

    assert outcome.stop == "confirm"
    assert executed == []
    item = outcome.pending_confirmation["items"][0]
    assert item["reason_code"] == "reviewer_unavailable"
    assert item["status"] == "pending"
