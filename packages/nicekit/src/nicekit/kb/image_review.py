"""Governed owner-only review mutations for KB image assets."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.domain.kb_media import (
    ImageEnrichmentStatus,
    ImageReviewAction,
    ImageReviewStatus,
    KbImageBulkReviewRequest,
    KbImageBulkReviewResult,
    KbImageRetryRequest,
    KbImageReviewRequest,
)
from nicekit.kb.eligibility import capture_active_knowledge_base_lease
from nicekit.kb.image_assets import KbImageAssetUnavailableError
from nicekit.kb.image_ingestion import (
    image_enrichment_run_identity,
    next_image_event_version,
)
from nicekit.kb.ingestion import enqueue_document_ingestion
from nicekit.kb.review_settlement import settle_document_review
from nicekit.models.kb import (
    DocStatus,
    DocumentRevision,
    IngestRun,
    IngestRunStatus,
    KbImageAsset,
    KbImageAssetEvent,
    KbSnapshotImageAsset,
    KnowledgeBase,
    KnowledgeBaseLifecycleStatus,
    KnowledgeSnapshot,
    RevisionStatus,
    SnapshotStatus,
    SourceDocument,
)


class KbImageReviewConflictError(RuntimeError):
    """Optimistic concurrency or invalid review transition."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class KbImageRetrySchedule:
    asset_id: UUID
    run_id: UUID
    lock_version: int


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


async def _load_owned_asset(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    asset_id: UUID,
) -> tuple[KbImageAsset, SourceDocument, DocumentRevision]:
    kb_id = await session.scalar(
        select(KbImageAsset.kb_id).where(
            KbImageAsset.id == asset_id,
            KbImageAsset.org_id == request_org_id,
        )
    )
    if kb_id is None or (
        await capture_active_knowledge_base_lease(
            session,
            kb_id=kb_id,
            owner_org_id=request_org_id,
            lock=True,
        )
        is None
    ):
        raise KbImageAssetUnavailableError()
    row = (
        await session.execute(
            select(KbImageAsset, SourceDocument, DocumentRevision)
            .join(
                KnowledgeBase,
                and_(
                    KnowledgeBase.id == KbImageAsset.kb_id,
                    KnowledgeBase.org_id == KbImageAsset.org_id,
                ),
            )
            .join(
                SourceDocument,
                and_(
                    SourceDocument.id == KbImageAsset.doc_id,
                    SourceDocument.org_id == KbImageAsset.org_id,
                    SourceDocument.kb_id == KbImageAsset.kb_id,
                ),
            )
            .join(
                DocumentRevision,
                and_(
                    DocumentRevision.id == KbImageAsset.revision_id,
                    DocumentRevision.doc_id == KbImageAsset.doc_id,
                    DocumentRevision.org_id == KbImageAsset.org_id,
                    DocumentRevision.kb_id == KbImageAsset.kb_id,
                ),
            )
            .where(
                KbImageAsset.id == asset_id,
                KbImageAsset.org_id == request_org_id,
                KnowledgeBase.org_id == request_org_id,
                KnowledgeBase.lifecycle_status
                == KnowledgeBaseLifecycleStatus.ACTIVE.value,
                DocumentRevision.status != RevisionStatus.TOMBSTONED.value,
            )
            .with_for_update(
                of=(KbImageAsset, SourceDocument, DocumentRevision)
            )
        )
    ).one_or_none()
    if row is None:
        raise KbImageAssetUnavailableError()
    return row


def _review_state(asset: KbImageAsset) -> dict:
    return {
        "caption": asset.caption,
        "ocr_text": asset.ocr_text,
        "enrichment_status": _value(asset.enrichment_status),
        "review_status": _value(asset.review_status),
        "failure_code": asset.failure_code,
        "review_source": asset.review_source,
        "reviewed_by": str(asset.reviewed_by) if asset.reviewed_by else None,
        "lock_version": asset.lock_version,
    }


async def _append_human_event(
    session: AsyncSession,
    *,
    asset: KbImageAsset,
    actor_user_id: UUID,
    action: str,
    reason: str,
    expected_lock_version: int,
    before_state: dict,
) -> None:
    event_version = await next_image_event_version(session, asset_id=asset.id)
    idempotency_key = (
        f"kb-image-review:{asset.id}:{expected_lock_version}:{action}"
    )
    session.add(
        KbImageAssetEvent(
            id=uuid5(asset.id, idempotency_key),
            org_id=asset.org_id,
            kb_id=asset.kb_id,
            image_asset_id=asset.id,
            event_version=event_version,
            action=action,
            actor_user_id=actor_user_id,
            actor_kind="human",
            before_state=before_state,
            after_state=_review_state(asset),
            reason=reason,
            idempotency_key=idempotency_key,
        )
    )


async def _invalidate_draft_snapshots(
    session: AsyncSession,
    *,
    asset: KbImageAsset,
    action: str,
    now: datetime,
) -> int:
    snapshots = list(
        (
            await session.scalars(
                select(KnowledgeSnapshot)
                .join(
                    KbSnapshotImageAsset,
                    and_(
                        KbSnapshotImageAsset.snapshot_id == KnowledgeSnapshot.id,
                        KbSnapshotImageAsset.org_id == KnowledgeSnapshot.org_id,
                        KbSnapshotImageAsset.kb_id == KnowledgeSnapshot.kb_id,
                    ),
                )
                .where(
                    KbSnapshotImageAsset.image_asset_id == asset.id,
                    KnowledgeSnapshot.status.in_(
                        {
                            SnapshotStatus.BUILDING.value,
                            SnapshotStatus.READY.value,
                        }
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    for snapshot in snapshots:
        snapshot.status = SnapshotStatus.FAILED
        snapshot.failed_at = now
        snapshot.error = f"image_asset_changed:{asset.id}:{action}"
        snapshot.build_stats = {
            **snapshot.build_stats,
            "image_asset_validation": {
                "status": "invalidated",
                "asset_id": str(asset.id),
                "action": action,
                "invalidated_at": now.isoformat(),
            },
        }
        session.add(snapshot)
    return len(snapshots)


def _require_lock_version(asset: KbImageAsset, expected: int) -> None:
    if asset.lock_version != expected:
        raise KbImageReviewConflictError("lock_version_conflict")


def _require_acceptable(asset: KbImageAsset) -> None:
    if _value(asset.enrichment_status) != ImageEnrichmentStatus.SUCCEEDED.value:
        raise KbImageReviewConflictError("enrichment_not_succeeded")
    if not ((asset.caption or "").strip() or (asset.ocr_text or "").strip()):
        raise KbImageReviewConflictError("accepted_content_empty")


async def review_image_asset(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    actor_user_id: UUID,
    asset_id: UUID,
    request: KbImageReviewRequest,
) -> None:
    asset, _document, _revision = await _load_owned_asset(
        session,
        request_org_id=request_org_id,
        asset_id=asset_id,
    )
    _require_lock_version(asset, request.expected_lock_version)
    before_state = _review_state(asset)
    now = datetime.now(UTC)

    if request.action is ImageReviewAction.EDIT_AND_ACCEPT:
        if request.caption is not None:
            asset.caption = request.caption.strip()
        if request.ocr_text is not None:
            asset.ocr_text = request.ocr_text.strip()
        _require_acceptable(asset)
        asset.review_status = ImageReviewStatus.ACCEPTED
    elif request.action is ImageReviewAction.ACCEPT:
        if _value(asset.review_status) == ImageReviewStatus.ACCEPTED.value:
            raise KbImageReviewConflictError("already_accepted")
        _require_acceptable(asset)
        asset.review_status = ImageReviewStatus.ACCEPTED
    elif request.action is ImageReviewAction.REJECT:
        if _value(asset.review_status) == ImageReviewStatus.REJECTED.value:
            raise KbImageReviewConflictError("already_rejected")
        asset.review_status = ImageReviewStatus.REJECTED
    else:
        if _value(asset.review_status) == ImageReviewStatus.EXCLUDED.value:
            raise KbImageReviewConflictError("already_excluded")
        asset.review_status = ImageReviewStatus.EXCLUDED

    asset.reviewed_by = actor_user_id
    asset.reviewed_at = now
    asset.review_source = "human"
    asset.review_note = request.reason
    asset.lock_version += 1
    session.add(asset)
    await _invalidate_draft_snapshots(
        session,
        asset=asset,
        action=request.action.value,
        now=now,
    )
    await _append_human_event(
        session,
        asset=asset,
        actor_user_id=actor_user_id,
        action=request.action.value,
        reason=request.reason,
        expected_lock_version=request.expected_lock_version,
        before_state=before_state,
    )
    await session.flush()
    # 最后一张待审图片处理完(且事实也审完)时把文档收回 completed
    await settle_document_review(
        session, org_id=request_org_id, doc_id=asset.doc_id
    )


async def retry_image_asset(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    actor_user_id: UUID,
    asset_id: UUID,
    request: KbImageRetryRequest,
) -> KbImageRetrySchedule:
    asset, document, revision = await _load_owned_asset(
        session,
        request_org_id=request_org_id,
        asset_id=asset_id,
    )
    _require_lock_version(asset, request.expected_lock_version)
    retryable = (
        _value(asset.enrichment_status) == ImageEnrichmentStatus.FAILED.value
        or _value(asset.review_status) == ImageReviewStatus.REJECTED.value
    )
    if not retryable:
        raise KbImageReviewConflictError("asset_not_retryable")
    if asset.original_object_key is None:
        raise KbImageReviewConflictError("stored_original_unavailable")

    active_run = await session.scalar(
        select(IngestRun.id)
        .where(
            IngestRun.revision_id == revision.id,
            IngestRun.status.in_(
                {
                    IngestRunStatus.QUEUED.value,
                    IngestRunStatus.RUNNING.value,
                }
            ),
        )
        .limit(1)
    )
    if active_run is not None:
        raise KbImageReviewConflictError("ingest_already_active")

    before_state = _review_state(asset)
    now = datetime.now(UTC)
    asset.enrichment_status = ImageEnrichmentStatus.PENDING
    asset.review_status = ImageReviewStatus.NEEDS_REVIEW
    asset.failure_code = None
    asset.failure_detail = None
    asset.reviewed_by = actor_user_id
    asset.reviewed_at = now
    asset.review_source = "human"
    asset.review_note = request.reason
    asset.lock_version += 1
    session.add(asset)
    await _invalidate_draft_snapshots(
        session,
        asset=asset,
        action="retry_requested",
        now=now,
    )
    await _append_human_event(
        session,
        asset=asset,
        actor_user_id=actor_user_id,
        action="retry_requested",
        reason=request.reason,
        expected_lock_version=request.expected_lock_version,
        before_state=before_state,
    )

    if asset.config_fingerprint is not None:
        stage, _idempotency_key = image_enrichment_run_identity(
            revision_id=revision.id,
            source_occurrence=asset.source_occurrence,
            config_fingerprint=asset.config_fingerprint,
        )
        await session.execute(
            update(IngestRun)
            .where(
                IngestRun.revision_id == revision.id,
                IngestRun.stage == stage,
            )
            .values(
                status=IngestRunStatus.FAILED.value,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                available_at=None,
                error="image review retry requested",
                finished_at=now,
            )
        )

    root_run = await session.scalar(
        select(IngestRun)
        .where(
            IngestRun.revision_id == revision.id,
            IngestRun.stage == "document",
            IngestRun.segment_no == 0,
        )
        .with_for_update()
    )
    if root_run is not None:
        root_run.status = IngestRunStatus.FAILED
        root_run.lease_owner = None
        root_run.lease_expires_at = None
        root_run.heartbeat_at = None
        root_run.available_at = None
        root_run.error = "image review retry requested"
        root_run.finished_at = now
        session.add(root_run)

    document.status = DocStatus.UPLOADED
    document.error = None
    document.progress = 0
    revision.status = RevisionStatus.UPLOADED
    revision.error = None
    session.add_all([document, revision])
    _revision, queued_run = await enqueue_document_ingestion(session, document)
    await session.flush()
    return KbImageRetrySchedule(
        asset_id=asset.id,
        run_id=queued_run.id,
        lock_version=asset.lock_version,
    )


__all__ = [
    "KbImageRetrySchedule",
    "KbImageReviewConflictError",
    "retry_image_asset",
    "review_image_asset",
]


async def bulk_review_image_assets(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    kb_id: UUID,
    actor_user_id: UUID,
    request: KbImageBulkReviewRequest,
) -> KbImageBulkReviewResult:
    """按筛选条件批量审核。逐条写审计与快照失效,只是省掉逐张往返。

    与单张审核的差别仅在于不校验 lock_version —— 批量的语义是"把当前符合
    条件的都处理掉",而不是"我看过这一张的第 N 版"。不满足接受条件的条目
    (富化未成功、内容为空、已是目标状态)如实计入 skipped,不谎报成功。
    """
    if (
        await capture_active_knowledge_base_lease(
            session,
            kb_id=kb_id,
            owner_org_id=request_org_id,
            lock=True,
        )
        is None
    ):
        return KbImageBulkReviewResult(
            matched=0,
            updated=0,
            skipped=0,
            truncated=False,
        )
    action = (
        ImageReviewAction.ACCEPT
        if request.action == "accept"
        else ImageReviewAction.EXCLUDE
    )
    target = (
        ImageReviewStatus.ACCEPTED
        if action is ImageReviewAction.ACCEPT
        else ImageReviewStatus.EXCLUDED
    )
    conditions = [
        KbImageAsset.org_id == request_org_id,
        KbImageAsset.kb_id == kb_id,
        KnowledgeBase.lifecycle_status
        == KnowledgeBaseLifecycleStatus.ACTIVE.value,
        DocumentRevision.status != RevisionStatus.TOMBSTONED.value,
    ]
    if request.asset_ids:
        conditions.append(KbImageAsset.id.in_(request.asset_ids))
    if request.document_id is not None:
        conditions.append(KbImageAsset.doc_id == request.document_id)
    if request.review_status is not None:
        conditions.append(
            KbImageAsset.review_status == request.review_status.value
        )
    if request.enrichment_status is not None:
        conditions.append(
            KbImageAsset.enrichment_status == request.enrichment_status.value
        )

    matched = int(
        await session.scalar(
            select(func.count())
            .select_from(KbImageAsset)
            .join(
                KnowledgeBase,
                and_(
                    KnowledgeBase.id == KbImageAsset.kb_id,
                    KnowledgeBase.org_id == KbImageAsset.org_id,
                ),
            )
            .join(
                DocumentRevision,
                and_(
                    DocumentRevision.id == KbImageAsset.revision_id,
                    DocumentRevision.org_id == KbImageAsset.org_id,
                ),
            )
            .where(*conditions, KnowledgeBase.org_id == request_org_id)
        )
        or 0
    )
    assets = list(
        (
            await session.execute(
                select(KbImageAsset)
                .join(
                    KnowledgeBase,
                    and_(
                        KnowledgeBase.id == KbImageAsset.kb_id,
                        KnowledgeBase.org_id == KbImageAsset.org_id,
                    ),
                )
                .join(
                    DocumentRevision,
                    and_(
                        DocumentRevision.id == KbImageAsset.revision_id,
                        DocumentRevision.org_id == KbImageAsset.org_id,
                    ),
                )
                .where(*conditions, KnowledgeBase.org_id == request_org_id)
                .order_by(KbImageAsset.doc_id, KbImageAsset.source_occurrence)
                .limit(request.max_assets)
                .with_for_update(of=KbImageAsset)
            )
        )
        .scalars()
        .all()
    )

    now = datetime.now(UTC)
    updated = 0
    touched_doc_ids: set[UUID] = set()
    for asset in assets:
        if _value(asset.review_status) == target.value:
            continue
        if action is ImageReviewAction.ACCEPT:
            try:
                _require_acceptable(asset)
            except KbImageReviewConflictError:
                continue  # 富化没成或内容为空的图不能被批量放行
        before_state = _review_state(asset)
        asset.review_status = target
        asset.reviewed_by = actor_user_id
        asset.reviewed_at = now
        asset.review_source = "human_bulk"
        asset.review_note = request.reason
        asset.lock_version += 1
        session.add(asset)
        await _invalidate_draft_snapshots(
            session,
            asset=asset,
            action=action.value,
            now=now,
        )
        await _append_human_event(
            session,
            asset=asset,
            actor_user_id=actor_user_id,
            action=action.value,
            reason=request.reason,
            expected_lock_version=asset.lock_version - 1,
            before_state=before_state,
        )
        touched_doc_ids.add(asset.doc_id)
        updated += 1

    await session.flush()
    for doc_id in touched_doc_ids:
        # 最后一张待审图片处理完(且事实也审完)时把文档收回 completed
        await settle_document_review(session, org_id=request_org_id, doc_id=doc_id)
    return KbImageBulkReviewResult(
        matched=matched,
        updated=updated,
        skipped=len(assets) - updated,
        truncated=matched > len(assets),
    )
