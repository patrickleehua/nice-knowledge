"""Durable document withdrawal and manifest-based support settlement."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.effective_scope import live_document_revision_filter
from nicekit.kb.snapshot import activate_snapshot, build_snapshot, rollback_snapshot
from nicekit.models.kb import (
    CanonicalEntity,
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbChunk,
    KbDocumentOperation,
    KbImageAsset,
    KbSnapshotEntityNode,
    KnowledgeBase,
    OutboxEvent,
    OutboxStatus,
    RevisionStatus,
    SnapshotFactSupport,
    SnapshotStatus,
    SourceDocument,
)


class DocumentDeletionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentDeletionResult:
    document_id: UUID
    revision_id: UUID
    operation_id: UUID
    tombstoned_at: datetime
    orphaned_claim_count: int
    already_tombstoned: bool
    operation: KbDocumentOperation


@dataclass(frozen=True, slots=True)
class DocumentWithdrawalImpact:
    document_id: UUID
    revision_count: int
    chunk_count: int
    image_count: int
    exclusive_fact_count: int
    shared_fact_count: int
    orphaned_fact_count: int
    exclusive_entity_count: int
    shared_entity_count: int
    exclusive_relation_count: int
    shared_relation_count: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "document_id": str(self.document_id),
            "revision_count": self.revision_count,
            "chunk_count": self.chunk_count,
            "image_count": self.image_count,
            "exclusive_fact_count": self.exclusive_fact_count,
            "shared_fact_count": self.shared_fact_count,
            "orphaned_fact_count": self.orphaned_fact_count,
            "exclusive_entity_count": self.exclusive_entity_count,
            "shared_entity_count": self.shared_entity_count,
            "exclusive_relation_count": self.exclusive_relation_count,
            "shared_relation_count": self.shared_relation_count,
        }


def _status(value: object) -> str:
    return str(getattr(value, "value", value))


def _document_revision_ids(
    *, org_id: UUID, kb_id: UUID, document_id: UUID
):
    return select(DocumentRevision.id).where(
        DocumentRevision.org_id == org_id,
        DocumentRevision.kb_id == kb_id,
        DocumentRevision.doc_id == document_id,
    )


def _affected_claim_filter(
    *, org_id: UUID, kb_id: UUID, document_id: UUID
):
    revision_ids = _document_revision_ids(
        org_id=org_id,
        kb_id=kb_id,
        document_id=document_id,
    )
    ingest_run_ids = select(IngestRun.id).where(
        IngestRun.revision_id.in_(revision_ids)
    )
    document_evidence = select(EvidenceSpan.id).where(
        EvidenceSpan.fact_claim_id == FactClaim.id,
        EvidenceSpan.revision_id.in_(revision_ids),
    ).exists()
    return or_(
        (FactClaim.subject_type == "source_document")
        & (FactClaim.subject_id == document_id),
        FactClaim.ingest_run_id.in_(ingest_run_ids),
        document_evidence,
    )


async def preview_document_withdrawal(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
) -> DocumentWithdrawalImpact:
    document = await session.scalar(
        select(SourceDocument).where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
        )
    )
    if document is None:
        raise DocumentDeletionError("source document does not exist")
    revision_ids = set(
        (
            await session.scalars(
                _document_revision_ids(
                    org_id=org_id,
                    kb_id=document.kb_id,
                    document_id=document_id,
                )
            )
        ).all()
    )
    kb = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == document.kb_id,
            KnowledgeBase.org_id == org_id,
        )
    )
    impacted_claims = list(
        (
            await session.scalars(
                select(FactClaim).where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == document.kb_id,
                    _affected_claim_filter(
                        org_id=org_id,
                        kb_id=document.kb_id,
                        document_id=document_id,
                    ),
                )
            )
        ).all()
    )
    claim_ids = [claim.id for claim in impacted_claims]
    live_supported_claim_ids: set[UUID] = set()
    if kb is not None and kb.active_snapshot_id is not None:
        live_supported_claim_ids = set(
            (
                await session.scalars(
                    select(SnapshotFactSupport.fact_claim_id)
                    .join(
                        DocumentRevision,
                        and_(
                            DocumentRevision.id
                            == SnapshotFactSupport.revision_id,
                            DocumentRevision.doc_id == SnapshotFactSupport.doc_id,
                            DocumentRevision.org_id == SnapshotFactSupport.org_id,
                            DocumentRevision.kb_id == SnapshotFactSupport.kb_id,
                        ),
                    )
                    .join(
                        SourceDocument,
                        and_(
                            SourceDocument.id == SnapshotFactSupport.doc_id,
                            SourceDocument.org_id == SnapshotFactSupport.org_id,
                            SourceDocument.kb_id == SnapshotFactSupport.kb_id,
                        ),
                    )
                    .where(
                        SnapshotFactSupport.snapshot_id == kb.active_snapshot_id,
                        SnapshotFactSupport.org_id == org_id,
                        SnapshotFactSupport.kb_id == document.kb_id,
                        SnapshotFactSupport.doc_id != document_id,
                        live_document_revision_filter(),
                    )
                    .distinct()
                )
            ).all()
        )
    shared_claim_ids = set(claim_ids) & live_supported_claim_ids
    exclusive_claim_ids = {claim.id for claim in impacted_claims} - shared_claim_ids
    entity_claim_ids: dict[UUID, set[UUID]] = {}
    relation_claim_ids: set[UUID] = set()
    for claim in impacted_claims:
        for entity_id in (claim.subject_entity_id, claim.object_entity_id):
            if entity_id is not None:
                entity_claim_ids.setdefault(entity_id, set()).add(claim.id)
        if claim.subject_entity_id is not None and claim.object_entity_id is not None:
            relation_claim_ids.add(claim.id)
    shared_entities: set[UUID] = set()
    if entity_claim_ids and live_supported_claim_ids:
        entity_rows = (
            await session.execute(
                select(
                    FactClaim.subject_entity_id,
                    FactClaim.object_entity_id,
                ).where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == document.kb_id,
                    FactClaim.id.in_(live_supported_claim_ids),
                    or_(
                        FactClaim.subject_entity_id.in_(entity_claim_ids),
                        FactClaim.object_entity_id.in_(entity_claim_ids),
                    ),
                )
            )
        ).all()
        touched_entity_ids = set(entity_claim_ids)
        shared_entities = {
            entity_id
            for endpoints in entity_rows
            for entity_id in endpoints
            if entity_id in touched_entity_ids
        }
    exclusive_entities = set(entity_claim_ids) - shared_entities
    shared_relations = relation_claim_ids & shared_claim_ids
    exclusive_relations = relation_claim_ids - shared_relations
    chunk_count = int(
        await session.scalar(
            select(func.count())
            .select_from(KbChunk)
            .where(
                KbChunk.org_id == org_id,
                KbChunk.kb_id == document.kb_id,
                KbChunk.source_doc_id == document_id,
            )
        )
        or 0
    )
    image_count = int(
        await session.scalar(
            select(func.count())
            .select_from(KbImageAsset)
            .where(
                KbImageAsset.org_id == org_id,
                KbImageAsset.kb_id == document.kb_id,
                KbImageAsset.revision_id.in_(revision_ids),
            )
        )
        or 0
    )
    return DocumentWithdrawalImpact(
        document_id=document_id,
        revision_count=len(revision_ids),
        chunk_count=chunk_count,
        image_count=image_count,
        exclusive_fact_count=len(exclusive_claim_ids),
        shared_fact_count=len(shared_claim_ids),
        orphaned_fact_count=sum(
            claim.review_status == FactReviewStatus.CONFIRMED.value
            for claim in impacted_claims
            if claim.id in exclusive_claim_ids
        ),
        exclusive_entity_count=len(exclusive_entities),
        shared_entity_count=len(shared_entities),
        exclusive_relation_count=len(exclusive_relations),
        shared_relation_count=len(shared_relations),
    )


async def _settle_withdrawn_support(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    snapshot_id: UUID,
) -> int:
    affected = _affected_claim_filter(
        org_id=org_id,
        kb_id=kb_id,
        document_id=document_id,
    )
    surviving_support = exists(
        select(SnapshotFactSupport.id).where(
            SnapshotFactSupport.snapshot_id == snapshot_id,
            SnapshotFactSupport.fact_claim_id == FactClaim.id,
            SnapshotFactSupport.org_id == org_id,
            SnapshotFactSupport.kb_id == kb_id,
        )
    )
    orphaned = await session.execute(
        update(FactClaim)
        .where(
            FactClaim.org_id == org_id,
            FactClaim.kb_id == kb_id,
            FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
            affected,
            ~surviving_support,
        )
        .values(review_status=FactReviewStatus.ORPHANED.value)
    )
    await reconcile_snapshot_entity_support(
        session,
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
    )
    return int(orphaned.rowcount or 0)


async def reconcile_snapshot_entity_support(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Recompute canonical entity support from one successfully activated snapshot."""

    active_entity_ids = set(
        (
            await session.scalars(
                select(KbSnapshotEntityNode.entity_id).where(
                    KbSnapshotEntityNode.snapshot_id == snapshot_id,
                    KbSnapshotEntityNode.org_id == org_id,
                    KbSnapshotEntityNode.kb_id == kb_id,
                )
            )
        ).all()
    )
    entities = list(
        (
            await session.scalars(
                select(CanonicalEntity).where(
                    CanonicalEntity.org_id == org_id,
                    CanonicalEntity.kb_id == kb_id,
                    CanonicalEntity.merged_into_entity_id.is_(None),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for entity in entities:
        next_status = (
            "supported" if entity.id in active_entity_ids else "unsupported"
        )
        reason = (
            None
            if next_status == "supported"
            else "candidate snapshot has no active support"
        )
        if entity.support_status != next_status:
            audit = (entity.metadata_ or {}).get("support_audit")
            audit_entries = list(audit) if isinstance(audit, list) else []
            audit_entries.append(
                {
                    "status": next_status,
                    "reason": reason,
                    "at": now.isoformat(),
                    "snapshot_id": str(snapshot_id),
                }
            )
            entity.metadata_ = {
                **(entity.metadata_ or {}),
                "support_audit": audit_entries,
            }
            entity.support_status_changed_at = now
            entity.unsupported_at = (
                now if next_status == "unsupported" else None
            )
        entity.support_status = next_status
        entity.support_status_reason = reason
        entity.support_status_snapshot_id = snapshot_id
        session.add(entity)


async def delete_source_document(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    actor_id: UUID,
    reason: str,
) -> DocumentDeletionResult:
    document = await session.scalar(
        select(SourceDocument).where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
        )
    )
    if document is None:
        raise DocumentDeletionError("source document does not exist")
    kb = await session.scalar(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == document.kb_id, KnowledgeBase.org_id == org_id)
        .with_for_update()
    )
    if kb is None:
        raise DocumentDeletionError("source document knowledge base does not exist")
    document = await session.scalar(
        select(SourceDocument)
        .where(SourceDocument.id == document_id, SourceDocument.org_id == org_id)
        .with_for_update()
    )
    if (
        document is not None
        and _status(document.lifecycle_status)
        == DocumentLifecycleStatus.REINGESTION_PENDING.value
    ):
        raise DocumentDeletionError("document reingestion is in progress")
    revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == kb.id,
            DocumentRevision.doc_id == document_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if revision is None:
        raise DocumentDeletionError("source document has no revision")

    operation_key = (
        f"document:{org_id.hex}:{document_id.hex}:withdrawal:{revision.id.hex}"
    )
    existing_operation = await session.scalar(
        select(KbDocumentOperation).where(
            KbDocumentOperation.idempotency_key == operation_key
        )
    )
    if _status(revision.status) == RevisionStatus.TOMBSTONED.value:
        if existing_operation is None or revision.tombstoned_at is None:
            raise DocumentDeletionError("tombstone state is incomplete")
        return DocumentDeletionResult(
            document_id=document_id,
            revision_id=revision.id,
            operation_id=existing_operation.id,
            tombstoned_at=revision.tombstoned_at,
            orphaned_claim_count=int(
                existing_operation.impact_summary.get("orphaned_fact_count", 0)
            ),
            already_tombstoned=True,
            operation=existing_operation,
        )

    impact = await preview_document_withdrawal(
        session,
        org_id=org_id,
        document_id=document_id,
    )
    now = datetime.now(UTC)
    revision.status = RevisionStatus.TOMBSTONED
    revision.tombstoned_at = now
    revision.tombstoned_by = actor_id
    revision.tombstone_reason = reason.strip() or "document withdrawn"
    document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWAL_PENDING
    kb.consumption_epoch += 1
    await session.execute(
        update(IngestRun)
        .where(
            IngestRun.revision_id.in_(
                select(DocumentRevision.id).where(
                    DocumentRevision.doc_id == document_id,
                    DocumentRevision.org_id == org_id,
                    DocumentRevision.kb_id == kb.id,
                )
            ),
            IngestRun.status.in_(
                (IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)
            ),
        )
        .values(
            status=IngestRunStatus.CANCELED.value,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=None,
            error="source document withdrawn",
            finished_at=now,
        )
    )
    operation = KbDocumentOperation(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb.id,
        document_id=document_id,
        revision_id=revision.id,
        operation_type=DocumentOperationType.WITHDRAWAL,
        status=DocumentOperationStatus.PENDING,
        stage="snapshot_rebuild",
        idempotency_key=operation_key,
        requested_by=actor_id,
        reason=revision.tombstone_reason,
        impact_summary=impact.as_dict(),
    )
    event = OutboxEvent(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb.id,
        aggregate_type="source_document",
        aggregate_id=document_id,
        event_type="document.tombstoned",
        idempotency_key=f"{operation_key}:dispatch",
        payload={
            "document_id": str(document_id),
            "revision_id": str(revision.id),
            "operation_id": str(operation.id),
            "actor": str(actor_id),
            "reason": revision.tombstone_reason,
            "consumption_epoch": kb.consumption_epoch,
        },
    )
    session.add_all([kb, document, revision, operation, event])
    await session.flush()
    return DocumentDeletionResult(
        document_id=document_id,
        revision_id=revision.id,
        operation_id=operation.id,
        tombstoned_at=now,
        orphaned_claim_count=impact.orphaned_fact_count,
        already_tombstoned=False,
        operation=operation,
    )


async def retry_document_operation(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
) -> KbDocumentOperation:
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.operation_type
            == DocumentOperationType.WITHDRAWAL.value,
        )
        .with_for_update()
    )
    if operation is None:
        raise DocumentDeletionError("withdrawal operation does not exist")
    if operation.status not in {
        DocumentOperationStatus.FAILED.value,
        DocumentOperationStatus.DEAD_LETTER.value,
    }:
        raise DocumentDeletionError("withdrawal operation is not retryable")
    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.org_id == org_id,
            OutboxEvent.kb_id == operation.kb_id,
            OutboxEvent.event_type == "document.tombstoned",
            OutboxEvent.payload["operation_id"].astext == str(operation.id),
        )
        .with_for_update()
    )
    if event is None:
        raise DocumentDeletionError("withdrawal dispatch event does not exist")
    event.status = OutboxStatus.PENDING
    event.attempts = 0
    event.available_at = datetime.now(UTC)
    event.claimed_by = None
    event.claimed_at = None
    event.claim_expires_at = None
    event.published_at = None
    event.last_error = None
    operation.status = DocumentOperationStatus.PENDING
    operation.stage = "retry_scheduled"
    operation.retryable = False
    operation.last_error_code = None
    operation.last_error = None
    operation.failed_at = None
    operation.completed_at = None
    session.add_all([operation, event])
    await session.flush()
    return operation


async def rebuild_after_document_tombstone(
    session: AsyncSession, event: OutboxEvent
) -> None:
    if event.event_type != "document.tombstoned":
        raise ValueError("unsupported document lifecycle event")
    actor_id = UUID(str(event.payload["actor"]))
    document_id = UUID(str(event.payload["document_id"]))
    operation_id = UUID(str(event.payload["operation_id"]))
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == event.org_id,
            KbDocumentOperation.kb_id == event.kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.operation_type
            == DocumentOperationType.WITHDRAWAL.value,
        )
        .with_for_update()
    )
    if operation is None:
        raise DocumentDeletionError("withdrawal operation does not exist")
    if operation.status == DocumentOperationStatus.COMPLETED.value:
        return
    operation.status = DocumentOperationStatus.PROCESSING
    operation.stage = "snapshot_rebuild"
    operation.attempts = event.attempts
    operation.started_at = operation.started_at or datetime.now(UTC)
    operation.retryable = False
    operation.last_error_code = None
    operation.last_error = None
    operation.failed_at = None
    session.add(operation)
    kb = await session.get(KnowledgeBase, event.kb_id)
    if kb is None:
        raise DocumentDeletionError("source document knowledge base does not exist")
    snapshot = await build_snapshot(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        actor_id=actor_id,
        reason=f"document withdrawn: {event.payload.get('reason', '')}",
    )
    snapshot_status = _status(snapshot.status)
    if snapshot_status == SnapshotStatus.READY.value:
        await activate_snapshot(
            session,
            org_id=event.org_id,
            kb_id=event.kb_id,
            snapshot_id=snapshot.id,
            actor_id=actor_id,
            reason=f"document withdrawn: {event.payload.get('reason', '')}",
            operation_id=operation.id,
        )
    elif snapshot_status == SnapshotStatus.RETIRED.value:
        await rollback_snapshot(
            session,
            org_id=event.org_id,
            kb_id=event.kb_id,
            target_snapshot_id=snapshot.id,
            actor_id=actor_id,
            reason=f"document withdrawn: {event.payload.get('reason', '')}",
            operation_id=operation.id,
        )
    elif snapshot_status != SnapshotStatus.ACTIVE.value:
        raise DocumentDeletionError(snapshot.error or "snapshot build failed")
    operation.stage = "support_settlement"
    orphaned = await _settle_withdrawn_support(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        document_id=document_id,
        snapshot_id=snapshot.id,
    )
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == event.org_id,
            SourceDocument.kb_id == event.kb_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentDeletionError("source document does not exist")
    document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWN
    operation.status = DocumentOperationStatus.COMPLETED
    operation.stage = "completed"
    operation.target_snapshot_id = snapshot.id
    operation.impact_summary = {
        **operation.impact_summary,
        "orphaned_fact_count": orphaned,
    }
    operation.retryable = False
    operation.completed_at = datetime.now(UTC)
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error = None
    session.add_all([document, operation])
