"""User-owned scoped permission grant listing and revocation."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.permissions.audit import (
    PermissionAuditAction,
    add_permission_audit,
)
from nicekit.models.agent_permission import AgentPermissionGrant


async def list_active_permission_grants(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    session_id: UUID | None = None,
    now: datetime | None = None,
) -> list[AgentPermissionGrant]:
    """List only unrevoked, unexpired grants visible to their owning user."""

    current_time = now or datetime.now(UTC)
    query = select(AgentPermissionGrant).where(
        AgentPermissionGrant.org_id == org_id,
        AgentPermissionGrant.user_id == user_id,
        AgentPermissionGrant.revoked_at.is_(None),
        AgentPermissionGrant.expires_at > current_time,
    )
    if session_id is not None:
        query = query.where(
            or_(
                AgentPermissionGrant.session_id == session_id,
                AgentPermissionGrant.session_id.is_(None),
            )
        )
    return list(
        (
            await session.execute(
                query.order_by(
                    AgentPermissionGrant.expires_at,
                    AgentPermissionGrant.created_at,
                )
            )
        )
        .scalars()
        .all()
    )


async def revoke_permission_grant(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    grant_id: UUID,
    revoked_by: UUID,
    now: datetime | None = None,
) -> AgentPermissionGrant | None:
    """Revoke one user-owned grant; cross-user and cross-org IDs are not observable."""

    grant = (
        await session.execute(
            select(AgentPermissionGrant)
            .where(
                AgentPermissionGrant.id == grant_id,
                AgentPermissionGrant.org_id == org_id,
                AgentPermissionGrant.user_id == user_id,
                AgentPermissionGrant.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None:
        return None
    grant.revoked_at = now or datetime.now(UTC)
    grant.revoked_by = revoked_by
    session.add(grant)
    add_permission_audit(
        session,
        org_id=org_id,
        actor_user_id=revoked_by,
        action=PermissionAuditAction.GRANT_REVOKED,
        entity_id=grant.id,
        detail={
            "session_id": grant.session_id,
            "grant_id": grant.id,
            "policy_id": grant.policy_id,
            "policy_version": grant.policy_version,
            "scope": grant.scope,
            "tool_name": grant.tool_name,
            "categories": [grant.category] if grant.category else [],
            "scope_id": grant.scope_id,
            "resource_type": grant.resource_type,
            "resource_id": grant.resource_id,
            "expires_at": grant.expires_at,
        },
    )
    return grant


def public_permission_grant(grant: AgentPermissionGrant) -> dict:
    """Return bounded grant metadata without raw arguments or action fingerprints."""

    return {
        "id": str(grant.id),
        "session_id": str(grant.session_id) if grant.session_id else None,
        "scope_type": grant.scope_type,
        "scope_id": str(grant.scope_id) if grant.scope_id else None,
        "tool_name": grant.tool_name,
        "category": str(grant.category) if grant.category else None,
        "scope": str(grant.scope),
        "resource_type": grant.resource_type,
        "resource_id": grant.resource_id,
        "policy_version": grant.policy_version,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "expires_at": grant.expires_at.isoformat(),
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
    }
