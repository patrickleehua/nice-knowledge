"""Organization-scoped queries for the KB image maintenance inventory."""

from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nicekit.domain.kb_media import (
    ImageAssetIssueKind,
    ImageEnrichmentStatus,
    ImageReviewStatus,
    KbImageAssetAuditEventRead,
    KbImageAssetAuditPage,
    KbImageAssetAuditState,
    KbImageAssetPage,
    KbImageAssetRead,
    KbImageAssetUsageSummary,
)
from nicekit.kb.image_assets import (
    KbImageAssetUnavailableError,
    visible_kb_filter,
)
from nicekit.models.kb import (
    DocumentRevision,
    EvidenceSpan,
    KbChunk,
    KbImageAsset,
    KbImageAssetEvent,
    KbSnapshotImageAsset,
    KnowledgeBase,
    RevisionStatus,
    SourceDocument,
)

_UNSUPPORTED_CODES = frozenset(
    {
        "unsupported_format",
        "unsupported_image",
        "empty_image",
        "malformed_image",
    }
)
_OVERSIZED_CODES = frozenset({"image_too_large", "invalid_dimensions"})
_MISSING_OBJECT_CODES = frozenset(
    {
        "image_asset_unavailable",
        "missing_object",
        "object_unavailable",
        "object_missing_after_write",
    }
)
_AUDIT_STATE_FIELDS = frozenset(KbImageAssetAuditState.model_fields)


def _issue_condition(issue_kind: ImageAssetIssueKind):
    if issue_kind is ImageAssetIssueKind.DUPLICATE:
        other_asset = aliased(KbImageAsset)
        return exists(
            select(other_asset.id).where(
                other_asset.id != KbImageAsset.id,
                other_asset.org_id == KbImageAsset.org_id,
                other_asset.kb_id == KbImageAsset.kb_id,
                other_asset.revision_id == KbImageAsset.revision_id,
                other_asset.image_sha256 == KbImageAsset.image_sha256,
            )
        )
    if issue_kind is ImageAssetIssueKind.UNSUPPORTED:
        return KbImageAsset.failure_code.in_(_UNSUPPORTED_CODES)
    if issue_kind is ImageAssetIssueKind.OVERSIZED:
        return KbImageAsset.failure_code.in_(_OVERSIZED_CODES)
    if issue_kind is ImageAssetIssueKind.MISSING_OBJECT:
        return KbImageAsset.failure_code.in_(_MISSING_OBJECT_CODES)
    if issue_kind is ImageAssetIssueKind.PROVIDER_FAILED:
        return or_(
            KbImageAsset.failure_code.like("ocr_%"),
            KbImageAsset.failure_code.like("caption_%"),
            KbImageAsset.failure_code.in_(
                {"image_enrichment_failed", "provider_unavailable"}
            ),
        )
    return or_(
        KbImageAsset.enrichment_status.in_(
            {
                ImageEnrichmentStatus.PENDING.value,
                ImageEnrichmentStatus.PROCESSING.value,
            }
        ),
        KbImageAsset.review_status.in_(
            {
                ImageReviewStatus.NEEDS_REVIEW.value,
                ImageReviewStatus.REJECTED.value,
            }
        ),
    )


def _asset_conditions(
    *,
    request_org_id: UUID,
    kb_id: UUID | None = None,
    asset_id: UUID | None = None,
    enrichment_status: ImageEnrichmentStatus | None = None,
    review_status: ImageReviewStatus | None = None,
    failure_code: str | None = None,
    document_id: UUID | None = None,
    issue_kind: ImageAssetIssueKind | None = None,
) -> list:
    conditions = [
        visible_kb_filter(request_org_id),
        DocumentRevision.status != RevisionStatus.TOMBSTONED.value,
    ]
    if kb_id is not None:
        conditions.append(KbImageAsset.kb_id == kb_id)
    if asset_id is not None:
        conditions.append(KbImageAsset.id == asset_id)
    if enrichment_status is not None:
        conditions.append(KbImageAsset.enrichment_status == enrichment_status.value)
    if review_status is not None:
        conditions.append(KbImageAsset.review_status == review_status.value)
    if failure_code is not None:
        conditions.append(KbImageAsset.failure_code == failure_code)
    if document_id is not None:
        conditions.append(KbImageAsset.doc_id == document_id)
    if issue_kind is not None:
        conditions.append(_issue_condition(issue_kind))
    return conditions


def _asset_joins(statement):
    return (
        statement.join(
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
    )


def _usage_columns():
    snapshot_count = (
        select(func.count(KbSnapshotImageAsset.id))
        .where(KbSnapshotImageAsset.image_asset_id == KbImageAsset.id)
        .correlate(KbImageAsset)
        .scalar_subquery()
        .label("snapshot_count")
    )
    image_chunk_count = (
        select(func.count(KbChunk.id))
        .where(KbChunk.image_asset_id == KbImageAsset.id)
        .correlate(KbImageAsset)
        .scalar_subquery()
        .label("image_chunk_count")
    )
    evidence_span_count = (
        select(func.count(EvidenceSpan.id))
        .where(EvidenceSpan.image_asset_id == KbImageAsset.id)
        .correlate(KbImageAsset)
        .scalar_subquery()
        .label("evidence_span_count")
    )
    return snapshot_count, image_chunk_count, evidence_span_count


def _media_url(asset_id: UUID, variant: str) -> str:
    return f"/api/v1/kb/image-assets/{asset_id}/{variant}"


def serialize_image_asset(
    asset: KbImageAsset,
    document: SourceDocument,
    revision: DocumentRevision,
    *,
    snapshot_count: int,
    image_chunk_count: int,
    evidence_span_count: int,
) -> KbImageAssetRead:
    """Project persistence state without exposing object keys or failure details."""
    content_url = (
        _media_url(asset.id, "content")
        if asset.original_object_key is not None
        else None
    )
    thumbnail_url = (
        _media_url(asset.id, "thumbnail")
        if asset.thumbnail_object_key is not None
        else None
    )
    return KbImageAssetRead(
        asset_id=asset.id,
        kb_id=asset.kb_id,
        source_document_id=asset.doc_id,
        source_filename=document.filename,
        revision_id=asset.revision_id,
        revision_number=revision.revision_no,
        revision_sha256=revision.sha256,
        ingest_run_id=asset.ingest_run_id,
        source_occurrence=asset.source_occurrence,
        parser_name=asset.parser_name,
        parser_item_ref=asset.parser_item_ref,
        page=asset.page,
        slide=asset.slide,
        bbox=asset.bbox,
        reading_order=asset.reading_order,
        surrounding_text=asset.surrounding_text,
        image_sha256=asset.image_sha256,
        size_bytes=asset.size_bytes,
        content_type=asset.content_type,
        width=asset.width,
        height=asset.height,
        thumbnail_sha256=asset.thumbnail_sha256,
        thumbnail_content_type=asset.thumbnail_content_type,
        thumbnail_size_bytes=asset.thumbnail_size_bytes,
        thumbnail_width=asset.thumbnail_width,
        thumbnail_height=asset.thumbnail_height,
        content_url=content_url,
        thumbnail_url=thumbnail_url,
        ocr_text=asset.ocr_text,
        caption=asset.caption,
        ocr_provider=asset.ocr_provider,
        ocr_model=asset.ocr_model,
        caption_provider=asset.caption_provider,
        caption_model=asset.caption_model,
        extraction_fingerprint=asset.extraction_fingerprint,
        ocr_fingerprint=asset.ocr_fingerprint,
        caption_fingerprint=asset.caption_fingerprint,
        config_fingerprint=asset.config_fingerprint,
        enrichment_status=asset.enrichment_status,
        review_status=asset.review_status,
        failure_code=asset.failure_code,
        reviewed_by=asset.reviewed_by,
        reviewed_at=asset.reviewed_at,
        review_source=asset.review_source,
        review_note=asset.review_note,
        lock_version=asset.lock_version,
        usage=KbImageAssetUsageSummary(
            snapshot_count=snapshot_count,
            image_chunk_count=image_chunk_count,
            evidence_span_count=evidence_span_count,
        ),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


async def list_image_assets(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    kb_id: UUID,
    page: int,
    page_size: int,
    enrichment_status: ImageEnrichmentStatus | None = None,
    review_status: ImageReviewStatus | None = None,
    failure_code: str | None = None,
    document_id: UUID | None = None,
    issue_kind: ImageAssetIssueKind | None = None,
) -> KbImageAssetPage:
    conditions = _asset_conditions(
        request_org_id=request_org_id,
        kb_id=kb_id,
        enrichment_status=enrichment_status,
        review_status=review_status,
        failure_code=failure_code,
        document_id=document_id,
        issue_kind=issue_kind,
    )
    total_result = await session.execute(
        _asset_joins(select(func.count()).select_from(KbImageAsset)).where(*conditions)
    )
    total = total_result.scalar_one()

    usage_columns = _usage_columns()
    result = await session.execute(
        _asset_joins(
            select(
                KbImageAsset,
                SourceDocument,
                DocumentRevision,
                *usage_columns,
            )
        )
        .where(*conditions)
        .order_by(KbImageAsset.created_at.desc(), KbImageAsset.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        serialize_image_asset(
            asset,
            document,
            revision,
            snapshot_count=snapshot_count,
            image_chunk_count=image_chunk_count,
            evidence_span_count=evidence_span_count,
        )
        for (
            asset,
            document,
            revision,
            snapshot_count,
            image_chunk_count,
            evidence_span_count,
        ) in result.all()
    ]
    return KbImageAssetPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_image_asset(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    asset_id: UUID,
) -> KbImageAssetRead:
    usage_columns = _usage_columns()
    result = await session.execute(
        _asset_joins(
            select(
                KbImageAsset,
                SourceDocument,
                DocumentRevision,
                *usage_columns,
            )
        )
        .where(
            *_asset_conditions(
                request_org_id=request_org_id,
                asset_id=asset_id,
            )
        )
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        raise KbImageAssetUnavailableError()
    (
        asset,
        document,
        revision,
        snapshot_count,
        image_chunk_count,
        evidence_span_count,
    ) = row
    return serialize_image_asset(
        asset,
        document,
        revision,
        snapshot_count=snapshot_count,
        image_chunk_count=image_chunk_count,
        evidence_span_count=evidence_span_count,
    )


def _serialize_audit_state(value: dict | None) -> KbImageAssetAuditState | None:
    if value is None:
        return None
    return KbImageAssetAuditState.model_validate(
        {key: item for key, item in value.items() if key in _AUDIT_STATE_FIELDS}
    )


async def list_image_asset_audit(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    asset_id: UUID,
    page: int,
    page_size: int,
) -> KbImageAssetAuditPage:
    """Return immutable, allowlisted history after resolving asset visibility."""
    await get_image_asset(
        session,
        request_org_id=request_org_id,
        asset_id=asset_id,
    )
    total = await session.scalar(
        select(func.count(KbImageAssetEvent.id)).where(
            KbImageAssetEvent.image_asset_id == asset_id
        )
    )
    events = (
        await session.scalars(
            select(KbImageAssetEvent)
            .where(KbImageAssetEvent.image_asset_id == asset_id)
            .order_by(
                KbImageAssetEvent.event_version.desc(),
                KbImageAssetEvent.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return KbImageAssetAuditPage(
        items=[
            KbImageAssetAuditEventRead(
                event_version=event.event_version,
                action=event.action,
                actor_kind=event.actor_kind,
                actor_user_id=event.actor_user_id,
                reason=event.reason,
                before_state=_serialize_audit_state(event.before_state),
                after_state=_serialize_audit_state(event.after_state),
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


__all__ = [
    "get_image_asset",
    "list_image_asset_audit",
    "list_image_assets",
    "serialize_image_asset",
]
