"""租户成员与邀请管理(org_admin / platform_admin;迁移自 TF backend/app/api/v1/members.py)。

memberships/users/invitations 为身份表(无 RLS),按 ctx.org_id 在应用层过滤。
邀请令牌只在创建时明文返回一次,库中仅存 sha256。

改造点:可授予角色集合不再是硬编码常量,改由 tenancy/roles.assignable_roles()
在请求时派生(内置 org_admin/member + 宿主启动期注册的角色);
platform_admin 只能由平台侧 seed / admin API 产生,永不可在租户内授予。
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.api.deps import EmailStr, OrgContext, require_role
from nicekit.core.db import get_session
from nicekit.core.security import new_refresh_token
from nicekit.models.tenancy import Invitation, Membership, Role, User
from nicekit.tenancy.roles import assignable_roles

router = APIRouter(prefix="/org/members")

Session = Annotated[AsyncSession, Depends(get_session)]
AdminCtx = Annotated[OrgContext, Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN))]

INVITE_EXPIRE_DAYS = 7


class MemberOut(BaseModel):
    membership_id: UUID
    user_id: UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime | None


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: str


class InviteOut(BaseModel):
    id: UUID
    email: str
    role: str
    expires_at: datetime
    created_at: datetime | None


class InviteCreated(InviteOut):
    token: str  # 明文只返回这一次,前端据此拼邀请链接


def _require_assignable(role: str) -> None:
    # 请求时读注册表(而非模块级快照):宿主在启动期 register_roles 后立即生效
    if role not in assignable_roles():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"角色 {role} 不可在租户内授予")


@router.get("", response_model=list[MemberOut])
async def list_members(ctx: AdminCtx, session: Session) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == ctx.org_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [
        MemberOut(
            membership_id=m.id,
            user_id=u.id,
            email=u.email,
            full_name=u.full_name,
            role=str(m.role),
            is_active=u.is_active,
            created_at=m.created_at,
        )
        for m, u in rows
    ]


async def _get_membership(
    session: AsyncSession, ctx: OrgContext, membership_id: UUID
) -> Membership:
    membership = (
        await session.execute(
            select(Membership).where(
                Membership.id == membership_id, Membership.org_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    return membership


@router.patch("/{membership_id}", response_model=MemberOut)
async def change_role(
    membership_id: UUID, body: RoleUpdate, ctx: AdminCtx, session: Session
) -> MemberOut:
    _require_assignable(body.role)
    membership = await _get_membership(session, ctx, membership_id)
    if membership.user_id == ctx.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能修改自己的角色")
    membership.role = body.role
    session.add(membership)
    await session.commit()
    user = (
        await session.execute(select(User).where(User.id == membership.user_id))
    ).scalar_one()
    return MemberOut(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=str(membership.role),
        is_active=user.is_active,
        created_at=membership.created_at,
    )


@router.delete("/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(membership_id: UUID, ctx: AdminCtx, session: Session) -> None:
    membership = await _get_membership(session, ctx, membership_id)
    if membership.user_id == ctx.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能移除自己")
    await session.delete(membership)
    await session.commit()


# ---------- 邀请 ----------


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(ctx: AdminCtx, session: Session) -> list[InviteOut]:
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(Invitation)
                .where(
                    Invitation.org_id == ctx.org_id,
                    Invitation.accepted_at.is_(None),
                    Invitation.expires_at > now,
                )
                .order_by(Invitation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        InviteOut(
            id=i.id,
            email=i.email,
            role=str(i.role),
            expires_at=i.expires_at,
            created_at=i.created_at,
        )
        for i in rows
    ]


@router.post("/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
async def create_invite(body: InviteCreate, ctx: AdminCtx, session: Session) -> InviteCreated:
    _require_assignable(body.role)
    existing_user = (
        await session.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing_user is not None:
        member = (
            await session.execute(
                select(Membership).where(
                    Membership.org_id == ctx.org_id, Membership.user_id == existing_user.id
                )
            )
        ).scalar_one_or_none()
        if member is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "该用户已是本组织成员")

    raw_token, token_hash = new_refresh_token()
    invitation = Invitation(
        org_id=ctx.org_id,
        email=body.email,
        role=body.role,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=INVITE_EXPIRE_DAYS),
    )
    session.add(invitation)
    await session.commit()
    await session.refresh(invitation)
    return InviteCreated(
        id=invitation.id,
        email=invitation.email,
        role=str(invitation.role),
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        token=raw_token,
    )


@router.delete("/invites/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(invitation_id: UUID, ctx: AdminCtx, session: Session) -> None:
    invitation = (
        await session.execute(
            select(Invitation).where(
                Invitation.id == invitation_id, Invitation.org_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请不存在")
    await session.delete(invitation)
    await session.commit()
