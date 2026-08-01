"""Authoritative permission loading and deterministic decision matrices.

迁移自 TF backend/app/services/agent/permissions/policy.py。唯一语义改造:
TF 的 `mutates_existing_project_from_general`(:627)泛化为"通用会话变更已有
作用域根"这条通用规则——判据来自 canonical action 的 scope,不 import 任何
业务模型。其余(CapabilityBoundary / 决策矩阵 / shadow / grant 匹配)原样。

字段命名跟随 models 的 scope 泛化:`project_id` → `scope_id`。
PermissionScope.RESOURCE 仍是"宿主业务作用域"这一档的落库取值(见
domain/agent_permission.py 的说明),故枚举成员名不变。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.permissions.actions import CanonicalToolAction
from nicekit.agent.tools import ToolDef
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionProfile,
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolReversibility,
    ToolRisk,
)
from nicekit.models.agent_permission import AgentApprovalPolicy, AgentPermissionGrant
from nicekit.models.chat import ChatSession

_DECISION_PRIORITY = {
    PermissionDecision.ALLOW: 0,
    PermissionDecision.AUTO_REVIEW: 1,
    PermissionDecision.ASK_USER: 2,
    PermissionDecision.DENY: 3,
}
_SCOPE_PRIORITY = {
    PermissionScope.SESSION: 0,
    PermissionScope.RESOURCE: 1,
    PermissionScope.ORGANIZATION: 2,
}


class PolicyConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityBoundary:
    agent_tool_allowed: bool = True
    service_ready: bool = True
    tenant_allowed: bool = True
    session_owned: bool = True
    role_allowed: bool = True
    business_allowed: bool = True
    budget_available: bool = True

    @property
    def denial_reason(self) -> str | None:
        checks = (
            (self.agent_tool_allowed, "agent_tool_not_allowed"),
            (self.service_ready, "service_unavailable"),
            (self.tenant_allowed, "tenant_boundary"),
            (self.session_owned, "session_not_owned"),
            (self.role_allowed, "role_not_allowed"),
            (self.business_allowed, "business_rule_denied"),
            (self.budget_available, "budget_exhausted"),
        )
        return next((reason for allowed, reason in checks if not allowed), None)


@dataclass(frozen=True, slots=True)
class ResolvedPermissionGrant:
    id: UUID
    tool_name: str | None
    category: ToolCategory | None
    scope: PermissionScope
    session_id: UUID | None
    scope_type: str | None
    scope_id: UUID | None
    material_fingerprint: str
    expires_at: datetime


def _empty_mapping() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class EffectivePermissionPolicy:
    org_id: UUID
    user_id: UUID
    session_id: UUID
    scope_id: UUID | None
    policy_id: UUID | None
    policy_version: int
    profile: PermissionProfile
    allowed_profiles: frozenset[PermissionProfile]
    scope: PermissionScope
    expires_at: datetime | None
    custom_rules: Mapping[ToolCategory, PermissionDecision] = field(
        default_factory=_empty_mapping
    )
    organization_tool_decisions: Mapping[str, PermissionDecision] = field(
        default_factory=_empty_mapping
    )
    organization_category_decisions: Mapping[ToolCategory, PermissionDecision] = field(
        default_factory=_empty_mapping
    )
    denied_tools: frozenset[str] = frozenset()
    denied_categories: frozenset[ToolCategory] = frozenset()
    user_required_tools: frozenset[str] = frozenset()
    user_required_categories: frozenset[ToolCategory] = frozenset()
    reviewer_enabled: bool = False
    reviewer_eligible_tools: frozenset[str] = frozenset()
    reviewer_eligible_categories: frozenset[ToolCategory] = frozenset()
    reviewer_overridable_categories: frozenset[ToolCategory] = frozenset()
    grants: tuple[ResolvedPermissionGrant, ...] = ()
    max_grant_ttl_seconds: int = 28800
    max_full_access_ttl_seconds: int = 3600
    compatibility_mode: bool = False
    is_enabled: bool = True
    shadow_evaluation: bool = False


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    decision: PermissionDecision
    reason_code: str
    source: str
    matched_rule: str | None = None
    grant_id: UUID | None = None
    reviewer_eligible: bool = False


@dataclass(frozen=True, slots=True)
class ShadowPolicyComparison:
    enforced: PolicyEvaluation
    shadow: PolicyEvaluation | None = None

    @property
    def differs(self) -> bool:
        return bool(
            self.shadow is not None
            and (
                self.enforced.decision is not self.shadow.decision
                or self.enforced.reason_code != self.shadow.reason_code
                or self.enforced.source != self.shadow.source
            )
        )


def _profile(value: object, *, field_name: str) -> PermissionProfile:
    try:
        return PermissionProfile(str(value))
    except ValueError as exc:
        raise PolicyConfigurationError(f"invalid {field_name}: {value}") from exc


def _scope(value: object, *, field_name: str) -> PermissionScope:
    try:
        return PermissionScope(str(value))
    except ValueError as exc:
        raise PolicyConfigurationError(f"invalid {field_name}: {value}") from exc


def _category(value: object, *, field_name: str) -> ToolCategory:
    try:
        return ToolCategory(str(value))
    except ValueError as exc:
        raise PolicyConfigurationError(f"invalid {field_name}: {value}") from exc


def _decision(value: object, *, field_name: str) -> PermissionDecision:
    try:
        return PermissionDecision(str(value))
    except ValueError as exc:
        raise PolicyConfigurationError(f"invalid {field_name}: {value}") from exc


def _string_set(value: object, *, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PolicyConfigurationError(f"{field_name} must be a string array")
    return frozenset(value)


def _category_set(value: object, *, field_name: str) -> frozenset[ToolCategory]:
    return frozenset(
        _category(item, field_name=field_name)
        for item in _string_set(value, field_name=field_name)
    )


def _decision_map(value: object, *, field_name: str) -> Mapping[str, PermissionDecision]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict):
        raise PolicyConfigurationError(f"{field_name} must be an object")
    return MappingProxyType(
        {
            str(key): _decision(item, field_name=f"{field_name}.{key}")
            for key, item in value.items()
        }
    )


def _category_decision_map(
    value: object, *, field_name: str
) -> Mapping[ToolCategory, PermissionDecision]:
    raw = _decision_map(value, field_name=field_name)
    return MappingProxyType(
        {
            _category(key, field_name=field_name): decision
            for key, decision in raw.items()
        }
    )


def _strictest(decisions: list[PermissionDecision]) -> PermissionDecision:
    return max(decisions, key=_DECISION_PRIORITY.__getitem__)


def _bounded_scope(requested: PermissionScope, maximum: PermissionScope) -> PermissionScope:
    return requested if _SCOPE_PRIORITY[requested] <= _SCOPE_PRIORITY[maximum] else maximum


def _strict_policy(
    *,
    org_id: UUID,
    user_id: UUID,
    chat_session: ChatSession,
) -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        org_id=org_id,
        user_id=user_id,
        session_id=chat_session.id,
        scope_id=chat_session.scope_id,
        policy_id=None,
        policy_version=0,
        profile=PermissionProfile.REQUEST_APPROVAL,
        allowed_profiles=frozenset({PermissionProfile.REQUEST_APPROVAL}),
        scope=(
            PermissionScope.RESOURCE
            if chat_session.scope_id
            else PermissionScope.SESSION
        ),
        expires_at=None,
        compatibility_mode=True,
    )


async def load_effective_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    chat_session: ChatSession,
    now: datetime | None = None,
) -> EffectivePermissionPolicy:
    """Load authoritative organization/session state; no client policy claims enter here."""

    current_time = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(AgentApprovalPolicy).where(
                AgentApprovalPolicy.org_id == org_id,
                AgentApprovalPolicy.is_active,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return _strict_policy(
            org_id=org_id,
            user_id=user_id,
            chat_session=chat_session,
        )

    if not isinstance(row.hard_rules, dict):
        raise PolicyConfigurationError("hard_rules must be an object")
    hard_rules = row.hard_rules
    configured_allowed_profiles = frozenset(
        _profile(item, field_name="allowed_profiles") for item in row.allowed_profiles
    )
    if PermissionProfile.REQUEST_APPROVAL not in configured_allowed_profiles:
        raise PolicyConfigurationError("request_approval must remain available")
    allowed_profiles = (
        configured_allowed_profiles
        if row.is_enabled
        else frozenset({PermissionProfile.REQUEST_APPROVAL})
    )
    try:
        requested_profile = PermissionProfile(str(chat_session.permission_profile))
    except ValueError:
        requested_profile = PermissionProfile.REQUEST_APPROVAL
    profile = (
        requested_profile
        if requested_profile in allowed_profiles and row.is_enabled
        else PermissionProfile.REQUEST_APPROVAL
    )
    expires_at = chat_session.permission_expires_at
    if profile is PermissionProfile.FULL_ACCESS and (
        expires_at is None or expires_at <= current_time
    ):
        profile = PermissionProfile.REQUEST_APPROVAL
        expires_at = None
    try:
        requested_scope = PermissionScope(str(chat_session.permission_scope))
    except ValueError:
        requested_scope = (
            PermissionScope.RESOURCE
            if chat_session.scope_id
            else PermissionScope.SESSION
        )
    maximum_scope = _scope(row.max_scope, field_name="max_scope")
    effective_scope = _bounded_scope(requested_scope, maximum_scope)

    custom_rules = _category_decision_map(
        chat_session.permission_custom_rules,
        field_name="permission_custom_rules",
    )
    grant_rows = list(
        (
            await session.execute(
                select(AgentPermissionGrant).where(
                    AgentPermissionGrant.org_id == org_id,
                    AgentPermissionGrant.user_id == user_id,
                    AgentPermissionGrant.revoked_at.is_(None),
                    AgentPermissionGrant.expires_at > current_time,
                    or_(
                        AgentPermissionGrant.session_id == chat_session.id,
                        AgentPermissionGrant.session_id.is_(None),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    grants = tuple(
        ResolvedPermissionGrant(
            id=grant.id,
            tool_name=grant.tool_name,
            category=(
                _category(grant.category, field_name="grant.category")
                if grant.category is not None
                else None
            ),
            scope=_scope(grant.scope, field_name="grant.scope"),
            session_id=grant.session_id,
            scope_type=grant.scope_type,
            scope_id=grant.scope_id,
            material_fingerprint=grant.action_fingerprint,
            expires_at=grant.expires_at,
        )
        for grant in grant_rows
        if grant.revoked_at is None
        and grant.expires_at > current_time
        and grant.org_id == org_id
        and grant.user_id == user_id
        and grant.session_id in (None, chat_session.id)
    )
    return EffectivePermissionPolicy(
        org_id=org_id,
        user_id=user_id,
        session_id=chat_session.id,
        scope_id=chat_session.scope_id,
        policy_id=row.id,
        policy_version=row.version,
        profile=profile,
        allowed_profiles=allowed_profiles,
        scope=effective_scope,
        expires_at=expires_at,
        custom_rules=custom_rules,
        organization_tool_decisions=_decision_map(
            hard_rules.get("tool_decisions"),
            field_name="hard_rules.tool_decisions",
        ),
        organization_category_decisions=_category_decision_map(
            hard_rules.get("category_decisions"),
            field_name="hard_rules.category_decisions",
        ),
        denied_tools=_string_set(
            hard_rules.get("denied_tools"), field_name="hard_rules.denied_tools"
        ),
        denied_categories=_category_set(
            hard_rules.get("denied_categories"),
            field_name="hard_rules.denied_categories",
        ),
        user_required_tools=_string_set(
            hard_rules.get("user_required_tools"),
            field_name="hard_rules.user_required_tools",
        ),
        user_required_categories=_category_set(
            hard_rules.get("user_required_categories"),
            field_name="hard_rules.user_required_categories",
        ),
        reviewer_enabled=bool(row.reviewer_enabled and row.is_enabled),
        reviewer_eligible_tools=_string_set(
            row.reviewer_eligible_tools,
            field_name="reviewer_eligible_tools",
        ),
        reviewer_eligible_categories=_category_set(
            row.reviewer_eligible_categories,
            field_name="reviewer_eligible_categories",
        ),
        reviewer_overridable_categories=_category_set(
            hard_rules.get("reviewer_overridable_categories"),
            field_name="hard_rules.reviewer_overridable_categories",
        ),
        grants=grants,
        max_grant_ttl_seconds=row.max_grant_ttl_seconds,
        max_full_access_ttl_seconds=row.max_full_access_ttl_seconds,
        compatibility_mode=bool(hard_rules.get("compatibility_mode", False)),
        is_enabled=bool(row.is_enabled),
        shadow_evaluation=bool(row.shadow_evaluation),
    )


def policy_snapshot_payload(policy: EffectivePermissionPolicy) -> dict:
    """Non-secret durable snapshot used by replay and queued-turn isolation."""

    return {
        "policy_id": str(policy.policy_id) if policy.policy_id else None,
        "policy_version": policy.policy_version,
        "profile": policy.profile.value,
        "scope": policy.scope.value,
        "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
        "grant_ids": [str(grant.id) for grant in policy.grants],
        "allowed_profiles": sorted(profile.value for profile in policy.allowed_profiles),
        "reviewer_enabled": policy.reviewer_enabled,
        "max_grant_ttl_seconds": policy.max_grant_ttl_seconds,
        "max_full_access_ttl_seconds": policy.max_full_access_ttl_seconds,
        "compatibility_mode": policy.compatibility_mode,
        "shadow_evaluation": policy.shadow_evaluation,
    }


def _organization_constraint(
    policy: EffectivePermissionPolicy, tool: ToolDef
) -> tuple[PermissionDecision | None, str | None]:
    if tool.name in policy.denied_tools or tool.permission.categories & policy.denied_categories:
        return PermissionDecision.DENY, "organization_denied"
    if (
        tool.name in policy.user_required_tools
        or tool.permission.categories & policy.user_required_categories
    ):
        return PermissionDecision.ASK_USER, "organization_user_required"
    decisions: list[PermissionDecision] = []
    matched: list[str] = []
    tool_decision = policy.organization_tool_decisions.get(tool.name)
    if tool_decision is not None:
        decisions.append(tool_decision)
        matched.append(f"tool:{tool.name}")
    for category in sorted(tool.permission.categories, key=lambda item: item.value):
        category_decision = policy.organization_category_decisions.get(category)
        if category_decision is not None:
            decisions.append(category_decision)
            matched.append(f"category:{category.value}")
    if not decisions:
        return None, None
    return _strictest(decisions), ",".join(matched)


def _reviewer_eligible(policy: EffectivePermissionPolicy, tool: ToolDef) -> bool:
    return bool(
        policy.reviewer_enabled
        and (
            tool.name in policy.reviewer_eligible_tools
            or tool.permission.categories & policy.reviewer_eligible_categories
        )
    )


def _matching_grant(
    policy: EffectivePermissionPolicy,
    tool: ToolDef,
    action: CanonicalToolAction,
) -> ResolvedPermissionGrant | None:
    if tool.permission.delegation in {
        ToolDelegation.USER_REQUIRED,
        ToolDelegation.AGENT_FORBIDDEN,
    }:
        return None
    now = datetime.now(UTC)
    for grant in policy.grants:
        if grant.expires_at <= now:
            continue
        if grant.tool_name != tool.name and grant.category not in tool.permission.categories:
            continue
        if grant.material_fingerprint != action.material_fingerprint:
            continue
        if grant.scope is PermissionScope.SESSION and grant.session_id != policy.session_id:
            continue
        if grant.scope is PermissionScope.RESOURCE and (
            grant.scope_id is None or grant.scope_id != action.scope.target_scope_id
        ):
            continue
        if grant.scope_id is not None and grant.scope_id != action.scope.target_scope_id:
            continue
        return grant
    return None


def _profile_decision(
    policy: EffectivePermissionPolicy,
    tool: ToolDef,
) -> tuple[PermissionDecision, str]:
    permission = tool.permission
    if policy.compatibility_mode and policy.profile is PermissionProfile.REQUEST_APPROVAL:
        return (
            (PermissionDecision.ASK_USER, "compatibility_confirmation")
            if tool.confirm or permission.delegation is not ToolDelegation.AUTOMATIC
            else (PermissionDecision.ALLOW, "compatibility_allow")
        )
    if policy.profile is PermissionProfile.REQUEST_APPROVAL:
        routine_local_read = (
            permission.effect is ToolEffect.READ
            and permission.risk is ToolRisk.ROUTINE
            and permission.categories == frozenset({ToolCategory.LOCAL_DATA})
        )
        return (
            (PermissionDecision.ALLOW, "profile_routine_read")
            if routine_local_read
            else (PermissionDecision.ASK_USER, "profile_request_approval")
        )
    if policy.profile is PermissionProfile.AUTO_REVIEW:
        routine_read = permission.effect is ToolEffect.READ and permission.risk is ToolRisk.ROUTINE
        routine_write = (
            permission.effect is ToolEffect.WRITE
            and permission.risk is ToolRisk.ROUTINE
            and permission.reversibility is ToolReversibility.REVERSIBLE
        )
        if routine_read or routine_write:
            return PermissionDecision.ALLOW, "profile_auto_routine"
        if permission.risk is ToolRisk.CRITICAL:
            return (
                (PermissionDecision.AUTO_REVIEW, "profile_critical_reviewable")
                if permission.delegation is ToolDelegation.REVIEWABLE
                and _reviewer_eligible(policy, tool)
                else (PermissionDecision.ASK_USER, "profile_critical")
            )
        if permission.delegation is ToolDelegation.AUTOMATIC:
            return PermissionDecision.ALLOW, "profile_automatic"
        return PermissionDecision.AUTO_REVIEW, "profile_reviewable"
    if policy.profile is PermissionProfile.FULL_ACCESS:
        return PermissionDecision.ALLOW, "profile_full_access"

    if (
        permission.effect is ToolEffect.READ
        and permission.categories == frozenset({ToolCategory.LOCAL_DATA})
    ):
        return PermissionDecision.ALLOW, "custom_local_read"
    matching = [
        policy.custom_rules[category]
        for category in permission.categories
        if category in policy.custom_rules
    ]
    if len(matching) != len(permission.categories):
        return PermissionDecision.ASK_USER, "custom_missing_rule"
    return _strictest(matching), "custom_rule"


def evaluate_tool_action(
    policy: EffectivePermissionPolicy,
    tool: ToolDef,
    action: CanonicalToolAction,
    *,
    capability: CapabilityBoundary | None = None,
) -> PolicyEvaluation:
    boundary = capability or CapabilityBoundary()
    if boundary.denial_reason:
        return PolicyEvaluation(
            PermissionDecision.DENY,
            boundary.denial_reason,
            "capability",
        )
    if tool.permission.delegation is ToolDelegation.AGENT_FORBIDDEN:
        return PolicyEvaluation(
            PermissionDecision.DENY,
            "agent_forbidden",
            "tool_metadata",
        )
    if action.scope.resolution_error:
        return PolicyEvaluation(
            PermissionDecision.DENY,
            action.scope.resolution_error,
            "capability",
        )
    if action.scope.cross_scope:
        return PolicyEvaluation(
            PermissionDecision.DENY,
            "scope_mismatch",
            "capability",
        )

    organization_decision, matched_rule = _organization_constraint(policy, tool)
    if organization_decision is PermissionDecision.DENY:
        return PolicyEvaluation(
            PermissionDecision.DENY,
            "organization_denied",
            "organization",
            matched_rule,
        )
    if organization_decision is PermissionDecision.ASK_USER:
        return PolicyEvaluation(
            PermissionDecision.ASK_USER,
            "organization_user_required",
            "organization",
            matched_rule,
        )
    if tool.permission.delegation is ToolDelegation.USER_REQUIRED:
        return PolicyEvaluation(
            PermissionDecision.ASK_USER,
            "tool_user_required",
            "tool_metadata",
        )

    grant = _matching_grant(policy, tool, action)
    if grant is not None:
        return PolicyEvaluation(
            PermissionDecision.ALLOW,
            "grant_match",
            "grant",
            grant_id=grant.id,
        )

    # 通用会话去改一个**已存在**的作用域根:用户没在那个作用域的语境里说话,
    # 变更必须显式确认。新建作用域根(creates_scope_root)不在此列——那正是
    # 通用会话该干的事。只读一律放行。
    mutates_existing_scope_from_general = bool(
        action.scope.mode == "general"
        and action.scope.target_scope_id
        and not action.scope.creates_scope_root
        and tool.permission.effect is not ToolEffect.READ
    )
    if mutates_existing_scope_from_general:
        return PolicyEvaluation(
            PermissionDecision.ASK_USER,
            "general_scope_mutation",
            "scope",
        )

    profile_decision, reason = _profile_decision(policy, tool)
    if organization_decision is PermissionDecision.AUTO_REVIEW:
        profile_decision = PermissionDecision.AUTO_REVIEW
        reason = "organization_review_required"
    reviewer_eligible = _reviewer_eligible(policy, tool)
    if profile_decision is PermissionDecision.AUTO_REVIEW and not reviewer_eligible:
        return PolicyEvaluation(
            PermissionDecision.ASK_USER,
            "reviewer_not_eligible",
            "reviewer",
            matched_rule,
        )
    return PolicyEvaluation(
        profile_decision,
        reason,
        "profile" if organization_decision is None else "organization",
        matched_rule,
        reviewer_eligible=reviewer_eligible,
    )


def evaluate_tool_action_with_shadow(
    policy: EffectivePermissionPolicy,
    tool: ToolDef,
    action: CanonicalToolAction,
    *,
    capability: CapabilityBoundary | None = None,
) -> ShadowPolicyComparison:
    """Evaluate the configured policy while optionally enforcing legacy gates.

    Shadow mode records the configured profile's outcome, but execution still
    follows the pre-rollout confirmation contract. Capability denials and
    organization hard denies/user-required rules remain authoritative in both
    paths; grants and permissive organization decision overrides are excluded
    from the enforced compatibility path.
    """

    shadow = evaluate_tool_action(policy, tool, action, capability=capability)
    if not policy.shadow_evaluation:
        return ShadowPolicyComparison(enforced=shadow)

    compatibility_policy = replace(
        policy,
        profile=PermissionProfile.REQUEST_APPROVAL,
        custom_rules=_empty_mapping(),
        organization_tool_decisions=_empty_mapping(),
        organization_category_decisions=_empty_mapping(),
        reviewer_enabled=False,
        reviewer_eligible_tools=frozenset(),
        reviewer_eligible_categories=frozenset(),
        reviewer_overridable_categories=frozenset(),
        grants=(),
        compatibility_mode=True,
    )
    enforced = evaluate_tool_action(
        compatibility_policy,
        tool,
        action,
        capability=capability,
    )
    return ShadowPolicyComparison(enforced=enforced, shadow=shadow)
