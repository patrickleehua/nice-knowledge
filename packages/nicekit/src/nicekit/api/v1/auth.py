"""认证 API(迁移自 TF backend/app/api/v1/auth.py,逻辑原样):
login / refresh(旋转)/ invite 查看与接受 / logout。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.api.deps import EmailStr
from nicekit.core.config import get_settings
from nicekit.core.db import get_session
from nicekit.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from nicekit.models.tenancy import Invitation, Membership, Organization, RefreshToken, User

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str
    org_slug: str | None = None


class OrgOut(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    org: OrgOut
    orgs: list[OrgOut]


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str


class LogoutRequest(RefreshRequest):
    pass


async def _memberships_of(session: AsyncSession, user_id: UUID) -> list[OrgOut]:
    rows = (
        await session.execute(
            select(Membership, Organization)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user_id, Organization.is_active)
        )
    ).all()
    return [
        OrgOut(id=org.id, slug=org.slug, name=org.name, role=str(m.role))
        for m, org in rows
    ]


async def _issue_tokens(
    session: AsyncSession, user: User, org: OrgOut, orgs: list[OrgOut]
) -> TokenResponse:
    settings = get_settings()
    raw_refresh, refresh_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            org_id=org.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await session.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, org.id, org.role),
        refresh_token=raw_refresh,
        org=org,
        orgs=orgs,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenResponse:
    user = (
        await session.execute(select(User).where(User.email == body.email, User.is_active))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    orgs = await _memberships_of(session, user.id)
    if not orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No organization membership")

    if body.org_slug is not None:
        selected = next((o for o in orgs if o.slug == body.org_slug), None)
        if selected is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this organization")
    else:
        selected = orgs[0]

    return await _issue_tokens(session, user, selected, orgs)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> TokenResponse:
    token_hash = hash_refresh_token(body.refresh_token)
    stored = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if stored is None or stored.revoked_at is not None or stored.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    user = (await session.execute(select(User).where(User.id == stored.user_id))).scalar_one()
    orgs = await _memberships_of(session, user.id)
    selected = next((o for o in orgs if o.id == stored.org_id), None)
    if selected is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Membership revoked")

    # 旋转:旧 refresh token 立即作废
    stored.revoked_at = now
    session.add(stored)
    return await _issue_tokens(session, user, selected, orgs)


class InviteInfoOut(BaseModel):
    email: str
    role: str
    org_name: str
    org_slug: str
    user_exists: bool


class InviteAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    # 新用户必填;已注册用户忽略(不覆盖既有密码)
    full_name: str | None = None
    password: str | None = None


async def _valid_invitation(
    session: AsyncSession, raw_token: str
) -> tuple[Invitation, Organization]:
    token_hash = hash_refresh_token(raw_token)
    invitation = (
        await session.execute(select(Invitation).where(Invitation.token_hash == token_hash))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if invitation is None or invitation.accepted_at is not None or invitation.expires_at < now:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请无效或已过期")
    org = (
        await session.execute(
            select(Organization).where(
                Organization.id == invitation.org_id, Organization.is_active
            )
        )
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "邀请无效或已过期")
    return invitation, org


@router.get("/invite/{token}", response_model=InviteInfoOut)
async def invite_info(
    token: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> InviteInfoOut:
    invitation, org = await _valid_invitation(session, token)
    user = (
        await session.execute(select(User).where(User.email == invitation.email))
    ).scalar_one_or_none()
    return InviteInfoOut(
        email=invitation.email,
        role=str(invitation.role),
        org_name=org.name,
        org_slug=org.slug,
        user_exists=user is not None,
    )


@router.post("/invite/accept", response_model=InviteInfoOut)
async def accept_invite(
    body: InviteAcceptRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> InviteInfoOut:
    invitation, org = await _valid_invitation(session, body.token)

    user = (
        await session.execute(select(User).where(User.email == invitation.email))
    ).scalar_one_or_none()
    user_existed = user is not None
    if user is None:
        if not body.password or len(body.password) < 8:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "新用户必须设置至少 8 位密码"
            )
        user = User(
            email=invitation.email,
            password_hash=hash_password(body.password),
            full_name=(body.full_name or invitation.email.split("@")[0]).strip(),
        )
        session.add(user)
        await session.flush()

    member = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == invitation.org_id, Membership.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        session.add(
            Membership(org_id=invitation.org_id, user_id=user.id, role=invitation.role)
        )

    invitation.accepted_at = datetime.now(UTC)
    session.add(invitation)
    await session.commit()
    return InviteInfoOut(
        email=invitation.email,
        role=str(invitation.role),
        org_name=org.name,
        org_slug=org.slug,
        user_exists=user_existed,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    token_hash = hash_refresh_token(body.refresh_token)
    stored = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        session.add(stored)
        await session.commit()
