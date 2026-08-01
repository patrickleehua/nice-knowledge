"""Strict, redacted audit projection for Agent permission decisions."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.domain.agent_permission import (
    PermissionProfile,
    PermissionScope,
    ToolCategory,
    ToolRisk,
)
from nicekit.models.tenancy import AuditLog

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundedName = Annotated[str, Field(min_length=1, max_length=100)]


class PermissionAuditAction(StrEnum):
    PROFILE_CHANGED = "agent.permission.profile_changed"
    PREFERENCE_CHANGED = "agent.permission.preference_changed"
    FULL_ACCESS_ACTIVATED = "agent.permission.full_access_activated"
    GRANT_CREATED = "agent.permission.grant_created"
    GRANT_REVOKED = "agent.permission.grant_revoked"
    REVIEWER_DECIDED = "agent.permission.reviewer_decided"
    MANUAL_DECIDED = "agent.permission.manual_decided"
    FULL_ACCESS_EXECUTED = "agent.permission.full_access_executed"
    POLICY_DENIED = "agent.permission.policy_denied"
    REVIEWER_OVERRIDE_COMPLETED = "agent.permission.reviewer_override_completed"
    ORGANIZATION_POLICY_CHANGED = "agent.permission.organization_policy_changed"
    POLICY_ROLLED_BACK = "agent.permission.policy_rolled_back"


class PermissionAuditDetail(BaseModel):
    """Only bounded, non-secret fields accepted by permission audit rows."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    run_id: UUID | None = None
    bundle_id: UUID | None = None
    tool_call_id: BoundedName | None = None
    grant_id: UUID | None = None
    candidate_id: UUID | None = None
    policy_id: UUID | None = None
    policy_version: int | None = Field(default=None, ge=0)
    previous_policy_version: int | None = Field(default=None, ge=0)
    profile: PermissionProfile | None = None
    previous_profile: PermissionProfile | None = None
    scope: PermissionScope | None = None
    previous_scope: PermissionScope | None = None
    tool_name: BoundedName | None = None
    decision: Literal[
        "allow",
        "auto_review",
        "ask_user",
        "deny",
        "approve",
        "escalate",
    ] | None = None
    decision_source: BoundedName | None = None
    reason_code: BoundedName | None = None
    risk: ToolRisk | None = None
    categories: list[ToolCategory] = Field(default_factory=list, max_length=len(ToolCategory))
    scope_id: UUID | None = None
    resource_type: Annotated[str, Field(min_length=1, max_length=50)] | None = None
    resource_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    argument_hash: Sha256 | None = None
    action_hash: Sha256 | None = None
    expires_at: datetime | None = None
    successful: bool | None = None
    feature_enabled: bool | None = None
    reviewer_available: bool | None = None
    override_eligible: bool | None = None
    circuit_breaker: bool | None = None


def permission_event_audit_detail(
    event: dict,
    *,
    session_id: UUID,
    run_id: UUID,
    policy_snapshot: dict | None = None,
) -> dict:
    """Whitelist one public event into bounded audit detail.

    Unknown event keys are intentionally ignored. In particular, raw/display
    inputs, outputs, notes, rationale, transcript, and reasoning never cross
    this projection boundary.
    """

    snapshot = policy_snapshot or {}
    raw_scope = event.get("action_scope") or event.get("scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    raw_categories = event.get("categories")
    categories = raw_categories if isinstance(raw_categories, list) else []
    raw: dict = {
        "session_id": session_id,
        "run_id": run_id,
        "bundle_id": event.get("bundle_id"),
        "tool_call_id": event.get("tool_call_id"),
        "grant_id": event.get("grant_id"),
        "candidate_id": event.get("candidate_id"),
        "policy_id": event.get("policy_id", snapshot.get("policy_id")),
        "policy_version": event.get(
            "policy_version",
            snapshot.get("policy_version"),
        ),
        "profile": event.get("profile", snapshot.get("profile")),
        "scope": event.get("permission_scope", snapshot.get("scope")),
        "tool_name": event.get("name") or event.get("tool_name"),
        "decision": event.get("decision"),
        "decision_source": event.get("source") or event.get("decision_source"),
        "reason_code": event.get("reason_code"),
        "risk": event.get("risk"),
        "categories": categories,
        "scope_id": scope.get("target_scope_id") or event.get("scope_id"),
        "resource_type": scope.get("resource_type") or event.get("resource_type"),
        "resource_id": scope.get("resource_id") or event.get("resource_id"),
        "argument_hash": event.get("argument_hash"),
        "action_hash": event.get("action_hash"),
        "successful": event.get("successful"),
        "reviewer_available": event.get("available"),
        "override_eligible": event.get("override_eligible"),
        "circuit_breaker": event.get("circuit_breaker"),
    }
    return PermissionAuditDetail.model_validate(
        {key: value for key, value in raw.items() if value is not None}
    ).model_dump(mode="json", exclude_none=True)


def add_permission_audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_user_id: UUID | None,
    action: PermissionAuditAction,
    entity_id: UUID | str | None,
    detail: PermissionAuditDetail | dict,
) -> AuditLog:
    """Add a validated Agent-permission audit row to the caller's transaction."""

    validated = (
        detail
        if isinstance(detail, PermissionAuditDetail)
        else PermissionAuditDetail.model_validate(detail)
    )
    entity = str(entity_id) if entity_id is not None else None
    if entity is not None and len(entity) > 100:
        raise ValueError("permission audit entity_id exceeds 100 characters")
    row = AuditLog(
        org_id=org_id,
        user_id=actor_user_id,
        action=action.value,
        entity_type="agent_permission",
        entity_id=entity,
        detail=validated.model_dump(mode="json", exclude_none=True),
    )
    session.add(row)
    return row
