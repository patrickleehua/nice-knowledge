"""Authoritative permission state mutation and organization-policy management.

迁移自 TF backend/app/services/agent/permissions/management.py。改造点:
TF 的 `_known_tools`(:90)直接读模块级 TOOLS;SDK 里工具来自多个来源,改为
注入 `known_tools: Callable[[], set[str]]`(不传时读 registry provider,
默认 nicekit.agent.tools.default_registry)。其余原样。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.permissions.audit import (
    PermissionAuditAction,
    add_permission_audit,
)
from nicekit.agent.permissions.policy import (
    EffectivePermissionPolicy,
    load_effective_policy,
    policy_snapshot_payload,
)
from nicekit.agent.permissions.reviewer import APPROVAL_REVIEW_TASK
from nicekit.agent.tools import ToolRegistry, default_registry
from nicekit.core.config import get_settings
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionProfile,
    PermissionScope,
    ToolCategory,
)
from nicekit.models.agent_permission import (
    AgentApprovalPolicy,
    AgentPermissionPreference,
)
from nicekit.models.chat import ChatSession
from nicekit.models.llm import ModelRoute, Prompt
from nicekit.models.tenancy import Organization

_SCOPE_RANK = {
    PermissionScope.SESSION: 0,
    PermissionScope.RESOURCE: 1,
    PermissionScope.ORGANIZATION: 2,
}
_HARD_RULE_KEYS = frozenset(
    {
        "compatibility_mode",
        "tool_decisions",
        "category_decisions",
        "denied_tools",
        "denied_categories",
        "user_required_tools",
        "user_required_categories",
        "reviewer_overridable_categories",
    }
)


class PermissionManagementError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# 工具名来源注入点。宿主装配时把自己的 registry 挂上来,策略校验(哪些工具名
# 合法、回滚时哪些工具需要确认)才认得宿主工具。
KnownTools = Callable[[], set[str]]

def _default_registry_provider() -> ToolRegistry:
    return default_registry


_registry_provider: Callable[[], ToolRegistry] = _default_registry_provider


def set_tool_registry_provider(provider: Callable[[], ToolRegistry] | None) -> None:
    """注入工具注册表来源(传 None 恢复 default_registry)。"""
    global _registry_provider
    _registry_provider = provider or _default_registry_provider


def _resolve_known_tools(known_tools: KnownTools | None) -> set[str]:
    if known_tools is not None:
        return set(known_tools())
    return set(_registry_provider().names())


def _profile(value: object) -> PermissionProfile:
    try:
        return PermissionProfile(str(value))
    except ValueError as exc:
        raise PermissionManagementError("invalid_profile", "权限模式非法") from exc


def _scope(value: object) -> PermissionScope:
    try:
        return PermissionScope(str(value))
    except ValueError as exc:
        raise PermissionManagementError("invalid_scope", "权限范围非法") from exc


def _decision(value: object) -> PermissionDecision:
    try:
        return PermissionDecision(str(value))
    except ValueError as exc:
        raise PermissionManagementError("invalid_decision", "权限决策非法") from exc


def _category(value: object) -> ToolCategory:
    try:
        return ToolCategory(str(value))
    except ValueError as exc:
        raise PermissionManagementError("invalid_category", "权限分类非法") from exc


def _known_tools(values: object, *, field: str, known: set[str]) -> list[str]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) and item for item in values
    ):
        raise PermissionManagementError("invalid_policy", f"{field} 必须是工具名数组")
    unknown = sorted(set(values) - known)
    if unknown:
        raise PermissionManagementError(
            "unknown_tool",
            f"{field} 包含未知工具：{', '.join(unknown)}",
        )
    return sorted(set(values))


def _categories(values: object, *, field: str) -> list[str]:
    if not isinstance(values, list):
        raise PermissionManagementError("invalid_policy", f"{field} 必须是分类数组")
    categories = sorted({_category(item).value for item in values})
    return categories


def validate_custom_rules(
    value: object,
) -> MappingProxyType[ToolCategory, PermissionDecision]:
    if not isinstance(value, dict):
        raise PermissionManagementError("invalid_custom_rules", "自定义权限必须是对象")
    return MappingProxyType(
        {_category(key): _decision(decision) for key, decision in value.items()}
    )


def validate_scope(
    requested: PermissionScope,
    maximum: PermissionScope,
    *,
    scope_id: UUID | None,
) -> None:
    if _SCOPE_RANK[requested] > _SCOPE_RANK[maximum]:
        raise PermissionManagementError("scope_too_broad", "权限范围超过组织上限")
    if requested is PermissionScope.RESOURCE and scope_id is None:
        raise PermissionManagementError(
            "scope_requires_binding",
            "未绑定作用域的会话不能选择作用域范围",
        )


async def reviewer_routing_healthy(
    session: AsyncSession,
    *,
    org_id: UUID,
) -> bool:
    """Configuration health gate for enabling the automatic Reviewer.

    This intentionally avoids a paid probe. Healthy means an active dedicated
    prompt exists and the organization can resolve an active org/platform route
    or the configured global default route.
    """

    prompt_id = (
        await session.execute(
            select(Prompt.id)
            .where(
                Prompt.task == APPROVAL_REVIEW_TASK,
                Prompt.is_active,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if prompt_id is None:
        return False
    route_id = (
        await session.execute(
            select(ModelRoute.id)
            .where(
                ModelRoute.task == APPROVAL_REVIEW_TASK,
                ModelRoute.is_active,
                (ModelRoute.org_id == org_id) | (ModelRoute.org_id.is_(None)),  # type: ignore[union-attr]
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return route_id is not None or bool(get_settings().llm_default_model)


async def new_session_permission_defaults(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    scope_id: UUID | None,
) -> tuple[PermissionProfile, PermissionScope, dict, AgentApprovalPolicy | None]:
    policy = await active_organization_policy(session, org_id=org_id)
    preference = (
        await session.execute(
            select(AgentPermissionPreference).where(
                AgentPermissionPreference.org_id == org_id,
                AgentPermissionPreference.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    allowed = (
        {_profile(item) for item in policy.allowed_profiles}
        if policy is not None and policy.is_enabled
        else {PermissionProfile.REQUEST_APPROVAL}
    )
    preferred_profile = (
        _profile(preference.default_profile)
        if preference is not None
        else (
            _profile(policy.default_profile)
            if policy is not None and policy.is_enabled
            else PermissionProfile.REQUEST_APPROVAL
        )
    )
    profile = (
        preferred_profile
        if preferred_profile in allowed
        and preferred_profile is not PermissionProfile.FULL_ACCESS
        else PermissionProfile.REQUEST_APPROVAL
    )
    maximum_scope = (
        _scope(policy.max_scope)
        if policy is not None
        else (PermissionScope.RESOURCE if scope_id else PermissionScope.SESSION)
    )
    preferred_scope = (
        _scope(preference.preferred_scope)
        if preference is not None
        else (PermissionScope.RESOURCE if scope_id else PermissionScope.SESSION)
    )
    if preferred_scope is PermissionScope.RESOURCE and scope_id is None:
        preferred_scope = PermissionScope.SESSION
    if _SCOPE_RANK[preferred_scope] > _SCOPE_RANK[maximum_scope]:
        preferred_scope = maximum_scope
    custom_rules = dict(preference.custom_rules or {}) if preference is not None else {}
    return profile, preferred_scope, custom_rules, policy


async def get_owned_chat_session(
    session: AsyncSession,
    *,
    session_id: UUID,
    user_id: UUID,
    for_update: bool = False,
) -> ChatSession:
    query = select(ChatSession).where(ChatSession.id == session_id)
    if for_update:
        query = query.with_for_update()
    chat = (await session.execute(query)).scalar_one_or_none()
    if chat is None or chat.user_id != user_id:
        raise PermissionManagementError("session_not_found", "会话不存在")
    return chat


async def change_session_permission(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    session_id: UUID,
    expected_revision: int,
    expected_policy_version: int,
    profile: PermissionProfile,
    scope: PermissionScope,
    custom_rules: dict | None,
    expires_in_seconds: int | None,
    acknowledge_full_access: bool,
    now: datetime | None = None,
) -> tuple[ChatSession, EffectivePermissionPolicy]:
    current_time = now or datetime.now(UTC)
    chat = await get_owned_chat_session(
        session,
        session_id=session_id,
        user_id=user_id,
        for_update=True,
    )
    previous_profile = _profile(chat.permission_profile)
    previous_scope = _scope(chat.permission_scope)
    if chat.active_run_id is not None:
        raise PermissionManagementError(
            "agent_busy",
            "Agent 正在运行，权限将在回合结束后才能修改",
        )
    effective = await load_effective_policy(
        session,
        org_id=org_id,
        user_id=user_id,
        chat_session=chat,
        now=current_time,
    )
    if chat.permission_revision != expected_revision:
        raise PermissionManagementError("stale_revision", "会话权限版本已变化")
    if effective.policy_version != expected_policy_version:
        raise PermissionManagementError("stale_policy", "组织权限策略版本已变化")
    if profile not in effective.allowed_profiles:
        raise PermissionManagementError("profile_not_allowed", "组织未开放该权限模式")
    organization_policy = await active_organization_policy(session, org_id=org_id)
    maximum_scope = (
        _scope(organization_policy.max_scope)
        if organization_policy is not None
        else (
            PermissionScope.RESOURCE
            if chat.scope_id is not None
            else PermissionScope.SESSION
        )
    )
    validate_scope(scope, maximum_scope, scope_id=chat.scope_id)

    parsed_custom: MappingProxyType[ToolCategory, PermissionDecision] | None = None
    if custom_rules is not None:
        parsed_custom = validate_custom_rules(custom_rules)
    if profile is PermissionProfile.CUSTOM and parsed_custom is None:
        raise PermissionManagementError(
            "custom_rules_required",
            "自定义模式需要提交分类规则",
        )

    expires_at: datetime | None = None
    if profile is PermissionProfile.FULL_ACCESS:
        if not acknowledge_full_access:
            raise PermissionManagementError(
                "full_access_ack_required",
                "启用完全访问前必须确认业务边界与风险",
            )
        if (
            expires_in_seconds is None
            or expires_in_seconds <= 0
            or expires_in_seconds > effective.max_full_access_ttl_seconds
        ):
            raise PermissionManagementError(
                "invalid_full_access_expiry",
                "完全访问有效期超过组织上限",
            )
        expires_at = current_time + timedelta(seconds=expires_in_seconds)
    elif expires_in_seconds is not None:
        raise PermissionManagementError(
            "expiry_not_applicable",
            "仅完全访问模式接受有效期",
        )

    chat.permission_profile = profile
    chat.permission_scope = scope
    chat.permission_expires_at = expires_at
    if parsed_custom is not None:
        chat.permission_custom_rules = {
            category.value: decision.value
            for category, decision in parsed_custom.items()
        }
    chat.permission_revision += 1
    updated = await load_effective_policy(
        session,
        org_id=org_id,
        user_id=user_id,
        chat_session=chat,
        now=current_time,
    )
    chat.permission_policy_id = updated.policy_id
    chat.permission_policy_version = updated.policy_version
    chat.permission_policy_snapshot = policy_snapshot_payload(updated)
    session.add(chat)
    add_permission_audit(
        session,
        org_id=org_id,
        actor_user_id=user_id,
        action=(
            PermissionAuditAction.FULL_ACCESS_ACTIVATED
            if profile is PermissionProfile.FULL_ACCESS
            else PermissionAuditAction.PROFILE_CHANGED
        ),
        entity_id=chat.id,
        detail={
            "session_id": chat.id,
            "policy_id": updated.policy_id,
            "policy_version": updated.policy_version,
            "profile": profile,
            "previous_profile": previous_profile,
            "scope": scope,
            "previous_scope": previous_scope,
            "expires_at": expires_at,
        },
    )
    return chat, updated


async def active_organization_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    for_update: bool = False,
) -> AgentApprovalPolicy | None:
    query = select(AgentApprovalPolicy).where(
        AgentApprovalPolicy.org_id == org_id,
        AgentApprovalPolicy.is_active,
    )
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def validate_hard_rules(value: object, *, known_tools: KnownTools | None = None) -> dict:
    known = _resolve_known_tools(known_tools)
    if not isinstance(value, dict):
        raise PermissionManagementError("invalid_policy", "hard_rules 必须是对象")
    unknown_keys = sorted(set(value) - _HARD_RULE_KEYS)
    if unknown_keys:
        raise PermissionManagementError(
            "invalid_policy",
            f"hard_rules 包含未知字段：{', '.join(unknown_keys)}",
        )
    result: dict[str, object] = {}
    compatibility = value.get("compatibility_mode")
    if compatibility is not None:
        if not isinstance(compatibility, bool):
            raise PermissionManagementError(
                "invalid_policy",
                "compatibility_mode 必须是布尔值",
            )
        result["compatibility_mode"] = compatibility
    for field in ("tool_decisions",):
        raw = value.get(field)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise PermissionManagementError("invalid_policy", f"{field} 必须是对象")
        unknown = sorted(set(raw) - known)
        if unknown:
            raise PermissionManagementError(
                "unknown_tool",
                f"{field} 包含未知工具：{', '.join(unknown)}",
            )
        result[field] = {
            name: _decision(decision).value for name, decision in raw.items()
        }
    raw_categories = value.get("category_decisions")
    if raw_categories is not None:
        if not isinstance(raw_categories, dict):
            raise PermissionManagementError(
                "invalid_policy",
                "category_decisions 必须是对象",
            )
        result["category_decisions"] = {
            _category(category).value: _decision(decision).value
            for category, decision in raw_categories.items()
        }
    for field in ("denied_tools", "user_required_tools"):
        if field in value:
            result[field] = _known_tools(value[field], field=field, known=known)
    for field in (
        "denied_categories",
        "user_required_categories",
        "reviewer_overridable_categories",
    ):
        if field in value:
            result[field] = _categories(value[field], field=field)
    return result


async def create_organization_policy_version(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    default_profile: PermissionProfile,
    allowed_profiles: list[PermissionProfile],
    hard_rules: dict,
    reviewer_enabled: bool,
    reviewer_eligible_categories: list[ToolCategory],
    reviewer_eligible_tools: list[str],
    max_scope: PermissionScope,
    max_grant_ttl_seconds: int,
    max_full_access_ttl_seconds: int,
    is_enabled: bool,
    shadow_evaluation: bool,
    known_tools: KnownTools | None = None,
    audit_action: PermissionAuditAction = (
        PermissionAuditAction.ORGANIZATION_POLICY_CHANGED
    ),
) -> AgentApprovalPolicy:
    known = _resolve_known_tools(known_tools)
    organization_exists = (
        await session.execute(
            select(Organization.id)
            .where(Organization.id == org_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if organization_exists is None:
        raise PermissionManagementError("organization_not_found", "组织不存在")
    current = await active_organization_policy(
        session,
        org_id=org_id,
        for_update=True,
    )
    current_version = current.version if current is not None else 0
    if current_version != expected_version:
        raise PermissionManagementError("stale_policy", "组织权限策略版本已变化")
    unique_profiles = sorted(set(allowed_profiles), key=lambda item: item.value)
    if PermissionProfile.REQUEST_APPROVAL not in unique_profiles:
        raise PermissionManagementError(
            "request_approval_required",
            "请求审批模式必须始终可用",
        )
    if default_profile not in unique_profiles:
        raise PermissionManagementError(
            "default_profile_not_allowed",
            "默认模式必须在允许列表中",
        )
    if default_profile is PermissionProfile.FULL_ACCESS:
        raise PermissionManagementError(
            "full_access_default_forbidden",
            "完全访问必须由用户在会话内显式启用，不能设为组织默认",
        )
    normalized_rules = validate_hard_rules(hard_rules, known_tools=lambda: known)
    eligible_tools = _known_tools(
        reviewer_eligible_tools, field="reviewer_eligible_tools", known=known
    )
    eligible_categories = sorted(
        {category.value for category in reviewer_eligible_categories}
    )
    overridable = set(normalized_rules.get("reviewer_overridable_categories", []))
    if not overridable.issubset(eligible_categories):
        raise PermissionManagementError(
            "override_not_reviewer_eligible",
            "可覆盖分类必须同时属于 Reviewer 可审批分类",
        )
    auto_review_allowed = PermissionProfile.AUTO_REVIEW in unique_profiles
    if auto_review_allowed and not reviewer_enabled:
        raise PermissionManagementError(
            "reviewer_not_enabled",
            "开放智能审批前必须启用独立 Reviewer",
        )
    if auto_review_allowed and not (eligible_tools or eligible_categories):
        raise PermissionManagementError(
            "reviewer_eligibility_required",
            "开放智能审批前必须配置 Reviewer 可审批工具或分类",
        )
    if auto_review_allowed and not await reviewer_routing_healthy(
        session,
        org_id=org_id,
    ):
        raise PermissionManagementError(
            "reviewer_route_unhealthy",
            "独立 Reviewer 的提示词或模型路由尚未就绪",
        )
    if max_grant_ttl_seconds <= 0 or max_grant_ttl_seconds > 30 * 24 * 3600:
        raise PermissionManagementError("invalid_grant_expiry", "授权有效期上限非法")
    if max_full_access_ttl_seconds <= 0 or max_full_access_ttl_seconds > 24 * 3600:
        raise PermissionManagementError(
            "invalid_full_access_expiry",
            "完全访问有效期上限非法",
        )
    if current is not None:
        current.is_active = False
        session.add(current)
        await session.flush()
    policy = AgentApprovalPolicy(
        org_id=org_id,
        version=current_version + 1,
        default_profile=default_profile,
        allowed_profiles=[profile.value for profile in unique_profiles],
        hard_rules=normalized_rules,
        reviewer_enabled=reviewer_enabled,
        reviewer_eligible_categories=eligible_categories,
        reviewer_eligible_tools=eligible_tools,
        max_scope=max_scope,
        max_grant_ttl_seconds=max_grant_ttl_seconds,
        max_full_access_ttl_seconds=max_full_access_ttl_seconds,
        is_enabled=is_enabled,
        shadow_evaluation=shadow_evaluation,
        is_active=True,
        created_by=actor_user_id,
    )
    session.add(policy)
    await session.flush()
    add_permission_audit(
        session,
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=audit_action,
        entity_id=policy.id,
        detail={
            "policy_id": policy.id,
            "policy_version": policy.version,
            "previous_policy_version": current_version,
            "profile": default_profile,
            "scope": max_scope,
            "feature_enabled": is_enabled,
        },
    )
    return policy


async def rollback_organization_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_user_id: UUID,
    expected_version: int,
    known_tools: KnownTools | None = None,
    confirm_required_tools: KnownTools | None = None,
) -> AgentApprovalPolicy:
    current = await active_organization_policy(session, org_id=org_id)
    max_scope = current.max_scope if current else PermissionScope.RESOURCE
    max_grant_ttl = current.max_grant_ttl_seconds if current else 28800
    max_full_ttl = current.max_full_access_ttl_seconds if current else 3600
    strict_tools = sorted(
        confirm_required_tools()
        if confirm_required_tools is not None
        else _registry_provider().confirm_required_names()
    )
    return await create_organization_policy_version(
        session,
        org_id=org_id,
        actor_user_id=actor_user_id,
        expected_version=expected_version,
        default_profile=PermissionProfile.REQUEST_APPROVAL,
        allowed_profiles=[PermissionProfile.REQUEST_APPROVAL],
        hard_rules={
            "compatibility_mode": True,
            "tool_decisions": {name: "ask_user" for name in strict_tools},
        },
        reviewer_enabled=False,
        reviewer_eligible_categories=[],
        reviewer_eligible_tools=[],
        max_scope=_scope(max_scope),
        max_grant_ttl_seconds=max_grant_ttl,
        max_full_access_ttl_seconds=max_full_ttl,
        is_enabled=False,
        shadow_evaluation=False,
        known_tools=known_tools,
        audit_action=PermissionAuditAction.POLICY_ROLLED_BACK,
    )


async def update_user_permission_preference(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    default_profile: PermissionProfile,
    preferred_scope: PermissionScope,
    custom_rules: dict,
) -> AgentPermissionPreference:
    policy = await active_organization_policy(session, org_id=org_id)
    allowed = (
        {_profile(item) for item in policy.allowed_profiles}
        if policy is not None and policy.is_enabled
        else {PermissionProfile.REQUEST_APPROVAL}
    )
    if default_profile not in allowed:
        raise PermissionManagementError("profile_not_allowed", "组织未开放该默认模式")
    if default_profile is PermissionProfile.FULL_ACCESS:
        raise PermissionManagementError(
            "full_access_default_forbidden",
            "完全访问必须在会话内显式启用，不能保存为用户默认",
        )
    maximum_scope = _scope(policy.max_scope) if policy else PermissionScope.SESSION
    if _SCOPE_RANK[preferred_scope] > _SCOPE_RANK[maximum_scope]:
        raise PermissionManagementError("scope_too_broad", "默认权限范围超过组织上限")
    parsed_rules = validate_custom_rules(custom_rules)
    preference = (
        await session.execute(
            select(AgentPermissionPreference)
            .where(
                AgentPermissionPreference.org_id == org_id,
                AgentPermissionPreference.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    previous_profile = (
        _profile(preference.default_profile)
        if preference is not None
        else (
            _profile(policy.default_profile)
            if policy is not None and policy.is_enabled
            else PermissionProfile.REQUEST_APPROVAL
        )
    )
    previous_scope = (
        _scope(preference.preferred_scope)
        if preference is not None
        else PermissionScope.SESSION
    )
    if preference is None:
        preference = AgentPermissionPreference(org_id=org_id, user_id=user_id)
    preference.default_profile = default_profile
    preference.preferred_scope = preferred_scope
    preference.custom_rules = {
        category.value: decision.value for category, decision in parsed_rules.items()
    }
    session.add(preference)
    add_permission_audit(
        session,
        org_id=org_id,
        actor_user_id=user_id,
        action=PermissionAuditAction.PREFERENCE_CHANGED,
        entity_id=preference.id,
        detail={
            "policy_id": policy.id if policy is not None else None,
            "policy_version": policy.version if policy is not None else 0,
            "profile": default_profile,
            "previous_profile": previous_profile,
            "scope": preferred_scope,
            "previous_scope": previous_scope,
        },
    )
    return preference
