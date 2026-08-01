"""审核收口:待审事实/图片处理完后把文档从 awaiting_review 收回 completed。

摄入结束时只要产出 suggested 事实或待审图片,文档就停在 awaiting_review
(见 ingestion.py 落状态处);而此前所有审核路径只改 FactClaim / KbImageAsset
自身,没有任何地方回写文档状态 —— 结果是审完的文档在列表里永远挂着
「待审核」,且只能靠重新摄入才能摘掉。

本模块给四条审核路径(人工单条 / 批量事实审核、图片审核、AI 审核扫描)提供
同一个幂等收口函数:条件不满足就原样返回,可重复调用。判定口径与 ingestion
落状态时同源 —— 图片侧复用 revision_image_stage(只有 completed 才算清空,
failed/pending/processing 保持原状交给既有流程),事实侧只看 suggested
(orphaned 是文档撤回后的孤儿态,不该阻塞另一篇文档收口)。
"""

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.kb import (
    DocStatus,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    SourceDocument,
)

DOCUMENT_SUBJECT_TYPE = "source_document"


async def _latest_revision_id(
    session: AsyncSession, *, org_id: UUID, doc_id: UUID
) -> UUID | None:
    return await session.scalar(
        select(DocumentRevision.id)
        .where(
            DocumentRevision.org_id == org_id,
            DocumentRevision.doc_id == doc_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )


async def _has_pending_claims(
    session: AsyncSession, *, org_id: UUID, kb_id: UUID, doc_id: UUID
) -> bool:
    """该文档是否仍有 suggested 事实(claim 无 doc_id 列,经 run/证据/主体反查)。"""
    revision_ids = select(DocumentRevision.id).where(
        DocumentRevision.org_id == org_id,
        DocumentRevision.kb_id == kb_id,
        DocumentRevision.doc_id == doc_id,
    )
    ingest_run_ids = select(IngestRun.id).where(IngestRun.revision_id.in_(revision_ids))
    doc_evidence = (
        select(EvidenceSpan.id)
        .where(
            EvidenceSpan.fact_claim_id == FactClaim.id,
            EvidenceSpan.revision_id.in_(revision_ids),
        )
        .exists()
    )
    pending = await session.scalar(
        select(FactClaim.id)
        .where(
            FactClaim.org_id == org_id,
            FactClaim.kb_id == kb_id,
            FactClaim.review_status == FactReviewStatus.SUGGESTED.value,
            or_(
                FactClaim.ingest_run_id.in_(ingest_run_ids),
                doc_evidence,
                (FactClaim.subject_type == DOCUMENT_SUBJECT_TYPE)
                & (FactClaim.subject_id == doc_id),
            ),
        )
        .limit(1)
    )
    return pending is not None


async def settle_document_review(
    session: AsyncSession, *, org_id: UUID, doc_id: UUID
) -> bool:
    """待审事实与待审图片都清空时把文档收回 completed,返回是否发生状态变更。

    幂等:非 awaiting_review 的文档直接返回 False;仍有待审项时不动状态。
    不 commit,由调用方(端点/扫描)与自身事务一起提交。
    """
    row = (
        await session.execute(
            select(SourceDocument.status, SourceDocument.kb_id).where(
                SourceDocument.id == doc_id,
                SourceDocument.org_id == org_id,
            )
        )
    ).one_or_none()
    if row is None:
        return False
    status, kb_id = row
    if str(getattr(status, "value", status)) != DocStatus.AWAITING_REVIEW.value:
        return False

    revision_id = await _latest_revision_id(session, org_id=org_id, doc_id=doc_id)
    if revision_id is not None:
        # 图片富化阶段属媒体波次(kb/image_ingestion.py);未装配时视为无图片待办。
        try:
            import nicekit.kb.image_ingestion as image_ingestion
        except (ImportError, ModuleNotFoundError):
            image_ingestion = None
        if image_ingestion is not None:
            stage = await image_ingestion.revision_image_stage(session, revision_id)
            if stage.state != "completed":
                return False
    if await _has_pending_claims(
        session, org_id=org_id, kb_id=kb_id, doc_id=doc_id
    ):
        return False

    result = await session.execute(
        update(SourceDocument)
        .where(
            SourceDocument.id == doc_id,
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.AWAITING_REVIEW.value,
        )
        .values(status=DocStatus.COMPLETED.value, progress=100)
        .execution_options(synchronize_session="fetch")
    )
    settled = bool(result.rowcount)
    if not settled or revision_id is None:
        return settled

    document = await session.get(SourceDocument, doc_id)
    revision = await session.get(DocumentRevision, revision_id)
    if document is not None and revision is not None:
        from nicekit.kb.document_reingestion import (
            settle_reingestion_ingest_result,
        )

        await settle_reingestion_ingest_result(
            session,
            document=document,
            revision=revision,
        )
    return True


async def settle_documents_for_claims(
    session: AsyncSession, *, org_id: UUID, claim_ids: list[UUID]
) -> int:
    """审完若干事实后,对这些事实牵涉到的文档逐个收口,返回收口文档数。"""
    if not claim_ids:
        return 0
    doc_ids: set[UUID] = set()
    by_run = await session.execute(
        select(DocumentRevision.doc_id)
        .join(IngestRun, IngestRun.revision_id == DocumentRevision.id)
        .join(FactClaim, FactClaim.ingest_run_id == IngestRun.id)
        .where(FactClaim.id.in_(claim_ids), FactClaim.org_id == org_id)
    )
    doc_ids.update(by_run.scalars())
    by_evidence = await session.execute(
        select(DocumentRevision.doc_id)
        .join(EvidenceSpan, EvidenceSpan.revision_id == DocumentRevision.id)
        .where(
            EvidenceSpan.fact_claim_id.in_(claim_ids),
            EvidenceSpan.org_id == org_id,
        )
    )
    doc_ids.update(by_evidence.scalars())
    by_subject = await session.execute(
        select(FactClaim.subject_id).where(
            FactClaim.id.in_(claim_ids),
            FactClaim.org_id == org_id,
            FactClaim.subject_type == DOCUMENT_SUBJECT_TYPE,
        )
    )
    doc_ids.update(by_subject.scalars())

    settled = 0
    for doc_id in sorted(doc_ids, key=str):
        if await settle_document_review(session, org_id=org_id, doc_id=doc_id):
            settled += 1
    return settled


async def settle_org_awaiting_documents(
    session: AsyncSession, *, org_id: UUID, limit: int = 500
) -> int:
    """扫描本 org 所有 awaiting_review 文档并逐个收口,返回收口文档数。

    AI 审核扫描每轮调用:既覆盖 AI 自动 confirm/reject 的收口,也顺带回补
    历史上审完却卡在 awaiting_review 的存量文档(修复前遗留)。
    """
    doc_ids = (
        (
            await session.execute(
                select(SourceDocument.id)
                .where(
                    SourceDocument.org_id == org_id,
                    SourceDocument.status == DocStatus.AWAITING_REVIEW.value,
                )
                .order_by(SourceDocument.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    settled = 0
    for doc_id in doc_ids:
        if await settle_document_review(session, org_id=org_id, doc_id=doc_id):
            settled += 1
    return settled
