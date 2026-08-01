"""Ingestion 管线(D8):不可变 revision → staging → 待审核队列。

doc_type 为已注册实体类型 key 的文档走 LLM 结构化抽取 + 审核(统一 generic 契约,
字段约束由 KbEntityType.field_schema 注入);general 类型切 chunk + embedding 后写
revision-scoped staging artifact，不改动当前在线 kb_chunks 投影。

切片配置化:摄入读所属 KB 的 ingest_profile(parser 后端/切片策略/
参数/表格模式),structure 策略走结构感知 chunk_markdown(锚点落库),
fixed 走旧 chunk_text(无锚点);结构化抽取按 h1/h2 标题边界聚段。
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import anyio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.domain.kb import (
    EntityGraphExtraction,
    ExtractedEntity,
    GenericEntityExtraction,
    IngestProfile,
)
from nicekit.kb import storage
from nicekit.kb.chunker import (
    ExtractionSegment,
    chunk_markdown,
    split_for_extraction,
    wrap_fixed_chunks,
)
from nicekit.kb.embedding import (
    EmbeddingService,
    EmbeddingUnavailableError,
    chunk_embedding_text,
)
from nicekit.kb.guardrails import (
    fence_untrusted_document,
    suspicious_instruction_reasons,
)
from nicekit.kb.image_refs import (
    ImageMarkdownRef,
    parsed_image_occurrences,
    resolve_parsed_image_placeholders,
)
from nicekit.kb.ingest_runs import (
    IngestLeaseLostError,
    cancel_ingest_run,
    claim_ingest_run,
    complete_ingest_run,
    fail_ingest_run,
    ingest_run_idempotency_key,
    maintain_ingest_lease,
    requeue_ingest_run,
)
from nicekit.kb.parsers import parse_document
from nicekit.kb.parsing import chunk_text
from nicekit.llm.providers import ProviderError
from nicekit.llm.service import (
    AllProvidersFailedError,
    LlmBudgetExceededError,
    LLMService,
)
from nicekit.models.kb import (
    DocStatus,
    DocType,
    DocumentLifecycleStatus,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbChunk,
    KbEntityType,
    KnowledgeBase,
    RevisionStatus,
    SnapshotFactSupport,
    SourceDocument,
)

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from nicekit.kb.image_enrichment import KbImageEnrichmentService

# ---- P3b 延迟 import 说明 --------------------------------------------------
# 下列 KB 模块在本波(P3a:模型层 + 摄入链)尚未搬运,按约定路径在**使用处**
# 延迟 import,以便 ingestion 本身可被导入与单测(实体抽取/图片富化/wiki 生成
# 三条支线在 P3b 落地后自动接通,届时可按需把 import 提回模块级):
#   nicekit.kb.caption            CaptionModelSelection
#   nicekit.kb.entity_binding     ENTITY_PREDICATE / RELATION_PREDICATES / allowed_entity_types
#   nicekit.kb.entity_resolution  EntityConflictError / normalize_alias
#   nicekit.kb.entity_types       EntityValidationError / get_entity_type /
#                                 validate_entity_attributes
#   nicekit.kb.evidence_locator   EvidenceNotFoundError / locate_evidence
#   nicekit.kb.image_enrichment   KbImageEnrichmentService / get_kb_image_enrichment_service
#   nicekit.kb.image_ingestion    persist_image_candidates / process_revision_image_enrichment /
#                                 revision_image_stage / summarize_image_assets
#   nicekit.kb.wiki_gen           WikiSnapshotManagedError / update_wiki_for_document

logger = logging.getLogger(__name__)

# LLM 限流退避(KB-5B):celery 模式下 throttle 文档延迟重派的秒数
LLM_THROTTLE_RETRY_DELAY_SECONDS = 900
LLM_THROTTLE_ERROR = "LLM 限流,已自动排队重试"

# 图片富化失败的可见标记。图片是增强项(与 wiki 自动生成、实体图谱同口径),失败不再
# 把文档打成 FAILED;但它必须可见可重试,所以标记挂在 doc.error / revision.error 上,
# root run 也如实收成 failed —— 后者正是 retry 能重新入队补跑图片的前提。
IMAGE_ENRICHMENT_ERROR = "image_enrichment_failed"

_STAGED_RUN_STATUSES = {IngestRunStatus.STAGED, IngestRunStatus.SUCCEEDED}
TYPED_REEXTRACTION_CONTRACT_VERSION = 1
_TYPED_REEXTRACTION_ROOT_PREFIX = "reextract:"
_TYPED_REEXTRACTION_CHILD_PREFIX = "extract_typed:"
_KB_NOT_ACTIVE_ERROR = "knowledge_base_not_active"
_KB_BOUNDARY_CHANGED_ERROR = "knowledge_base_consumption_boundary_changed"


class IngestRunBusyError(RuntimeError):
    """Another healthy worker owns the segment; this duplicate delivery exits quietly."""


@dataclass(frozen=True, slots=True)
class TypedReextractionEligibility:
    eligible: bool
    reason_code: str | None
    revision: DocumentRevision | None
    existing_run: IngestRun | None


def _captured_consumption_epoch(run: IngestRun) -> int | None:
    raw_stats = getattr(run, "stats", None)
    stats = raw_stats if isinstance(raw_stats, dict) else {}
    value = stats.get("consumption_epoch")
    return value if type(value) is int and value >= 0 else None


async def _knowledge_base_boundary_error(
    session: AsyncSession,
    *,
    kb_id: UUID,
    org_id: UUID,
    captured_epoch: int | None,
    lock: bool = False,
) -> str | None:
    """Return a stable error when an ingestion intent crossed the KB boundary."""
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
    if captured_epoch is None or int(kb.consumption_epoch) != captured_epoch:
        return _KB_BOUNDARY_CHANGED_ERROR
    return None


async def _invalidate_unpublished_fact_outputs(
    session: AsyncSession,
    *,
    run_id: UUID,
) -> None:
    """Make stale, unpublished extraction output permanently non-reviewable."""
    published = (
        select(SnapshotFactSupport.id)
        .where(SnapshotFactSupport.fact_claim_id == FactClaim.id)
        .exists()
    )
    await session.execute(
        sa_update(FactClaim)
        .where(
            FactClaim.ingest_run_id == run_id,
            FactClaim.review_status.in_(
                (
                    FactReviewStatus.SUGGESTED.value,
                    FactReviewStatus.ORPHANED.value,
                )
            ),
            ~published,
        )
        .values(
            review_status=FactReviewStatus.REJECTED.value,
            reviewed_by="system:consumption_epoch",
            review_note=_KB_BOUNDARY_CHANGED_ERROR,
        )
    )


def typed_reextraction_stage(target_doc_type: str) -> str:
    """Build a target- and contract-qualified root stage within varchar(50)."""
    target_hash = hashlib.sha256(target_doc_type.encode("utf-8")).hexdigest()[:16]
    return (
        f"{_TYPED_REEXTRACTION_ROOT_PREFIX}"
        f"v{TYPED_REEXTRACTION_CONTRACT_VERSION}:{target_hash}"
    )


def typed_extraction_stage(target_doc_type: str) -> str:
    """Build the matching target-qualified child stage."""
    target_hash = hashlib.sha256(target_doc_type.encode("utf-8")).hexdigest()[:16]
    return (
        f"{_TYPED_REEXTRACTION_CHILD_PREFIX}"
        f"v{TYPED_REEXTRACTION_CONTRACT_VERSION}:{target_hash}"
    )


async def _pending_fact_claim_count(session: AsyncSession, revision_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(FactClaim)
            .join(IngestRun, IngestRun.id == FactClaim.ingest_run_id)
            .where(
                IngestRun.revision_id == revision_id,
                FactClaim.review_status == FactReviewStatus.SUGGESTED,
            )
        )
        or 0
    )


async def _enqueue_fact_claim(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    entity_type: str,
    payload: dict,
    segment_markdown: str,
    line_offset: int,
    structured_document: dict | None,
    model_name: str,
    prompt_version: str,
) -> None:
    evidence_quote = payload.get("evidence_quote")
    if not isinstance(evidence_quote, str):
        raise ValueError("extracted fact has no evidence_quote")
    # P3b 延迟 import(见文件头说明)
    from nicekit.kb.evidence_locator import locate_evidence

    location = locate_evidence(
        evidence_quote,
        segment_markdown,
        structured_document,
        line_offset=line_offset,
    )
    claim_id = uuid4()
    value_json = {
        key: value
        for key, value in payload.items()
        if key not in {"confidence", "evidence_quote"}
    }
    session.add(
        FactClaim(
            id=claim_id,
            org_id=doc.org_id,
            kb_id=doc.kb_id,
            ingest_run_id=ingest_run_id,
            subject_type="source_document",
            subject_id=doc.id,
            predicate=entity_type,
            value_json=value_json,
            raw_payload=payload,
            valid_from=_payload_date(payload.get("valid_from")),
            valid_to=_payload_date(payload.get("valid_to")),
            confidence=payload.get("confidence"),
            review_status=FactReviewStatus.SUGGESTED,
            model_name=model_name,
            prompt_version=prompt_version,
        )
    )
    await session.flush()
    session.add(
        EvidenceSpan(
            org_id=doc.org_id,
            kb_id=doc.kb_id,
            fact_claim_id=claim_id,
            revision_id=revision.id,
            page=location.page,
            start_line=location.start_line,
            end_line=location.end_line,
            cell_ref=location.cell_ref,
            quote_text=location.quote_text,
        )
    )


async def _get_or_create_revision(
    session: AsyncSession, doc: SourceDocument
) -> DocumentRevision:
    """Return the immutable revision represented by the legacy SourceDocument row."""
    stmt = (
        select(DocumentRevision)
        .where(
            DocumentRevision.doc_id == doc.id,
            DocumentRevision.sha256 == doc.sha256,
            DocumentRevision.status != RevisionStatus.TOMBSTONED.value,
            DocumentRevision.tombstoned_at.is_(None),
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    revision = (await session.execute(stmt)).scalar_one_or_none()
    if revision is not None:
        return revision

    # Serialize revision number allocation per logical document. The second lookup
    # handles a concurrent worker that created this content while we waited.
    await session.execute(
        select(SourceDocument.id).where(SourceDocument.id == doc.id).with_for_update()
    )
    revision = (await session.execute(stmt)).scalar_one_or_none()
    if revision is not None:
        return revision
    next_number = int(
        await session.scalar(
            select(func.coalesce(func.max(DocumentRevision.revision_no), 0) + 1).where(
                DocumentRevision.doc_id == doc.id
            )
        )
        or 1
    )
    revision = DocumentRevision(
        org_id=doc.org_id,
        kb_id=doc.kb_id,
        doc_id=doc.id,
        revision_no=next_number,
        sha256=doc.sha256,
        original_object_key=doc.object_key,
    )
    session.add(revision)
    await session.flush()
    return revision


async def enqueue_document_ingestion(
    session: AsyncSession,
    doc: SourceDocument,
    *,
    revision: DocumentRevision | None = None,
) -> tuple[DocumentRevision, IngestRun]:
    """Persist the recoverable root run in the caller's upload/retry transaction."""
    if str(doc.doc_type) == DocType.UNCLASSIFIED.value:
        raise ValueError("document must be classified before ingestion is queued")
    kb = await session.get(
        KnowledgeBase,
        doc.kb_id,
        populate_existing=True,
        with_for_update={"read": True},
    )
    if kb is None or kb.org_id != doc.org_id:
        raise ValueError("document knowledge base does not exist")
    if kb.lifecycle_status != "active":
        raise ValueError("document knowledge base is not active")
    if revision is None:
        revision = await _get_or_create_revision(session, doc)
    elif (
        revision.doc_id != doc.id
        or revision.org_id != doc.org_id
        or revision.kb_id != doc.kb_id
        or revision.sha256 != doc.sha256
        or revision.tombstoned_at is not None
        or str(getattr(revision.status, "value", revision.status))
        == RevisionStatus.TOMBSTONED.value
    ):
        raise ValueError("explicit revision does not represent the source document")
    session.add(revision)
    await session.flush()
    key = ingest_run_idempotency_key(revision.id, "document", 0)
    stmt = (
        pg_insert(IngestRun)
        .values(
            org_id=doc.org_id,
            kb_id=doc.kb_id,
            revision_id=revision.id,
            stage="document",
            segment_no=0,
            status=IngestRunStatus.QUEUED.value,
            idempotency_key=key,
            stats={"consumption_epoch": int(kb.consumption_epoch)},
        )
        .on_conflict_do_update(
            constraint="uq_ingest_run_stage_segment",
            set_={
                "status": IngestRunStatus.QUEUED.value,
                "idempotency_key": key,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "available_at": None,
                "error": None,
                "finished_at": None,
                "stats": {"consumption_epoch": int(kb.consumption_epoch)},
            },
            where=IngestRun.status.in_(
                (IngestRunStatus.FAILED.value, IngestRunStatus.CANCELED.value)
            ),
        )
        .returning(IngestRun.id)
    )
    run_id = (await session.execute(stmt)).scalar_one_or_none()
    if run_id is None:
        run_id = (
            await session.execute(
                select(IngestRun.id).where(
                    IngestRun.revision_id == revision.id,
                    IngestRun.stage == "document",
                    IngestRun.segment_no == 0,
                )
            )
        ).scalar_one()
    run = await session.get(IngestRun, run_id)
    if run is None:  # pragma: no cover - RETURNING/select and same transaction guarantee it
        raise RuntimeError(f"queued ingest run disappeared: {run_id}")
    return revision, run


async def assess_typed_reextraction(
    session: AsyncSession,
    doc: SourceDocument,
    target_doc_type: str,
) -> TypedReextractionEligibility:
    """Return stable eligibility without touching source bytes or projections."""
    revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.doc_id == doc.id,
            DocumentRevision.org_id == doc.org_id,
            DocumentRevision.kb_id == doc.kb_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    existing_run = None
    if revision is not None:
        existing_run = await session.scalar(
            select(IngestRun).where(
                IngestRun.revision_id == revision.id,
                IngestRun.stage == typed_reextraction_stage(target_doc_type),
                IngestRun.segment_no == 0,
            )
        )
    lifecycle = str(getattr(doc.lifecycle_status, "value", doc.lifecycle_status))
    if lifecycle != DocumentLifecycleStatus.ACTIVE.value:
        return TypedReextractionEligibility(
            False, "document_not_active", revision, existing_run
        )
    if revision is None:
        return TypedReextractionEligibility(
            False, "revision_missing", None, existing_run
        )
    if (
        existing_run is not None
        and str(getattr(existing_run.status, "value", existing_run.status))
        in {
            IngestRunStatus.QUEUED.value,
            IngestRunStatus.RUNNING.value,
            IngestRunStatus.STAGED.value,
            IngestRunStatus.SUCCEEDED.value,
        }
    ):
        return TypedReextractionEligibility(True, None, revision, existing_run)
    if str(doc.doc_type) == target_doc_type and existing_run is None:
        return TypedReextractionEligibility(
            False, "same_extraction_type", revision, None
        )
    document_status = str(getattr(doc.status, "value", doc.status))
    if document_status not in {
        DocStatus.COMPLETED.value,
        DocStatus.AWAITING_REVIEW.value,
    }:
        return TypedReextractionEligibility(
            False, "document_not_terminal", revision, existing_run
        )
    revision_status = str(getattr(revision.status, "value", revision.status))
    if revision_status == RevisionStatus.TOMBSTONED.value:
        return TypedReextractionEligibility(
            False, "revision_tombstoned", revision, existing_run
        )
    if revision_status not in {
        RevisionStatus.STAGED.value,
        RevisionStatus.ACTIVE.value,
    }:
        return TypedReextractionEligibility(
            False, "revision_not_staged", revision, existing_run
        )
    if not revision.markdown_key:
        return TypedReextractionEligibility(
            False, "markdown_artifact_missing", revision, existing_run
        )
    parse_run = await session.scalar(
        select(IngestRun).where(
            IngestRun.revision_id == revision.id,
            IngestRun.stage == "parse",
            IngestRun.segment_no == 0,
            IngestRun.status.in_(
                (IngestRunStatus.STAGED.value, IngestRunStatus.SUCCEEDED.value)
            ),
        )
    )
    if parse_run is None:
        return TypedReextractionEligibility(
            False, "parse_artifact_not_staged", revision, existing_run
        )
    parse_stats = parse_run.stats if isinstance(parse_run.stats, dict) else {}
    if (
        parse_stats.get("parser_name") == "docling"
        and not revision.structured_json_key
    ):
        return TypedReextractionEligibility(
            False, "structured_artifact_missing", revision, existing_run
        )
    active_other_run = await session.scalar(
        select(IngestRun.id)
        .where(
            IngestRun.revision_id == revision.id,
            IngestRun.stage.like(f"{_TYPED_REEXTRACTION_ROOT_PREFIX}%"),
            IngestRun.stage != typed_reextraction_stage(target_doc_type),
            IngestRun.status.in_(
                (IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)
            ),
        )
        .limit(1)
    )
    if active_other_run is not None:
        return TypedReextractionEligibility(
            False, "reclassification_in_progress", revision, existing_run
        )
    return TypedReextractionEligibility(True, None, revision, existing_run)


async def enqueue_typed_reextraction(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    target_doc_type: str,
) -> IngestRun:
    """Persist one retryable typed extraction intent for a revision and target."""
    kb = await session.get(
        KnowledgeBase,
        doc.kb_id,
        populate_existing=True,
        with_for_update={"read": True},
    )
    if kb is None or kb.org_id != doc.org_id:
        raise ValueError("document knowledge base does not exist")
    if str(getattr(kb.lifecycle_status, "value", kb.lifecycle_status)) != "active":
        raise ValueError("document knowledge base is not active")
    stage = typed_reextraction_stage(target_doc_type)
    existing = await session.scalar(
        select(IngestRun).where(
            IngestRun.revision_id == revision.id,
            IngestRun.stage == stage,
            IngestRun.segment_no == 0,
        )
    )
    existing_stats = (
        existing.stats
        if existing is not None and isinstance(existing.stats, dict)
        else {}
    )
    previous_doc_type = existing_stats.get("previous_doc_type", str(doc.doc_type))
    stats = {
        **existing_stats,
        "previous_doc_type": previous_doc_type,
        "target_doc_type": target_doc_type,
        "contract_version": TYPED_REEXTRACTION_CONTRACT_VERSION,
        "consumption_epoch": int(kb.consumption_epoch),
    }
    key = ingest_run_idempotency_key(revision.id, stage, 0)
    stmt = (
        pg_insert(IngestRun)
        .values(
            org_id=doc.org_id,
            kb_id=doc.kb_id,
            revision_id=revision.id,
            stage=stage,
            segment_no=0,
            status=IngestRunStatus.QUEUED.value,
            idempotency_key=key,
            stats=stats,
        )
        .on_conflict_do_update(
            constraint="uq_ingest_run_stage_segment",
            set_={
                "status": IngestRunStatus.QUEUED.value,
                "idempotency_key": key,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "available_at": None,
                "error": None,
                "finished_at": None,
                "stats": stats,
            },
            where=IngestRun.status.in_(
                (IngestRunStatus.FAILED.value, IngestRunStatus.CANCELED.value)
            ),
        )
        .returning(IngestRun.id)
    )
    run_id = (await session.execute(stmt)).scalar_one_or_none()
    if run_id is None:
        run_id = (
            await session.execute(
                select(IngestRun.id).where(
                    IngestRun.revision_id == revision.id,
                    IngestRun.stage == stage,
                    IngestRun.segment_no == 0,
                )
            )
        ).scalar_one()
    run = await session.get(IngestRun, run_id)
    if run is None:  # pragma: no cover - same transaction guarantees it
        raise RuntimeError(f"queued typed re-extraction run disappeared: {run_id}")
    return run


def _is_llm_throttled(exc: Exception) -> bool:
    """Identify budget/rate-limit failures from the sanitized structured contract."""
    if isinstance(exc, LlmBudgetExceededError):
        return True
    if isinstance(exc, ProviderError):
        return exc.code == "rate_limit" or exc.status_code == 429
    if isinstance(exc, AllProvidersFailedError):
        return any(
            diagnostic == "rate_limit"
            or diagnostic.startswith("rate_limit;")
            or ";status=429" in diagnostic
            for diagnostic in exc.errors
        )
    return False


def _schedule_throttle_retry(run_id: UUID, org_id: UUID) -> None:
    """限流文档的延迟重试:celery 模式用原生 countdown 延迟重派(broker 持久);
    inline 模式由可靠性 sweep 重派持久化的 QUEUED root，不创建进程内定时器。"""
    if get_settings().task_dispatch_mode != "celery":
        return
    try:
        # 延迟 import:celery 装配属于 runtime 层(MIGRATION-PLAN §5.7,P4 阶段搬运)。
        # 该模块尚未存在时 ImportError 与 broker 抖动同路径处理——状态已回 uploaded,
        # 可靠性 sweep / 人工重试兜底,不阻塞摄入。
        from nicekit.runtime.celery_app import celery_app

        celery_app.send_task(
            "kb.ingest_document",
            args=[str(run_id), str(org_id)],
            countdown=LLM_THROTTLE_RETRY_DELAY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - broker 抖动不升级,人工重试兜底
        logger.warning("限流重试派发失败(状态已回 uploaded,可人工重试): %s", exc)

# 结构化抽取的唯一 LLM 任务(MIGRATION-PLAN §5.5 B4/B5:TF 的四条行业专用契约
# EXTRACTION_SPECS 已删除)。所有 doc_type 一律走 generic 契约,字段约束运行时注入。
GENERIC_EXTRACTION_TASK = "kb.extract.generic"

# LLM 单次抽取的输入窗口上限(字符);超长文档分段抽取
MAX_EXTRACT_CHARS = 12000

# 实体图谱抽取任务(全类型统一,与 doc_type 无关)
_ENTITY_TASK = "kb.extract.entities"

# 各阶段在全局进度条上占的区间。图片链路与文本链路并行跑,但文本链路决定文档何时
# 进入可用终态,所以并行期间由文本链路独占进度条(见 _ProgressReporter.report);
# 文本收工后进度条让给还在跑的图片链路,图片阶段排在文本阶段之后。
# 无图文档直接把图片区间让给抽取,不会出现进度条卡在中段不动。
_STAGE_SPANS_WITH_IMAGES = {
    "parse": (0, 20),
    "chunk": (20, 50),
    "extract": (20, 50),
    "image": (50, 99),
}
_STAGE_SPANS_TEXT_ONLY = {
    "parse": (0, 40),
    "chunk": (40, 80),
    "extract": (40, 99),
}


class _ProgressReporter:
    """把阶段内的 done/total 折算成文档级 0-100 进度并落库。

    每次上报一次 UPDATE:摄入是分钟级的长任务,这点写入相对 LLM 与 OCR 可忽略,
    换来的是前端能显示"图片理解 76/244"而不是一条不动的空条。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        org_id: UUID,
        doc_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._org_id = org_id
        self._doc_id = doc_id
        self._spans = _STAGE_SPANS_TEXT_ONLY
        self._text_line_active = True

    def use_image_layout(self, has_images: bool) -> None:
        self._spans = (
            _STAGE_SPANS_WITH_IMAGES if has_images else _STAGE_SPANS_TEXT_ONLY
        )

    def release_to_image_line(self) -> None:
        """文本链路收工:把进度条交给可能还在跑的图片链路。"""
        self._text_line_active = False

    async def report(self, stage: str, done: int, total: int) -> None:
        if stage == "image" and self._text_line_active:
            # 两条链路并行上报会让 UI 上的阶段名来回跳。文本链路决定文档何时可用,
            # 并行期间让它独占进度条;这段时间图片的实时进展由 ingestion-status 的
            # image_stage(total/pending/processing/completed/...)如实体现。
            return
        start, end = self._spans.get(stage, (0, 99))
        ratio = (done / total) if total > 0 else 0.0
        progress = min(99, max(0, int(start + (end - start) * ratio)))
        # 独立 session:上报要能穿过主 session 正在跑的长事务,且不污染其状态
        async with org_session(self._session_factory, self._org_id) as session:
            await session.execute(
                sa_update(SourceDocument)
                .where(SourceDocument.id == self._doc_id)
                .values(
                    progress=progress,
                    progress_stage=stage,
                    progress_done=done,
                    progress_total=total,
                )
            )
            await session.commit()


async def _resolve_extraction_spec(
    session: AsyncSession,
    doc: SourceDocument,
    *,
    target_doc_type: str | None = None,
) -> tuple[str, type, str, KbEntityType]:
    """doc_type → 抽取规格。**永远返回 generic 契约**。

    SDK 化裁剪(MIGRATION-PLAN §5.5 B4/B5):TF 里内置四类走专用契约的分支已删除,
    doc_type 现在只是一个已注册实体类型 key —— 抽取任务恒为 kb.extract.generic,
    输出契约恒为 GenericEntityExtraction,字段约束由 KbEntityType.field_schema
    注入用户消息并在落库前用 jsonschema 强校验。未注册的类型直接报错,
    不再有"内置兜底",行业模型一律通过注册实体类型表达。
    """
    # P3b 延迟 import(见文件头说明)
    from nicekit.kb.entity_types import get_entity_type

    doc_type_key = target_doc_type or str(doc.doc_type)
    entity_type = await get_entity_type(session, doc.org_id, doc_type_key)
    if entity_type is None:
        raise ValueError(f"未注册的文档类型:{doc_type_key}")
    return (
        GENERIC_EXTRACTION_TASK,
        GenericEntityExtraction,
        entity_type.type_key,
        entity_type,
    )


def _generic_spec_block(entity_type: KbEntityType) -> str:
    """通用抽取的用户消息前缀:类型说明 + 字段 JSON Schema(强校验同源)。"""
    return (
        "ENTITY_TYPE_SPEC:\n"
        f"类型:{entity_type.type_key}({entity_type.display_name})\n"
        f"说明:{entity_type.description or '(无)'}\n"
        "attributes_json 必须符合的字段 JSON Schema:\n"
        f"{json.dumps(entity_type.field_schema, ensure_ascii=False)}\n\n"
    )

# ---- 并行度门闸(进程级;生产切 Celery 后由 worker 并发度接管)----------

_max_concurrency = 2
_active = 0
_gate: asyncio.Condition | None = None


def _get_gate() -> asyncio.Condition:
    global _gate
    if _gate is None:
        _gate = asyncio.Condition()
    return _gate


def get_max_concurrency() -> int:
    return _max_concurrency


async def set_max_concurrency(n: int) -> None:
    global _max_concurrency
    _max_concurrency = n
    async with _get_gate():
        _get_gate().notify_all()


def get_active_count() -> int:
    return _active


async def _acquire_slot() -> None:
    global _active
    gate = _get_gate()
    async with gate:
        while _active >= _max_concurrency:
            await gate.wait()
        _active += 1


async def _release_slot() -> None:
    global _active
    gate = _get_gate()
    async with gate:
        _active -= 1
        gate.notify_all()


# 人工请求停止的两种终态:取消丢弃续跑意图,暂停保留;两者都在阶段边界协作式生效
_STOP_REQUESTED_STATUSES = (DocStatus.CANCELED, DocStatus.PAUSED)


async def _stop_requested(session: AsyncSession, doc_id: UUID) -> bool:
    return await _db_status(session, doc_id) in _STOP_REQUESTED_STATUSES


async def _db_status(session: AsyncSession, doc_id: UUID) -> str:
    """绕过 identity map 直查 DB,读取其他事务提交的取消状态。"""
    return (
        await session.execute(
            select(SourceDocument.status).where(SourceDocument.id == doc_id)
        )
    ).scalar_one()


async def _load_profile(session: AsyncSession, kb_id: UUID) -> IngestProfile | None:
    """读所属 KB 的 ingest_profile;无配置返回 None(用默认),脏数据不炸摄入。"""
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or not kb.ingest_profile:
        return None
    try:
        return IngestProfile.model_validate(kb.ingest_profile)
    except ValidationError as exc:
        logger.warning("kb %s 的 ingest_profile 不合法,回退默认值: %s", kb_id, exc)
        return None


async def _claim_stage(
    session: AsyncSession,
    revision: DocumentRevision,
    *,
    stage: str,
    segment_no: int,
    lease_owner: str,
    captured_epoch: int,
):
    boundary_error = await _knowledge_base_boundary_error(
        session,
        kb_id=revision.kb_id,
        org_id=revision.org_id,
        captured_epoch=captured_epoch,
    )
    if boundary_error is not None:
        raise RuntimeError(boundary_error)
    claim = await claim_ingest_run(
        session,
        org_id=revision.org_id,
        kb_id=revision.kb_id,
        revision_id=revision.id,
        stage=stage,
        segment_no=segment_no,
        lease_owner=lease_owner,
    )
    run = await session.get(IngestRun, claim.run_id, populate_existing=True)
    if run is None:
        raise RuntimeError(f"claimed ingest run is missing: {claim.run_id}")
    if (
        not claim.acquired
        and claim.status in _STAGED_RUN_STATUSES
        and _captured_consumption_epoch(run) != captured_epoch
    ):
        await _invalidate_unpublished_fact_outputs(session, run_id=run.id)
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
            segment_no=segment_no,
            lease_owner=lease_owner,
        )
        if not claim.acquired:
            raise IngestRunBusyError(
                "stale ingest run could not be reclaimed: "
                f"{revision.id}:{stage}:{segment_no}"
            )
        run = await session.get(IngestRun, claim.run_id, populate_existing=True)
        if run is None:
            raise RuntimeError(f"reclaimed ingest run is missing: {claim.run_id}")
    if claim.acquired:
        existing_stats = run.stats if isinstance(run.stats, dict) else {}
        run.stats = {**existing_stats, "consumption_epoch": captured_epoch}
        session.add(run)
    await session.commit()
    if claim.acquired or claim.status in _STAGED_RUN_STATUSES:
        return claim
    raise IngestRunBusyError(
        f"ingest run is owned by another worker: {revision.id}:{stage}:{segment_no}"
    )


async def _fail_claimed_stage(
    session: AsyncSession, run_id: UUID, lease_owner: str, exc: BaseException
) -> None:
    await session.rollback()
    await fail_ingest_run(
        session,
        run_id=run_id,
        lease_owner=lease_owner,
        error=str(exc)[:2000],
    )
    await session.commit()


async def _load_staged_parse_artifacts(
    revision: DocumentRevision, parse_run: IngestRun
) -> str:
    """Load a staged parse only after validating its authoritative artifact."""
    if not revision.markdown_key:
        raise RuntimeError(f"staged parse run has no markdown artifact: {revision.id}")

    run_stats = parse_run.stats if isinstance(parse_run.stats, dict) else {}
    if run_stats.get("parser_name") == "docling":
        if not revision.structured_json_key:
            raise RuntimeError(
                f"staged Docling parse run has no structured artifact: {revision.id}"
            )
        structured_data = await storage.get_object(revision.structured_json_key)
        structured_document = json.loads(structured_data)
        if not isinstance(structured_document, dict):
            raise RuntimeError(
                f"staged Docling structured artifact is invalid: {revision.id}"
            )

    return (await storage.get_object(revision.markdown_key)).decode("utf-8")


async def ingest_document(
    run_id: UUID,
    org_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMService,
    embedder: EmbeddingService | None = None,
    image_enricher: "KbImageEnrichmentService | None" = None,
) -> None:
    # P3b defer-import (see module header note)
    from nicekit.kb.caption import CaptionModelSelection
    from nicekit.kb.image_enrichment import get_kb_image_enrichment_service
    from nicekit.kb.image_ingestion import (
        persist_image_candidates,
        process_revision_image_enrichment,
        revision_image_stage,
        summarize_image_assets,
    )

    session = org_session(session_factory, org_id)
    acquired = False
    lease_owner = f"ingest:{uuid4().hex}"
    attempt_id = hashlib.sha256(lease_owner.encode("utf-8")).hexdigest()[:32]
    root_lease = None
    try:
        root_run = await session.get(IngestRun, run_id)
        if root_run is None:
            logger.error("ingest: root ingest_run %s 不存在", run_id)
            return
        if root_run.stage != "document" or root_run.segment_no != 0:
            logger.error("ingest: root ingest_run %s 不存在或类型错误", run_id)
            return
        if root_run.status not in (IngestRunStatus.QUEUED, IngestRunStatus.RUNNING):
            logger.info("ingest: root run %s 状态 %s 不接受自动执行", run_id, root_run.status)
            return
        revision = await session.get(DocumentRevision, root_run.revision_id)
        if revision is None:
            logger.error("ingest: revision %s 不存在", root_run.revision_id)
            return
        doc = await session.get(SourceDocument, revision.doc_id)
        if doc is None:
            logger.error("ingest: source_document %s 不存在", revision.doc_id)
            return
        raw_root_stats = getattr(root_run, "stats", None)
        root_stats = dict(raw_root_stats) if isinstance(raw_root_stats, dict) else {}
        captured_epoch = _captured_consumption_epoch(root_run)
        boundary_error = await _knowledge_base_boundary_error(
            session,
            kb_id=doc.kb_id,
            org_id=org_id,
            captured_epoch=captured_epoch,
        )
        if boundary_error is not None:
            root_run.status = IngestRunStatus.CANCELED
            root_run.error = boundary_error
            root_run.finished_at = datetime.now(UTC)
            doc.status = DocStatus.CANCELED
            doc.error = boundary_error
            session.add_all([root_run, doc])
            from nicekit.kb.document_reingestion import (
                settle_reingestion_ingest_result,
            )

            await settle_reingestion_ingest_result(
                session,
                document=doc,
                revision=revision,
            )
            await session.commit()
            return
        doc_id = doc.id
        # 图片链路跑在自己的 session 上,只认这两个纯值:主 session 一旦 rollback
        # (阶段失败路径)会 expire 它的 ORM 副本,那时跨链路读属性会触发懒加载,
        # 把两条链路挤到同一个 AsyncSession 上并发操作。
        revision_id = revision.id
        await session.commit()  # 释放事务,排队等槽位期间不占连接快照

        await _acquire_slot()
        acquired = True
        root_claim = await _claim_stage(
            session,
            revision,
            stage="document",
            segment_no=0,
            lease_owner=lease_owner,
            captured_epoch=captured_epoch,
        )
        if not root_claim.acquired:
            return
        boundary_error = await _knowledge_base_boundary_error(
            session,
            kb_id=doc.kb_id,
            org_id=org_id,
            captured_epoch=captured_epoch,
        )
        if boundary_error is not None:
            doc.status = DocStatus.CANCELED
            doc.error = boundary_error
            session.add(doc)
            canceled_root = await cancel_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                reason=boundary_error,
            )
            if not canceled_root:
                await session.rollback()
                raise IngestLeaseLostError(str(root_claim.run_id))
            from nicekit.kb.document_reingestion import (
                settle_reingestion_ingest_result,
            )

            await settle_reingestion_ingest_result(
                session,
                document=doc,
                revision=revision,
            )
            await session.commit()
            return
        if await _stop_requested(session, doc_id):
            canceled_root = await cancel_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                reason="source document canceled or paused before execution",
            )
            if not canceled_root:
                await session.rollback()
                raise IngestLeaseLostError(str(root_claim.run_id))
            await session.commit()
            return
        root_lease_context = maintain_ingest_lease(
            session_factory,
            org_id=org_id,
            run_id=root_claim.run_id,
            lease_owner=lease_owner,
        )
        await root_lease_context.__aenter__()
        root_lease = root_lease_context
        revision.status = RevisionStatus.PARSING
        revision.error = None
        doc.status = DocStatus.PARSING
        doc.progress = 0
        doc.progress_stage = None
        doc.progress_done = 0
        doc.progress_total = 0
        doc.parsing_started_at = datetime.now(UTC)
        session.add_all([doc, revision])
        await session.commit()

        canceled = False
        final_boundary_error: str | None = None
        profile: IngestProfile | None = None
        progress = _ProgressReporter(
            session_factory, org_id=org_id, doc_id=doc_id
        )
        try:
            profile = await _load_profile(session, doc.kb_id)
            # 解析是不可分割的一段(Docling 内部无回调),只标阶段:UI 至少知道在做什么
            await progress.report("parse", 0, 1)
            parse_claim = await _claim_stage(
                session,
                revision,
                stage="parse",
                segment_no=0,
                lease_owner=lease_owner,
                captured_epoch=captured_epoch,
            )
            if parse_claim.acquired:
                try:
                    async with maintain_ingest_lease(
                        session_factory,
                        org_id=org_id,
                        run_id=parse_claim.run_id,
                        lease_owner=lease_owner,
                    ):
                        data = await storage.get_object(revision.original_object_key)
                        # Parsing may be CPU-bound; keep the event loop available so
                        # the independent lease heartbeat can keep running.
                        backend = profile.parser if profile is not None else None
                        parsed = await anyio.to_thread.run_sync(
                            parse_document, data, doc.filename, backend
                        )
                        boundary_error = await _knowledge_base_boundary_error(
                            session,
                            kb_id=doc.kb_id,
                            org_id=org_id,
                            captured_epoch=captured_epoch,
                            lock=True,
                        )
                        if boundary_error is not None:
                            raise RuntimeError(boundary_error)
                        structured_json_key: str | None = None
                        if parsed.structured_json is not None:
                            structured_json_key = storage.kb_revision_structured_json_key(
                                doc.org_id,
                                doc.kb_id,
                                doc.id,
                                revision.id,
                                attempt_id,
                            )
                            await storage.put_object(
                                structured_json_key,
                                json.dumps(
                                    parsed.structured_json,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ).encode("utf-8"),
                                "application/json; charset=utf-8",
                            )
                        # 图片先入库再定稿 markdown:解析期锚点要换成真实
                        # asset_id,而装饰性图标与校验失败的图不产出可寻址资产,
                        # 只有入库结果能决定哪些锚点该落成图片引用。
                        image_assets = await persist_image_candidates(
                            session,
                            doc=doc,
                            revision=revision,
                            ingest_run_id=parse_claim.run_id,
                            parser_name=parsed.parser_name,
                            candidates=parsed.images,
                        )
                        image_stage = summarize_image_assets(image_assets)
                        markdown_refs = {
                            asset.source_occurrence: ImageMarkdownRef(
                                asset_id=asset.id, page=asset.page
                            )
                            for asset in image_assets
                            if asset.original_object_key is not None
                        }
                        anchors = parsed_image_occurrences(parsed.markdown)
                        markdown = resolve_parsed_image_placeholders(
                            parsed.markdown, markdown_refs
                        )
                        markdown_key = storage.kb_revision_markdown_key(
                            doc.org_id, doc.kb_id, doc.id, revision.id, attempt_id
                        )
                        await storage.put_object(
                            markdown_key,
                            markdown.encode("utf-8"),
                            "text/markdown; charset=utf-8",
                        )
                    boundary_error = await _knowledge_base_boundary_error(
                        session,
                        kb_id=doc.kb_id,
                        org_id=org_id,
                        captured_epoch=captured_epoch,
                        lock=True,
                    )
                    if boundary_error is not None:
                        raise RuntimeError(boundary_error)
                    revision.structured_json_key = structured_json_key
                    revision.markdown_key = markdown_key
                    doc.markdown_key = markdown_key
                    doc.parser_name = parsed.parser_name
                    session.add_all([doc, revision])
                    completed = await complete_ingest_run(
                        session,
                        run_id=parse_claim.run_id,
                        lease_owner=lease_owner,
                        status=IngestRunStatus.STAGED,
                        stats={
                            "consumption_epoch": captured_epoch,
                            "page_count": parsed.page_count,
                            "parser_name": parsed.parser_name,
                            "image_candidates": image_stage.total,
                            "image_filtered_decorative": (
                                len(parsed.images) - len(image_assets)
                            ),
                            # 正文里的插图锚点总数与其中真正接上资产的条数:
                            # 两者不等即说明有图只进了图库、没进阅读/检索主链路
                            "image_markdown_anchors": len(anchors),
                            "image_markdown_refs": sum(
                                occurrence in markdown_refs for occurrence in anchors
                            ),
                            "image_stage": image_stage.state,
                            "structured_artifact": {
                                "available": structured_json_key is not None,
                                "schema_name": (
                                    parsed.structured_json.get("schema_name")
                                    if parsed.structured_json is not None
                                    else None
                                ),
                                "schema_version": (
                                    parsed.structured_json.get("version")
                                    if parsed.structured_json is not None
                                    else None
                                ),
                            },
                        },
                    )
                    if not completed:
                        raise IngestLeaseLostError(str(parse_claim.run_id))
                    await session.commit()
                    full_text = markdown
                except Exception as exc:
                    await _fail_claimed_stage(
                        session, parse_claim.run_id, lease_owner, exc
                    )
                    raise
            else:
                parse_run = await session.get(IngestRun, parse_claim.run_id)
                if parse_run is None:
                    raise RuntimeError(f"staged parse run is missing: {parse_claim.run_id}")
                full_text = await _load_staged_parse_artifacts(revision, parse_run)
                doc.markdown_key = revision.markdown_key

            image_stage = await revision_image_stage(session, revision.id)
            progress.use_image_layout(bool(image_stage.total))
            image_failed = False

            async def text_line() -> None:
                """文本链路:跑在主 session 上,独占 doc/revision 的写权,决定 doc.status。

                图片链路只读这两个 ORM 对象已加载的列(sessionmaker 为
                expire_on_commit=False),不会跟这里的写并发。
                """
                nonlocal canceled
                try:
                    if doc.doc_type == DocType.GENERAL:
                        await progress.report("chunk", 0, 1)
                        chunk_claim = await _claim_stage(
                            session,
                            revision,
                            stage="chunk",
                            segment_no=0,
                            lease_owner=lease_owner,
                            captured_epoch=captured_epoch,
                        )
                        if chunk_claim.acquired:
                            try:
                                async with maintain_ingest_lease(
                                    session_factory,
                                    org_id=org_id,
                                    run_id=chunk_claim.run_id,
                                    lease_owner=lease_owner,
                                ):
                                    staged_chunks = await _prepare_chunks(
                                        doc, full_text, embedder, profile, llm=llm
                                    )
                                    boundary_error = (
                                        await _knowledge_base_boundary_error(
                                            session,
                                            kb_id=doc.kb_id,
                                            org_id=org_id,
                                            captured_epoch=captured_epoch,
                                            lock=True,
                                        )
                                    )
                                    if boundary_error is not None:
                                        raise RuntimeError(boundary_error)
                                    chunk_artifact_key = storage.kb_revision_chunks_key(
                                        doc.org_id,
                                        doc.kb_id,
                                        doc.id,
                                        revision.id,
                                        attempt_id,
                                    )
                                    await storage.put_object(
                                        chunk_artifact_key,
                                        json.dumps(
                                            [
                                                _chunk_staging_payload(row)
                                                for row in staged_chunks
                                            ],
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                        ).encode("utf-8"),
                                        "application/json",
                                    )
                                boundary_error = await _knowledge_base_boundary_error(
                                    session,
                                    kb_id=doc.kb_id,
                                    org_id=org_id,
                                    captured_epoch=captured_epoch,
                                    lock=True,
                                )
                                if boundary_error is not None:
                                    raise RuntimeError(boundary_error)
                                completed = await complete_ingest_run(
                                    session,
                                    run_id=chunk_claim.run_id,
                                    lease_owner=lease_owner,
                                    status=IngestRunStatus.STAGED,
                                    stats={
                                        "consumption_epoch": captured_epoch,
                                        "artifact_key": chunk_artifact_key,
                                        "chunk_count": len(staged_chunks),
                                    },
                                )
                                if not completed:
                                    raise IngestLeaseLostError(str(chunk_claim.run_id))
                                await session.commit()
                            except Exception as exc:
                                await _fail_claimed_stage(
                                    session, chunk_claim.run_id, lease_owner, exc
                                )
                                raise
                        doc.status = DocStatus.COMPLETED
                        doc.progress = 100
                    else:
                        # 业务事实与图谱事实是两条互不依赖的抽取线,并行跑。图谱线用自己的
                        # session:AsyncSession 不允许并发操作,共用主 session 会直接报错。
                        async def graph_line() -> None:
                            async with org_session(
                                session_factory, org_id
                            ) as graph_session:
                                await _maybe_ingest_entity_graph(
                                    graph_session,
                                    doc,
                                    full_text,
                                    llm,
                                    revision=revision,
                                    session_factory=session_factory,
                                    lease_owner=lease_owner,
                                    profile=profile,
                                    captured_epoch=captured_epoch,
                                )

                        (count, canceled), _ = await asyncio.gather(
                            _ingest_structured(
                                session,
                                doc,
                                full_text,
                                llm,
                                revision=revision,
                                session_factory=session_factory,
                                lease_owner=lease_owner,
                                progress=progress,
                                captured_epoch=captured_epoch,
                            ),
                            graph_line(),
                        )
                        if canceled:
                            # 停止请求的真实终态(canceled/paused)由收尾统一按 DB 判定
                            doc.status = DocStatus.CANCELED
                        else:
                            doc.status = (
                                DocStatus.AWAITING_REVIEW if count else DocStatus.COMPLETED
                            )
                            doc.progress = 100
                    if not canceled:
                        if doc.doc_type == DocType.GENERAL:
                            # general 没有结构化抽取线可并行,图谱在这里单独跑
                            await _maybe_ingest_entity_graph(
                                session,
                                doc,
                                full_text,
                                llm,
                                revision=revision,
                                session_factory=session_factory,
                                lease_owner=lease_owner,
                                profile=profile,
                                captured_epoch=captured_epoch,
                            )
                        # 图谱事实与业务事实并列产出;含 general,故待审计数在此重算
                        if (
                            doc.status == DocStatus.COMPLETED
                            and await _pending_fact_claim_count(session, revision.id)
                        ):
                            doc.status = DocStatus.AWAITING_REVIEW
                finally:
                    progress.release_to_image_line()

            async def image_line() -> None:
                """图片链路:独立 session、best-effort,只决定 image_stage 与发布门禁。

                AsyncSession 不允许并发操作,所以这里必须开自己的 session(与
                graph_line 同一范式)。失败只标 image_failed 记日志,不反噬文本链路
                —— 与 wiki 自动生成、实体图谱抽取既有口径一致。
                """
                nonlocal image_stage, image_failed
                if not image_stage.total:
                    return
                effective_enricher = (
                    image_enricher
                    if image_enricher is not None
                    else get_kb_image_enrichment_service(llm)
                )
                await progress.report("image", 0, image_stage.total)
                try:
                    async with org_session(session_factory, org_id) as image_session:
                        # 在自己的 session 上重新取 doc/revision:不与文本链路共用 ORM
                        # 副本,文本链路的 commit/rollback 就影响不到这里
                        image_revision = await image_session.get(
                            DocumentRevision, revision_id
                        )
                        image_doc = await image_session.get(SourceDocument, doc_id)
                        if image_revision is None or image_doc is None:
                            raise RuntimeError(
                                f"image line lost its document rows: {revision_id}"
                            )
                        image_stage = await process_revision_image_enrichment(
                            image_session,
                            session_factory=session_factory,
                            revision=image_revision,
                            doc=image_doc,
                            lease_owner=lease_owner,
                            enricher=effective_enricher,
                            on_progress=lambda done, total: progress.report(
                                "image", done, total
                            ),
                            caption_selection=(
                                CaptionModelSelection(
                                    provider=profile.caption_provider,
                                    model=profile.caption_model,
                                )
                                if profile is not None
                                and profile.caption_provider is not None
                                and profile.caption_model is not None
                                else None
                            ),
                            caption_enabled=(
                                profile.caption_images if profile is not None else True
                            ),
                            captured_epoch=captured_epoch,
                        )
                except Exception:  # noqa: BLE001 - 图片是增强项,失败不反噬摄入状态
                    logger.exception("图片富化失败(不影响文本链路状态): doc=%s", doc_id)
                    image_failed = True
                    async with org_session(session_factory, org_id) as probe:
                        # 整条链路挂掉时投影可能还停在开工前的快照,按 DB 重算一次,
                        # 让 root run stats 与发布门禁看到的是真实的图片进度
                        image_stage = await revision_image_stage(probe, revision_id)

            # 两条链路并行:文本决定文档终态,图片只决定 image_stage 与发布门禁。
            # return_exceptions=True 是必须的 —— 默认 gather 会在第一个异常处直接抛,
            # 把另一条链路留成脱管的后台任务,它还持着 session 与租约,函数返回后再
            # 动就会踩已关闭的连接。这里等两条都收尾,再按各自语义分别处理异常。
            text_outcome, image_outcome = await asyncio.gather(
                text_line(), image_line(), return_exceptions=True
            )
            if isinstance(text_outcome, BaseException):
                raise text_outcome
            if isinstance(image_outcome, BaseException):
                # image_line 自己吞掉 Exception,能到这里的只有 BaseException
                raise image_outcome

            # 图片阶段不再绑架文档状态:pending/processing 不再把文档拉回 PARSING
            # (那正是"文本已切好却不可用"的根因),failed 也不再打成 FAILED。
            # 发布门禁(ImageStageProjection.publishable / 快照激活)保持严格不变。
            doc.error = None
            if image_failed or image_stage.state == "failed":
                # 失败必须可见可重试:状态保持文本链路的结论,失败挂在 doc.error 上,
                # root run 收成 failed(见收尾)以便 retry 重新入队补跑图片;
                # 具体哪张图失败见 image_enrich:* 的 ingest_run 与资产 failure_code
                doc.error = IMAGE_ENRICHMENT_ERROR
            elif image_stage.state == "needs_review":
                # 有图片等人工看:这条语义保留,升级为待审核
                doc.status = DocStatus.AWAITING_REVIEW
        except Exception as exc:  # noqa: BLE001 - 状态必须落库,不能让 worker 静默吞掉
            if _is_llm_throttled(exc):
                # 限流/配额不是文档的错(KB-5B):回 uploaded 可重试态,
                # celery 模式延迟自动重派,inline 模式如实留给人工重试
                logger.warning("ingest %s 遇 LLM 限流/配额,回 uploaded 待重试: %s", doc_id, exc)
                doc.status = DocStatus.UPLOADED
                doc.progress = 0
                doc.error = LLM_THROTTLE_ERROR
            else:
                logger.exception("ingest %s 失败", doc_id)
                doc.status = DocStatus.FAILED
                doc.error = str(exc)[:2000]
        # 最后一段期间的取消/暂停不被覆盖。停止的真实终态以 DB 为准:worker 只知道
        # "该停了",是取消还是暂停由发起方写在 source_documents.status 上。
        requested_stop = await _db_status(session, doc_id)
        if requested_stop == DocStatus.CANCELED:
            doc.status = DocStatus.CANCELED
        elif requested_stop == DocStatus.PAUSED and doc.status not in (
            DocStatus.COMPLETED,
            DocStatus.AWAITING_REVIEW,
        ):
            # 暂停不丢弃已跑完的成果:恰好在暂停生效前完成的文档保持成功终态
            doc.status = DocStatus.PAUSED
        if doc.status in (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW):
            final_boundary_error = await _knowledge_base_boundary_error(
                session,
                kb_id=doc.kb_id,
                org_id=org_id,
                captured_epoch=captured_epoch,
                lock=True,
            )
            if final_boundary_error is not None:
                doc.status = DocStatus.CANCELED
                doc.error = final_boundary_error
        if doc.status in (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW):
            revision.status = RevisionStatus.STAGED
            # 文本产物确实已 staged;图片链路的失败标记原样带上,不改 staged 结论
            # (正常路径 doc.error 为 None,与旧行为逐字节一致)
            revision.error = doc.error
        elif doc.status == DocStatus.FAILED:
            revision.status = RevisionStatus.FAILED
            revision.error = doc.error
        else:
            revision.status = RevisionStatus.UPLOADED
            revision.error = doc.error
        session.add_all([doc, revision])
        # 进度由 _ProgressReporter 走独立 session 写入,主 session 的 doc 副本对此
        # 一无所知:赋回同值不产生 dirty,ORM 就不会发 UPDATE,终态会残留
        # "图片理解 76/244"。所以离开 parsing 时必须显式 UPDATE 收口。
        if doc.status != DocStatus.PARSING:
            await session.execute(
                sa_update(SourceDocument)
                .where(SourceDocument.id == doc_id)
                .values(
                    progress=(
                        100
                        if doc.status
                        in (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW)
                        else 0
                    ),
                    progress_stage=None,
                    progress_done=0,
                    progress_total=0,
                )
            )
        lease_to_close = root_lease
        root_lease = None
        await lease_to_close.__aexit__(None, None, None)
        throttled = doc.error == LLM_THROTTLE_ERROR
        # 图片失败时文档保持文本链路的终态,但这次摄入尝试确实没跑全:root run 如实
        # 收成 failed,既让 ingestion-status 上看得见,也是 retry 能重新入队(见
        # enqueue_document_ingestion 的 on_conflict where)补跑图片的唯一前提。
        image_enrichment_failed = doc.error == IMAGE_ENRICHMENT_ERROR
        if throttled:
            root_finished = await requeue_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                reason=LLM_THROTTLE_ERROR,
                delay_seconds=LLM_THROTTLE_RETRY_DELAY_SECONDS,
            )
        elif (
            doc.status in (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW)
            and not image_enrichment_failed
        ):
            root_finished = await complete_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                status=IngestRunStatus.SUCCEEDED,
                stats={
                    **root_stats,
                    "document_status": DocStatus(doc.status).value,
                    "image_stage": image_stage.state,
                    "image_total": image_stage.total,
                    "image_publishable": image_stage.publishable,
                },
            )
        elif final_boundary_error is not None:
            root_finished = await cancel_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                reason=final_boundary_error,
            )
        elif doc.status in _STOP_REQUESTED_STATUSES:
            # 暂停与取消都收掉 root run;续跑由 resume 重新入队,
            # 各阶段的 staged 产物靠幂等键复用,不会重复花 LLM/OCR 的钱
            root_finished = await cancel_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                reason=f"source document {DocStatus(doc.status).value}",
            )
        else:
            root_finished = await fail_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                error=doc.error or "source document ingestion failed",
            )
        if not root_finished:
            await session.rollback()
            raise IngestLeaseLostError(str(root_claim.run_id))
        from nicekit.kb.document_reingestion import (
            settle_reingestion_ingest_result,
        )

        await settle_reingestion_ingest_result(
            session,
            document=doc,
            revision=revision,
        )
        await session.commit()
        if throttled:
            _schedule_throttle_retry(root_claim.run_id, org_id)
        # KB-5A:摄入进入成功终态后 best-effort 触发 wiki 自动生成
        if doc.status in (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW):
            await _maybe_update_wiki(
                doc, org_id, session_factory=session_factory, llm=llm, profile=profile
            )
    finally:
        try:
            if root_lease is not None:
                await root_lease.__aexit__(None, None, None)
        finally:
            if acquired:
                await _release_slot()
            await session.close()


async def reextract_document(
    run_id: UUID,
    org_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMService,
) -> None:
    """Run additive typed extraction from an immutable staged parse artifact."""
    session = org_session(session_factory, org_id)
    lease_owner = f"reextract:{uuid4().hex}"
    acquired = False
    try:
        root_run = await session.get(IngestRun, run_id)
        if root_run is None:
            logger.error("typed re-extraction run %s does not exist", run_id)
            return
        if (
            not root_run.stage.startswith(_TYPED_REEXTRACTION_ROOT_PREFIX)
            or root_run.segment_no != 0
        ):
            logger.error("run %s is not a typed re-extraction root", run_id)
            return
        if str(root_run.status) not in {
            IngestRunStatus.QUEUED.value,
            IngestRunStatus.RUNNING.value,
        }:
            logger.info(
                "typed re-extraction run %s is already %s",
                run_id,
                root_run.status,
            )
            return
        revision = await session.get(DocumentRevision, root_run.revision_id)
        if revision is None:
            logger.error(
                "typed re-extraction revision %s does not exist",
                root_run.revision_id,
            )
            return
        doc = await session.get(SourceDocument, revision.doc_id)
        if doc is None:
            logger.error("typed re-extraction document %s does not exist", revision.doc_id)
            return
        stats = root_run.stats if isinstance(root_run.stats, dict) else {}
        target_doc_type = stats.get("target_doc_type")
        if not isinstance(target_doc_type, str) or not target_doc_type:
            logger.error("typed re-extraction run %s has no target type", run_id)
            return
        expected_stage = typed_reextraction_stage(target_doc_type)
        if root_run.stage != expected_stage:
            logger.error("typed re-extraction run %s target does not match stage", run_id)
            return
        captured_epoch = _captured_consumption_epoch(root_run)
        boundary_error = await _knowledge_base_boundary_error(
            session,
            kb_id=doc.kb_id,
            org_id=org_id,
            captured_epoch=captured_epoch,
        )
        if boundary_error is not None:
            root_run.status = IngestRunStatus.CANCELED
            root_run.error = boundary_error
            root_run.finished_at = datetime.now(UTC)
            session.add(root_run)
            await session.commit()
            return
        await session.commit()

        await _acquire_slot()
        acquired = True
        root_claim = await _claim_stage(
            session,
            revision,
            stage=expected_stage,
            segment_no=0,
            lease_owner=lease_owner,
            captured_epoch=captured_epoch,
        )
        if not root_claim.acquired:
            return
        try:
            boundary_error = await _knowledge_base_boundary_error(
                session,
                kb_id=doc.kb_id,
                org_id=org_id,
                captured_epoch=captured_epoch,
            )
            if boundary_error is not None:
                raise RuntimeError(boundary_error)
            lifecycle = str(
                getattr(doc.lifecycle_status, "value", doc.lifecycle_status)
            )
            if lifecycle != DocumentLifecycleStatus.ACTIVE.value:
                raise RuntimeError("document_not_active")
            revision_status = str(
                getattr(revision.status, "value", revision.status)
            )
            if revision_status == RevisionStatus.TOMBSTONED.value:
                raise RuntimeError("revision_tombstoned")
            if revision_status not in {
                RevisionStatus.STAGED.value,
                RevisionStatus.ACTIVE.value,
            }:
                raise RuntimeError("revision_not_staged")
            parse_run = await session.scalar(
                select(IngestRun).where(
                    IngestRun.revision_id == revision.id,
                    IngestRun.stage == "parse",
                    IngestRun.segment_no == 0,
                    IngestRun.status.in_(
                        (
                            IngestRunStatus.STAGED.value,
                            IngestRunStatus.SUCCEEDED.value,
                        )
                    ),
                )
            )
            if parse_run is None:
                raise RuntimeError("parse_artifact_not_staged")
            full_text = await _load_staged_parse_artifacts(revision, parse_run)
            child_stage = typed_extraction_stage(target_doc_type)
            async with maintain_ingest_lease(
                session_factory,
                org_id=org_id,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
            ):
                pending_count, canceled = await _ingest_structured(
                    session,
                    doc,
                    full_text,
                    llm,
                    revision=revision,
                    session_factory=session_factory,
                    lease_owner=lease_owner,
                    progress=None,
                    target_doc_type=target_doc_type,
                    stage=child_stage,
                    captured_epoch=captured_epoch,
                )
            if canceled:
                raise RuntimeError("document_stop_requested")
            child_runs = list(
                (
                    await session.scalars(
                        select(IngestRun).where(
                            IngestRun.revision_id == revision.id,
                            IngestRun.stage == child_stage,
                        )
                    )
                ).all()
            )
            created_claims = sum(
                int(run.stats.get("created_claims", 0))
                for run in child_runs
                if isinstance(run.stats, dict)
            )
            boundary_error = await _knowledge_base_boundary_error(
                session,
                kb_id=doc.kb_id,
                org_id=org_id,
                captured_epoch=captured_epoch,
                lock=True,
            )
            if boundary_error is not None:
                for child_run in child_runs:
                    if _captured_consumption_epoch(child_run) != captured_epoch:
                        continue
                    await _invalidate_unpublished_fact_outputs(
                        session,
                        run_id=child_run.id,
                    )
                    child_run.status = IngestRunStatus.FAILED
                    child_run.error = boundary_error
                    child_run.lease_owner = None
                    child_run.lease_expires_at = None
                    session.add(child_run)
                # Persist invalidation before the generic root failure handler
                # rolls back its own transaction.
                await session.commit()
                raise RuntimeError(boundary_error)
            if pending_count and str(doc.status) == DocStatus.COMPLETED.value:
                doc.status = DocStatus.AWAITING_REVIEW
                session.add(doc)
            completed = await complete_ingest_run(
                session,
                run_id=root_claim.run_id,
                lease_owner=lease_owner,
                status=IngestRunStatus.SUCCEEDED,
                stats={
                    **stats,
                    "created_claims": created_claims,
                    "segment_count": len(child_runs),
                    "pending_revision_claims": pending_count,
                },
            )
            if not completed:
                raise IngestLeaseLostError(str(root_claim.run_id))
            await session.commit()
        except Exception as exc:
            await _fail_claimed_stage(session, root_claim.run_id, lease_owner, exc)
            raise
    except Exception:  # noqa: BLE001 - durable run already holds sanitized failure
        logger.exception("typed re-extraction failed: run=%s", run_id)
    finally:
        if acquired:
            await _release_slot()
        await session.close()


async def _maybe_update_wiki(
    doc: SourceDocument,
    org_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMService,
    profile: IngestProfile | None,
) -> None:
    """best-effort:profile.auto_wiki(无 profile 默认开)时更新 wiki 页;
    任何失败只打日志,绝不影响已落库的摄入状态。LLM 走同一 org 预算/并发护栏。"""
    auto_wiki = profile.auto_wiki if profile is not None else IngestProfile().auto_wiki
    if not auto_wiki:
        return
    # P3b 延迟 import(见文件头说明)
    from nicekit.kb.wiki_gen import WikiSnapshotManagedError, update_wiki_for_document

    try:
        result = await update_wiki_for_document(
            doc.id, org_id, session_factory=session_factory, llm=llm
        )
        logger.info(
            "wiki 自动生成完成: doc=%s created=%s updated=%s warnings=%s",
            doc.id, result.created, result.updated, result.warnings,
        )
    except WikiSnapshotManagedError:
        logger.info("跳过旧 Wiki 自动生成：知识库已由快照管理 doc=%s", doc.id)
    except Exception:  # noqa: BLE001 - wiki 生成是增强项,失败不反噬摄入状态
        logger.exception("wiki 自动生成失败(不影响摄入状态): doc=%s", doc.id)


async def _enqueue_graph_claim(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    ingest_run_id: UUID,
    predicate: str,
    payload: dict,
    segment: ExtractionSegment,
    model_name: str,
    prompt_version: str,
) -> bool:
    """落一条图谱事实,证据定位不到就丢弃该条并返回 False。

    与结构化抽取不同:那里定位失败要让整段失败(结构化事实必须可追溯),
    而图谱是增强项,单条引文对不上原文时丢弃它比让整篇文档摄入失败更合适。
    """
    # P3b 延迟 import(见文件头说明)
    from nicekit.kb.evidence_locator import EvidenceNotFoundError

    try:
        await _enqueue_fact_claim(
            session,
            doc=doc,
            revision=revision,
            ingest_run_id=ingest_run_id,
            entity_type=predicate,
            payload=payload,
            segment_markdown=segment.source_text,
            line_offset=segment.line_offset,
            structured_document=None,
            model_name=model_name,
            prompt_version=prompt_version,
        )
    except (EvidenceNotFoundError, ValueError) as exc:
        logger.info(
            "图谱事实证据定位失败,丢弃该条(doc=%s predicate=%s): %s",
            doc.id, predicate, exc,
        )
        return False
    return True


def _entity_key(name: str) -> str | None:
    """实体名的段内去重键(与 entity_binding 归一同一套规范化)。"""
    # P3b defer-import (see module header note)
    from nicekit.kb.entity_resolution import EntityConflictError, normalize_alias

    try:
        return normalize_alias(name)
    except EntityConflictError:
        return None


def _entity_spec_block(allowed: dict[str, str]) -> str:
    """实体抽取的用户消息前缀:类型白名单 + 关系谓词说明(与落库校验同源)。"""
    # P3b defer-import (see module header note)
    from nicekit.kb.entity_binding import RELATION_PREDICATES

    types = "\n".join(f"- {key}:{desc}" for key, desc in sorted(allowed.items()))
    predicates = "、".join(sorted(RELATION_PREDICATES))
    return (
        "ENTITY_TYPE_WHITELIST(type_key 只能取以下之一):\n"
        f"{types}\n\n"
        "RELATION_SPEC(predicate 只能取以下之一):\n"
        f"{predicates}\n"
        "located_in=位于/隶属于某地,part_of=属于某个整体,includes=包含下级,"
        "serves=服务于/受理,supports=支持/提供支撑,near=邻近,"
        "derived_from=来源于,related=其它明确关联。\n\n"
    )


async def _ingest_entity_graph(
    session: AsyncSession,
    doc: SourceDocument,
    full_text: str,
    llm: LLMService,
    *,
    revision: DocumentRevision,
    session_factory: async_sessionmaker[AsyncSession],
    lease_owner: str,
    captured_epoch: int,
) -> int:
    """抽取实体与实体间关系(全类型统一,含 general);返回新增待审事实数。

    与结构化抽取并列的一道:结构化抽取产出注册实体类型的字段事实,
    这里产出图谱事实 —— 实体事实(predicate=entity_mention)与关系事实
    (predicate 取 GraphPredicate)。两者都不进 SQL/wiki 投影,只在 AI 审核
    判定通过时归一绑定到 canonical entity(entity_binding),快照 graph 投影
    据此连直接边与共现边,同名实体跨文档归一到同一节点即完成串联。
    """
    # P3b defer-import (see module header note)
    from nicekit.kb.entity_binding import allowed_entity_types

    allowed = await allowed_entity_types(session, doc.org_id)
    spec_prefix = _entity_spec_block(allowed)
    segments = split_for_extraction(
        full_text, MAX_EXTRACT_CHARS, include_offsets=True
    ) or [
        ExtractionSegment(
            heading_path="", content=full_text, source_text=full_text, line_offset=0
        )
    ]
    for extraction_segment in segments:
        if not isinstance(extraction_segment, ExtractionSegment):
            raise TypeError("extraction splitter must provide stable line offsets")
    await session.commit()

    semaphore = asyncio.Semaphore(max(1, get_settings().kb_extract_concurrency))
    canceled = asyncio.Event()

    async def graph_segment(idx: int, extraction_segment: ExtractionSegment) -> int:
        async with semaphore:
            if canceled.is_set():
                return 0
            async with org_session(session_factory, doc.org_id) as worker:
                if await _stop_requested(worker, doc.id):
                    canceled.set()
                    return 0
                return await _graph_one_segment(
                    worker,
                    doc=doc,
                    revision=revision,
                    session_factory=session_factory,
                    lease_owner=lease_owner,
                    llm=llm,
                    idx=idx,
                    total=len(segments),
                    extraction_segment=extraction_segment,
                    spec_prefix=spec_prefix,
                    allowed=allowed,
                    captured_epoch=captured_epoch,
                )

    counts = await asyncio.gather(
        *(
            graph_segment(idx, extraction_segment)
            for idx, extraction_segment in enumerate(segments)
        ),
        return_exceptions=True,
    )
    created_total = 0
    for outcome in counts:
        if isinstance(outcome, BaseException):
            raise outcome
        created_total += outcome
    return created_total


async def _graph_one_segment(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    session_factory: async_sessionmaker[AsyncSession],
    lease_owner: str,
    llm: LLMService,
    idx: int,
    total: int,
    extraction_segment: ExtractionSegment,
    spec_prefix: str,
    allowed: dict[str, str],
    captured_epoch: int,
) -> int:
    """Claim, extract and stage graph facts for exactly one segment."""
    # P3b defer-import (see module header note)
    from nicekit.kb.entity_binding import ENTITY_PREDICATE, RELATION_PREDICATES

    claim = await _claim_stage(
        session,
        revision,
        stage="entity_extract",
        segment_no=idx,
        lease_owner=lease_owner,
        captured_epoch=captured_epoch,
    )
    if not claim.acquired:
        return 0
    try:
        async with maintain_ingest_lease(
            session_factory,
            org_id=doc.org_id,
            run_id=claim.run_id,
            lease_owner=lease_owner,
        ):
            generation = await llm.generate_structured_with_metadata(
                task=_ENTITY_TASK,
                messages=[
                    {
                        "role": "user",
                        "content": spec_prefix
                        + fence_untrusted_document(
                            extraction_segment.content,
                            label=(
                                f"source document segment {idx + 1}/{total}"
                            ),
                        ),
                    }
                ],
                output_model=EntityGraphExtraction,
                org_id=doc.org_id,
            )
            result = generation.parsed
        model_name = f"{generation.provider}:{generation.model}"
        prompt_version = f"{_ENTITY_TASK}:v{generation.prompt_version}"
        created = 0
        dropped = 0
        entities_by_key: dict[str, ExtractedEntity] = {}
        for item in result.entities:
            key = _entity_key(item.name)
            if key is None or item.type_key not in allowed:
                dropped += 1  # 空名或越界类型:不让模型自造类型污染实体库
                continue
            if key in entities_by_key:
                continue  # 段内同名只留一条,跨文档归一由 entity_binding 负责
            staged = await _enqueue_graph_claim(
                session,
                doc=doc,
                revision=revision,
                ingest_run_id=claim.run_id,
                predicate=ENTITY_PREDICATE,
                payload={
                    "name": item.name.strip(),
                    "type_key": item.type_key,
                    "aliases": [alias.strip() for alias in item.aliases if alias.strip()],
                    "summary": item.summary,
                    "confidence": item.confidence,
                    "evidence_quote": item.evidence_quote,
                },
                segment=extraction_segment,
                model_name=model_name,
                prompt_version=prompt_version,
            )
            if not staged:
                dropped += 1
                continue
            entities_by_key[key] = item
            created += 1
        for relation in result.relations:
            source = entities_by_key.get(_entity_key(relation.source_name) or "")
            target = entities_by_key.get(_entity_key(relation.target_name) or "")
            if (
                relation.predicate not in RELATION_PREDICATES
                or source is None
                or target is None
                or source is target
            ):
                dropped += 1  # 越界谓词/两端未抽到实体/自环:整条丢弃
                continue
            staged = await _enqueue_graph_claim(
                session,
                doc=doc,
                revision=revision,
                ingest_run_id=claim.run_id,
                predicate=relation.predicate,
                payload={
                    "source_name": source.name.strip(),
                    "source_type_key": source.type_key,
                    "target_name": target.name.strip(),
                    "target_type_key": target.type_key,
                    "confidence": relation.confidence,
                    "evidence_quote": relation.evidence_quote,
                },
                segment=extraction_segment,
                model_name=model_name,
                prompt_version=prompt_version,
            )
            if not staged:
                dropped += 1
                continue
            created += 1
        boundary_error = await _knowledge_base_boundary_error(
            session,
            kb_id=doc.kb_id,
            org_id=doc.org_id,
            captured_epoch=captured_epoch,
            lock=True,
        )
        if boundary_error is not None:
            raise RuntimeError(boundary_error)
        completed = await complete_ingest_run(
            session,
            run_id=claim.run_id,
            lease_owner=lease_owner,
            status=IngestRunStatus.STAGED,
            stats={
                "consumption_epoch": captured_epoch,
                "created_claims": created,
                "extracted_entities": len(result.entities),
                "extracted_relations": len(result.relations),
                "dropped": dropped,
            },
        )
        if not completed:
            raise IngestLeaseLostError(str(claim.run_id))
        await session.commit()
        return created
    except Exception as exc:
        await _fail_claimed_stage(session, claim.run_id, lease_owner, exc)
        raise


async def _maybe_ingest_entity_graph(
    session: AsyncSession,
    doc: SourceDocument,
    full_text: str,
    llm: LLMService,
    *,
    revision: DocumentRevision,
    session_factory: async_sessionmaker[AsyncSession],
    lease_owner: str,
    profile: IngestProfile | None,
    captured_epoch: int,
) -> None:
    """best-effort 包装:实体抽取失败只记日志,不反噬已落库的摄入结果。

    与 wiki 自动生成同一口径 —— 图谱是增强项,LLM 抖动不该把文档打成 failed。
    """
    auto = profile.auto_entities if profile is not None else IngestProfile().auto_entities
    if not auto:
        return
    try:
        created = await _ingest_entity_graph(
            session,
            doc,
            full_text,
            llm,
            revision=revision,
            session_factory=session_factory,
            lease_owner=lease_owner,
            captured_epoch=captured_epoch,
        )
        logger.info("实体图谱抽取完成: doc=%s created=%s", doc.id, created)
    except Exception:  # noqa: BLE001 - 图谱是增强项,失败不反噬摄入状态
        logger.exception("实体图谱抽取失败(不影响摄入状态): doc=%s", doc.id)


async def _ingest_structured(
    session: AsyncSession,
    doc: SourceDocument,
    full_text: str,
    llm: LLMService,
    *,
    revision: DocumentRevision,
    session_factory: async_sessionmaker[AsyncSession],
    lease_owner: str,
    progress: _ProgressReporter | None,
    captured_epoch: int,
    target_doc_type: str | None = None,
    stage: str = "extract",
) -> tuple[int, bool]:
    """返回 (待审核事实数, 是否被取消)。逐段 staging,段间检查取消。

    每条自动事实与独立 EvidenceSpan 同事务写入；证据先在当前 segment 内
    确定性定位，再换算为派生 Markdown 的全局 1-based 行号。
    """
    task_name, contract, entity_type, generic_type = await _resolve_extraction_spec(
        session,
        doc,
        target_doc_type=target_doc_type,
    )
    spec_prefix = _generic_spec_block(generic_type)
    segments = split_for_extraction(
        full_text,
        MAX_EXTRACT_CHARS,
        include_offsets=True,
    ) or [
        ExtractionSegment(
            heading_path="",
            content=full_text,
            source_text=full_text,
            line_offset=0,
        )
    ]
    structured_document: dict | None = None
    if revision.structured_json_key:
        structured_data = json.loads(
            await storage.get_object(revision.structured_json_key)
        )
        if not isinstance(structured_data, dict):
            raise RuntimeError(
                f"Docling structured artifact is invalid: {revision.id}"
            )
        structured_document = structured_data

    for extraction_segment in segments:
        if not isinstance(extraction_segment, ExtractionSegment):
            raise TypeError("extraction splitter must provide stable line offsets")
    # 并发期间主 session 不得持有事务快照,否则每个分段都在等它的连接
    await session.commit()

    total = len(segments)
    semaphore = asyncio.Semaphore(max(1, get_settings().kb_extract_concurrency))
    canceled = asyncio.Event()
    done = 0
    done_lock = asyncio.Lock()

    async def extract_segment(idx: int, extraction_segment: ExtractionSegment) -> None:
        nonlocal done
        async with semaphore:
            if canceled.is_set():
                return
            async with org_session(session_factory, doc.org_id) as worker:
                # 取消是协作式的:在飞的分段跑完,未开工的分段直接放弃
                if await _stop_requested(worker, doc.id):
                    canceled.set()
                    return
                await _extract_one_segment(
                    worker,
                    doc=doc,
                    revision=revision,
                    session_factory=session_factory,
                    lease_owner=lease_owner,
                    llm=llm,
                    idx=idx,
                    total=total,
                    extraction_segment=extraction_segment,
                    task_name=task_name,
                    contract=contract,
                    entity_type=entity_type,
                    generic_type=generic_type,
                    spec_prefix=spec_prefix,
                    structured_document=structured_document,
                    stage=stage,
                    captured_epoch=captured_epoch,
                )
        async with done_lock:
            done += 1
            if progress is not None:
                await progress.report("extract", done, total)

    results = await asyncio.gather(
        *(
            extract_segment(idx, extraction_segment)
            for idx, extraction_segment in enumerate(segments)
        ),
        return_exceptions=True,
    )
    for outcome in results:
        if isinstance(outcome, BaseException):
            raise outcome
    count = await _pending_fact_claim_count(session, revision.id)
    return count, canceled.is_set()


async def _extract_one_segment(
    session: AsyncSession,
    *,
    doc: SourceDocument,
    revision: DocumentRevision,
    session_factory: async_sessionmaker[AsyncSession],
    lease_owner: str,
    llm: LLMService,
    idx: int,
    total: int,
    extraction_segment: ExtractionSegment,
    task_name: str,
    contract: type,
    entity_type: str,
    generic_type: KbEntityType,
    spec_prefix: str,
    structured_document: dict | None,
    stage: str,
    captured_epoch: int,
) -> None:
    """Claim, extract and stage exactly one segment on its own session."""
    # P3b defer-import (see module header note)
    from nicekit.kb.entity_types import EntityValidationError, validate_entity_attributes

    claim = await _claim_stage(
        session,
        revision,
        stage=stage,
        segment_no=idx,
        lease_owner=lease_owner,
        captured_epoch=captured_epoch,
    )
    if not claim.acquired:
        return  # 已 staged 的分段不重跑,续跑天然去重
    try:
        async with maintain_ingest_lease(
            session_factory,
            org_id=doc.org_id,
            run_id=claim.run_id,
            lease_owner=lease_owner,
        ):
            generation = await llm.generate_structured_with_metadata(
                task=task_name,
                messages=[
                    {
                        "role": "user",
                        "content": spec_prefix
                        + fence_untrusted_document(
                            extraction_segment.content,
                            label=f"source document segment {idx + 1}/{total}",
                        ),
                    }
                ],
                output_model=contract,
                org_id=doc.org_id,
            )
            result = generation.parsed
        created = 0
        schema_dropped = 0
        for item in result.items:
            # 强校验层:抽取条目必过类型 field_schema,不合规整条丢弃
            try:
                attrs = item.parsed_attributes()
                validate_entity_attributes(generic_type, attrs)
            except (EntityValidationError, ValueError) as exc:
                schema_dropped += 1
                logger.warning(
                    "通用抽取条目未过类型 schema,丢弃(doc=%s type=%s): %s",
                    doc.id, generic_type.type_key, exc,
                )
                continue
            payload = {
                **attrs,
                "confidence": item.confidence,
                "evidence_quote": item.evidence_quote,
            }
            await _enqueue_fact_claim(
                session,
                doc=doc,
                revision=revision,
                ingest_run_id=claim.run_id,
                entity_type=entity_type,
                payload=payload,
                segment_markdown=extraction_segment.source_text,
                line_offset=extraction_segment.line_offset,
                structured_document=structured_document,
                model_name=f"{generation.provider}:{generation.model}",
                prompt_version=f"{task_name}:v{generation.prompt_version}",
            )
            created += 1
        boundary_error = await _knowledge_base_boundary_error(
            session,
            kb_id=doc.kb_id,
            org_id=doc.org_id,
            captured_epoch=captured_epoch,
            lock=True,
        )
        if boundary_error is not None:
            raise RuntimeError(boundary_error)
        completed = await complete_ingest_run(
            session,
            run_id=claim.run_id,
            lease_owner=lease_owner,
            status=IngestRunStatus.STAGED,
            stats={
                "consumption_epoch": captured_epoch,
                "created_claims": created,
                "created_evidence_spans": created,
                "extracted_items": len(result.items),
                "schema_dropped": schema_dropped,
            },
        )
        if not completed:
            raise IngestLeaseLostError(str(claim.run_id))
        await session.commit()
    except Exception as exc:
        await _fail_claimed_stage(session, claim.run_id, lease_owner, exc)
        raise


async def _generate_chunk_contexts(
    doc: SourceDocument,
    full_text: str,
    chunk_contents: list[str],
    safe_indexes: list[int],
    llm: LLMService | None,
) -> list[str | None]:
    """Contextual Retrieval(默认关):flag 开启且 doc_type=GENERAL 时为非隔离
    chunk 批量生成定位上下文。任何失败整体降级为无 context 摄入(warning,
    不中断、不重试阻塞——与 caption 同姿态);隔离 chunk 不送 LLM 也无 context。"""
    contexts: list[str | None] = [None] * len(chunk_contents)
    if (
        llm is None
        or not safe_indexes
        or str(doc.doc_type) != DocType.GENERAL.value
        or not get_settings().kb_contextual_chunking_enabled
    ):
        return contexts
    from nicekit.kb.contextualizer import generate_chunk_contexts

    try:
        generated = await generate_chunk_contexts(
            [chunk_contents[index] for index in safe_indexes],
            full_text=full_text,
            llm=llm,
            org_id=doc.org_id,
        )
        for index, context in zip(safe_indexes, generated, strict=True):
            contexts[index] = context
    except Exception as exc:  # noqa: BLE001 - context 是增强项,失败不中断摄入
        logger.warning("chunk 上下文生成失败,整体降级为无 context 摄入: %s", exc)
        return [None] * len(chunk_contents)
    return contexts


def _chunk_meta(reasons: tuple[str, ...], context: str | None) -> dict | None:
    meta: dict = {}
    if reasons:
        meta["quarantine_reasons"] = list(reasons)
    if context:
        meta["context_text"] = context
    return meta or None


async def _prepare_chunks(
    doc: SourceDocument,
    full_text: str,
    embedder: EmbeddingService | None,
    profile: IngestProfile | None = None,
    llm: LLMService | None = None,
) -> list[KbChunk]:
    p = profile or IngestProfile()
    if p.chunk_strategy == "fixed":
        chunks = wrap_fixed_chunks(
            chunk_text(full_text, max_chars=p.chunk_max_chars, overlap=p.chunk_overlap_chars)
        )
    else:
        chunks = chunk_markdown(
            full_text,
            max_chars=p.chunk_max_chars,
            overlap=p.chunk_overlap_chars,
            table_mode=p.table_mode,
        )
    quarantine_reasons = [suspicious_instruction_reasons(c.content) for c in chunks]
    safe_indexes = [index for index, reasons in enumerate(quarantine_reasons) if not reasons]
    contexts = await _generate_chunk_contexts(
        doc, full_text, [c.content for c in chunks], safe_indexes, llm
    )
    # 嵌入文本 = 定位上下文 + 标题面包屑 + 内容(检索时命中"年度报表 > 成本"
    # 这类路径词;context 默认关闭时口径与旧版逐字节一致)
    embed_texts = [
        chunk_embedding_text(c.content, c.heading_path or None, contexts[i])
        for i, c in enumerate(chunks)
    ]
    vectors: list[list[float] | None] = [None] * len(chunks)
    label: str | None = None
    if embedder is not None and safe_indexes:
        try:
            safe_vectors = await embedder.embed(
                [embed_texts[index] for index in safe_indexes],
                org_id=doc.org_id,
                task="kb.ingestion.embedding",
            )
            for index, vector in zip(safe_indexes, safe_vectors, strict=True):
                vectors[index] = vector
            label = embedder.label
        except EmbeddingUnavailableError as exc:
            logger.warning("embedding 不可用,chunk 先入库不带向量: %s", exc)
    rows: list[KbChunk] = []
    for i, chunk in enumerate(chunks):
        reasons = quarantine_reasons[i]
        rows.append(
            KbChunk(
                org_id=doc.org_id,
                kb_id=doc.kb_id,
                source_doc_id=doc.id,
                content=chunk.content,
                embedding=vectors[i],
                embedding_model=label if vectors[i] is not None else None,
                quarantined=bool(reasons),
                source_ref=f"{doc.filename}#chunk{i}",
                heading_path=(chunk.heading_path or None) and chunk.heading_path[:500],
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                page=chunk.page,
                chunk_index=i,
                meta=_chunk_meta(reasons, contexts[i]),
            )
        )
    return rows


def _chunk_staging_payload(row: KbChunk) -> dict:
    return {
        "chunk_index": row.chunk_index,
        "content": row.content,
        "embedding": row.embedding,
        "embedding_model": row.embedding_model,
        "end_line": row.end_line,
        "heading_path": row.heading_path,
        "meta": row.meta,
        "page": row.page,
        "quarantined": row.quarantined,
        "source_ref": row.source_ref,
        "start_line": row.start_line,
    }


def _payload_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"无效有效期: {value!r}") from exc


__all__ = [
    "ingest_document",
    "GENERIC_EXTRACTION_TASK",
    "get_max_concurrency",
    "set_max_concurrency",
    "get_active_count",
]
