"""``kb/ports.py::IncidentRecorder`` 的默认 SQL 实现(MIGRATION-PLAN §5.9 A1)。

KB 子包只认协议:登记 / 统计 / 清理三个动作。本模块把它们落到
``kb_operational_incidents`` 表(dedupe_key 去重 + occurrence_count 累加),
装配期由 ``runtime.bootstrap.install_default_ports()`` 注册。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.operations import KbOperationalIncident


def _dedupe_key(category: str, kb_id: UUID, image_asset_id: UUID | None, code: str) -> str:
    return ":".join(
        (
            category,
            str(kb_id),
            str(image_asset_id) if image_asset_id else "none",
            code,
        )
    )[:200]


async def record_kb_operational_incident(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    category: str,
    code: str,
    image_asset_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    observed_at = now or datetime.now(UTC)
    statement = insert(KbOperationalIncident).values(
        org_id=org_id,
        kb_id=kb_id,
        image_asset_id=image_asset_id,
        category=category,
        code=code[:100],
        dedupe_key=_dedupe_key(category, kb_id, image_asset_id, code[:100]),
        occurrence_count=1,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_kb_operational_incident_dedupe_key",
        set_={
            "occurrence_count": KbOperationalIncident.occurrence_count + 1,
            "last_seen_at": statement.excluded.last_seen_at,
            "resolved_at": None,
        },
    )
    await session.execute(statement)


async def count_open_kb_incidents(
    session: AsyncSession,
    *,
    org_id: UUID,
    category: str,
    now: datetime | None = None,
) -> tuple[int, float]:
    """未解决事件数 + 最老事件年龄秒(health_sweep 的水位口径)。"""
    current = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(
                func.count(),
                func.min(KbOperationalIncident.first_seen_at),
            ).where(
                KbOperationalIncident.org_id == org_id,
                KbOperationalIncident.category == category,
                KbOperationalIncident.resolved_at.is_(None),
            )
        )
    ).one()
    count = int(row[0] or 0)
    oldest = row[1]
    if not count or oldest is None:
        return count, 0.0
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return count, max(0.0, (current - oldest).total_seconds())


async def purge_kb_incidents(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    image_asset_ids: Sequence[UUID],
) -> int:
    if not image_asset_ids:
        return 0
    result = await session.execute(
        delete(KbOperationalIncident).where(
            KbOperationalIncident.org_id == org_id,
            KbOperationalIncident.kb_id == kb_id,
            KbOperationalIncident.image_asset_id.in_(list(image_asset_ids)),
        )
    )
    return int(result.rowcount or 0)


class SqlIncidentRecorder:
    """``IncidentRecorder`` 协议的 SDK 自带实现(SQL 落 kb_operational_incidents)。"""

    async def record(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        kb_id: UUID,
        category: str,
        code: str,
        image_asset_id: UUID | None = None,
    ) -> None:
        await record_kb_operational_incident(
            session,
            org_id=org_id,
            kb_id=kb_id,
            category=category,
            code=code,
            image_asset_id=image_asset_id,
        )

    async def count_open(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        category: str,
    ) -> tuple[int, float]:
        return await count_open_kb_incidents(session, org_id=org_id, category=category)

    async def purge(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        kb_id: UUID,
        image_asset_ids: Sequence[UUID],
    ) -> int:
        return await purge_kb_incidents(
            session, org_id=org_id, kb_id=kb_id, image_asset_ids=image_asset_ids
        )


__all__ = [
    "SqlIncidentRecorder",
    "count_open_kb_incidents",
    "purge_kb_incidents",
    "record_kb_operational_incident",
]
