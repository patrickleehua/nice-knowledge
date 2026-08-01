"""Deterministic persistence and lifecycle projection for parsed KB images."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.domain.kb_media import ImageEnrichmentStatus, ImageReviewStatus
from nicekit.kb.caption import CaptionModelSelection
from nicekit.kb.image_assets import (
    KbImageAssetUnavailableError,
    resolve_image_object,
    store_image_objects,
)
from nicekit.kb.image_enrichment import (
    KbImageEnrichmentError,
    KbImageEnrichmentService,
)
from nicekit.kb.image_validation import (
    KbImageValidationError,
    ValidatedKbImage,
    is_decorative_image,
    validate_kb_image,
)
from nicekit.kb.ingest_runs import (
    IngestLeaseLostError,
    claim_ingest_run,
    complete_ingest_run,
    fail_ingest_run,
    maintain_ingest_lease,
)
from nicekit.kb.parsers.base import ParsedImageCandidate
from nicekit.llm.model_catalog import (
    ModelCatalogLookupError,
    lookup_eligible_provider_model_session,
)
from nicekit.models.kb import (
    DocStatus,
    DocumentRevision,
    IngestRun,
    IngestRunStatus,
    KbImageAsset,
    KbImageAssetEvent,
    KbSnapshotImageAsset,
    KnowledgeBase,
    SourceDocument,
)

logger = logging.getLogger(__name__)

# (已完成张数, 总张数) → 供摄入侧把图片阶段折算进文档进度
ImageProgressCallback = Callable[[int, int], Awaitable[None]]

ImageStageState = Literal[
    "pending",
    "processing",
    "needs_review",
    "failed",
    "completed",
]

_ASSET_OCCURRENCE_CONSTRAINT = "uq_kb_image_asset_revision_occurrence"
_EVENT_IDEMPOTENCY_CONSTRAINT = "uq_kb_image_asset_event_idempotency_key"
_PERSISTENCE_VERSION = "kb-image-persist:v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KB_NOT_ACTIVE_ERROR = "knowledge_base_not_active"
_KB_BOUNDARY_CHANGED_ERROR = "knowledge_base_consumption_boundary_changed"
_ImageEpochDisposition = Literal["published", "reconfirm", "reprocess"]


class ImageCandidateConflictError(RuntimeError):
    """A parser replay disagrees with immutable persisted provenance."""


class ImageStageBusyError(RuntimeError):
    """A healthy worker already owns this image/configuration stage."""


def _run_consumption_epoch(run: IngestRun) -> int | None:
    stats = run.stats if isinstance(run.stats, dict) else {}
    value = stats.get("consumption_epoch")
    return value if type(value) is int and value >= 0 else None


def _asset_epoch_state(asset: KbImageAsset) -> dict:
    def value(item) -> str | None:
        if item is None:
            return None
        return item.value if hasattr(item, "value") else str(item)

    return {
        "caption": asset.caption,
        "ocr_text": asset.ocr_text,
        "enrichment_status": value(asset.enrichment_status),
        "review_status": value(asset.review_status),
        "failure_code": asset.failure_code,
        "review_source": asset.review_source,
        "reviewed_by": str(asset.reviewed_by) if asset.reviewed_by else None,
        "lock_version": asset.lock_version,
    }


async def _record_epoch_invalidation_event(
    session: AsyncSession,
    *,
    asset: KbImageAsset,
    stale_epoch: int | None,
    disposition: _ImageEpochDisposition,
    before_state: dict,
) -> None:
    epoch_label = "missing" if stale_epoch is None else str(stale_epoch)
    idempotency_key = (
        f"kb-image-epoch:{asset.id}:{epoch_label}:{disposition}"
    )
    event_version = await next_image_event_version(session, asset_id=asset.id)
    await session.execute(
        pg_insert(KbImageAssetEvent)
        .values(
            id=uuid5(asset.id, idempotency_key),
            org_id=asset.org_id,
            kb_id=asset.kb_id,
            image_asset_id=asset.id,
            event_version=event_version,
            action=f"consumption_epoch_{disposition}",
            actor_kind="system",
            before_state=before_state,
            after_state=_asset_epoch_state(asset),
            reason=_KB_BOUNDARY_CHANGED_ERROR,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(constraint=_EVENT_IDEMPOTENCY_CONSTRAINT)
    )


async def _knowledge_base_boundary_error(
    session: AsyncSession,
    *,
    kb_id: UUID,
    org_id: UUID,
    captured_epoch: int,
    lock: bool = False,
) -> str | None:
    kb = await session.get(
        KnowledgeBase,
        kb_id,
        populate_existing=True,
        with_for_update={"read": True, "key_share": True} if lock else False,
    )
    lifecycle = (
        str(getattr(kb.lifecycle_status, "value", kb.lifecycle_status))
        if kb is not None
        else None
    )
    if kb is None or kb.org_id != org_id or lifecycle != "active":
        return _KB_NOT_ACTIVE_ERROR
    if int(kb.consumption_epoch) != captured_epoch:
        return _KB_BOUNDARY_CHANGED_ERROR
    return None


async def _invalidate_unpublished_image_result(
    session: AsyncSession,
    *,
    asset_id: UUID,
    stale_epoch: int | None,
) -> _ImageEpochDisposition:
    published = await session.scalar(
        select(KbSnapshotImageAsset.id)
        .where(KbSnapshotImageAsset.image_asset_id == asset_id)
        .limit(1)
    )
    if published is not None:
        return "published"
    asset = await session.scalar(
        select(KbImageAsset).where(KbImageAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        return "reprocess"
    before_state = _asset_epoch_state(asset)
    if asset.review_source in {"human", "human_bulk"}:
        asset.review_status = ImageReviewStatus.NEEDS_REVIEW
        asset.review_note = _KB_BOUNDARY_CHANGED_ERROR
        asset.lock_version += 1
        session.add(asset)
        await _record_epoch_invalidation_event(
            session,
            asset=asset,
            stale_epoch=stale_epoch,
            disposition="reconfirm",
            before_state=before_state,
        )
        return "reconfirm"
    asset.ocr_text = None
    asset.caption = None
    asset.ocr_provider = None
    asset.ocr_model = None
    asset.caption_provider = None
    asset.caption_model = None
    asset.ocr_fingerprint = None
    asset.caption_fingerprint = None
    asset.enrichment_status = ImageEnrichmentStatus.FAILED
    asset.review_status = ImageReviewStatus.REJECTED
    asset.failure_code = _KB_BOUNDARY_CHANGED_ERROR
    asset.failure_detail = None
    asset.review_source = "system"
    asset.review_note = _KB_BOUNDARY_CHANGED_ERROR
    asset.lock_version += 1
    session.add(asset)
    await _record_epoch_invalidation_event(
        session,
        asset=asset,
        stale_epoch=stale_epoch,
        disposition="reprocess",
        before_state=before_state,
    )
    return "reprocess"


@dataclass(frozen=True, slots=True)
class ImageStageProjection:
    state: ImageStageState
    total: int
    pending: int
    processing: int
    needs_review: int
    failed: int
    completed: int

    @property
    def publishable(self) -> bool:
        return self.state == "completed"


def _normalized_bbox(
    candidate: ParsedImageCandidate,
) -> dict[str, float] | None:
    if candidate.bbox is None:
        return None
    left, top, right, bottom = candidate.bbox
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
    }


def _require_bounded_text(value: str | None, *, field: str, limit: int) -> None:
    if value is not None and len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")


def _extraction_fingerprint(
    *,
    parser_name: str,
    candidate: ParsedImageCandidate,
    image_sha256: str,
) -> str:
    payload = {
        "version": _PERSISTENCE_VERSION,
        "parser_name": parser_name,
        "source_occurrence": candidate.source_occurrence,
        "parser_item_ref": candidate.parser_item_ref,
        "page": candidate.page,
        "slide": candidate.slide,
        "reading_order": candidate.reading_order,
        "bbox": _normalized_bbox(candidate),
        "surrounding_text": candidate.surrounding_text,
        "image_sha256": image_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _asset_id(revision_id: UUID, source_occurrence: str) -> UUID:
    return uuid5(revision_id, source_occurrence)


def _event_idempotency_key(asset_id: UUID) -> str:
    digest = hashlib.sha256(
        f"{asset_id}:{_PERSISTENCE_VERSION}".encode()
    ).hexdigest()
    return f"kb-image-persist:{digest}"


def _base_asset_values(
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    parser_name: str,
    candidate: ParsedImageCandidate,
    image_sha256: str,
    size_bytes: int,
    extraction_fingerprint: str,
) -> dict:
    return {
        "id": _asset_id(revision.id, candidate.source_occurrence),
        "org_id": doc.org_id,
        "kb_id": doc.kb_id,
        "doc_id": doc.id,
        "revision_id": revision.id,
        "ingest_run_id": ingest_run_id,
        "source_occurrence": candidate.source_occurrence,
        "parser_name": parser_name,
        "parser_item_ref": candidate.parser_item_ref,
        "page": candidate.page,
        "slide": candidate.slide,
        "reading_order": candidate.reading_order,
        "bbox": _normalized_bbox(candidate),
        "surrounding_text": candidate.surrounding_text,
        "image_sha256": image_sha256,
        "size_bytes": size_bytes,
        "extraction_fingerprint": extraction_fingerprint,
        "review_status": ImageReviewStatus.NEEDS_REVIEW.value,
    }


def _valid_asset_values(
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    parser_name: str,
    candidate: ParsedImageCandidate,
    image: ValidatedKbImage,
    stored,
) -> dict:
    values = _base_asset_values(
        doc=doc,
        revision=revision,
        ingest_run_id=ingest_run_id,
        parser_name=parser_name,
        candidate=candidate,
        image_sha256=image.sha256,
        size_bytes=image.size_bytes,
        extraction_fingerprint=_extraction_fingerprint(
            parser_name=parser_name,
            candidate=candidate,
            image_sha256=image.sha256,
        ),
    )
    values.update(
        {
            "content_type": image.content_type,
            "width": image.width,
            "height": image.height,
            "original_object_key": stored.original_object_key,
            "thumbnail_object_key": stored.thumbnail_object_key,
            "thumbnail_sha256": stored.thumbnail.sha256,
            "thumbnail_content_type": stored.thumbnail.content_type,
            "thumbnail_size_bytes": stored.thumbnail.size_bytes,
            "thumbnail_width": stored.thumbnail.width,
            "thumbnail_height": stored.thumbnail.height,
            "enrichment_status": ImageEnrichmentStatus.PENDING.value,
            "failure_code": None,
            "failure_detail": None,
        }
    )
    return values


def _invalid_asset_values(
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    parser_name: str,
    candidate: ParsedImageCandidate,
    error: KbImageValidationError,
) -> dict:
    values = _base_asset_values(
        doc=doc,
        revision=revision,
        ingest_run_id=ingest_run_id,
        parser_name=parser_name,
        candidate=candidate,
        image_sha256=error.sha256,
        size_bytes=error.size_bytes,
        extraction_fingerprint=_extraction_fingerprint(
            parser_name=parser_name,
            candidate=candidate,
            image_sha256=error.sha256,
        ),
    )
    values.update(
        {
            "enrichment_status": ImageEnrichmentStatus.FAILED.value,
            "failure_code": error.code,
            "failure_detail": None,
        }
    )
    return values


_IMMUTABLE_REPLAY_FIELDS = (
    "id",
    "org_id",
    "kb_id",
    "doc_id",
    "revision_id",
    "ingest_run_id",
    "source_occurrence",
    "parser_name",
    "parser_item_ref",
    "page",
    "slide",
    "reading_order",
    "bbox",
    "surrounding_text",
    "image_sha256",
    "size_bytes",
    "content_type",
    "width",
    "height",
    "original_object_key",
    "thumbnail_object_key",
    "thumbnail_sha256",
    "thumbnail_content_type",
    "thumbnail_size_bytes",
    "thumbnail_width",
    "thumbnail_height",
    "extraction_fingerprint",
)


def _assert_replay_matches(asset: KbImageAsset, values: dict) -> None:
    mismatches = [
        field
        for field in _IMMUTABLE_REPLAY_FIELDS
        if getattr(asset, field) != values.get(field)
    ]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ImageCandidateConflictError(
            "parsed image occurrence conflicts with immutable asset fields: "
            f"{joined}"
        )


async def _record_persistence_event(
    session: AsyncSession,
    *,
    values: dict,
) -> None:
    asset_id = values["id"]
    action = (
        "candidate_validation_failed"
        if values["enrichment_status"] == ImageEnrichmentStatus.FAILED.value
        else "candidate_persisted"
    )
    statement = (
        pg_insert(KbImageAssetEvent)
        .values(
            id=uuid5(asset_id, _PERSISTENCE_VERSION),
            org_id=values["org_id"],
            kb_id=values["kb_id"],
            image_asset_id=asset_id,
            event_version=1,
            action=action,
            actor_kind="system",
            after_state={
                "enrichment_status": values["enrichment_status"],
                "review_status": values["review_status"],
                "image_sha256": values["image_sha256"],
                "failure_code": values.get("failure_code"),
            },
            idempotency_key=_event_idempotency_key(asset_id),
        )
        .on_conflict_do_nothing(constraint=_EVENT_IDEMPOTENCY_CONSTRAINT)
    )
    await session.execute(statement)


async def _persist_candidate(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    parser_name: str,
    candidate: ParsedImageCandidate,
) -> KbImageAsset | None:
    """入库一处图片出现;装饰性图标返回 None,既不落资产也不进富化队列。"""
    try:
        image = validate_kb_image(candidate.content)
    except KbImageValidationError as error:
        values = _invalid_asset_values(
            doc=doc,
            revision=revision,
            ingest_run_id=ingest_run_id,
            parser_name=parser_name,
            candidate=candidate,
            error=error,
        )
    else:
        if is_decorative_image(image.width, image.height):
            return None
        stored = await store_image_objects(
            org_id=doc.org_id,
            kb_id=doc.kb_id,
            image=image,
        )
        values = _valid_asset_values(
            doc=doc,
            revision=revision,
            ingest_run_id=ingest_run_id,
            parser_name=parser_name,
            candidate=candidate,
            image=image,
            stored=stored,
        )

    inserted_id = await session.scalar(
        pg_insert(KbImageAsset)
        .values(**values)
        .on_conflict_do_nothing(constraint=_ASSET_OCCURRENCE_CONSTRAINT)
        .returning(KbImageAsset.id)
    )
    if inserted_id is not None:
        await _record_persistence_event(session, values=values)
    asset = await session.scalar(
        select(KbImageAsset).where(
            KbImageAsset.revision_id == revision.id,
            KbImageAsset.source_occurrence == candidate.source_occurrence,
        )
    )
    if asset is None:
        raise RuntimeError("persisted image asset cannot be reloaded")
    _assert_replay_matches(asset, values)
    return asset


async def persist_image_candidates(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    parser_name: str,
    candidates: Sequence[ParsedImageCandidate],
) -> list[KbImageAsset]:
    """Persist every source occurrence before the parse run becomes staged."""
    _require_bounded_text(parser_name, field="parser_name", limit=50)
    ordered = sorted(candidates, key=lambda item: item.source_occurrence)
    seen: set[str] = set()
    for candidate in ordered:
        _require_bounded_text(
            candidate.source_occurrence,
            field="source_occurrence",
            limit=500,
        )
        _require_bounded_text(
            candidate.parser_item_ref,
            field="parser_item_ref",
            limit=500,
        )
        if candidate.source_occurrence in seen:
            raise ValueError(
                "parser emitted duplicate image source_occurrence: "
                f"{candidate.source_occurrence}"
            )
        seen.add(candidate.source_occurrence)
    assets: list[KbImageAsset] = []
    decorative = 0
    for candidate in ordered:
        asset = await _persist_candidate(
            session,
            doc=doc,
            revision=revision,
            ingest_run_id=ingest_run_id,
            parser_name=parser_name,
            candidate=candidate,
        )
        if asset is None:
            decorative += 1
            continue
        assets.append(asset)
    if decorative:
        # 过滤是有损的,必须可观测:parse 阶段 stats 另记同口径计数
        logger.info(
            "图片入库过滤装饰性图标 %d/%d(revision=%s)",
            decorative,
            len(ordered),
            revision.id,
        )
    return assets


def summarize_image_assets(
    assets: Iterable[KbImageAsset],
) -> ImageStageProjection:
    rows = list(assets)
    pending = sum(
        asset.enrichment_status == ImageEnrichmentStatus.PENDING for asset in rows
    )
    processing = sum(
        asset.enrichment_status == ImageEnrichmentStatus.PROCESSING
        for asset in rows
    )
    failed = sum(
        asset.enrichment_status == ImageEnrichmentStatus.FAILED for asset in rows
    )
    needs_review = sum(
        asset.enrichment_status == ImageEnrichmentStatus.SUCCEEDED
        and asset.review_status == ImageReviewStatus.NEEDS_REVIEW
        for asset in rows
    )
    completed = len(rows) - pending - processing - failed - needs_review
    if failed:
        state: ImageStageState = "failed"
    elif processing:
        state = "processing"
    elif pending:
        state = "pending"
    elif needs_review:
        state = "needs_review"
    else:
        state = "completed"
    return ImageStageProjection(
        state=state,
        total=len(rows),
        pending=pending,
        processing=processing,
        needs_review=needs_review,
        failed=failed,
        completed=completed,
    )


async def revision_image_stage(
    session: AsyncSession,
    revision_id: UUID,
) -> ImageStageProjection:
    # populate_existing:富化跑在各自 session 上,调用方 identity map 里的副本必然陈旧
    # (sessionmaker 为 expire_on_commit=False),不强制刷新会把 pending 当成终态读回。
    assets = (
        await session.execute(
            select(KbImageAsset)
            .where(KbImageAsset.revision_id == revision_id)
            .order_by(KbImageAsset.source_occurrence, KbImageAsset.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return summarize_image_assets(assets)


async def next_image_event_version(
    session: AsyncSession,
    *,
    asset_id: UUID,
) -> int:
    current = await session.scalar(
        select(func.coalesce(func.max(KbImageAssetEvent.event_version), 0)).where(
            KbImageAssetEvent.image_asset_id == asset_id
        )
    )
    return int(current or 0) + 1


def _enrichment_event_key(
    *,
    asset_id: UUID,
    config_fingerprint: str,
    outcome: str,
) -> str:
    payload = f"{asset_id}:{config_fingerprint}:{outcome}"
    return f"kb-image-enrichment:{hashlib.sha256(payload.encode()).hexdigest()}"


async def _record_enrichment_event(
    session: AsyncSession,
    *,
    asset: KbImageAsset,
    action: str,
    outcome: str,
    failure_code: str | None = None,
    result_fingerprint: str | None = None,
) -> None:
    config_fingerprint = asset.config_fingerprint
    if config_fingerprint is None:
        raise RuntimeError("image enrichment event requires a config fingerprint")
    idempotency_key = _enrichment_event_key(
        asset_id=asset.id,
        config_fingerprint=config_fingerprint,
        outcome=outcome,
    )
    existing = await session.scalar(
        select(KbImageAssetEvent.id).where(
            KbImageAssetEvent.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return
    version = await next_image_event_version(session, asset_id=asset.id)
    await session.execute(
        pg_insert(KbImageAssetEvent)
        .values(
            id=uuid5(asset.id, idempotency_key),
            org_id=asset.org_id,
            kb_id=asset.kb_id,
            image_asset_id=asset.id,
            event_version=version,
            action=action,
            actor_kind="system",
            after_state={
                "enrichment_status": str(asset.enrichment_status),
                "review_status": str(asset.review_status),
                "config_fingerprint": config_fingerprint,
                "result_fingerprint": result_fingerprint,
                "failure_code": failure_code,
            },
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(constraint=_EVENT_IDEMPOTENCY_CONSTRAINT)
    )


def image_enrichment_run_identity(
    *,
    revision_id: UUID,
    source_occurrence: str,
    config_fingerprint: str,
) -> tuple[str, str]:
    """Return a bounded stage name and a full-tuple deterministic run key."""
    if not source_occurrence.strip():
        raise ValueError("source_occurrence must not be blank")
    if _SHA256.fullmatch(config_fingerprint) is None:
        raise ValueError("config_fingerprint must be a lowercase SHA-256")
    tuple_payload = json.dumps(
        {
            "revision_id": str(revision_id),
            "source_occurrence": source_occurrence,
            "stage": "image_enrichment",
            "config_fingerprint": config_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(tuple_payload.encode("utf-8")).hexdigest()
    source_digest = hashlib.sha256(source_occurrence.encode("utf-8")).hexdigest()
    stage = f"image_enrich:{source_digest[:10]}:{config_fingerprint[:10]}"
    return stage, f"kb-image-stage:{digest}"


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, KbImageEnrichmentError | KbImageAssetUnavailableError):
        return exc.code
    return "image_enrichment_failed"


async def _mark_enrichment_failure(
    session: AsyncSession,
    *,
    asset_id: UUID,
    config_fingerprint: str,
    run_id: UUID,
    lease_owner: str,
    exc: BaseException,
) -> None:
    await session.rollback()
    asset = await session.scalar(
        select(KbImageAsset)
        .where(KbImageAsset.id == asset_id)
        .with_for_update()
    )
    if asset is None:
        raise RuntimeError("image asset disappeared during enrichment failure")
    code = _failure_code(exc)
    if code in {_KB_NOT_ACTIVE_ERROR, _KB_BOUNDARY_CHANGED_ERROR}:
        published = await session.scalar(
            select(KbSnapshotImageAsset.id)
            .where(KbSnapshotImageAsset.image_asset_id == asset.id)
            .limit(1)
        )
        if published is None:
            asset.ocr_text = None
            asset.caption = None
            asset.ocr_provider = None
            asset.ocr_model = None
            asset.caption_provider = None
            asset.caption_model = None
            asset.ocr_fingerprint = None
            asset.caption_fingerprint = None
    asset.config_fingerprint = config_fingerprint
    asset.enrichment_status = ImageEnrichmentStatus.FAILED
    asset.review_status = (
        ImageReviewStatus.REJECTED
        if code in {_KB_NOT_ACTIVE_ERROR, _KB_BOUNDARY_CHANGED_ERROR}
        else ImageReviewStatus.NEEDS_REVIEW
    )
    asset.failure_code = code
    asset.failure_detail = None
    session.add(asset)
    await _record_enrichment_event(
        session,
        asset=asset,
        action="enrichment_failed",
        outcome=f"failed:{code}",
        failure_code=code,
    )
    failed = await fail_ingest_run(
        session,
        run_id=run_id,
        lease_owner=lease_owner,
        error=code,
    )
    if not failed:
        await session.rollback()
        raise IngestLeaseLostError(str(run_id))
    await session.commit()


async def has_duplicate_payload(session: AsyncSession, asset: KbImageAsset) -> bool:
    """同一 revision 内是否还有别的资产用着同一份图像字节。"""
    duplicate = await session.scalar(
        select(KbImageAsset.id)
        .where(
            KbImageAsset.id != asset.id,
            KbImageAsset.org_id == asset.org_id,
            KbImageAsset.kb_id == asset.kb_id,
            KbImageAsset.revision_id == asset.revision_id,
            KbImageAsset.image_sha256 == asset.image_sha256,
        )
        .limit(1)
    )
    return duplicate is not None


async def _auto_accepts(
    session: AsyncSession,
    asset: KbImageAsset,
    result,
) -> bool:
    """富化干净的插图直接判通过,只把真正需要人看的留在审核队列。

    一份研报能出上百张图,逐张点通过不具可维护性。判定口径刻意保守:
    caption 该有却没有、或与同版本内另一张图字节重复的,一律升级人工。
    """
    if not get_settings().kb_image_auto_accept:
        return False
    if result.caption_provider and not (result.caption or "").strip():
        return False  # 配了 caption 路由却没产出描述:退回人工
    return not await has_duplicate_payload(session, asset)


async def _complete_enrichment(
    session: AsyncSession,
    *,
    asset_id: UUID,
    run_id: UUID,
    lease_owner: str,
    result,
    captured_epoch: int,
) -> None:
    run = await session.get(IngestRun, run_id)
    if run is None:
        raise RuntimeError("image enrichment run disappeared during completion")
    boundary_error = await _knowledge_base_boundary_error(
        session,
        kb_id=run.kb_id,
        org_id=run.org_id,
        captured_epoch=captured_epoch,
        lock=True,
    )
    if boundary_error is not None:
        raise KbImageEnrichmentError(boundary_error)
    asset = await session.scalar(
        select(KbImageAsset)
        .where(KbImageAsset.id == asset_id)
        .with_for_update()
    )
    if asset is None:
        raise RuntimeError("image asset disappeared during enrichment completion")
    asset.ocr_text = result.ocr_text
    asset.caption = result.caption
    asset.ocr_provider = result.ocr_provider
    asset.ocr_model = result.ocr_model
    asset.caption_provider = result.caption_provider
    asset.caption_model = result.caption_model
    asset.ocr_fingerprint = result.ocr_fingerprint
    asset.caption_fingerprint = result.caption_fingerprint
    asset.config_fingerprint = result.config_fingerprint
    asset.enrichment_status = ImageEnrichmentStatus.SUCCEEDED
    auto_accepted = await _auto_accepts(session, asset, result)
    asset.review_status = (
        ImageReviewStatus.ACCEPTED if auto_accepted else result.review_status
    )
    asset.review_source = "auto" if auto_accepted else asset.review_source
    asset.failure_code = None
    asset.failure_detail = None
    session.add(asset)
    await _record_enrichment_event(
        session,
        asset=asset,
        action=(
            "enrichment_auto_accepted" if auto_accepted else "enrichment_completed"
        ),
        outcome=f"succeeded:{result.result_fingerprint}",
        result_fingerprint=result.result_fingerprint,
    )
    completed = await complete_ingest_run(
        session,
        run_id=run_id,
        lease_owner=lease_owner,
        stats={
            "consumption_epoch": captured_epoch,
            "image_asset_id": str(asset.id),
            "config_fingerprint": result.config_fingerprint,
            "request_fingerprint": result.request_fingerprint,
            "result_fingerprint": result.result_fingerprint,
            "review_status": str(result.review_status),
        },
    )
    if not completed:
        await session.rollback()
        raise IngestLeaseLostError(str(run_id))
    await session.commit()


async def process_revision_image_enrichment(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    revision: DocumentRevision,
    doc: SourceDocument,
    lease_owner: str,
    enricher: KbImageEnrichmentService,
    captured_epoch: int,
    caption_selection: CaptionModelSelection | None = None,
    caption_enabled: bool = True,
    on_progress: ImageProgressCallback | None = None,
) -> ImageStageProjection:
    """Run one leased, retryable enrichment stage per image/config tuple.

    每张图一个独立协程与独立 session,并发度由 kb_image_enrich_concurrency 限。
    OCR 侧本身 CPU 饱和(单例 converter 锁自动串行),并发在这里的意义是让一张图的
    caption 网络等待与下一张图的 OCR 计算重叠,总时长从 Σ(OCR+caption) 降为
    max(ΣOCR, Σcaption/并发)。每张图开工前协作式检查文档取消。
    """
    readiness = enricher.readiness(
        caption_selection,
        caption_enabled=caption_enabled,
    )
    config_fingerprint = readiness.config_fingerprint
    catalog_error: str | None = None
    caption_provider = getattr(readiness, "caption_provider", "")
    caption_model = getattr(readiness, "caption_model", "")
    if caption_provider and caption_model:
        try:
            await lookup_eligible_provider_model_session(
                session,
                provider=caption_provider,
                model=caption_model,
            )
        except ModelCatalogLookupError as exc:
            catalog_error = exc.code
    rows = [
        (row.id, row.source_occurrence)
        for row in (
            await session.execute(
                select(KbImageAsset.id, KbImageAsset.source_occurrence)
                .where(
                    KbImageAsset.revision_id == revision.id,
                    KbImageAsset.original_object_key.is_not(None),
                )
                .order_by(KbImageAsset.source_occurrence, KbImageAsset.id)
            )
        ).all()
    ]
    # 并发期间主 session 不得持有事务快照,否则每个 worker 都在等它的连接
    await session.commit()

    total = len(rows)
    if not total:
        return await revision_image_stage(session, revision.id)

    settings = get_settings()
    semaphore = asyncio.Semaphore(max(1, settings.kb_image_enrich_concurrency))
    canceled = asyncio.Event()
    done = 0
    done_lock = asyncio.Lock()

    async def enrich_one(asset_id: UUID, source_occurrence: str) -> None:
        nonlocal done
        async with semaphore:
            if canceled.is_set():
                return
            async with org_session(session_factory, revision.org_id) as worker:
                # 取消/暂停是协作式的:已在飞的图跑完当前一张,未开工的直接放弃
                if await _document_stop_requested(worker, doc.id):
                    canceled.set()
                    return
                await _enrich_one_asset(
                    worker,
                    session_factory=session_factory,
                    revision=revision,
                    doc=doc,
                    asset_id=asset_id,
                    source_occurrence=source_occurrence,
                    lease_owner=lease_owner,
                    enricher=enricher,
                    readiness=readiness,
                    config_fingerprint=config_fingerprint,
                    catalog_error=catalog_error,
                    caption_selection=caption_selection,
                    caption_enabled=caption_enabled,
                    captured_epoch=captured_epoch,
                )
        if on_progress is not None:
            async with done_lock:
                done += 1
                await on_progress(done, total)

    results = await asyncio.gather(
        *(enrich_one(asset_id, occurrence) for asset_id, occurrence in rows),
        return_exceptions=True,
    )
    for outcome in results:
        if isinstance(outcome, BaseException):
            raise outcome
    return await revision_image_stage(session, revision.id)


async def _document_stop_requested(session: AsyncSession, doc_id: UUID) -> bool:
    """绕过 identity map 直查 DB,读取其他事务提交的取消/暂停状态。

    图片链路与文本链路并行跑,两条各自读 DB 判停:任一方看到停止请求都在自己的
    边界退出,不互相依赖。暂停同样要停 —— 图片是最贵的一段,让它跑完再"暂停"
    等于暂停不生效;未开工的图保持 pending,resume 后按幂等键续跑。
    """
    status = await session.scalar(
        select(SourceDocument.status).where(SourceDocument.id == doc_id)
    )
    return status in (DocStatus.CANCELED, DocStatus.PAUSED)


async def _enrich_one_asset(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    revision: DocumentRevision,
    doc: SourceDocument,
    asset_id: UUID,
    source_occurrence: str,
    lease_owner: str,
    enricher: KbImageEnrichmentService,
    readiness,
    config_fingerprint: str,
    catalog_error: str | None,
    caption_selection: CaptionModelSelection | None,
    caption_enabled: bool,
    captured_epoch: int,
) -> None:
    """Claim, enrich and settle exactly one image/config tuple on its own session."""
    stage, idempotency_key = image_enrichment_run_identity(
        revision_id=revision.id,
        source_occurrence=source_occurrence,
        config_fingerprint=config_fingerprint,
    )
    boundary_error = await _knowledge_base_boundary_error(
        session,
        kb_id=revision.kb_id,
        org_id=revision.org_id,
        captured_epoch=captured_epoch,
    )
    if boundary_error is not None:
        raise KbImageEnrichmentError(boundary_error)
    claim = await claim_ingest_run(
        session,
        org_id=revision.org_id,
        kb_id=revision.kb_id,
        revision_id=revision.id,
        stage=stage,
        segment_no=0,
        lease_owner=lease_owner,
        idempotency_key=idempotency_key,
    )
    run = await session.get(IngestRun, claim.run_id, populate_existing=True)
    if run is None:
        raise RuntimeError("claimed image enrichment run cannot be reloaded")
    if (
        not claim.acquired
        and claim.status in {IngestRunStatus.STAGED, IngestRunStatus.SUCCEEDED}
        and _run_consumption_epoch(run) != captured_epoch
    ):
        stale_epoch = _run_consumption_epoch(run)
        disposition = await _invalidate_unpublished_image_result(
            session,
            asset_id=asset_id,
            stale_epoch=stale_epoch,
        )
        if disposition in {"published", "reconfirm"}:
            stats = run.stats if isinstance(run.stats, dict) else {}
            run.stats = {**stats, "consumption_epoch": captured_epoch}
            session.add(run)
            await session.commit()
            return
        run.status = IngestRunStatus.FAILED
        run.error = _KB_BOUNDARY_CHANGED_ERROR
        run.lease_owner = None
        run.lease_expires_at = None
        session.add(run)
        await session.commit()
        claim = await claim_ingest_run(
            session,
            org_id=revision.org_id,
            kb_id=revision.kb_id,
            revision_id=revision.id,
            stage=stage,
            segment_no=0,
            lease_owner=lease_owner,
            idempotency_key=idempotency_key,
        )
        run = await session.get(IngestRun, claim.run_id, populate_existing=True)
        if run is None:
            raise RuntimeError("reclaimed image enrichment run cannot be reloaded")
    if claim.acquired:
        stats = run.stats if isinstance(run.stats, dict) else {}
        run.stats = {**stats, "consumption_epoch": captured_epoch}
        session.add(run)
    await session.commit()
    if not claim.acquired:
        if claim.status.value in {"staged", "succeeded"}:
            return  # 已完成的图不重跑,续跑天然去重
        raise ImageStageBusyError(
            "image enrichment run is owned by another worker: "
            f"{revision.id}:{asset_id}"
        )

    asset = await session.get(KbImageAsset, asset_id)
    if asset is None:
        raise RuntimeError("claimed image asset cannot be reloaded")
    asset.config_fingerprint = config_fingerprint
    asset.enrichment_status = ImageEnrichmentStatus.PROCESSING
    asset.review_status = ImageReviewStatus.NEEDS_REVIEW
    asset.failure_code = None
    asset.failure_detail = None
    session.add(asset)
    await session.commit()
    try:
        async with maintain_ingest_lease(
            session_factory,
            org_id=revision.org_id,
            run_id=claim.run_id,
            lease_owner=lease_owner,
        ):
            if catalog_error is not None:
                raise KbImageEnrichmentError(catalog_error)
            if not readiness.ready:
                raise KbImageEnrichmentError(readiness.code)
            resolved = await resolve_image_object(
                session,
                request_org_id=doc.org_id,
                asset_id=asset_id,
                variant="original",
                lock_lifecycle=False,
            )
            result = await enricher.enrich(
                resolved.data,
                org_id=doc.org_id,
                filename=doc.filename,
                selection=caption_selection,
                caption_enabled=caption_enabled,
            )
        await _complete_enrichment(
            session,
            asset_id=asset_id,
            run_id=claim.run_id,
            lease_owner=lease_owner,
            result=result,
            captured_epoch=captured_epoch,
        )
    except Exception as exc:  # noqa: BLE001 - persist one sanitized stage failure
        if not isinstance(
            exc,
            KbImageEnrichmentError | KbImageAssetUnavailableError,
        ):
            logger.exception(
                "unexpected KB image enrichment failure",
                extra={"image_asset_id": str(asset_id)},
            )
        await _mark_enrichment_failure(
            session,
            asset_id=asset_id,
            config_fingerprint=config_fingerprint,
            run_id=claim.run_id,
            lease_owner=lease_owner,
            exc=exc,
        )


async def image_run_ids(
    session: AsyncSession,
    revision_id: UUID,
) -> list[UUID]:
    return list(
        (
            await session.scalars(
                select(IngestRun.id)
                .where(
                    IngestRun.revision_id == revision_id,
                    IngestRun.stage.like("image_enrich:%"),
                )
                .order_by(IngestRun.stage, IngestRun.id)
            )
        ).all()
    )


__all__ = [
    "ImageCandidateConflictError",
    "ImageStageBusyError",
    "ImageStageProjection",
    "ImageStageState",
    "image_enrichment_run_identity",
    "image_run_ids",
    "next_image_event_version",
    "persist_image_candidates",
    "process_revision_image_enrichment",
    "revision_image_stage",
    "summarize_image_assets",
]
