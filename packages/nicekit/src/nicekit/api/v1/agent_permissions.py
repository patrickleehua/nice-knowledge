"""Conversation, user, and organization Agent permission APIs.

搬自 TF backend/app/api/v1/agent_permissions.py。SDK 化改造:
- `PermissionScope.PROJECT` → `RESOURCE`(宿主业务作用域这一档);
- 会话状态里的 `project_id` → `scope_type`/`scope_id`;
- 工具目录从模块级 TOOLS dict 换成注入的 `ToolRegistry`
  (与 permissions/management.py 的 `set_tool_registry_provider` 同源,
  管理端能勾的工具名与策略校验接受的工具名永远是同一份);
- 角色守卫用字符串化的内置角色(tenancy/roles.py)。
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.loop import (
    public_pending_confirmation,
    public_reviewer_override,
)
from nicekit.agent.permissions import (
    PermissionAuditAction,
    active_reviewer_overrides,
    list_active_permission_grants,
    public_permission_grant,
    revoke_permission_grant,
)
from nicekit.agent.permissions.management import (
    PermissionManagementError,
    active_organization_policy,
    change_session_permission,
    create_organization_policy_version,
    get_owned_chat_session,
    reviewer_routing_healthy,
    rollback_organization_policy,
    update_user_permission_preference,
)
from nicekit.agent.permissions.policy import (
    load_effective_policy,
    policy_snapshot_payload,
)
from nicekit.agent.tools import default_registry
from nicekit.api.deps import OrgContext, get_org_context, get_org_session, require_role
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionProfile,
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolRisk,
)
from nicekit.models.agent_permission import (
    AgentApprovalPolicy,
    AgentPermissionPreference,
)
from nicekit.models.tenancy import AuditLog, Role

router = APIRouter()

Session = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]
AdminCtx = Annotated[
    OrgContext,
    Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
]

_PROFILE_COPY = {
    PermissionProfile.REQUEST_APPROVAL: (
        "请求审批",
        "写入、联网和付费操作由你确认",
    ),
    PermissionProfile.AUTO_REVIEW: (
        "智能审批",
        "常规操作自动执行，敏感操作由独立 Reviewer 判断",
    ),
    PermissionProfile.FULL_ACCESS: (
        "完全访问（授权范围内）",
        "在当前授权作用域和有效期内免询问执行可委托工具",
    ),
    PermissionProfile.CUSTOM: (
        "自定义",
        "按稳定工具分类配置审批方式",
    ),
}


class PermissionProfileOptionOut(BaseModel):
    id: PermissionProfile
    label: str
    description: str
    allowed: bool
    restriction: str | None = None


class OrganizationConstraintsOut(BaseModel):
    policy_id: UUID | None
    policy_version: int
    is_enabled: bool
    shadow_evaluation: bool
    max_scope: PermissionScope
    max_grant_ttl_seconds: int
    max_full_access_ttl_seconds: int
    reviewer_enabled: bool
    reviewer_eligible_categories: list[ToolCategory]
    reviewer_eligible_tools: list[str]
    denied_categories: list[ToolCategory]
    denied_tools: list[str]
    user_required_categories: list[ToolCategory]
    user_required_tools: list[str]


class SessionPermissionStateOut(BaseModel):
    session_id: UUID
    revision: int
    profile: PermissionProfile
    scope: PermissionScope
    expires_at: datetime | None
    active_run: bool
    scope_type: str | None
    scope_id: UUID | None
    custom_rules: dict[ToolCategory, PermissionDecision]
    policy_snapshot: dict
    profile_options: list[PermissionProfileOptionOut]
    organization: OrganizationConstraintsOut
    grants: list[dict]
    pending_decision: dict | None
    reviewer_overrides: list[dict]


class SessionPermissionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_policy_version: int = Field(ge=0)
    profile: PermissionProfile
    scope: PermissionScope
    custom_rules: dict[ToolCategory, PermissionDecision] | None = None
    expires_in_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)
    acknowledge_full_access: bool = False


class PermissionPreferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: PermissionProfile
    preferred_scope: PermissionScope
    custom_rules: dict[ToolCategory, PermissionDecision] = Field(default_factory=dict)


class PermissionPreferenceOut(BaseModel):
    default_profile: PermissionProfile
    preferred_scope: PermissionScope
    custom_rules: dict[ToolCategory, PermissionDecision]
    allowed_profiles: list[PermissionProfile]
    max_scope: PermissionScope


class HardRulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatibility_mode: bool | None = None
    tool_decisions: dict[str, PermissionDecision] | None = None
    category_decisions: dict[ToolCategory, PermissionDecision] | None = None
    denied_tools: list[str] | None = None
    denied_categories: list[ToolCategory] | None = None
    user_required_tools: list[str] | None = None
    user_required_categories: list[ToolCategory] | None = None
    reviewer_overridable_categories: list[ToolCategory] | None = None


class OrganizationPolicyUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    default_profile: PermissionProfile
    allowed_profiles: list[PermissionProfile] = Field(min_length=1, max_length=4)
    hard_rules: HardRulesIn = Field(default_factory=HardRulesIn)
    reviewer_enabled: bool = False
    reviewer_eligible_categories: list[ToolCategory] = Field(
        default_factory=list,
        max_length=len(ToolCategory),
    )
    reviewer_eligible_tools: list[str] = Field(default_factory=list, max_length=100)
    max_scope: PermissionScope = PermissionScope.RESOURCE
    max_grant_ttl_seconds: int = Field(default=28800, ge=60, le=30 * 24 * 3600)
    max_full_access_ttl_seconds: int = Field(default=3600, ge=60, le=24 * 3600)
    is_enabled: bool = True
    shadow_evaluation: bool = False


class OrganizationPolicyRollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)


class OrganizationPolicyOut(BaseModel):
    id: UUID | None
    version: int
    default_profile: PermissionProfile
    allowed_profiles: list[PermissionProfile]
    hard_rules: dict
    reviewer_enabled: bool
    reviewer_eligible_categories: list[ToolCategory]
    reviewer_eligible_tools: list[str]
    max_scope: PermissionScope
    max_grant_ttl_seconds: int
    max_full_access_ttl_seconds: int
    is_enabled: bool
    shadow_evaluation: bool
    is_active: bool
    created_at: datetime | None
    # 只读:智能审批需要 Reviewer 提示词与模型路由就位,供管理端在提交前提示而非吃 422。
    reviewer_route_healthy: bool = False


class ToolCatalogItemOut(BaseModel):
    """Non-secret tool metadata accepted by organization hard rules."""

    name: str
    label: str
    group: str
    description: str
    effect: ToolEffect
    risk: ToolRisk
    categories: list[ToolCategory]
    delegation: ToolDelegation


class PermissionAuditOut(BaseModel):
    id: UUID
    user_id: UUID | None
    action: PermissionAuditAction
    entity_type: str
    entity_id: str | None
    detail: dict
    created_at: datetime | None


def _raise_management(error: PermissionManagementError) -> None:
    if error.code == "session_not_found":
        code = status.HTTP_404_NOT_FOUND
    elif error.code in {"agent_busy", "stale_revision", "stale_policy"}:
        code = status.HTTP_409_CONFLICT
    elif error.code in {"profile_not_allowed", "scope_too_broad"}:
        code = status.HTTP_403_FORBIDDEN
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(code, {"code": error.code, "message": str(error)}) from error


def _policy_out(
    policy: AgentApprovalPolicy | None,
    *,
    reviewer_route_healthy: bool = False,
) -> OrganizationPolicyOut:
    if policy is None:
        return OrganizationPolicyOut(
            id=None,
            version=0,
            default_profile=PermissionProfile.REQUEST_APPROVAL,
            allowed_profiles=[PermissionProfile.REQUEST_APPROVAL],
            hard_rules={"compatibility_mode": True},
            reviewer_enabled=False,
            reviewer_eligible_categories=[],
            reviewer_eligible_tools=[],
            max_scope=PermissionScope.RESOURCE,
            max_grant_ttl_seconds=28800,
            max_full_access_ttl_seconds=3600,
            is_enabled=True,
            shadow_evaluation=False,
            is_active=True,
            created_at=None,
            reviewer_route_healthy=reviewer_route_healthy,
        )
    return OrganizationPolicyOut(
        id=policy.id,
        version=policy.version,
        default_profile=PermissionProfile(str(policy.default_profile)),
        allowed_profiles=[PermissionProfile(value) for value in policy.allowed_profiles],
        hard_rules=policy.hard_rules,
        reviewer_enabled=policy.reviewer_enabled,
        reviewer_eligible_categories=[
            ToolCategory(value) for value in policy.reviewer_eligible_categories
        ],
        reviewer_eligible_tools=policy.reviewer_eligible_tools,
        max_scope=PermissionScope(str(policy.max_scope)),
        max_grant_ttl_seconds=policy.max_grant_ttl_seconds,
        max_full_access_ttl_seconds=policy.max_full_access_ttl_seconds,
        is_enabled=policy.is_enabled,
        shadow_evaluation=policy.shadow_evaluation,
        is_active=policy.is_active,
        created_at=policy.created_at,
        reviewer_route_healthy=reviewer_route_healthy,
    )


async def _session_state(
    session: AsyncSession,
    ctx: OrgContext,
    session_id: UUID,
) -> SessionPermissionStateOut:
    try:
        chat = await get_owned_chat_session(
            session,
            session_id=session_id,
            user_id=ctx.user_id,
        )
    except PermissionManagementError as exc:
        _raise_management(exc)
    effective = await load_effective_policy(
        session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        chat_session=chat,
    )
    policy = await active_organization_policy(session, org_id=ctx.org_id)
    grants = await list_active_permission_grants(
        session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        session_id=chat.id,
    )
    hard_rules = policy.hard_rules if policy is not None else {}
    max_scope = (
        PermissionScope(str(policy.max_scope))
        if policy is not None
        else (PermissionScope.RESOURCE if chat.scope_id else PermissionScope.SESSION)
    )
    return SessionPermissionStateOut(
        session_id=chat.id,
        revision=chat.permission_revision,
        profile=effective.profile,
        scope=effective.scope,
        expires_at=effective.expires_at,
        active_run=chat.active_run_id is not None,
        scope_type=chat.scope_type,
        scope_id=chat.scope_id,
        custom_rules={
            ToolCategory(key): PermissionDecision(value)
            for key, value in (chat.permission_custom_rules or {}).items()
        },
        policy_snapshot=policy_snapshot_payload(effective),
        profile_options=[
            PermissionProfileOptionOut(
                id=profile,
                label=_PROFILE_COPY[profile][0],
                description=_PROFILE_COPY[profile][1],
                allowed=profile in effective.allowed_profiles,
                restriction=(
                    None
                    if profile in effective.allowed_profiles
                    else "组织未开放该模式"
                ),
            )
            for profile in PermissionProfile
        ],
        organization=OrganizationConstraintsOut(
            policy_id=effective.policy_id,
            policy_version=effective.policy_version,
            is_enabled=effective.is_enabled,
            shadow_evaluation=effective.shadow_evaluation,
            max_scope=max_scope,
            max_grant_ttl_seconds=effective.max_grant_ttl_seconds,
            max_full_access_ttl_seconds=effective.max_full_access_ttl_seconds,
            reviewer_enabled=effective.reviewer_enabled,
            reviewer_eligible_categories=sorted(
                effective.reviewer_eligible_categories,
                key=lambda item: item.value,
            ),
            reviewer_eligible_tools=sorted(effective.reviewer_eligible_tools),
            denied_categories=[
                ToolCategory(value)
                for value in hard_rules.get("denied_categories", [])
            ],
            denied_tools=sorted(hard_rules.get("denied_tools", [])),
            user_required_categories=[
                ToolCategory(value)
                for value in hard_rules.get("user_required_categories", [])
            ],
            user_required_tools=sorted(hard_rules.get("user_required_tools", [])),
        ),
        grants=[public_permission_grant(grant) for grant in grants],
        pending_decision=public_pending_confirmation(chat.pending_confirmation),
        reviewer_overrides=[
            public_reviewer_override(candidate)
            for candidate in active_reviewer_overrides(
                chat.reviewer_override_candidates or []
            )
        ],
    )


@router.get(
    "/chat/sessions/{session_id}/permissions",
    response_model=SessionPermissionStateOut,
)
async def get_session_permissions(
    session_id: UUID,
    session: Session,
    ctx: Ctx,
) -> SessionPermissionStateOut:
    return await _session_state(session, ctx, session_id)


@router.put(
    "/chat/sessions/{session_id}/permissions",
    response_model=SessionPermissionStateOut,
)
async def update_session_permissions(
    session_id: UUID,
    body: SessionPermissionUpdateIn,
    session: Session,
    ctx: Ctx,
) -> SessionPermissionStateOut:
    try:
        await change_session_permission(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            session_id=session_id,
            expected_revision=body.expected_revision,
            expected_policy_version=body.expected_policy_version,
            profile=body.profile,
            scope=body.scope,
            custom_rules=(
                {key.value: value.value for key, value in body.custom_rules.items()}
                if body.custom_rules is not None
                else None
            ),
            expires_in_seconds=body.expires_in_seconds,
            acknowledge_full_access=body.acknowledge_full_access,
        )
    except PermissionManagementError as exc:
        _raise_management(exc)
    await session.commit()
    return await _session_state(session, ctx, session_id)


async def _preference_out(
    session: AsyncSession,
    ctx: OrgContext,
) -> PermissionPreferenceOut:
    policy = await active_organization_policy(session, org_id=ctx.org_id)
    preference = (
        await session.execute(
            select(AgentPermissionPreference).where(
                AgentPermissionPreference.org_id == ctx.org_id,
                AgentPermissionPreference.user_id == ctx.user_id,
            )
        )
    ).scalar_one_or_none()
    allowed = (
        [PermissionProfile(value) for value in policy.allowed_profiles]
        if policy is not None and policy.is_enabled
        else [PermissionProfile.REQUEST_APPROVAL]
    )
    return PermissionPreferenceOut(
        default_profile=(
            PermissionProfile(str(preference.default_profile))
            if preference is not None
            else (
                PermissionProfile(str(policy.default_profile))
                if policy is not None and policy.is_enabled
                else PermissionProfile.REQUEST_APPROVAL
            )
        ),
        preferred_scope=(
            PermissionScope(str(preference.preferred_scope))
            if preference is not None
            else PermissionScope.SESSION
        ),
        custom_rules={
            ToolCategory(key): PermissionDecision(value)
            for key, value in (preference.custom_rules if preference else {}).items()
        },
        allowed_profiles=allowed,
        max_scope=(
            PermissionScope(str(policy.max_scope))
            if policy is not None
            else PermissionScope.SESSION
        ),
    )


@router.get(
    "/agent/permissions/preferences",
    response_model=PermissionPreferenceOut,
)
async def get_permission_preference(
    session: Session,
    ctx: Ctx,
) -> PermissionPreferenceOut:
    return await _preference_out(session, ctx)


@router.put(
    "/agent/permissions/preferences",
    response_model=PermissionPreferenceOut,
)
async def put_permission_preference(
    body: PermissionPreferenceIn,
    session: Session,
    ctx: Ctx,
) -> PermissionPreferenceOut:
    try:
        await update_user_permission_preference(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            default_profile=body.default_profile,
            preferred_scope=body.preferred_scope,
            custom_rules={key.value: value.value for key, value in body.custom_rules.items()},
        )
    except PermissionManagementError as exc:
        _raise_management(exc)
    await session.commit()
    return await _preference_out(session, ctx)


@router.get("/agent/permissions/grants")
async def list_permission_grants(
    session: Session,
    ctx: Ctx,
    session_id: UUID | None = None,
) -> list[dict]:
    if session_id is not None:
        try:
            await get_owned_chat_session(
                session,
                session_id=session_id,
                user_id=ctx.user_id,
            )
        except PermissionManagementError as exc:
            _raise_management(exc)
    grants = await list_active_permission_grants(
        session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        session_id=session_id,
    )
    return [public_permission_grant(grant) for grant in grants]


@router.delete(
    "/agent/permissions/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_permission_grant(
    grant_id: UUID,
    session: Session,
    ctx: Ctx,
) -> Response:
    grant = await revoke_permission_grant(
        session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        grant_id=grant_id,
        revoked_by=ctx.user_id,
    )
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "授权不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/org/agent-permissions/policy",
    response_model=OrganizationPolicyOut,
)
async def get_organization_permission_policy(
    session: Session,
    ctx: AdminCtx,
) -> OrganizationPolicyOut:
    return _policy_out(
        await active_organization_policy(session, org_id=ctx.org_id),
        reviewer_route_healthy=await reviewer_routing_healthy(session, org_id=ctx.org_id),
    )


@router.get(
    "/org/agent-permissions/tool-catalog",
    response_model=list[ToolCatalogItemOut],
    dependencies=[Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN))],
)
async def list_permission_tool_catalog() -> list[ToolCatalogItemOut]:
    """Tool names and permission metadata that organization hard rules accept.

    The catalog is the registry itself, so an administrator can only name tools
    that `validate_hard_rules` will also accept.
    """

    return [
        ToolCatalogItemOut(
            name=tool.name,
            label=tool.label or tool.name,
            group=tool.category,
            description=tool.description,
            effect=tool.permission.effect,
            risk=tool.permission.risk,
            categories=sorted(tool.permission.categories, key=lambda item: item.value),
            delegation=tool.permission.delegation,
        )
        for tool in sorted(default_registry, key=lambda item: (item.category, item.name))
    ]


@router.put(
    "/org/agent-permissions/policy",
    response_model=OrganizationPolicyOut,
)
async def put_organization_permission_policy(
    body: OrganizationPolicyUpdateIn,
    session: Session,
    ctx: AdminCtx,
) -> OrganizationPolicyOut:
    try:
        policy = await create_organization_policy_version(
            session,
            org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            expected_version=body.expected_version,
            default_profile=body.default_profile,
            allowed_profiles=body.allowed_profiles,
            hard_rules=body.hard_rules.model_dump(mode="json", exclude_none=True),
            reviewer_enabled=body.reviewer_enabled,
            reviewer_eligible_categories=body.reviewer_eligible_categories,
            reviewer_eligible_tools=body.reviewer_eligible_tools,
            max_scope=body.max_scope,
            max_grant_ttl_seconds=body.max_grant_ttl_seconds,
            max_full_access_ttl_seconds=body.max_full_access_ttl_seconds,
            is_enabled=body.is_enabled,
            shadow_evaluation=body.shadow_evaluation,
        )
    except PermissionManagementError as exc:
        _raise_management(exc)
    await session.commit()
    await session.refresh(policy)
    return _policy_out(
        policy,
        reviewer_route_healthy=await reviewer_routing_healthy(session, org_id=ctx.org_id),
    )


@router.post(
    "/org/agent-permissions/policy/rollback",
    response_model=OrganizationPolicyOut,
)
async def rollback_permission_policy(
    body: OrganizationPolicyRollbackIn,
    session: Session,
    ctx: AdminCtx,
) -> OrganizationPolicyOut:
    try:
        policy = await rollback_organization_policy(
            session,
            org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            expected_version=body.expected_version,
        )
    except PermissionManagementError as exc:
        _raise_management(exc)
    await session.commit()
    await session.refresh(policy)
    return _policy_out(
        policy,
        reviewer_route_healthy=await reviewer_routing_healthy(session, org_id=ctx.org_id),
    )


@router.get(
    "/org/agent-permissions/audit",
    response_model=list[PermissionAuditOut],
)
async def list_permission_audit(
    session: Session,
    ctx: AdminCtx,
    action: PermissionAuditAction | None = None,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PermissionAuditOut]:
    """Return organization-scoped, already-redacted permission audit rows."""

    query = select(AuditLog).where(
        AuditLog.org_id == ctx.org_id,
        AuditLog.action.like("agent.permission.%"),
    )
    if action is not None:
        query = query.where(AuditLog.action == action.value)
    if before is not None:
        query = query.where(AuditLog.created_at < before)
    rows = (
        (
            await session.execute(
                query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(
                    limit
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        PermissionAuditOut(
            id=row.id,
            user_id=row.user_id,
            action=PermissionAuditAction(row.action),
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            detail=row.detail or {},
            created_at=row.created_at,
        )
        for row in rows
    ]
