from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.domain.kb_media import ImageReviewStatus
from nicekit.models.kb import (
    DocStatus,
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentRevision,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbDocumentOperation,
    KbImageAsset,
    KnowledgeBase,
    KnowledgeSnapshot,
    SnapshotStatus,
    SourceDocument,
)

STATUS_BOARD_QUERY_COUNT = 8

_CURRENT_DOCUMENT_LIFECYCLES = (
    DocumentLifecycleStatus.ACTIVE.value,
    DocumentLifecycleStatus.REINGESTION_PENDING.value,
)
_ACTIVE_OPERATION_STATUSES = (
    DocumentOperationStatus.PENDING.value,
    DocumentOperationStatus.PROCESSING.value,
)
_REPORTED_OPERATION_STATUSES = (
    *_ACTIVE_OPERATION_STATUSES,
    DocumentOperationStatus.FAILED.value,
    DocumentOperationStatus.DEAD_LETTER.value,
)
_STATUS_BOARD_SNAPSHOT_STATUSES = (
    SnapshotStatus.BUILDING.value,
    SnapshotStatus.READY.value,
    SnapshotStatus.ACTIVE.value,
    SnapshotStatus.RETIRED.value,
    SnapshotStatus.FAILED.value,
)
_DOCUMENT_BUCKETS = {
    DocStatus.STAGED.value: "staged",
    DocStatus.UPLOADED.value: "queued",
    DocStatus.PARSING.value: "running",
    DocStatus.AWAITING_REVIEW.value: "awaiting_review",
    DocStatus.COMPLETED.value: "completed",
    DocStatus.FAILED.value: "failed",
    DocStatus.PAUSED.value: "paused",
    DocStatus.CANCELED.value: "canceled",
}
_STAGE_NAMES = {
    "parse": "layout_parse",
    "image": "image_understanding",
    "chunk": "chunk_embedding",
    "extract": "information_extraction",
    "entity_extract": "entity_extraction",
}


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _count(value: Any) -> int:
    return int(value or 0)


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _append_alert(
    alerts: list[dict[str, Any]],
    *,
    code: str,
    severity: str,
    count: int,
) -> None:
    if count > 0:
        alerts.append({"code": code, "severity": severity, "count": count})


def _release_state(
    *,
    active_snapshot_id: UUID | None,
    snapshots: dict[str, Any],
) -> tuple[str, UUID | None]:
    for status in (
        SnapshotStatus.READY.value,
        SnapshotStatus.BUILDING.value,
        SnapshotStatus.FAILED.value,
    ):
        row = snapshots.get(status)
        if row is not None:
            return status, row.id
    if active_snapshot_id is not None:
        return "active", None
    return "unpublished", None


def _snapshot_is_superseded(
    snapshots: dict[str, Any],
    *,
    status: str,
    superseding_statuses: tuple[str, ...],
    include_ties: bool,
) -> bool:
    row = snapshots.get(status)
    if row is None:
        return False
    for superseding_status in superseding_statuses:
        other = snapshots.get(superseding_status)
        if other is None:
            continue
        if other.latest_activity_at > row.latest_activity_at:
            return True
        if include_ties and other.latest_activity_at == row.latest_activity_at:
            return True
    return False


def _primary_state(
    *,
    blocked_count: int,
    counts: dict[str, int],
    review_total: int,
    release_state: str,
    has_building_snapshot: bool,
    has_running_operation: bool,
    has_queued_operation: bool,
    failure_count: int,
    active_snapshot_id: UUID | None,
) -> str:
    if blocked_count:
        return "blocked"
    if counts["running"] or has_building_snapshot or has_running_operation:
        return "running"
    if counts["queued"] or has_queued_operation:
        return "queued"
    if release_state == SnapshotStatus.READY.value:
        return "release_ready"
    if review_total:
        return "review_required"
    if counts["staged"]:
        return "classification_required"
    if failure_count:
        return "needs_attention"
    if active_snapshot_id is not None:
        return "ready"
    if counts["total"]:
        return "unpublished"
    return "empty"


async def collect_status_board(
    session: AsyncSession,
    *,
    lifecycle_status: str = "active",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect one RLS-scoped KB runtime board using a fixed number of grouped reads."""

    generated_at = now or datetime.now(UTC)
    settings = get_settings()
    knowledge_bases = (
        (
            await session.execute(
                select(
                    KnowledgeBase.id,
                    KnowledgeBase.active_snapshot_id,
                    KnowledgeBase.created_at,
                )
                .where(KnowledgeBase.lifecycle_status == lifecycle_status)
                .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc())
            )
        )
        .all()
    )
    kb_ids = [row.id for row in knowledge_bases]
    if not kb_ids:
        return {
            "generated_at": generated_at,
            "poll_after_ms": 30_000,
            "has_active_work": False,
            "items": [],
        }

    processing_cutoff = generated_at - timedelta(
        seconds=settings.kb_health_processing_old_age_seconds
    )
    document_rows = (
        await session.execute(
            select(
                SourceDocument.kb_id,
                SourceDocument.status,
                SourceDocument.progress_stage,
                func.count(SourceDocument.id).label("document_count"),
                func.sum(SourceDocument.progress_done).label("progress_done"),
                func.sum(SourceDocument.progress_total).label("progress_total"),
                func.sum(
                    case(
                        (
                            and_(
                                SourceDocument.status == DocStatus.PARSING.value,
                                SourceDocument.parsing_started_at.is_not(None),
                                SourceDocument.parsing_started_at <= processing_cutoff,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("stalled_processing_count"),
                func.max(
                    func.coalesce(
                        SourceDocument.parsing_started_at,
                        SourceDocument.created_at,
                    )
                ).label("latest_activity_at"),
            )
            .where(
                SourceDocument.kb_id.in_(kb_ids),
                SourceDocument.lifecycle_status.in_(_CURRENT_DOCUMENT_LIFECYCLES),
            )
            .group_by(
                SourceDocument.kb_id,
                SourceDocument.status,
                SourceDocument.progress_stage,
            )
        )
    ).all()

    ranked_root_runs = (
        select(
            IngestRun.kb_id.label("kb_id"),
            DocumentRevision.doc_id.label("document_id"),
            IngestRun.status.label("status"),
            IngestRun.lease_expires_at.label("lease_expires_at"),
            IngestRun.heartbeat_at.label("heartbeat_at"),
            IngestRun.started_at.label("started_at"),
            IngestRun.finished_at.label("finished_at"),
            IngestRun.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=DocumentRevision.doc_id,
                order_by=(
                    DocumentRevision.revision_no.desc(),
                    IngestRun.created_at.desc(),
                    IngestRun.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .join(SourceDocument, SourceDocument.id == DocumentRevision.doc_id)
        .where(
            IngestRun.kb_id.in_(kb_ids),
            IngestRun.stage == "document",
            IngestRun.segment_no == 0,
            SourceDocument.lifecycle_status.in_(_CURRENT_DOCUMENT_LIFECYCLES),
        )
        .subquery("ranked_status_board_root_runs")
    )
    root_activity = func.coalesce(
        ranked_root_runs.c.heartbeat_at,
        ranked_root_runs.c.finished_at,
        ranked_root_runs.c.started_at,
        ranked_root_runs.c.created_at,
    )
    heartbeat_cutoff = generated_at - timedelta(
        seconds=settings.kb_health_ingest_lease_old_age_seconds
    )
    queue_cutoff = generated_at - timedelta(
        seconds=settings.kb_health_uploaded_old_age_seconds
    )
    running_run = ranked_root_runs.c.status == IngestRunStatus.RUNNING.value
    root_run_rows = (
        await session.execute(
            select(
                ranked_root_runs.c.kb_id,
                func.sum(
                    case(
                        (
                            and_(
                                running_run,
                                or_(
                                    ranked_root_runs.c.lease_expires_at.is_(None),
                                    ranked_root_runs.c.lease_expires_at <= generated_at,
                                ),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("expired_lease_count"),
                func.sum(
                    case(
                        (
                            and_(
                                running_run,
                                ranked_root_runs.c.lease_expires_at > generated_at,
                                root_activity <= heartbeat_cutoff,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("stale_heartbeat_count"),
                func.sum(
                    case(
                        (
                            and_(
                                ranked_root_runs.c.status
                                == IngestRunStatus.QUEUED.value,
                                ranked_root_runs.c.created_at <= queue_cutoff,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("stale_queue_count"),
                func.max(root_activity).label("latest_activity_at"),
            )
            .where(ranked_root_runs.c.row_number == 1)
            .group_by(ranked_root_runs.c.kb_id)
        )
    ).all()

    active_stage_rows = (
        await session.execute(
            select(
                IngestRun.kb_id,
                IngestRun.stage,
                func.count(func.distinct(DocumentRevision.doc_id)).label(
                    "document_count"
                ),
                func.max(
                    func.coalesce(
                        IngestRun.heartbeat_at,
                        IngestRun.started_at,
                        IngestRun.created_at,
                    )
                ).label("latest_activity_at"),
            )
            .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
            .join(SourceDocument, SourceDocument.id == DocumentRevision.doc_id)
            .where(
                IngestRun.kb_id.in_(kb_ids),
                IngestRun.status == IngestRunStatus.RUNNING.value,
                IngestRun.stage == "entity_extract",
                SourceDocument.lifecycle_status.in_(_CURRENT_DOCUMENT_LIFECYCLES),
            )
            .group_by(IngestRun.kb_id, IngestRun.stage)
        )
    ).all()

    fact_rows = (
        await session.execute(
            select(
                FactClaim.kb_id,
                FactClaim.review_status,
                func.count(FactClaim.id).label("claim_count"),
                func.max(
                    func.coalesce(FactClaim.updated_at, FactClaim.created_at)
                ).label("latest_activity_at"),
            )
            .where(
                FactClaim.kb_id.in_(kb_ids),
                FactClaim.review_status.in_(
                    (
                        FactReviewStatus.SUGGESTED.value,
                        FactReviewStatus.ORPHANED.value,
                    )
                ),
            )
            .group_by(FactClaim.kb_id, FactClaim.review_status)
        )
    ).all()

    image_rows = (
        await session.execute(
            select(
                KbImageAsset.kb_id,
                func.count(KbImageAsset.id).label("image_count"),
                func.max(
                    func.coalesce(KbImageAsset.updated_at, KbImageAsset.created_at)
                ).label("latest_activity_at"),
            )
            .where(
                KbImageAsset.kb_id.in_(kb_ids),
                KbImageAsset.review_status == ImageReviewStatus.NEEDS_REVIEW.value,
            )
            .group_by(KbImageAsset.kb_id)
        )
    ).all()

    snapshot_activity = case(
        (
            KnowledgeSnapshot.status == SnapshotStatus.READY.value,
            func.coalesce(
                KnowledgeSnapshot.ready_at,
                KnowledgeSnapshot.updated_at,
                KnowledgeSnapshot.created_at,
            ),
        ),
        (
            KnowledgeSnapshot.status == SnapshotStatus.ACTIVE.value,
            func.coalesce(
                KnowledgeSnapshot.activated_at,
                KnowledgeSnapshot.updated_at,
                KnowledgeSnapshot.created_at,
            ),
        ),
        (
            KnowledgeSnapshot.status == SnapshotStatus.RETIRED.value,
            func.coalesce(
                KnowledgeSnapshot.retired_at,
                KnowledgeSnapshot.updated_at,
                KnowledgeSnapshot.created_at,
            ),
        ),
        (
            KnowledgeSnapshot.status == SnapshotStatus.FAILED.value,
            func.coalesce(
                KnowledgeSnapshot.failed_at,
                KnowledgeSnapshot.updated_at,
                KnowledgeSnapshot.created_at,
            ),
        ),
        else_=func.coalesce(
            KnowledgeSnapshot.updated_at,
            KnowledgeSnapshot.created_at,
        ),
    )
    ranked_snapshots = (
        select(
            KnowledgeSnapshot.kb_id.label("kb_id"),
            KnowledgeSnapshot.id.label("id"),
            KnowledgeSnapshot.status.label("status"),
            snapshot_activity.label("latest_activity_at"),
            func.row_number()
            .over(
                partition_by=(KnowledgeSnapshot.kb_id, KnowledgeSnapshot.status),
                order_by=(snapshot_activity.desc(), KnowledgeSnapshot.id.desc()),
            )
            .label("row_number"),
            func.count()
            .over(
                partition_by=(KnowledgeSnapshot.kb_id, KnowledgeSnapshot.status)
            )
            .label("status_count"),
        )
        .where(
            KnowledgeSnapshot.kb_id.in_(kb_ids),
            KnowledgeSnapshot.status.in_(_STATUS_BOARD_SNAPSHOT_STATUSES),
        )
        .subquery("ranked_status_board_snapshots")
    )
    snapshot_rows = (
        await session.execute(
            select(ranked_snapshots).where(ranked_snapshots.c.row_number == 1)
        )
    ).all()

    operation_rows = (
        await session.execute(
            select(
                KbDocumentOperation.kb_id,
                KbDocumentOperation.operation_type,
                KbDocumentOperation.stage,
                KbDocumentOperation.status,
                func.count(KbDocumentOperation.id).label("operation_count"),
                func.max(
                    func.coalesce(
                        KbDocumentOperation.updated_at,
                        KbDocumentOperation.created_at,
                    )
                ).label("latest_activity_at"),
            )
            .where(
                KbDocumentOperation.kb_id.in_(kb_ids),
                KbDocumentOperation.status.in_(_REPORTED_OPERATION_STATUSES),
            )
            .group_by(
                KbDocumentOperation.kb_id,
                KbDocumentOperation.operation_type,
                KbDocumentOperation.stage,
                KbDocumentOperation.status,
            )
        )
    ).all()

    documents_by_kb: dict[UUID, list[Any]] = defaultdict(list)
    root_runs_by_kb = {row.kb_id: row for row in root_run_rows}
    entity_stages_by_kb: dict[UUID, list[Any]] = defaultdict(list)
    facts_by_kb: dict[UUID, dict[str, Any]] = defaultdict(dict)
    images_by_kb = {row.kb_id: row for row in image_rows}
    snapshots_by_kb: dict[UUID, dict[str, Any]] = defaultdict(dict)
    operations_by_kb: dict[UUID, list[Any]] = defaultdict(list)
    for row in document_rows:
        documents_by_kb[row.kb_id].append(row)
    for row in active_stage_rows:
        entity_stages_by_kb[row.kb_id].append(row)
    for row in fact_rows:
        facts_by_kb[row.kb_id][_value(row.review_status)] = row
    for row in snapshot_rows:
        snapshots_by_kb[row.kb_id][_value(row.status)] = row
    for row in operation_rows:
        operations_by_kb[row.kb_id].append(row)

    items: list[dict[str, Any]] = []
    board_has_active_work = False
    board_has_manual_work = False
    snapshot_stall_cutoff = generated_at - timedelta(
        seconds=settings.kb_health_snapshot_build_old_age_seconds
    )

    for knowledge_base in knowledge_bases:
        kb_id = knowledge_base.id
        counts = {
            "total": 0,
            "ingested": 0,
            "remaining": 0,
            "staged": 0,
            "queued": 0,
            "running": 0,
            "awaiting_review": 0,
            "completed": 0,
            "failed": 0,
            "paused": 0,
            "canceled": 0,
        }
        stages: dict[str, dict[str, Any]] = {}
        latest_activity_at = knowledge_base.created_at
        stalled_processing_count = 0
        for row in documents_by_kb.get(kb_id, ()):
            status = _value(row.status)
            amount = _count(row.document_count)
            counts["total"] += amount
            bucket = _DOCUMENT_BUCKETS.get(status)
            if bucket is not None:
                counts[bucket] += amount
            stalled_processing_count += _count(row.stalled_processing_count)
            latest_activity_at = _latest(latest_activity_at, row.latest_activity_at)
            if status != DocStatus.PARSING.value:
                continue
            stage = _STAGE_NAMES.get(row.progress_stage or "", row.progress_stage or "processing")
            summary = stages.setdefault(
                stage,
                {"stage": stage, "document_count": 0, "done": 0, "total": 0},
            )
            summary["document_count"] += amount
            summary["done"] += _count(row.progress_done)
            summary["total"] += _count(row.progress_total)
        for row in entity_stages_by_kb.get(kb_id, ()):
            stage = _STAGE_NAMES.get(_value(row.stage), _value(row.stage))
            if stage not in stages:
                stages[stage] = {
                    "stage": stage,
                    "document_count": _count(row.document_count),
                    "done": 0,
                    "total": 0,
                }
            latest_activity_at = _latest(latest_activity_at, row.latest_activity_at)

        counts["ingested"] = counts["awaiting_review"] + counts["completed"]
        counts["remaining"] = max(0, counts["total"] - counts["ingested"])

        root_row = root_runs_by_kb.get(kb_id)
        expired_lease_count = (
            _count(root_row.expired_lease_count) if root_row is not None else 0
        )
        stale_heartbeat_count = (
            _count(root_row.stale_heartbeat_count) if root_row is not None else 0
        )
        stale_queue_count = (
            _count(root_row.stale_queue_count) if root_row is not None else 0
        )
        if root_row is not None:
            latest_activity_at = _latest(
                latest_activity_at, root_row.latest_activity_at
            )

        fact_rows_for_kb = facts_by_kb.get(kb_id, {})
        suggested_row = fact_rows_for_kb.get(FactReviewStatus.SUGGESTED.value)
        orphaned_row = fact_rows_for_kb.get(FactReviewStatus.ORPHANED.value)
        image_row = images_by_kb.get(kb_id)
        suggested_facts = (
            _count(suggested_row.claim_count) if suggested_row is not None else 0
        )
        orphaned_facts = (
            _count(orphaned_row.claim_count) if orphaned_row is not None else 0
        )
        images_needing_review = (
            _count(image_row.image_count) if image_row is not None else 0
        )
        review_total = suggested_facts + orphaned_facts + images_needing_review
        latest_activity_at = _latest(
            latest_activity_at,
            suggested_row.latest_activity_at if suggested_row is not None else None,
            orphaned_row.latest_activity_at if orphaned_row is not None else None,
            image_row.latest_activity_at if image_row is not None else None,
        )

        kb_snapshots = snapshots_by_kb.get(kb_id, {})
        building_snapshot = kb_snapshots.get(SnapshotStatus.BUILDING.value)
        current_building_snapshot = (
            None
            if _snapshot_is_superseded(
                kb_snapshots,
                status=SnapshotStatus.BUILDING.value,
                superseding_statuses=(
                    SnapshotStatus.READY.value,
                    SnapshotStatus.ACTIVE.value,
                    SnapshotStatus.RETIRED.value,
                    SnapshotStatus.FAILED.value,
                ),
                include_ties=False,
            )
            else building_snapshot
        )
        failed_snapshot = kb_snapshots.get(SnapshotStatus.FAILED.value)
        unresolved_failed_snapshot = (
            None
            if _snapshot_is_superseded(
                kb_snapshots,
                status=SnapshotStatus.FAILED.value,
                superseding_statuses=(
                    SnapshotStatus.BUILDING.value,
                    SnapshotStatus.READY.value,
                    SnapshotStatus.ACTIVE.value,
                    SnapshotStatus.RETIRED.value,
                ),
                include_ties=True,
            )
            else failed_snapshot
        )
        current_release_snapshots = dict(kb_snapshots)
        if current_building_snapshot is None:
            current_release_snapshots.pop(SnapshotStatus.BUILDING.value, None)
        if unresolved_failed_snapshot is None:
            current_release_snapshots.pop(SnapshotStatus.FAILED.value, None)
        release_state, candidate_snapshot_id = _release_state(
            active_snapshot_id=knowledge_base.active_snapshot_id,
            snapshots=current_release_snapshots,
        )
        for row in kb_snapshots.values():
            latest_activity_at = _latest(
                latest_activity_at, row.latest_activity_at
            )

        kb_operations = operations_by_kb.get(kb_id, ())
        active_operations = [
            row
            for row in kb_operations
            if _value(row.status) in _ACTIVE_OPERATION_STATUSES
        ]
        active_operations.sort(
            key=lambda row: (
                0
                if _value(row.status) == DocumentOperationStatus.PROCESSING.value
                else 1,
                _value(row.operation_type),
                row.stage or "",
            )
        )
        operation = None
        if active_operations:
            selected_operation = active_operations[0]
            operation = {
                "kind": _value(selected_operation.operation_type),
                "stage": selected_operation.stage,
                "status": _value(selected_operation.status),
                "count": _count(selected_operation.operation_count),
            }
        operation_failure_count = sum(
            _count(row.operation_count)
            for row in kb_operations
            if _value(row.status)
            in (
                DocumentOperationStatus.FAILED.value,
                DocumentOperationStatus.DEAD_LETTER.value,
            )
        )
        for row in kb_operations:
            latest_activity_at = _latest(
                latest_activity_at, row.latest_activity_at
            )

        stalled_snapshot_count = (
            _count(current_building_snapshot.status_count)
            if current_building_snapshot is not None
            and current_building_snapshot.latest_activity_at
            <= snapshot_stall_cutoff
            else 0
        )
        failed_snapshot_count = int(unresolved_failed_snapshot is not None)
        blocked_count = (
            expired_lease_count
            + stale_heartbeat_count
            + stale_queue_count
            + stalled_processing_count
            + stalled_snapshot_count
        )
        alerts: list[dict[str, Any]] = []
        _append_alert(
            alerts,
            code="ingest_lease_expired",
            severity="error",
            count=expired_lease_count,
        )
        _append_alert(
            alerts,
            code="ingest_heartbeat_stale",
            severity="error",
            count=stale_heartbeat_count,
        )
        _append_alert(
            alerts,
            code="ingest_queue_stalled",
            severity="error",
            count=stale_queue_count,
        )
        _append_alert(
            alerts,
            code="ingest_processing_stalled",
            severity="error",
            count=stalled_processing_count,
        )
        _append_alert(
            alerts,
            code="snapshot_build_stalled",
            severity="error",
            count=stalled_snapshot_count,
        )
        _append_alert(
            alerts,
            code="document_failed",
            severity="warning",
            count=counts["failed"],
        )
        _append_alert(
            alerts,
            code="snapshot_failed",
            severity="warning",
            count=failed_snapshot_count,
        )
        _append_alert(
            alerts,
            code="document_operation_failed",
            severity="warning",
            count=operation_failure_count,
        )

        has_running_operation = any(
            _value(row.status) == DocumentOperationStatus.PROCESSING.value
            for row in active_operations
        )
        has_queued_operation = any(
            _value(row.status) == DocumentOperationStatus.PENDING.value
            for row in active_operations
        )
        failure_count = (
            counts["failed"] + failed_snapshot_count + operation_failure_count
        )
        primary_state = _primary_state(
            blocked_count=blocked_count,
            counts=counts,
            review_total=review_total,
            release_state=release_state,
            has_building_snapshot=current_building_snapshot is not None,
            has_running_operation=has_running_operation,
            has_queued_operation=has_queued_operation,
            failure_count=failure_count,
            active_snapshot_id=knowledge_base.active_snapshot_id,
        )
        item_has_active_work = bool(
            counts["queued"]
            or counts["running"]
            or current_building_snapshot is not None
            or active_operations
        )
        item_has_manual_work = bool(
            review_total
            or release_state == SnapshotStatus.READY.value
            or counts["staged"]
            or counts["failed"]
            or counts["paused"]
            or counts["canceled"]
            or failed_snapshot_count
            or operation_failure_count
        )
        board_has_active_work = board_has_active_work or item_has_active_work
        board_has_manual_work = board_has_manual_work or item_has_manual_work
        items.append(
            {
                "kb_id": kb_id,
                "primary_state": primary_state,
                "alerts": alerts,
                "document_counts": counts,
                "stages": sorted(
                    stages.values(), key=lambda summary: summary["stage"]
                ),
                "review": {
                    "suggested_facts": suggested_facts,
                    "orphaned_facts": orphaned_facts,
                    "images_needing_review": images_needing_review,
                    "total": review_total,
                },
                "release": {
                    "state": release_state,
                    "active_snapshot_id": knowledge_base.active_snapshot_id,
                    "candidate_snapshot_id": candidate_snapshot_id,
                },
                "operation": operation,
                "latest_activity_at": latest_activity_at,
            }
        )

    poll_after_ms = (
        2_000 if board_has_active_work else 15_000 if board_has_manual_work else 30_000
    )
    return {
        "generated_at": generated_at,
        "poll_after_ms": poll_after_ms,
        "has_active_work": board_has_active_work,
        "items": items,
    }
