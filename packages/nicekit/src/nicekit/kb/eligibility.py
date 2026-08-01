"""Shared active knowledge-base lease for current reads and long-running work."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.kb import KnowledgeBase


@dataclass(frozen=True, slots=True)
class ActiveKnowledgeBaseLease:
    kb_id: UUID
    owner_org_id: UUID
    consumption_epoch: int
    active_snapshot_id: UUID | None


async def capture_active_knowledge_base_lease(
    session: AsyncSession,
    *,
    kb_id: UUID,
    owner_org_id: UUID | None = None,
    lock: bool = False,
) -> ActiveKnowledgeBaseLease | None:
    """Capture the current consumption boundary or return None fail-closed."""

    statement = select(
        KnowledgeBase.id,
        KnowledgeBase.org_id,
        KnowledgeBase.consumption_epoch,
        KnowledgeBase.active_snapshot_id,
    ).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.lifecycle_status == "active",
    )
    if owner_org_id is not None:
        statement = statement.where(KnowledgeBase.org_id == owner_org_id)
    if lock:
        statement = statement.with_for_update(
            read=True,
            key_share=True,
            of=KnowledgeBase,
        )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    return ActiveKnowledgeBaseLease(
        kb_id=row.id,
        owner_org_id=row.org_id,
        consumption_epoch=int(row.consumption_epoch),
        active_snapshot_id=row.active_snapshot_id,
    )


async def capture_active_knowledge_base_leases(
    session: AsyncSession,
    *,
    kb_ids: list[UUID] | set[UUID] | tuple[UUID, ...] | None = None,
    lock: bool = False,
) -> dict[UUID, ActiveKnowledgeBaseLease]:
    """Capture every currently visible active KB in a requested search scope."""

    statement = (
        select(
            KnowledgeBase.id,
            KnowledgeBase.org_id,
            KnowledgeBase.consumption_epoch,
            KnowledgeBase.active_snapshot_id,
        )
        .where(KnowledgeBase.lifecycle_status == "active")
        .order_by(KnowledgeBase.id)
    )
    if kb_ids is not None:
        if not kb_ids:
            return {}
        statement = statement.where(KnowledgeBase.id.in_(kb_ids))
    if lock:
        statement = statement.with_for_update(
            read=True,
            key_share=True,
            of=KnowledgeBase,
        )
    rows = (await session.execute(statement)).all()
    return {
        row.id: ActiveKnowledgeBaseLease(
            kb_id=row.id,
            owner_org_id=row.org_id,
            consumption_epoch=int(row.consumption_epoch),
            active_snapshot_id=row.active_snapshot_id,
        )
        for row in rows
    }


async def active_knowledge_base_lease_is_current(
    session: AsyncSession,
    lease: ActiveKnowledgeBaseLease,
    *,
    lock: bool = False,
) -> bool:
    """Revalidate lifecycle, epoch, and active snapshot as one boundary."""

    current = await capture_active_knowledge_base_lease(
        session,
        kb_id=lease.kb_id,
        owner_org_id=lease.owner_org_id,
        lock=lock,
    )
    return current == lease


__all__ = [
    "ActiveKnowledgeBaseLease",
    "active_knowledge_base_lease_is_current",
    "capture_active_knowledge_base_lease",
    "capture_active_knowledge_base_leases",
]
