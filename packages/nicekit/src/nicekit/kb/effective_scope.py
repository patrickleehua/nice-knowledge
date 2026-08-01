"""Shared fail-closed predicates for active knowledge consumption."""

from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.sql.elements import ColumnElement

from nicekit.models.kb import (
    DocumentLifecycleStatus,
    DocumentRevision,
    FactClaim,
    FactReviewStatus,
    KbChunk,
    KnowledgeBase,
    RevisionStatus,
    SnapshotFactSupport,
    SnapshotProjectionSupport,
    SourceDocument,
)


def active_knowledge_base_filter(
    knowledge_base: type[KnowledgeBase] = KnowledgeBase,
) -> ColumnElement[bool]:
    """Require a knowledge base to be eligible for current consumption."""

    return knowledge_base.lifecycle_status == "active"


def active_knowledge_base_membership_filter(
    model: type[Any],
) -> ColumnElement[bool]:
    """Require a scoped row to belong to an active knowledge base."""

    if not all(hasattr(model, field) for field in ("org_id", "kb_id")):
        raise TypeError("knowledge-base-scoped model must expose org_id and kb_id")
    return exists(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == model.kb_id,
            KnowledgeBase.org_id == model.org_id,
            active_knowledge_base_filter(),
        )
    )


def live_document_revision_filter(
    revision: type[DocumentRevision] = DocumentRevision,
    document: type[SourceDocument] = SourceDocument,
) -> ColumnElement[bool]:
    """Return the document withdrawal fence shared by all source readers."""

    return and_(
        revision.status != RevisionStatus.TOMBSTONED.value,
        revision.tombstoned_at.is_(None),
        document.lifecycle_status == DocumentLifecycleStatus.ACTIVE.value,
    )


def current_snapshot_filter(model: type[Any]) -> ColumnElement[bool]:
    """Require a row to belong to its KB's current active snapshot."""

    if not all(hasattr(model, field) for field in ("org_id", "kb_id", "snapshot_id")):
        raise TypeError("snapshot-scoped model must expose org_id, kb_id, and snapshot_id")
    return exists(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == model.kb_id,
            KnowledgeBase.org_id == model.org_id,
            KnowledgeBase.active_snapshot_id == model.snapshot_id,
            active_knowledge_base_filter(),
        )
    )


def effective_chunk_filter(
    chunk: type[KbChunk] = KbChunk,
) -> ColumnElement[bool]:
    """Require current snapshot membership and live source provenance.

    Snapshot entity cards intentionally have neither revision nor document.
    They remain candidate-only and must be proven by active fact support during
    final hydration.
    """

    active_source_revision = exists(
        select(DocumentRevision.id)
        .join(
            SourceDocument,
            and_(
                SourceDocument.id == DocumentRevision.doc_id,
                SourceDocument.org_id == DocumentRevision.org_id,
                SourceDocument.kb_id == DocumentRevision.kb_id,
            ),
        )
        .where(
            DocumentRevision.id == chunk.revision_id,
            DocumentRevision.doc_id == chunk.source_doc_id,
            DocumentRevision.org_id == chunk.org_id,
            DocumentRevision.kb_id == chunk.kb_id,
            live_document_revision_filter(),
        )
    )
    snapshot_card = and_(
        chunk.revision_id.is_(None),
        chunk.source_doc_id.is_(None),
    )
    return and_(
        chunk.snapshot_id.is_not(None),
        current_snapshot_filter(chunk),
        or_(snapshot_card, active_source_revision),
    )


def live_projection_source_filter(model: type[Any]) -> ColumnElement[bool]:
    """Fence projection rows that explicitly retain a source document."""

    if not all(hasattr(model, field) for field in ("org_id", "kb_id", "source_doc_id")):
        raise TypeError(
            "source-backed projection must expose org_id, kb_id, and source_doc_id"
        )
    return or_(
        model.source_doc_id.is_(None),
        exists(
            select(SourceDocument.id).where(
                SourceDocument.id == model.source_doc_id,
                SourceDocument.org_id == model.org_id,
                SourceDocument.kb_id == model.kb_id,
                SourceDocument.lifecycle_status
                == DocumentLifecycleStatus.ACTIVE.value,
            )
        ),
    )


def live_snapshot_projection_filter(
    model: type[Any],
    projection_type: str,
) -> ColumnElement[bool]:
    """Require a fact-derived row to retain at least one live snapshot support."""

    if not all(
        hasattr(model, field)
        for field in ("id", "org_id", "kb_id", "snapshot_id")
    ):
        raise TypeError(
            "fact-derived projection must expose id, org_id, kb_id, and snapshot_id"
        )
    normalized_type = projection_type.strip()
    if not normalized_type:
        raise ValueError("projection_type must not be blank")

    live_support = exists(
        select(SnapshotProjectionSupport.id)
        .join(
            SnapshotFactSupport,
            and_(
                SnapshotFactSupport.id
                == SnapshotProjectionSupport.fact_support_id,
                SnapshotFactSupport.org_id == SnapshotProjectionSupport.org_id,
                SnapshotFactSupport.kb_id == SnapshotProjectionSupport.kb_id,
                SnapshotFactSupport.snapshot_id
                == SnapshotProjectionSupport.snapshot_id,
            ),
        )
        .join(
            FactClaim,
            and_(
                FactClaim.id == SnapshotFactSupport.fact_claim_id,
                FactClaim.org_id == SnapshotFactSupport.org_id,
                FactClaim.kb_id == SnapshotFactSupport.kb_id,
            ),
        )
        .join(
            DocumentRevision,
            and_(
                DocumentRevision.id == SnapshotFactSupport.revision_id,
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
            SnapshotProjectionSupport.org_id == model.org_id,
            SnapshotProjectionSupport.kb_id == model.kb_id,
            SnapshotProjectionSupport.snapshot_id == model.snapshot_id,
            SnapshotProjectionSupport.projection_type == normalized_type,
            SnapshotProjectionSupport.projection_row_id == model.id,
            FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
            live_document_revision_filter(),
        )
    )
    managed = and_(
        model.snapshot_id.is_not(None),
        current_snapshot_filter(model),
        live_support,
    )
    if hasattr(model, "source_doc_id"):
        legacy = and_(
            model.snapshot_id.is_(None),
            live_projection_source_filter(model),
        )
        return and_(
            active_knowledge_base_membership_filter(model),
            or_(managed, legacy),
        )
    return and_(
        active_knowledge_base_membership_filter(model),
        managed,
    )


__all__ = [
    "active_knowledge_base_filter",
    "active_knowledge_base_membership_filter",
    "current_snapshot_filter",
    "effective_chunk_filter",
    "live_document_revision_filter",
    "live_projection_source_filter",
    "live_snapshot_projection_filter",
]
