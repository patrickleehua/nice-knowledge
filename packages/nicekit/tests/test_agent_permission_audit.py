"""Permission audit redaction and bounded-detail tests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nicekit.agent.permissions.audit import (
    PermissionAuditAction,
    PermissionAuditDetail,
    add_permission_audit,
    permission_event_audit_detail,
)


@pytest.mark.parametrize(
    "forged_field",
    [
        "raw_arguments",
        "input",
        "output",
        "token",
        "credential",
        "note",
        "reviewer_rationale",
        "hidden_reasoning",
        "transcript",
    ],
)
def test_permission_audit_detail_rejects_unbounded_or_secret_fields(
    forged_field: str,
) -> None:
    with pytest.raises(ValidationError):
        PermissionAuditDetail.model_validate(
            {
                "session_id": str(uuid4()),
                "reason_code": "user_approved",
                forged_field: "must-not-be-stored",
            }
        )


def test_event_projection_keeps_only_bounded_decision_metadata() -> None:
    session_id, run_id, scope_id, resource_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    detail = permission_event_audit_detail(
        {
            "type": "reviewer.decision",
            "tool_call_id": "call-1",
            "name": "paid_network",
            "decision": "approve",
            "source": "reviewer",
            "reason_code": "reviewer_approved",
            "risk": "sensitive",
            "categories": ["network", "external_cost"],
            "argument_hash": "a" * 64,
            "action_hash": "b" * 64,
            "scope": {
                "target_scope_id": str(scope_id),
                "resource_type": "case",
                "resource_id": str(resource_id),
            },
            "input": {"prompt": "private", "token": "secret"},
            "output": {"credential": "secret"},
            "note": "private note",
            "rationale": "user-safe but not organization audit data",
            "hidden_reasoning": "never persist",
        },
        session_id=session_id,
        run_id=run_id,
        policy_snapshot={
            "policy_id": str(uuid4()),
            "policy_version": 7,
            "profile": "auto_review",
            "scope": "resource",
        },
    )

    assert detail["session_id"] == str(session_id)
    assert detail["run_id"] == str(run_id)
    assert detail["scope_id"] == str(scope_id)
    assert detail["resource_id"] == str(resource_id)
    assert detail["argument_hash"] == "a" * 64
    assert detail["action_hash"] == "b" * 64
    encoded = str(detail)
    for forbidden in (
        "private",
        "secret",
        "credential",
        "note",
        "rationale",
        "hidden_reasoning",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "detail",
    [
        {"argument_hash": "short"},
        {"action_hash": "A" * 64},
        {"categories": ["future_category"]},
        {"reason_code": "x" * 101},
    ],
)
def test_permission_audit_detail_rejects_invalid_bounded_values(detail: dict) -> None:
    with pytest.raises(ValidationError):
        PermissionAuditDetail.model_validate(detail)


def test_add_permission_audit_serializes_only_validated_detail() -> None:
    class SessionStub:
        def __init__(self) -> None:
            self.rows = []

        def add(self, row) -> None:
            self.rows.append(row)

    session = SessionStub()
    org_id, user_id, session_id = uuid4(), uuid4(), uuid4()
    row = add_permission_audit(
        session,
        org_id=org_id,
        actor_user_id=user_id,
        action=PermissionAuditAction.PROFILE_CHANGED,
        entity_id=session_id,
        detail={
            "session_id": session_id,
            "policy_version": 3,
            "profile": "auto_review",
            "previous_profile": "request_approval",
            "scope": "session",
        },
    )

    assert session.rows == [row]
    assert row.action == "agent.permission.profile_changed"
    assert row.detail == {
        "session_id": str(session_id),
        "policy_version": 3,
        "profile": "auto_review",
        "previous_profile": "request_approval",
        "scope": "session",
        "categories": [],
    }
