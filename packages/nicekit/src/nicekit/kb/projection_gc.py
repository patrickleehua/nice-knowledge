"""Outbox-driven garbage collection for snapshot-scoped SQL/Wiki projections."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb import ports
from nicekit.models.kb import (
    GraphEdge,
    KbChunk,
    KbEntity,
    KbPage,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    KbSnapshotImageAsset,
    KnowledgeBase,
    KnowledgeSnapshot,
    OutboxEvent,
    SnapshotFactSupport,
    SnapshotProjectionSupport,
    SnapshotStatus,
)

_GC_EVENT_TYPES = frozenset({"knowledge_snapshot.activated", "knowledge_snapshot.rolled_back"})
_PROJECTION_MODELS = (
    # Delete dependants before their referenced edge, node, and fact-support rows.
    KbSnapshotEntityNodeSupport,
    GraphEdge,
    KbSnapshotEntityNode,
    KbSnapshotImageAsset,
    KbChunk,
    SnapshotProjectionSupport,
    KbPage,
    KbEntity,
    SnapshotFactSupport,
)
_RETENTION_POLICY = "reference-aware-active-plus-latest-retired-v3"
_ASSET_ID_KEYS = frozenset(
    {
        "asset_id",
        "image_asset_id",
        "selected_asset_id",
        "selected_asset_ids",
    }
)


def _payload_snapshot_id(event: OutboxEvent, key: str) -> UUID | None:
    value = event.payload.get(key)
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"snapshot event payload {key!r} must be a UUID or null") from exc


def _gc_completed(snapshot: KnowledgeSnapshot) -> bool:
    marker = snapshot.build_stats.get("projection_gc")
    return isinstance(marker, dict) and marker.get("status") == "completed"


def _manages_retrieval(snapshot: KnowledgeSnapshot) -> bool:
    required = snapshot.config_manifest.get("required_projection_builders")
    return isinstance(required, list) and any(
        isinstance(builder, dict) and builder.get("name") == "retrieval" for builder in required
    )


def _collect_asset_ids(value: object, *, parent_key: str | None = None) -> set[UUID]:
    found: set[UUID] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            found.update(_collect_asset_ids(child, parent_key=str(key)))
        return found
    if isinstance(value, list):
        for child in value:
            found.update(_collect_asset_ids(child, parent_key=parent_key))
        return found
    if parent_key not in _ASSET_ID_KEYS:
        return found
    try:
        found.add(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return found
    return found


def _collect_snapshot_ids(
    value: object,
    *,
    parent_key: str | None = None,
) -> set[UUID]:
    found: set[UUID] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            found.update(_collect_snapshot_ids(child, parent_key=str(key)))
        return found
    if isinstance(value, list):
        for child in value:
            found.update(_collect_snapshot_ids(child, parent_key=parent_key))
        return found
    if parent_key not in {"snapshot_id", "knowledge_snapshot_id"}:
        return found
    try:
        found.add(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return found
    return found


def _collect_unscoped_asset_ids(value: object) -> set[UUID]:
    """Collect legacy asset references only when no snapshot identity is present."""

    if _collect_snapshot_ids(value):
        return set()
    return _collect_asset_ids(value)


async def _externally_referenced_snapshot_ids(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
) -> set[UUID]:
    """本 KB 里仍被外部(宿主业务)引用、必须保留投影的快照 id。

    TF 在这里直接扫业务表的 JSON 列;SDK 改走 ReferenceScanner 协议
    (MIGRATION-PLAN §4):把本 KB 的全部快照 id 交给注册的扫描器计数,
    另外把仍被引用的图片资产回溯到其所属快照。无扫描器 = 无外部引用。
    """

    snapshot_ids = list(
        (
            await session.scalars(
                select(KnowledgeSnapshot.id).where(
                    KnowledgeSnapshot.org_id == org_id,
                    KnowledgeSnapshot.kb_id == kb_id,
                )
            )
        ).all()
    )
    if not snapshot_ids:
        return set()
    protected = await ports.referenced_ids(
        session, org_id=org_id, kind="snapshot", ids=snapshot_ids
    )

    asset_ids = list(
        (
            await session.scalars(
                select(KbSnapshotImageAsset.image_asset_id)
                .where(
                    KbSnapshotImageAsset.org_id == org_id,
                    KbSnapshotImageAsset.kb_id == kb_id,
                )
                .distinct()
            )
        ).all()
    )
    referenced_assets = await ports.referenced_ids(
        session, org_id=org_id, kind="media", ids=asset_ids
    )
    if referenced_assets:
        protected.update(
            (
                await session.scalars(
                    select(KbSnapshotImageAsset.snapshot_id).where(
                        KbSnapshotImageAsset.org_id == org_id,
                        KbSnapshotImageAsset.kb_id == kb_id,
                        KbSnapshotImageAsset.image_asset_id.in_(referenced_assets),
                    )
                )
            ).all()
        )
    return {snapshot_id for snapshot_id in protected if snapshot_id in set(snapshot_ids)}


async def gc_retired_snapshot_projections(session: AsyncSession, event: OutboxEvent) -> None:
    """Delete projections older than the one-generation pointer rollback window.

    The active snapshot, the latest retired snapshot, and the transition's `from`
    snapshot are retained. Keeping `from` makes a delayed activation event safe;
    a later transition event can collect it once a newer rollback generation exists.
    """
    if event.event_type not in _GC_EVENT_TYPES:
        raise ValueError(f"unsupported projection GC event type: {event.event_type}")
    event_from_id = _payload_snapshot_id(event, "from")
    event_to_id = _payload_snapshot_id(event, "to")
    if event_to_id != event.aggregate_id:
        raise ValueError("snapshot event payload 'to' must match aggregate_id")

    kb = (
        await session.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.id == event.kb_id,
                KnowledgeBase.org_id == event.org_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if kb is None:
        raise ValueError("snapshot event knowledge base does not exist")

    retired = list(
        (
            await session.execute(
                select(KnowledgeSnapshot)
                .where(
                    KnowledgeSnapshot.org_id == event.org_id,
                    KnowledgeSnapshot.kb_id == event.kb_id,
                    KnowledgeSnapshot.status == SnapshotStatus.RETIRED.value,
                )
                .order_by(
                    KnowledgeSnapshot.retired_at.desc().nullslast(),
                    KnowledgeSnapshot.activated_at.desc().nullslast(),
                    KnowledgeSnapshot.id.desc(),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    protected_ids = {
        snapshot_id for snapshot_id in (kb.active_snapshot_id, event_from_id) if snapshot_id
    }
    if retired:
        protected_ids.add(retired[0].id)
    protected_ids.update(
        await _externally_referenced_snapshot_ids(
            session,
            org_id=event.org_id,
            kb_id=event.kb_id,
        )
    )

    protected_snapshots = list(
        (
            await session.execute(
                select(KnowledgeSnapshot)
                .where(
                    KnowledgeSnapshot.org_id == event.org_id,
                    KnowledgeSnapshot.kb_id == event.kb_id,
                    KnowledgeSnapshot.id.in_(protected_ids),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    protected_by_id = {snapshot.id: snapshot for snapshot in protected_snapshots}
    if protected_ids != protected_by_id.keys():
        raise ValueError("snapshot event rollback window references an unknown snapshot")

    # Unmanaged snapshots read the legacy NULL generation. BUILDING can still become
    # READY, and READY can still be activated, so neither may lose that dependency.
    activatable_unmanaged = list(
        (
            await session.execute(
                select(KnowledgeSnapshot)
                .where(
                    KnowledgeSnapshot.org_id == event.org_id,
                    KnowledgeSnapshot.kb_id == event.kb_id,
                    KnowledgeSnapshot.status.in_(
                        [SnapshotStatus.BUILDING.value, SnapshotStatus.READY.value]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )

    active = protected_by_id.get(kb.active_snapshot_id)
    if active is None:
        raise ValueError("snapshot event knowledge base has no active snapshot")
    existing_legacy_gc = active.build_stats.get("legacy_retrieval_gc")
    legacy_gc_already_completed = (
        isinstance(existing_legacy_gc, dict)
        and existing_legacy_gc.get("status") == "completed"
        and existing_legacy_gc.get("event_id") == str(event.id)
    )
    unmanaged_ids = sorted(
        {
            snapshot.id
            for snapshot in [*protected_snapshots, *activatable_unmanaged]
            if not _manages_retrieval(snapshot)
        },
        key=str,
    )
    legacy_gc: dict[str, object] = {
        "status": "deferred" if unmanaged_ids else "completed",
        "policy": _RETENTION_POLICY,
        "event_id": str(event.id),
        "deleted": {"kb_chunks": 0},
    }
    if legacy_gc_already_completed:
        legacy_gc = existing_legacy_gc
    elif unmanaged_ids:
        legacy_gc["protected_unmanaged_snapshot_ids"] = [
            str(snapshot_id) for snapshot_id in unmanaged_ids
        ]
    else:
        await session.execute(
            select(func.set_config("app.build_snapshot_id", str(active.id), True))
        )
        await session.execute(select(func.set_config("app.legacy_projection_gc", "on", True)))
        result = await session.execute(
            delete(KbChunk).where(
                KbChunk.org_id == event.org_id,
                KbChunk.kb_id == event.kb_id,
                KbChunk.snapshot_id.is_(None),
            )
        )
        await session.execute(select(func.set_config("app.legacy_projection_gc", "off", True)))
        legacy_gc["completed_at"] = datetime.now(UTC).isoformat()
        legacy_gc["deleted"] = {"kb_chunks": result.rowcount}
    if not legacy_gc_already_completed:
        active.build_stats = {
            **active.build_stats,
            "legacy_retrieval_gc": legacy_gc,
        }
        session.add(active)

    for snapshot in retired:
        if snapshot.id in protected_ids or _gc_completed(snapshot):
            continue
        await session.execute(
            select(func.set_config("app.build_snapshot_id", str(snapshot.id), True))
        )
        deleted: dict[str, int] = {}
        for model in _PROJECTION_MODELS:
            result = await session.execute(
                delete(model).where(
                    model.org_id == event.org_id,
                    model.kb_id == event.kb_id,
                    model.snapshot_id == snapshot.id,
                )
            )
            deleted[model.__tablename__] = result.rowcount

        snapshot.build_stats = {
            **snapshot.build_stats,
            "projection_gc": {
                "status": "completed",
                "policy": _RETENTION_POLICY,
                "event_id": str(event.id),
                "completed_at": datetime.now(UTC).isoformat(),
                "deleted": deleted,
                "media_object_gc": {
                    "status": "retained",
                    "deleted_object_count": 0,
                    "reason": "source_revision_metadata_retained",
                },
            },
        }
        session.add(snapshot)
