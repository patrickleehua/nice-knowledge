"""通知 API:本人视角的站内信列表 / 未读数 / 已读。

RLS 只到 org 级,user_id 过滤在本层查询里做——所有端点都锚定 ctx.user_id,
读写他人通知一律 404(不暴露存在性)。
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.models.tenancy import Notification

router = APIRouter(prefix="/notifications")

Session = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]


def _out(row: Notification) -> dict:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "link": row.link,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
async def list_notifications(
    session: Session,
    ctx: Ctx,
    unread_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    stmt = select(Notification).where(Notification.user_id == ctx.user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(Notification.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_out(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/unread-count")
async def unread_count(session: Session, ctx: Ctx) -> dict:
    count = (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == ctx.user_id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()
    return {"count": count}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: UUID, session: Session, ctx: Ctx) -> dict:
    row = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == ctx.user_id,  # 他人通知 404,不暴露存在性
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "通知不存在")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
    return _out(row)


@router.post("/read-all")
async def mark_all_read(session: Session, ctx: Ctx) -> dict:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == ctx.user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
    return {"marked": result.rowcount}
