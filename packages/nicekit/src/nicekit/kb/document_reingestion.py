"""Fail-closed restoration of withdrawn source documents through new revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.document_lifecycle import reconcile_snapshot_entity_support
from nicekit.kb.snapshot import activate_snapshot, build_snapshot
from nicekit.models.kb import (
    DocStatus,
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    DocumentRevision,
    KbDocumentOperation,
    KnowledgeBase,
    KnowledgeSnapshot,
    OutboxEvent,
    OutboxStatus,
    RevisionStatus,
    SnapshotStatus,
    SourceDocument,
)

_REINGESTION_EVENT = "document.reingestion.staged"
_INGESTION_FAILURE_STATUSES = {
    DocStatus.FAILED.value,
    DocStatus.CANCELED.value,
    DocStatus.PAUSED.value,
}
_PUBLISHABLE_DOCUMENT_STATUSES = {
    DocStatus.COMPLETED.value,
}
_PUBLISHABLE_REVISION_STATUSES = {
    RevisionStatus.STAGED.value,
    RevisionStatus.ACTIVE.value,
}


class DocumentReingestionError(RuntimeError):
    """A withdrawn document cannot advance through the restoration state machine."""


@dataclass(frozen=True, slots=True)
class DocumentReingestionRequest:
    document: SourceDocument
    revision: DocumentRevision
    operation: KbDocumentOperation
    enqueue_ingestion: bool


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _reset_operation(operation: KbDocumentOperation, *, stage: str) -> None:
    operation.status = DocumentOperationStatus.PENDING
    operation.stage = stage
    operation.retryable = False
    operation.last_error_code = None
    operation.last_error = None
    operation.failed_at = None
    operation.completed_at = None


def _prepare_document_for_ingestion(document: SourceDocument) -> None:
    document.lifecycle_status = DocumentLifecycleStatus.REINGESTION_PENDING
    document.status = DocStatus.UPLOADED
    document.error = None
    document.progress = 0
    document.progress_stage = None
    document.progress_done = 0
    document.progress_total = 0
    document.parsing_started_at = None


async def _latest_withdrawal_operation(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
) -> KbDocumentOperation | None:
    return await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.operation_type
            == DocumentOperationType.WITHDRAWAL.value,
            KbDocumentOperation.status == DocumentOperationStatus.COMPLETED.value,
        )
        .order_by(
            KbDocumentOperation.completed_at.desc(),
            KbDocumentOperation.created_at.desc(),
            KbDocumentOperation.id.desc(),
        )
        .limit(1)
    )


async def _reset_reingestion_event(
    session: AsyncSession,
    *,
    operation: KbDocumentOperation,
) -> bool:
    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.org_id == operation.org_id,
            OutboxEvent.kb_id == operation.kb_id,
            OutboxEvent.event_type == _REINGESTION_EVENT,
            OutboxEvent.payload["operation_id"].astext == str(operation.id),
        )
        .with_for_update()
    )
    if event is None:
        return False
    event.status = OutboxStatus.PENDING
    event.attempts = 0
    event.available_at = datetime.now(UTC)
    event.claimed_by = None
    event.claimed_at = None
    event.claim_expires_at = None
    event.published_at = None
    event.last_error = None
    _reset_operation(operation, stage="retry_scheduled")
    session.add_all([operation, event])
    return True


async def start_or_retry_document_reingestion(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    actor_id: UUID,
) -> DocumentReingestionRequest:
    """Create one recovery revision per completed withdrawal, or retry its failed work."""

    kb = await session.scalar(
        select(KnowledgeBase)
        .join(SourceDocument, SourceDocument.kb_id == KnowledgeBase.id)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
            KnowledgeBase.org_id == org_id,
        )
        .with_for_update(
            read=True,
            key_share=True,
            of=KnowledgeBase,
        )
    )
    if kb is None:
        raise DocumentReingestionError("source document does not exist")
    if _value(kb.lifecycle_status) != "active":
        raise DocumentReingestionError("document knowledge base is not active")

    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentReingestionError("source document does not exist")
    if _value(document.doc_type) == "unclassified":
        raise DocumentReingestionError(
            "document must be classified before reingestion"
        )

    lifecycle = _value(document.lifecycle_status)
    if lifecycle not in {
        DocumentLifecycleStatus.WITHDRAWN.value,
        DocumentLifecycleStatus.REINGESTION_PENDING.value,
    }:
        raise DocumentReingestionError(
            f"document lifecycle {_value(document.lifecycle_status)} cannot be reingested"
        )

    withdrawal = await _latest_withdrawal_operation(
        session,
        org_id=org_id,
        document_id=document.id,
    )
    if withdrawal is None:
        raise DocumentReingestionError(
            "withdrawn document has no completed withdrawal operation"
        )
    operation_key = (
        f"document:{org_id.hex}:{document.id.hex}:"
        f"reingestion:{withdrawal.id.hex}"
    )
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(KbDocumentOperation.idempotency_key == operation_key)
        .with_for_update()
    )
    if operation is not None:
        if operation.revision_id is None:
            raise DocumentReingestionError("reingestion operation has no revision")
        revision = await session.scalar(
            select(DocumentRevision)
            .where(
                DocumentRevision.id == operation.revision_id,
                DocumentRevision.doc_id == document.id,
                DocumentRevision.org_id == org_id,
                DocumentRevision.kb_id == document.kb_id,
            )
            .with_for_update()
        )
        if revision is None:
            raise DocumentReingestionError("reingestion revision does not exist")
        operation_status = _value(operation.status)
        if operation_status in {
            DocumentOperationStatus.PENDING.value,
            DocumentOperationStatus.PROCESSING.value,
        }:
            if lifecycle != DocumentLifecycleStatus.REINGESTION_PENDING.value:
                raise DocumentReingestionError(
                    "reingestion operation and document lifecycle are inconsistent"
                )
            return DocumentReingestionRequest(document, revision, operation, False)
        if operation_status not in {
            DocumentOperationStatus.FAILED.value,
            DocumentOperationStatus.DEAD_LETTER.value,
        }:
            raise DocumentReingestionError("reingestion operation is not retryable")
        if lifecycle != DocumentLifecycleStatus.WITHDRAWN.value:
            raise DocumentReingestionError(
                "failed reingestion must return the document to withdrawn"
            )
        if await _reset_reingestion_event(session, operation=operation):
            document.lifecycle_status = DocumentLifecycleStatus.REINGESTION_PENDING
            operation.impact_summary = {
                **operation.impact_summary,
                "consumption_epoch": kb.consumption_epoch,
            }
            session.add(document)
            await session.flush()
            return DocumentReingestionRequest(document, revision, operation, False)

        revision.status = RevisionStatus.UPLOADED
        revision.error = None
        _prepare_document_for_ingestion(document)
        _reset_operation(operation, stage="ingestion")
        operation.impact_summary = {
            **operation.impact_summary,
            "consumption_epoch": kb.consumption_epoch,
        }
        session.add_all([document, revision, operation])
        await session.flush()
        return DocumentReingestionRequest(document, revision, operation, True)

    if lifecycle != DocumentLifecycleStatus.WITHDRAWN.value:
        raise DocumentReingestionError("reingestion is already in progress")

    latest_revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.doc_id == document.id,
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == document.kb_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if latest_revision is None:
        raise DocumentReingestionError("source document has no revision")
    if (
        _value(latest_revision.status) != RevisionStatus.TOMBSTONED.value
        or latest_revision.tombstoned_at is None
    ):
        raise DocumentReingestionError(
            "withdrawn document latest revision has no complete tombstone"
        )

    revision = DocumentRevision(
        id=uuid4(),
        org_id=document.org_id,
        kb_id=document.kb_id,
        doc_id=document.id,
        revision_no=latest_revision.revision_no + 1,
        sha256=latest_revision.sha256,
        original_object_key=latest_revision.original_object_key,
    )
    operation = KbDocumentOperation(
        id=uuid4(),
        org_id=document.org_id,
        kb_id=document.kb_id,
        document_id=document.id,
        revision_id=revision.id,
        operation_type=DocumentOperationType.REINGESTION,
        status=DocumentOperationStatus.PENDING,
        stage="ingestion",
        idempotency_key=operation_key,
        requested_by=actor_id,
        reason="reingest withdrawn source document",
        impact_summary={
            "withdrawal_operation_id": str(withdrawal.id),
            "withdrawn_revision_id": str(latest_revision.id),
            "consumption_epoch": kb.consumption_epoch,
        },
    )
    document.sha256 = revision.sha256
    document.object_key = revision.original_object_key
    document.markdown_key = None
    document.parser_name = None
    _prepare_document_for_ingestion(document)
    session.add_all([document, revision, operation])
    await session.flush()
    return DocumentReingestionRequest(document, revision, operation, True)


async def settle_reingestion_ingest_result(
    session: AsyncSession,
    *,
    document: SourceDocument,
    revision: DocumentRevision,
) -> None:
    """Advance a recovery operation after its root ingest run reaches a durable state."""

    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document.id,
            SourceDocument.org_id == document.org_id,
            SourceDocument.kb_id == document.kb_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentReingestionError("reingestion document does not exist")
    revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.id == revision.id,
            DocumentRevision.doc_id == document.id,
            DocumentRevision.org_id == document.org_id,
            DocumentRevision.kb_id == document.kb_id,
        )
        .with_for_update()
    )
    if revision is None:
        raise DocumentReingestionError("reingestion revision does not exist")
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.org_id == document.org_id,
            KbDocumentOperation.kb_id == document.kb_id,
            KbDocumentOperation.document_id == document.id,
            KbDocumentOperation.revision_id == revision.id,
            KbDocumentOperation.operation_type
            == DocumentOperationType.REINGESTION.value,
        )
        .with_for_update()
    )
    if operation is None:
        return
    if _value(operation.status) == DocumentOperationStatus.COMPLETED.value:
        return
    if (
        _value(document.lifecycle_status)
        != DocumentLifecycleStatus.REINGESTION_PENDING.value
    ):
        raise DocumentReingestionError(
            "reingestion ingest result has an inconsistent document lifecycle"
        )

    document_status = _value(document.status)
    publishable = (
        document_status in _PUBLISHABLE_DOCUMENT_STATUSES
        and _value(revision.status) in _PUBLISHABLE_REVISION_STATUSES
        and not document.error
        and not revision.error
    )
    if publishable:
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.idempotency_key == f"{operation.idempotency_key}:release"
            )
        )
        if event is None:
            event = OutboxEvent(
                id=uuid4(),
                org_id=document.org_id,
                kb_id=document.kb_id,
                aggregate_type="source_document",
                aggregate_id=document.id,
                event_type=_REINGESTION_EVENT,
                idempotency_key=f"{operation.idempotency_key}:release",
                payload={
                    "document_id": str(document.id),
                    "revision_id": str(revision.id),
                    "operation_id": str(operation.id),
                },
            )
            session.add(event)
        _reset_operation(operation, stage="snapshot_rebuild")
        session.add(operation)
        await session.flush()
        return

    if (
        document_status not in _INGESTION_FAILURE_STATUSES
        and document_status not in _PUBLISHABLE_DOCUMENT_STATUSES
    ):
        return

    now = datetime.now(UTC)
    document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWN
    operation.status = DocumentOperationStatus.FAILED
    operation.stage = "ingestion"
    operation.retryable = True
    operation.last_error_code = "reingestion_ingest_failed"
    operation.last_error = (
        document.error or revision.error or f"document ingestion ended as {document_status}"
    )
    operation.failed_at = now
    operation.completed_at = None
    session.add_all([document, operation])
    await session.flush()


async def _eligible_active_baseline_revision_ids(
    session: AsyncSession,
    *,
    snapshot: KnowledgeSnapshot | None,
    excluded_document_id: UUID,
) -> list[UUID]:
    if snapshot is None:
        return []
    candidates: list[UUID] = []
    for item in snapshot.revision_manifest:
        if not isinstance(item, dict):
            continue
        try:
            document_id = UUID(str(item["doc_id"]))
            revision_id = UUID(str(item["revision_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if document_id != excluded_document_id:
            candidates.append(revision_id)
    if not candidates:
        return []
    return list(
        (
            await session.scalars(
                select(DocumentRevision.id)
                .join(SourceDocument, SourceDocument.id == DocumentRevision.doc_id)
                .where(
                    DocumentRevision.id.in_(candidates),
                    DocumentRevision.org_id == snapshot.org_id,
                    DocumentRevision.kb_id == snapshot.kb_id,
                    DocumentRevision.status.in_(_PUBLISHABLE_REVISION_STATUSES),
                    DocumentRevision.tombstoned_at.is_(None),
                    SourceDocument.org_id == snapshot.org_id,
                    SourceDocument.kb_id == snapshot.kb_id,
                    SourceDocument.lifecycle_status
                    == DocumentLifecycleStatus.ACTIVE.value,
                )
            )
        ).all()
    )


async def publish_reingested_document(
    session: AsyncSession,
    event: OutboxEvent,
) -> None:
    """Atomically activate the recovery snapshot and make its document active."""

    if event.event_type != _REINGESTION_EVENT:
        raise ValueError("unsupported document reingestion event")
    try:
        document_id = UUID(str(event.payload["document_id"]))
        revision_id = UUID(str(event.payload["revision_id"]))
        operation_id = UUID(str(event.payload["operation_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DocumentReingestionError("reingestion event payload is invalid") from exc
    if event.aggregate_type != "source_document" or event.aggregate_id != document_id:
        raise DocumentReingestionError("reingestion event aggregate identity is invalid")

    kb = await session.scalar(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.id == event.kb_id,
            KnowledgeBase.org_id == event.org_id,
        )
        .with_for_update()
    )
    if kb is None:
        raise DocumentReingestionError("reingestion knowledge base does not exist")
    if kb.lifecycle_status != "active":
        raise DocumentReingestionError("reingestion knowledge base is not active")
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == event.org_id,
            SourceDocument.kb_id == event.kb_id,
        )
        .with_for_update()
    )
    revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.id == revision_id,
            DocumentRevision.doc_id == document_id,
            DocumentRevision.org_id == event.org_id,
            DocumentRevision.kb_id == event.kb_id,
        )
        .with_for_update()
    )
    if document is None or revision is None:
        raise DocumentReingestionError("reingestion document or revision does not exist")
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == event.org_id,
            KbDocumentOperation.kb_id == event.kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.revision_id == revision_id,
            KbDocumentOperation.operation_type
            == DocumentOperationType.REINGESTION.value,
        )
        .with_for_update()
    )
    if operation is None:
        raise DocumentReingestionError("reingestion operation does not exist")
    if _value(operation.status) == DocumentOperationStatus.COMPLETED.value:
        return
    if (
        _value(document.lifecycle_status)
        != DocumentLifecycleStatus.REINGESTION_PENDING.value
    ):
        raise DocumentReingestionError("document is not awaiting reingestion publication")
    captured_epoch = operation.impact_summary.get("consumption_epoch")
    if (
        isinstance(captured_epoch, bool)
        or not isinstance(captured_epoch, int)
        or captured_epoch != kb.consumption_epoch
    ):
        raise DocumentReingestionError(
            "knowledge base consumption boundary changed during reingestion"
        )
    if (
        _value(revision.status) not in _PUBLISHABLE_REVISION_STATUSES
        or revision.tombstoned_at is not None
        or revision.error
    ):
        raise DocumentReingestionError("reingestion revision is not publishable")
    latest_revision_id = await session.scalar(
        select(DocumentRevision.id)
        .where(DocumentRevision.doc_id == document.id)
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    if latest_revision_id != revision.id:
        raise DocumentReingestionError("reingestion revision is not the latest revision")

    active_snapshot = (
        await session.get(KnowledgeSnapshot, kb.active_snapshot_id)
        if kb.active_snapshot_id is not None
        else None
    )
    baseline_revision_ids = await _eligible_active_baseline_revision_ids(
        session,
        snapshot=active_snapshot,
        excluded_document_id=document.id,
    )

    operation.status = DocumentOperationStatus.PROCESSING
    operation.stage = "snapshot_rebuild"
    operation.attempts = event.attempts
    operation.started_at = operation.started_at or datetime.now(UTC)
    operation.retryable = False
    operation.last_error_code = None
    operation.last_error = None
    operation.failed_at = None
    document.lifecycle_status = DocumentLifecycleStatus.ACTIVE
    kb.consumption_epoch += 1
    session.add_all([kb, document, operation])
    await session.flush()

    snapshot = await build_snapshot(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        revision_ids=[*baseline_revision_ids, revision.id],
        actor_id=operation.requested_by,
        reason="withdrawn document reingested",
    )
    manifest_revision_ids = {
        UUID(str(item["revision_id"]))
        for item in snapshot.revision_manifest
        if isinstance(item, dict) and item.get("revision_id")
    }
    if revision.id not in manifest_revision_ids:
        raise DocumentReingestionError(
            "reingestion snapshot does not contain the recovery revision"
        )
    snapshot_status = _value(snapshot.status)
    operation.stage = "snapshot_activation"
    if snapshot_status != SnapshotStatus.READY.value:
        raise DocumentReingestionError(snapshot.error or "reingestion snapshot build failed")
    await activate_snapshot(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        snapshot_id=snapshot.id,
        actor_id=operation.requested_by,
        reason="withdrawn document reingested",
        operation_id=operation.id,
    )
    await reconcile_snapshot_entity_support(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        snapshot_id=snapshot.id,
    )

    operation.status = DocumentOperationStatus.COMPLETED
    operation.stage = "completed"
    operation.target_snapshot_id = snapshot.id
    operation.retryable = False
    operation.completed_at = datetime.now(UTC)
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error = None
    session.add_all([document, operation])
    await session.flush()
