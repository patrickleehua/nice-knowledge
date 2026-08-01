"""Reference-aware, object-first permanent purge for withdrawn KB documents.

The durable purge plan and per-object progress live in
``KbDocumentOperation.impact_summary["purge"]``.  Object keys are present only
while an operation is incomplete; successful completion replaces the internal
manifest with a content-free ledger.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb import ports, storage
from nicekit.kb.projection_gc import _externally_referenced_snapshot_ids
from nicekit.kb.reference_registry import (
    ReferenceTargets as _ReferenceTargets,
)
from nicekit.kb.reference_registry import (
    business_reference_counts as _business_reference_counts,
)
from nicekit.kb.reference_registry import (
    feedback_reference_count as _feedback_reference_count,
)
from nicekit.models.kb import (
    CanonicalEntity,
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    GraphEdge,
    IngestRun,
    KbChunk,
    KbChunkEmbedding,
    KbDocumentOperation,
    KbEntity,
    KbImageAsset,
    KbImageAssetEvent,
    KbPage,
    KbSnapshotEntityNodeSupport,
    KbSnapshotImageAsset,
    KnowledgeSnapshot,
    SnapshotFactSupport,
    SnapshotStatus,
    SourceDocument,
)

PURGE_PLAN_VERSION = "kb-document-purge/v1"
_TERMINAL_OBJECT_STATES = frozenset({"deleted", "already_missing"})
ObjectExists = Callable[[str], Awaitable[bool]]
DeleteObject = Callable[[str], Awaitable[None]]
PersistProgress = Callable[[list[dict[str, object]]], Awaitable[None]]


class PurgeBlockerCode(StrEnum):
    DOCUMENT_NOT_WITHDRAWN = "DOCUMENT_NOT_WITHDRAWN"
    RETENTION_STATE_UNAVAILABLE = "RETENTION_STATE_UNAVAILABLE"
    RETENTION_PERIOD_ACTIVE = "RETENTION_PERIOD_ACTIVE"
    LEGAL_HOLD_ACTIVE = "LEGAL_HOLD_ACTIVE"
    REFERENCE_REGISTRY_UNAVAILABLE = "REFERENCE_REGISTRY_UNAVAILABLE"
    KNOWLEDGE_SNAPSHOT_REFERENCE = "KNOWLEDGE_SNAPSHOT_REFERENCE"
    RETRIEVAL_SNAPSHOT_REFERENCE = "RETRIEVAL_SNAPSHOT_REFERENCE"
    BUSINESS_ARTIFACT_REFERENCE = "BUSINESS_ARTIFACT_REFERENCE"
    FEEDBACK_OR_CITATION_REFERENCE = "FEEDBACK_OR_CITATION_REFERENCE"
    PINNED_ENTITY_REFERENCE = "PINNED_ENTITY_REFERENCE"
    MANUAL_OR_PENDING_FACT_REFERENCE = "MANUAL_OR_PENDING_FACT_REFERENCE"
    MEDIA_REFERENCE = "MEDIA_REFERENCE"
    SHARED_OBJECT_KEY_REFERENCE = "SHARED_OBJECT_KEY_REFERENCE"
    CROSS_REVISION_CONTENT_REFERENCE = "CROSS_REVISION_CONTENT_REFERENCE"


# 管理员强制清理可跳过的 blocker:保留期与引用类(跳过即接受引用方死链,前端明示)。
# 不可跳过:法律保留(合规)、登记不可用(无法安全枚举)、未撤回状态门禁(必须先走撤回
# 重建流程)、共享对象/跨修订共享内容(删了会物理损坏其他资料)。
FORCE_BYPASSABLE_BLOCKER_CODES = frozenset(
    {
        PurgeBlockerCode.RETENTION_PERIOD_ACTIVE,
        PurgeBlockerCode.KNOWLEDGE_SNAPSHOT_REFERENCE,
        PurgeBlockerCode.RETRIEVAL_SNAPSHOT_REFERENCE,
        PurgeBlockerCode.BUSINESS_ARTIFACT_REFERENCE,
        PurgeBlockerCode.FEEDBACK_OR_CITATION_REFERENCE,
        PurgeBlockerCode.PINNED_ENTITY_REFERENCE,
        PurgeBlockerCode.MANUAL_OR_PENDING_FACT_REFERENCE,
        PurgeBlockerCode.MEDIA_REFERENCE,
    }
)


class DocumentPurgeError(RuntimeError):
    """Base class for stable purge errors."""


class DocumentPurgeNotFound(DocumentPurgeError):
    pass


class DocumentPurgeForbidden(DocumentPurgeError):
    pass


class DocumentPurgeBlocked(DocumentPurgeError):
    def __init__(self, plan: DocumentPurgePlan) -> None:
        super().__init__("document purge is blocked")
        self.plan = plan


class DocumentPurgePlanDrift(DocumentPurgeError):
    def __init__(self, plan: DocumentPurgePlan) -> None:
        super().__init__("document purge plan has changed")
        self.plan = plan


class DocumentPurgeExecutionError(DocumentPurgeError):
    def __init__(self, code: str, stage: str) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage


@dataclass(frozen=True, slots=True)
class PurgeBlocker:
    code: PurgeBlockerCode
    count: int
    retry_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code.value,
            "count": self.count,
        }
        if self.retry_at is not None:
            result["retry_at"] = self.retry_at.isoformat()
        return result


@dataclass(frozen=True, slots=True)
class PurgeObjectCandidate:
    key: str
    category: str

    @property
    def key_fingerprint(self) -> str:
        return sha256(self.key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentPurgeInventory:
    org_id: UUID
    kb_id: UUID
    document_id: UUID
    lifecycle_status: str
    withdrawn_at: datetime | None
    retention_days: int
    legal_hold_active: bool
    reference_registry_complete: bool
    revision_ids: tuple[UUID, ...] = ()
    chunk_ids: tuple[UUID, ...] = ()
    image_asset_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    exclusive_claim_ids: tuple[UUID, ...] = ()
    shared_claim_ids: tuple[UUID, ...] = ()
    exclusive_entity_ids: tuple[UUID, ...] = ()
    shared_entity_ids: tuple[UUID, ...] = ()
    exclusive_relation_count: int = 0
    shared_relation_count: int = 0
    ingest_run_ids: tuple[UUID, ...] = ()
    object_candidates: tuple[PurgeObjectCandidate, ...] = ()
    shared_object_key_count: int = 0
    snapshot_reference_count: int = 0
    retrieval_reference_count: int = 0
    business_reference_count: int = 0
    feedback_or_citation_reference_count: int = 0
    pinned_entity_count: int = 0
    manual_or_pending_fact_count: int = 0
    media_reference_count: int = 0
    cross_revision_content_reference_count: int = 0


@dataclass(frozen=True, slots=True)
class DocumentPurgePlan:
    version: str
    org_id: UUID
    kb_id: UUID
    document_id: UUID
    plan_hash: str
    eligible: bool
    blockers: tuple[PurgeBlocker, ...]
    delete_counts: Mapping[str, int]
    retain_counts: Mapping[str, int]
    retention_deadline: datetime | None
    revision_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    image_asset_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    exclusive_claim_ids: tuple[UUID, ...]
    ingest_run_ids: tuple[UUID, ...]
    object_candidates: tuple[PurgeObjectCandidate, ...]

    def as_public_dict(self) -> dict[str, object]:
        """Return the API-safe view; object keys and internal row IDs stay private."""

        return {
            "version": self.version,
            "document_id": str(self.document_id),
            "plan_hash": self.plan_hash,
            "eligible": self.eligible,
            "blockers": [blocker.as_dict() for blocker in self.blockers],
            "delete_counts": dict(self.delete_counts),
            "retain_counts": dict(self.retain_counts),
            "retention_deadline": (
                self.retention_deadline.isoformat() if self.retention_deadline is not None else None
            ),
        }


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _sorted_uuids(values: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values), key=str))


def _canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _plan_payload(
    inventory: DocumentPurgeInventory,
    *,
    blockers: Sequence[PurgeBlocker],
    retention_deadline: datetime | None,
    delete_counts: Mapping[str, int],
    retain_counts: Mapping[str, int],
) -> dict[str, object]:
    return {
        "version": PURGE_PLAN_VERSION,
        "org_id": str(inventory.org_id),
        "kb_id": str(inventory.kb_id),
        "document_id": str(inventory.document_id),
        "retention_deadline": (
            retention_deadline.isoformat() if retention_deadline is not None else None
        ),
        "blockers": [
            {
                "code": blocker.code.value,
                "count": blocker.count,
                "retry_at": (
                    blocker.retry_at.isoformat() if blocker.retry_at is not None else None
                ),
            }
            for blocker in blockers
        ],
        "delete_counts": dict(sorted(delete_counts.items())),
        "retain_counts": dict(sorted(retain_counts.items())),
        "targets": {
            "revision_ids": [str(value) for value in inventory.revision_ids],
            "chunk_ids": [str(value) for value in inventory.chunk_ids],
            "image_asset_ids": [str(value) for value in inventory.image_asset_ids],
            "evidence_ids": [str(value) for value in inventory.evidence_ids],
            "exclusive_claim_ids": [str(value) for value in inventory.exclusive_claim_ids],
            "ingest_run_ids": [str(value) for value in inventory.ingest_run_ids],
        },
        "objects": [
            {
                "category": candidate.category,
                "key": candidate.key,
                "key_fingerprint": candidate.key_fingerprint,
            }
            for candidate in inventory.object_candidates
        ],
    }


def build_document_purge_plan(
    inventory: DocumentPurgeInventory,
    *,
    now: datetime | None = None,
    allow_purge_pending: bool = False,
) -> DocumentPurgePlan:
    """Build a deterministic, versioned plan from a tenant-scoped inventory."""

    current_time = now or datetime.now(UTC)
    blockers: list[PurgeBlocker] = []
    allowed_lifecycle = {DocumentLifecycleStatus.WITHDRAWN.value}
    if allow_purge_pending:
        allowed_lifecycle.add(DocumentLifecycleStatus.PURGE_PENDING.value)
    if inventory.lifecycle_status not in allowed_lifecycle:
        blockers.append(PurgeBlocker(PurgeBlockerCode.DOCUMENT_NOT_WITHDRAWN, 1))

    retention_deadline: datetime | None = None
    if inventory.withdrawn_at is None:
        blockers.append(PurgeBlocker(PurgeBlockerCode.RETENTION_STATE_UNAVAILABLE, 1))
    else:
        retention_deadline = inventory.withdrawn_at + timedelta(days=inventory.retention_days)
        if current_time < retention_deadline:
            blockers.append(
                PurgeBlocker(
                    PurgeBlockerCode.RETENTION_PERIOD_ACTIVE,
                    1,
                    retry_at=retention_deadline,
                )
            )
    if inventory.legal_hold_active:
        blockers.append(PurgeBlocker(PurgeBlockerCode.LEGAL_HOLD_ACTIVE, 1))
    if not inventory.reference_registry_complete:
        blockers.append(PurgeBlocker(PurgeBlockerCode.REFERENCE_REGISTRY_UNAVAILABLE, 1))

    counted_blockers = (
        (
            PurgeBlockerCode.KNOWLEDGE_SNAPSHOT_REFERENCE,
            inventory.snapshot_reference_count,
        ),
        (
            PurgeBlockerCode.RETRIEVAL_SNAPSHOT_REFERENCE,
            inventory.retrieval_reference_count,
        ),
        (
            PurgeBlockerCode.BUSINESS_ARTIFACT_REFERENCE,
            inventory.business_reference_count,
        ),
        (
            PurgeBlockerCode.FEEDBACK_OR_CITATION_REFERENCE,
            inventory.feedback_or_citation_reference_count,
        ),
        (PurgeBlockerCode.PINNED_ENTITY_REFERENCE, inventory.pinned_entity_count),
        (
            PurgeBlockerCode.MANUAL_OR_PENDING_FACT_REFERENCE,
            inventory.manual_or_pending_fact_count,
        ),
        (PurgeBlockerCode.MEDIA_REFERENCE, inventory.media_reference_count),
        (
            PurgeBlockerCode.SHARED_OBJECT_KEY_REFERENCE,
            inventory.shared_object_key_count,
        ),
        (
            PurgeBlockerCode.CROSS_REVISION_CONTENT_REFERENCE,
            inventory.cross_revision_content_reference_count,
        ),
    )
    blockers.extend(PurgeBlocker(code, count) for code, count in counted_blockers if count > 0)
    blockers.sort(key=lambda blocker: blocker.code.value)

    delete_counts = {
        "objects": len(inventory.object_candidates),
        "revisions": len(inventory.revision_ids),
        "chunks": len(inventory.chunk_ids),
        "media": len(inventory.image_asset_ids),
        "evidence": len(inventory.evidence_ids),
        "fact_claims": len(inventory.exclusive_claim_ids),
        "ingest_runs": len(inventory.ingest_run_ids),
    }
    retain_counts = {
        "shared_fact_claims": len(inventory.shared_claim_ids),
        "shared_entities": len(inventory.shared_entity_ids),
        "shared_relations": inventory.shared_relation_count,
        "exclusive_entities_for_gc": len(inventory.exclusive_entity_ids),
        "shared_object_keys": inventory.shared_object_key_count,
    }
    payload = _plan_payload(
        inventory,
        blockers=blockers,
        retention_deadline=retention_deadline,
        delete_counts=delete_counts,
        retain_counts=retain_counts,
    )
    return DocumentPurgePlan(
        version=PURGE_PLAN_VERSION,
        org_id=inventory.org_id,
        kb_id=inventory.kb_id,
        document_id=inventory.document_id,
        plan_hash=_canonical_hash(payload),
        eligible=not blockers,
        blockers=tuple(blockers),
        delete_counts=delete_counts,
        retain_counts=retain_counts,
        retention_deadline=retention_deadline,
        revision_ids=inventory.revision_ids,
        chunk_ids=inventory.chunk_ids,
        image_asset_ids=inventory.image_asset_ids,
        evidence_ids=inventory.evidence_ids,
        exclusive_claim_ids=inventory.exclusive_claim_ids,
        ingest_run_ids=inventory.ingest_run_ids,
        object_candidates=inventory.object_candidates,
    )


def _manifest_for_plan(plan: DocumentPurgePlan) -> dict[str, object]:
    return {
        "version": plan.version,
        "plan_hash": plan.plan_hash,
        "phase": "planned",
        "summary": plan.as_public_dict(),
        "targets": {
            "revision_ids": [str(value) for value in plan.revision_ids],
            "chunk_ids": [str(value) for value in plan.chunk_ids],
            "image_asset_ids": [str(value) for value in plan.image_asset_ids],
            "evidence_ids": [str(value) for value in plan.evidence_ids],
            "exclusive_claim_ids": [str(value) for value in plan.exclusive_claim_ids],
            "ingest_run_ids": [str(value) for value in plan.ingest_run_ids],
        },
        "objects": [
            {
                "category": candidate.category,
                "key": candidate.key,
                "key_fingerprint": candidate.key_fingerprint,
                "status": "pending",
                "attempts": 0,
            }
            for candidate in plan.object_candidates
        ],
        "verification": {},
    }


def _uuid_values(manifest: Mapping[str, object], key: str) -> tuple[UUID, ...]:
    targets = manifest.get("targets")
    if not isinstance(targets, Mapping):
        raise DocumentPurgeExecutionError("PURGE_MANIFEST_INVALID", "load_manifest")
    values = targets.get(key)
    if not isinstance(values, list):
        raise DocumentPurgeExecutionError("PURGE_MANIFEST_INVALID", "load_manifest")
    try:
        return tuple(UUID(str(value)) for value in values)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DocumentPurgeExecutionError("PURGE_MANIFEST_INVALID", "load_manifest") from exc


def _add_object_key(
    catalog: dict[str, set[str]],
    key: str | None,
    category: str,
) -> None:
    if key and key.strip():
        catalog.setdefault(key, set()).add(category)


def _snapshot_has_revision(
    snapshot: KnowledgeSnapshot,
    revision_ids: set[UUID],
) -> bool:
    for item in snapshot.revision_manifest:
        if not isinstance(item, Mapping):
            continue
        value = item.get("revision_id")
        try:
            if value is not None and UUID(str(value)) in revision_ids:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def _target_strings(values: Iterable[UUID]) -> frozenset[str]:
    return frozenset(str(value) for value in values)


async def _shared_object_keys(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    revision_ids: set[UUID],
    image_asset_ids: set[UUID],
    keys: set[str],
) -> set[str]:
    if not keys:
        return set()
    referenced: set[str] = set()
    document_rows = (
        await session.execute(
            select(SourceDocument.object_key, SourceDocument.markdown_key).where(
                SourceDocument.org_id == org_id,
                SourceDocument.id != document_id,
                or_(
                    SourceDocument.object_key.in_(keys),
                    SourceDocument.markdown_key.in_(keys),
                ),
            )
        )
    ).all()
    revision_rows = (
        await session.execute(
            select(
                DocumentRevision.original_object_key,
                DocumentRevision.structured_json_key,
                DocumentRevision.markdown_key,
            ).where(
                DocumentRevision.org_id == org_id,
                DocumentRevision.id.not_in(revision_ids),
                or_(
                    DocumentRevision.original_object_key.in_(keys),
                    DocumentRevision.structured_json_key.in_(keys),
                    DocumentRevision.markdown_key.in_(keys),
                ),
            )
        )
    ).all()
    image_rows = (
        await session.execute(
            select(
                KbImageAsset.original_object_key,
                KbImageAsset.thumbnail_object_key,
            ).where(
                KbImageAsset.org_id == org_id,
                KbImageAsset.id.not_in(image_asset_ids),
                or_(
                    KbImageAsset.original_object_key.in_(keys),
                    KbImageAsset.thumbnail_object_key.in_(keys),
                ),
            )
        )
    ).all()
    for row in [*document_rows, *revision_rows, *image_rows]:
        referenced.update(str(value) for value in row if value is not None and str(value) in keys)
    return referenced


async def _collect_document_purge_inventory(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    retention_days: int,
    legal_hold_active: bool | None,
    reference_registry_complete: bool,
) -> DocumentPurgeInventory:
    document = await session.scalar(
        select(SourceDocument).where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
        )
    )
    if document is None:
        raise DocumentPurgeNotFound("source document does not exist")
    effective_legal_hold = (
        document.legal_hold_at is not None
        if legal_hold_active is None
        else legal_hold_active
    )

    revisions = tuple(
        (
            await session.scalars(
                select(DocumentRevision).where(
                    DocumentRevision.org_id == org_id,
                    DocumentRevision.kb_id == document.kb_id,
                    DocumentRevision.doc_id == document_id,
                )
            )
        ).all()
    )
    revision_ids = {revision.id for revision in revisions}
    ingest_runs = tuple(
        (
            await session.scalars(
                select(IngestRun).where(
                    IngestRun.org_id == org_id,
                    IngestRun.kb_id == document.kb_id,
                    IngestRun.revision_id.in_(revision_ids),
                )
            )
        ).all()
    )
    ingest_run_ids = {run.id for run in ingest_runs}
    chunks = tuple(
        (
            await session.scalars(
                select(KbChunk).where(
                    KbChunk.org_id == org_id,
                    KbChunk.kb_id == document.kb_id,
                    or_(
                        KbChunk.source_doc_id == document_id,
                        KbChunk.revision_id.in_(revision_ids),
                    ),
                )
            )
        ).all()
    )
    chunk_ids = {chunk.id for chunk in chunks}
    images = tuple(
        (
            await session.scalars(
                select(KbImageAsset).where(
                    KbImageAsset.org_id == org_id,
                    KbImageAsset.kb_id == document.kb_id,
                    or_(
                        KbImageAsset.doc_id == document_id,
                        KbImageAsset.revision_id.in_(revision_ids),
                    ),
                )
            )
        ).all()
    )
    image_asset_ids = {image.id for image in images}
    evidence = tuple(
        (
            await session.scalars(
                select(EvidenceSpan).where(
                    EvidenceSpan.org_id == org_id,
                    EvidenceSpan.kb_id == document.kb_id,
                    EvidenceSpan.revision_id.in_(revision_ids),
                )
            )
        ).all()
    )
    evidence_ids = {item.id for item in evidence}
    affected_claim_ids = {item.fact_claim_id for item in evidence}
    direct_claim_ids = set(
        (
            await session.scalars(
                select(FactClaim.id).where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == document.kb_id,
                    or_(
                        (FactClaim.subject_type == "source_document")
                        & (FactClaim.subject_id == document_id),
                        FactClaim.ingest_run_id.in_(ingest_run_ids),
                    ),
                )
            )
        ).all()
    )
    affected_claim_ids.update(direct_claim_ids)
    claims = tuple(
        (
            await session.scalars(
                select(FactClaim).where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == document.kb_id,
                    FactClaim.id.in_(affected_claim_ids),
                )
            )
        ).all()
    )
    shared_claim_ids = set(
        (
            await session.scalars(
                select(EvidenceSpan.fact_claim_id)
                .where(
                    EvidenceSpan.org_id == org_id,
                    EvidenceSpan.kb_id == document.kb_id,
                    EvidenceSpan.fact_claim_id.in_(affected_claim_ids),
                    EvidenceSpan.revision_id.not_in(revision_ids),
                )
                .distinct()
            )
        ).all()
    )
    exclusive_claim_ids = affected_claim_ids - shared_claim_ids
    manual_or_pending = sum(
        claim.review_status == "suggested"
        or claim.ingest_run_id is None
        or claim.reviewed_by == "human"
        for claim in claims
        if claim.id in exclusive_claim_ids
    )

    entity_claims: dict[UUID, set[UUID]] = {}
    relation_claim_ids: set[UUID] = set()
    for claim in claims:
        for entity_id in (claim.subject_entity_id, claim.object_entity_id):
            if entity_id is not None:
                entity_claims.setdefault(entity_id, set()).add(claim.id)
        if claim.subject_entity_id is not None and claim.object_entity_id is not None:
            relation_claim_ids.add(claim.id)
    shared_entity_ids = {
        entity_id for entity_id, claim_ids in entity_claims.items() if claim_ids & shared_claim_ids
    }
    exclusive_entity_ids = set(entity_claims) - shared_entity_ids
    pinned_entity_count = int(
        await session.scalar(
            select(func.count())
            .select_from(CanonicalEntity)
            .where(
                CanonicalEntity.org_id == org_id,
                CanonicalEntity.kb_id == document.kb_id,
                CanonicalEntity.id.in_(entity_claims),
                CanonicalEntity.is_pinned.is_(True),
            )
        )
        or 0
    )

    object_catalog: dict[str, set[str]] = {}
    _add_object_key(object_catalog, document.object_key, "source")
    _add_object_key(object_catalog, document.markdown_key, "document_markdown")
    for revision in revisions:
        _add_object_key(object_catalog, revision.original_object_key, "revision_source")
        _add_object_key(object_catalog, revision.structured_json_key, "structured_parse")
        _add_object_key(object_catalog, revision.markdown_key, "revision_markdown")
    for image in images:
        _add_object_key(object_catalog, image.original_object_key, "media_original")
        _add_object_key(object_catalog, image.thumbnail_object_key, "media_thumbnail")
    shared_keys = await _shared_object_keys(
        session,
        org_id=org_id,
        document_id=document_id,
        revision_ids=revision_ids,
        image_asset_ids=image_asset_ids,
        keys=set(object_catalog),
    )
    object_candidates = tuple(
        PurgeObjectCandidate(
            key=key,
            category="+".join(sorted(categories)),
        )
        for key, categories in sorted(object_catalog.items())
        if key not in shared_keys
    )

    snapshots = tuple(
        (
            await session.scalars(
                select(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.org_id == org_id,
                    KnowledgeSnapshot.kb_id == document.kb_id,
                )
            )
        ).all()
    )
    snapshots_with_document = {
        snapshot.id for snapshot in snapshots if _snapshot_has_revision(snapshot, revision_ids)
    }
    protected_snapshot_ids = {
        snapshot.id
        for snapshot in snapshots
        if _value(snapshot.status)
        in {
            SnapshotStatus.ACTIVE.value,
            SnapshotStatus.BUILDING.value,
            SnapshotStatus.READY.value,
        }
    }
    retired = sorted(
        (
            snapshot
            for snapshot in snapshots
            if _value(snapshot.status) == SnapshotStatus.RETIRED.value
        ),
        key=lambda snapshot: (
            snapshot.retired_at or datetime.min.replace(tzinfo=UTC),
            snapshot.activated_at or datetime.min.replace(tzinfo=UTC),
            str(snapshot.id),
        ),
        reverse=True,
    )
    if retired:
        protected_snapshot_ids.add(retired[0].id)
    try:
        protected_snapshot_ids.update(
            await _externally_referenced_snapshot_ids(
                session,
                org_id=org_id,
                kb_id=document.kb_id,
            )
        )
    except Exception:
        reference_registry_complete = False
    snapshot_reference_ids = snapshots_with_document & protected_snapshot_ids

    targets = _ReferenceTargets(
        document_ids=frozenset({str(document_id)}),
        revision_ids=_target_strings(revision_ids),
        fact_ids=_target_strings(affected_claim_ids),
        evidence_ids=_target_strings(evidence_ids),
        entity_ids=_target_strings(entity_claims),
        image_ids=_target_strings(image_asset_ids),
        snapshot_ids=_target_strings(snapshots_with_document),
    )
    retrieval_count, business_count, media_business_count = await _business_reference_counts(
        session,
        org_id=org_id,
        targets=targets,
    )
    feedback_count = await _feedback_reference_count(
        session,
        org_id=org_id,
        targets=targets,
    )
    protected_media_count = int(
        await session.scalar(
            select(func.count())
            .select_from(KbSnapshotImageAsset)
            .where(
                KbSnapshotImageAsset.org_id == org_id,
                KbSnapshotImageAsset.kb_id == document.kb_id,
                KbSnapshotImageAsset.snapshot_id.in_(protected_snapshot_ids),
                or_(
                    KbSnapshotImageAsset.image_asset_id.in_(image_asset_ids),
                    KbSnapshotImageAsset.revision_id.in_(revision_ids),
                    KbSnapshotImageAsset.source_doc_id == document_id,
                ),
            )
        )
        or 0
    )
    cross_revision_content = int(
        await session.scalar(
            select(func.count())
            .select_from(EvidenceSpan)
            .where(
                EvidenceSpan.org_id == org_id,
                EvidenceSpan.kb_id == document.kb_id,
                EvidenceSpan.revision_id.not_in(revision_ids),
                or_(
                    EvidenceSpan.chunk_id.in_(chunk_ids),
                    EvidenceSpan.image_asset_id.in_(image_asset_ids),
                ),
            )
        )
        or 0
    )
    withdrawal = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.kb_id == document.kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.operation_type == DocumentOperationType.WITHDRAWAL.value,
            KbDocumentOperation.status == DocumentOperationStatus.COMPLETED.value,
        )
        .order_by(
            KbDocumentOperation.completed_at.desc().nullslast(),
            KbDocumentOperation.created_at.desc(),
        )
        .limit(1)
    )
    return DocumentPurgeInventory(
        org_id=org_id,
        kb_id=document.kb_id,
        document_id=document_id,
        lifecycle_status=_value(document.lifecycle_status),
        withdrawn_at=withdrawal.completed_at if withdrawal is not None else None,
        retention_days=retention_days,
        legal_hold_active=effective_legal_hold,
        reference_registry_complete=reference_registry_complete,
        revision_ids=_sorted_uuids(revision_ids),
        chunk_ids=_sorted_uuids(chunk_ids),
        image_asset_ids=_sorted_uuids(image_asset_ids),
        evidence_ids=_sorted_uuids(evidence_ids),
        exclusive_claim_ids=_sorted_uuids(exclusive_claim_ids),
        shared_claim_ids=_sorted_uuids(shared_claim_ids),
        exclusive_entity_ids=_sorted_uuids(exclusive_entity_ids),
        shared_entity_ids=_sorted_uuids(shared_entity_ids),
        exclusive_relation_count=len(relation_claim_ids & exclusive_claim_ids),
        shared_relation_count=len(relation_claim_ids & shared_claim_ids),
        ingest_run_ids=_sorted_uuids(ingest_run_ids),
        object_candidates=object_candidates,
        shared_object_key_count=len(shared_keys),
        snapshot_reference_count=len(snapshot_reference_ids),
        retrieval_reference_count=retrieval_count,
        business_reference_count=business_count,
        feedback_or_citation_reference_count=feedback_count,
        pinned_entity_count=pinned_entity_count,
        manual_or_pending_fact_count=manual_or_pending,
        media_reference_count=protected_media_count + media_business_count,
        cross_revision_content_reference_count=cross_revision_content,
    )


async def preview_document_purge(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    retention_days: int | None = None,
    legal_hold_active: bool | None = None,
    reference_registry_complete: bool = True,
    now: datetime | None = None,
    allow_purge_pending: bool = False,
) -> DocumentPurgePlan:
    inventory = await _collect_document_purge_inventory(
        session,
        org_id=org_id,
        document_id=document_id,
        retention_days=(
            get_settings().kb_document_purge_retention_days
            if retention_days is None
            else retention_days
        ),
        legal_hold_active=legal_hold_active,
        reference_registry_complete=reference_registry_complete,
    )
    return build_document_purge_plan(
        inventory,
        now=now,
        allow_purge_pending=allow_purge_pending,
    )


def _eligible_with_force(plan: DocumentPurgePlan, *, force: bool) -> bool:
    """force 模式下仅当全部 blocker 都在可跳过清单内才放行;否则按原 eligible。"""
    if plan.eligible:
        return True
    if not force:
        return False
    return all(
        blocker.code in FORCE_BYPASSABLE_BLOCKER_CODES for blocker in plan.blockers
    )


def _validate_submission(
    plan: DocumentPurgePlan,
    *,
    authorized: bool,
    reason: str,
    expected_plan_hash: str,
    force: bool = False,
) -> str:
    if not authorized:
        raise DocumentPurgeForbidden("document purge permission is required")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise DocumentPurgeError("document purge reason is required")
    if not _eligible_with_force(plan, force=force):
        raise DocumentPurgeBlocked(plan)
    if expected_plan_hash != plan.plan_hash:
        raise DocumentPurgePlanDrift(plan)
    return normalized_reason


def _operation_manifest(operation: KbDocumentOperation) -> dict[str, object]:
    purge = operation.impact_summary.get("purge")
    if not isinstance(purge, dict):
        raise DocumentPurgeExecutionError("PURGE_MANIFEST_INVALID", "load_manifest")
    return purge


def public_document_purge_operation_detail(
    operation: KbDocumentOperation,
) -> dict[str, object]:
    """Return progress without exposing object keys or content-bearing metadata."""

    ledger = operation.impact_summary.get("purge_ledger")
    if isinstance(ledger, Mapping):
        return {"ledger": dict(ledger)}
    manifest = _operation_manifest(operation)
    summary = manifest.get("summary")
    objects = manifest.get("objects")
    object_status_counts: dict[str, int] = {}
    if isinstance(objects, list):
        for item in objects:
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "pending")
            object_status_counts[status] = object_status_counts.get(status, 0) + 1
    return {
        "version": manifest.get("version"),
        "plan_hash": manifest.get("plan_hash"),
        "phase": manifest.get("phase"),
        "summary": dict(summary) if isinstance(summary, Mapping) else {},
        "object_status_counts": object_status_counts,
        "verification": (
            dict(manifest["verification"])
            if isinstance(manifest.get("verification"), Mapping)
            else {}
        ),
    }


def _manifest_has_irreversible_progress(manifest: Mapping[str, object]) -> bool:
    objects = manifest.get("objects")
    return isinstance(objects, list) and any(
        isinstance(item, Mapping) and item.get("status") in _TERMINAL_OBJECT_STATES
        for item in objects
    )


async def submit_document_purge(
    session: AsyncSession,
    *,
    org_id: UUID,
    document_id: UUID,
    actor_id: UUID,
    reason: str,
    expected_plan_hash: str,
    authorized: bool,
    retention_days: int | None = None,
    legal_hold_active: bool | None = None,
    reference_registry_complete: bool = True,
    force: bool = False,
    now: datetime | None = None,
) -> KbDocumentOperation:
    if not authorized:
        raise DocumentPurgeForbidden("document purge permission is required")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise DocumentPurgeError("document purge reason is required")
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentPurgeNotFound("source document does not exist")
    idempotency_key = f"document:{org_id.hex}:{document_id.hex}:purge:{PURGE_PLAN_VERSION}"
    existing = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.operation_type == DocumentOperationType.PURGE.value,
            KbDocumentOperation.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None and existing.status in {
        DocumentOperationStatus.PENDING.value,
        DocumentOperationStatus.PROCESSING.value,
        DocumentOperationStatus.COMPLETED.value,
    }:
        return existing
    plan = await preview_document_purge(
        session,
        org_id=org_id,
        document_id=document_id,
        retention_days=retention_days,
        legal_hold_active=legal_hold_active,
        reference_registry_complete=reference_registry_complete,
        now=now,
    )
    normalized_reason = _validate_submission(
        plan,
        authorized=authorized,
        reason=reason,
        expected_plan_hash=expected_plan_hash,
        force=force,
    )
    if existing is not None:
        manifest = _operation_manifest(existing)
        if manifest.get("plan_hash") != plan.plan_hash and _manifest_has_irreversible_progress(
            manifest
        ):
            raise DocumentPurgePlanDrift(plan)
        if manifest.get("plan_hash") != plan.plan_hash:
            existing.impact_summary = {
                **existing.impact_summary,
                "purge": {**_manifest_for_plan(plan), "force": force},
            }
        else:
            # 重复提交也要同步 force 口径,执行复核依赖 manifest 里的标记
            manifest["force"] = force
            existing.impact_summary = {
                **existing.impact_summary,
                "purge": dict(manifest),
            }
        existing.status = DocumentOperationStatus.PENDING
        existing.stage = str(_operation_manifest(existing).get("phase") or "planned")
        existing.reason = normalized_reason
        existing.requested_by = actor_id
        existing.retryable = False
        existing.last_error_code = None
        existing.last_error = None
        existing.failed_at = None
        existing.completed_at = None
        document.lifecycle_status = DocumentLifecycleStatus.PURGE_PENDING
        session.add_all([document, existing])
        await session.flush()
        return existing

    latest_revision_id = await session.scalar(
        select(DocumentRevision.id)
        .where(
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == plan.kb_id,
            DocumentRevision.doc_id == document_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    operation = KbDocumentOperation(
        id=uuid4(),
        org_id=org_id,
        kb_id=plan.kb_id,
        document_id=document_id,
        revision_id=latest_revision_id,
        operation_type=DocumentOperationType.PURGE,
        status=DocumentOperationStatus.PENDING,
        stage="planned",
        idempotency_key=idempotency_key,
        requested_by=actor_id,
        reason=normalized_reason,
        impact_summary={"purge": {**_manifest_for_plan(plan), "force": force}},
    )
    document.lifecycle_status = DocumentLifecycleStatus.PURGE_PENDING
    session.add_all([document, operation])
    await session.flush()
    return operation


async def _delete_manifest_objects(
    objects: list[dict[str, object]],
    *,
    object_exists: ObjectExists,
    delete_object: DeleteObject,
    persist: PersistProgress,
) -> None:
    for item in objects:
        if item.get("status") in _TERMINAL_OBJECT_STATES:
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise DocumentPurgeExecutionError("PURGE_MANIFEST_INVALID", "object_deletion")
        item["attempts"] = int(item.get("attempts", 0)) + 1
        try:
            existed = await object_exists(key)
            if existed:
                await delete_object(key)
            if await object_exists(key):
                raise DocumentPurgeExecutionError("PURGE_OBJECT_RESIDUAL", "object_deletion")
        except DocumentPurgeExecutionError:
            item["status"] = "failed"
            item["error_code"] = "PURGE_OBJECT_RESIDUAL"
            await persist(objects)
            raise
        except Exception as exc:
            item["status"] = "failed"
            item["error_code"] = "PURGE_OBJECT_STORE_FAILED"
            await persist(objects)
            raise DocumentPurgeExecutionError(
                "PURGE_OBJECT_STORE_FAILED", "object_deletion"
            ) from exc
        item["status"] = "deleted" if existed else "already_missing"
        item.pop("error_code", None)
        await persist(objects)


async def _delete_document_metadata(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    manifest: Mapping[str, object],
) -> None:
    revision_ids = _uuid_values(manifest, "revision_ids")
    chunk_ids = _uuid_values(manifest, "chunk_ids")
    image_asset_ids = _uuid_values(manifest, "image_asset_ids")
    evidence_ids = _uuid_values(manifest, "evidence_ids")
    exclusive_claim_ids = _uuid_values(manifest, "exclusive_claim_ids")
    ingest_run_ids = _uuid_values(manifest, "ingest_run_ids")

    fact_support_ids = tuple(
        (
            await session.scalars(
                select(SnapshotFactSupport.id).where(
                    SnapshotFactSupport.org_id == org_id,
                    SnapshotFactSupport.kb_id == kb_id,
                    or_(
                        SnapshotFactSupport.doc_id == document_id,
                        SnapshotFactSupport.revision_id.in_(revision_ids),
                        SnapshotFactSupport.evidence_span_id.in_(evidence_ids),
                    ),
                )
            )
        ).all()
    )
    graph_edge_ids = tuple(
        (
            await session.scalars(
                select(GraphEdge.id).where(
                    GraphEdge.org_id == org_id,
                    GraphEdge.kb_id == kb_id,
                    or_(
                        GraphEdge.source_revision_id.in_(revision_ids),
                        GraphEdge.related_source_revision_id.in_(revision_ids),
                        GraphEdge.evidence_span_id.in_(evidence_ids),
                        GraphEdge.related_evidence_span_id.in_(evidence_ids),
                        GraphEdge.fact_claim_id.in_(exclusive_claim_ids),
                        GraphEdge.related_fact_claim_id.in_(exclusive_claim_ids),
                    ),
                )
            )
        ).all()
    )
    await session.execute(
        delete(KbSnapshotEntityNodeSupport).where(
            KbSnapshotEntityNodeSupport.org_id == org_id,
            KbSnapshotEntityNodeSupport.kb_id == kb_id,
            or_(
                KbSnapshotEntityNodeSupport.fact_support_id.in_(fact_support_ids),
                KbSnapshotEntityNodeSupport.graph_edge_id.in_(graph_edge_ids),
            ),
        )
    )
    await session.execute(
        delete(GraphEdge).where(
            GraphEdge.org_id == org_id,
            GraphEdge.kb_id == kb_id,
            GraphEdge.id.in_(graph_edge_ids),
        )
    )
    await session.execute(
        delete(KbSnapshotImageAsset).where(
            KbSnapshotImageAsset.org_id == org_id,
            KbSnapshotImageAsset.kb_id == kb_id,
            or_(
                KbSnapshotImageAsset.source_doc_id == document_id,
                KbSnapshotImageAsset.revision_id.in_(revision_ids),
                KbSnapshotImageAsset.image_asset_id.in_(image_asset_ids),
            ),
        )
    )
    await session.execute(
        delete(SnapshotFactSupport).where(
            SnapshotFactSupport.org_id == org_id,
            SnapshotFactSupport.kb_id == kb_id,
            SnapshotFactSupport.id.in_(fact_support_ids),
        )
    )
    await session.execute(
        delete(EvidenceSpan).where(
            EvidenceSpan.org_id == org_id,
            EvidenceSpan.kb_id == kb_id,
            EvidenceSpan.id.in_(evidence_ids),
        )
    )
    await session.execute(
        delete(FactClaim).where(
            FactClaim.org_id == org_id,
            FactClaim.kb_id == kb_id,
            FactClaim.id.in_(exclusive_claim_ids),
        )
    )
    await session.execute(
        update(FactClaim)
        .where(
            FactClaim.org_id == org_id,
            FactClaim.kb_id == kb_id,
            FactClaim.ingest_run_id.in_(ingest_run_ids),
        )
        .values(ingest_run_id=None)
    )
    await session.execute(
        delete(KbChunkEmbedding).where(
            KbChunkEmbedding.org_id == org_id,
            KbChunkEmbedding.kb_id == kb_id,
            KbChunkEmbedding.chunk_id.in_(chunk_ids),
        )
    )
    await session.execute(
        delete(KbChunk).where(
            KbChunk.org_id == org_id,
            KbChunk.kb_id == kb_id,
            or_(
                KbChunk.id.in_(chunk_ids),
                KbChunk.source_doc_id == document_id,
                KbChunk.revision_id.in_(revision_ids),
            ),
        )
    )
    await session.execute(
        delete(KbImageAssetEvent).where(
            KbImageAssetEvent.org_id == org_id,
            KbImageAssetEvent.kb_id == kb_id,
            KbImageAssetEvent.image_asset_id.in_(image_asset_ids),
        )
    )
    # 运维事件表属 operations 子系统(不在 KB schema):走 IncidentRecorder 清理,
    # 无宿主实现即无事可清。
    await ports.purge_incidents(
        session, org_id=org_id, kb_id=kb_id, image_asset_ids=list(image_asset_ids)
    )
    await session.execute(
        delete(KbImageAsset).where(
            KbImageAsset.org_id == org_id,
            KbImageAsset.kb_id == kb_id,
            KbImageAsset.id.in_(image_asset_ids),
        )
    )
    for model in (KbPage, KbEntity):
        await session.execute(
            delete(model).where(
                model.org_id == org_id,
                model.kb_id == kb_id,
                model.source_doc_id == document_id,
            )
        )
    await session.execute(
        delete(IngestRun).where(
            IngestRun.org_id == org_id,
            IngestRun.kb_id == kb_id,
            IngestRun.id.in_(ingest_run_ids),
        )
    )
    await session.execute(
        update(KbDocumentOperation)
        .where(
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.kb_id == kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.revision_id.in_(revision_ids),
        )
        .values(revision_id=None)
    )
    await session.execute(
        delete(DocumentRevision).where(
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == kb_id,
            DocumentRevision.id.in_(revision_ids),
        )
    )
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == org_id,
            SourceDocument.kb_id == kb_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentPurgeExecutionError("PURGE_DOCUMENT_MISSING", "metadata_deletion")
    document.filename = "[purged]"
    document.object_key = ""
    document.markdown_key = None
    document.parser_name = None
    document.sha256 = sha256(f"purged:{org_id}:{kb_id}:{document_id}".encode()).hexdigest()
    document.error = None
    document.rel_path = None
    document.progress = 0
    document.progress_stage = None
    document.progress_done = 0
    document.progress_total = 0
    document.expires_at = None
    document.expiry_notified_at = None
    document.parsing_started_at = None
    session.add(document)


async def _count_rows(
    session: AsyncSession,
    model: type[Any],
    *where: object,
) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _verify_database_residuals(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    manifest: Mapping[str, object],
) -> dict[str, int]:
    revision_ids = _uuid_values(manifest, "revision_ids")
    chunk_ids = _uuid_values(manifest, "chunk_ids")
    image_asset_ids = _uuid_values(manifest, "image_asset_ids")
    evidence_ids = _uuid_values(manifest, "evidence_ids")
    exclusive_claim_ids = _uuid_values(manifest, "exclusive_claim_ids")
    ingest_run_ids = _uuid_values(manifest, "ingest_run_ids")
    return {
        "revisions": await _count_rows(
            session,
            DocumentRevision,
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == kb_id,
            DocumentRevision.id.in_(revision_ids),
        ),
        "chunks": await _count_rows(
            session,
            KbChunk,
            KbChunk.org_id == org_id,
            KbChunk.kb_id == kb_id,
            or_(
                KbChunk.id.in_(chunk_ids),
                KbChunk.source_doc_id == document_id,
                KbChunk.revision_id.in_(revision_ids),
            ),
        ),
        "media": await _count_rows(
            session,
            KbImageAsset,
            KbImageAsset.org_id == org_id,
            KbImageAsset.kb_id == kb_id,
            KbImageAsset.id.in_(image_asset_ids),
        ),
        "evidence": await _count_rows(
            session,
            EvidenceSpan,
            EvidenceSpan.org_id == org_id,
            EvidenceSpan.kb_id == kb_id,
            EvidenceSpan.id.in_(evidence_ids),
        ),
        "fact_claims": await _count_rows(
            session,
            FactClaim,
            FactClaim.org_id == org_id,
            FactClaim.kb_id == kb_id,
            FactClaim.id.in_(exclusive_claim_ids),
        ),
        "ingest_runs": await _count_rows(
            session,
            IngestRun,
            IngestRun.org_id == org_id,
            IngestRun.kb_id == kb_id,
            IngestRun.id.in_(ingest_run_ids),
        ),
        "fact_supports": await _count_rows(
            session,
            SnapshotFactSupport,
            SnapshotFactSupport.org_id == org_id,
            SnapshotFactSupport.kb_id == kb_id,
            or_(
                SnapshotFactSupport.doc_id == document_id,
                SnapshotFactSupport.revision_id.in_(revision_ids),
                SnapshotFactSupport.evidence_span_id.in_(evidence_ids),
            ),
        ),
        "snapshot_media": await _count_rows(
            session,
            KbSnapshotImageAsset,
            KbSnapshotImageAsset.org_id == org_id,
            KbSnapshotImageAsset.kb_id == kb_id,
            or_(
                KbSnapshotImageAsset.source_doc_id == document_id,
                KbSnapshotImageAsset.revision_id.in_(revision_ids),
                KbSnapshotImageAsset.image_asset_id.in_(image_asset_ids),
            ),
        ),
        "graph_edges": await _count_rows(
            session,
            GraphEdge,
            GraphEdge.org_id == org_id,
            GraphEdge.kb_id == kb_id,
            or_(
                GraphEdge.source_revision_id.in_(revision_ids),
                GraphEdge.related_source_revision_id.in_(revision_ids),
                GraphEdge.evidence_span_id.in_(evidence_ids),
                GraphEdge.related_evidence_span_id.in_(evidence_ids),
                GraphEdge.fact_claim_id.in_(exclusive_claim_ids),
                GraphEdge.related_fact_claim_id.in_(exclusive_claim_ids),
            ),
        ),
        "operation_revision_links": await _count_rows(
            session,
            KbDocumentOperation,
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.kb_id == kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.revision_id.in_(revision_ids),
        ),
    }


async def _mark_failed(
    session: AsyncSession,
    *,
    operation: KbDocumentOperation,
    document: SourceDocument,
    code: str,
    stage: str,
    retryable: bool,
) -> KbDocumentOperation:
    operation.status = DocumentOperationStatus.FAILED
    operation.stage = stage
    operation.retryable = retryable
    operation.last_error_code = code
    operation.last_error = "Permanent purge did not complete; source remains withdrawn."
    operation.failed_at = datetime.now(UTC)
    operation.completed_at = None
    document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWN
    session.add_all([operation, document])
    await session.flush()
    return operation


async def execute_document_purge(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
    legal_hold_active: bool | None = None,
    reference_registry_complete: bool = True,
    retention_days: int | None = None,
    now: datetime | None = None,
    object_exists: ObjectExists = storage.object_exists,
    delete_object: DeleteObject = storage.remove_object,
) -> KbDocumentOperation:
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.operation_type == DocumentOperationType.PURGE.value,
        )
        .with_for_update()
    )
    if operation is None:
        raise DocumentPurgeNotFound("document purge operation does not exist")
    if operation.status == DocumentOperationStatus.COMPLETED.value:
        return operation
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == operation.document_id,
            SourceDocument.org_id == org_id,
            SourceDocument.kb_id == operation.kb_id,
        )
        .with_for_update()
    )
    if document is None:
        raise DocumentPurgeNotFound("source document does not exist")
    manifest = _operation_manifest(operation)
    phase = str(manifest.get("phase") or "planned")
    if phase in {"planned", "object_deletion"}:
        current_plan = await preview_document_purge(
            session,
            org_id=org_id,
            document_id=operation.document_id,
            retention_days=retention_days,
            legal_hold_active=legal_hold_active,
            reference_registry_complete=reference_registry_complete,
            now=now,
            allow_purge_pending=True,
        )
        # 强制清理的操作在复核时沿用同一跳过口径(manifest 里带 force 标记)
        if not _eligible_with_force(
            current_plan, force=bool(manifest.get("force"))
        ) or current_plan.plan_hash != manifest.get("plan_hash"):
            return await _mark_failed(
                session,
                operation=operation,
                document=document,
                code="PURGE_PLAN_DRIFT",
                stage="plan_revalidation",
                retryable=False,
            )

    operation.status = DocumentOperationStatus.PROCESSING
    operation.stage = phase
    operation.attempts += 1
    operation.started_at = operation.started_at or datetime.now(UTC)
    operation.retryable = False
    operation.last_error_code = None
    operation.last_error = None
    operation.failed_at = None
    session.add(operation)
    await session.flush()

    objects = manifest.get("objects")
    if not isinstance(objects, list) or not all(isinstance(item, dict) for item in objects):
        return await _mark_failed(
            session,
            operation=operation,
            document=document,
            code="PURGE_MANIFEST_INVALID",
            stage="load_manifest",
            retryable=False,
        )

    async def _persist_object_progress(
        updated_objects: list[dict[str, object]],
    ) -> None:
        manifest["phase"] = "object_deletion"
        manifest["objects"] = updated_objects
        operation.stage = "object_deletion"
        operation.impact_summary = {
            **operation.impact_summary,
            "purge": dict(manifest),
        }
        session.add(operation)
        await session.flush()

    if phase in {"planned", "object_deletion"}:
        try:
            await _delete_manifest_objects(
                objects,
                object_exists=object_exists,
                delete_object=delete_object,
                persist=_persist_object_progress,
            )
        except DocumentPurgeExecutionError as exc:
            return await _mark_failed(
                session,
                operation=operation,
                document=document,
                code=exc.code,
                stage=exc.stage,
                retryable=True,
            )
        manifest["phase"] = "metadata_deletion"
        operation.stage = "metadata_deletion"
        operation.impact_summary = {
            **operation.impact_summary,
            "purge": dict(manifest),
        }
        session.add(operation)
        await session.flush()

    if manifest.get("phase") == "metadata_deletion":
        try:
            async with session.begin_nested():
                await _delete_document_metadata(
                    session,
                    org_id=org_id,
                    kb_id=operation.kb_id,
                    document_id=operation.document_id,
                    manifest=manifest,
                )
        except Exception:
            return await _mark_failed(
                session,
                operation=operation,
                document=document,
                code="PURGE_METADATA_DELETE_FAILED",
                stage="metadata_deletion",
                retryable=True,
            )
        manifest["phase"] = "verification"
        operation.stage = "verification"
        operation.impact_summary = {
            **operation.impact_summary,
            "purge": dict(manifest),
        }
        session.add(operation)
        await session.flush()

    database_residuals = await _verify_database_residuals(
        session,
        org_id=org_id,
        kb_id=operation.kb_id,
        document_id=operation.document_id,
        manifest=manifest,
    )
    object_residual_count = 0
    try:
        for item in objects:
            key = item.get("key")
            if isinstance(key, str) and await object_exists(key):
                object_residual_count += 1
    except Exception:
        return await _mark_failed(
            session,
            operation=operation,
            document=document,
            code="PURGE_OBJECT_VERIFY_FAILED",
            stage="verification",
            retryable=True,
        )
    verification = {
        "database_residuals": database_residuals,
        "object_residual_count": object_residual_count,
    }
    manifest["verification"] = verification
    if object_residual_count or any(database_residuals.values()):
        operation.impact_summary = {
            **operation.impact_summary,
            "purge": dict(manifest),
        }
        return await _mark_failed(
            session,
            operation=operation,
            document=document,
            code="PURGE_VERIFICATION_RESIDUAL",
            stage="verification",
            retryable=True,
        )

    completed_at = datetime.now(UTC)
    ledger = {
        "version": PURGE_PLAN_VERSION,
        "plan_hash": manifest.get("plan_hash"),
        "document_id": str(operation.document_id),
        "deleted_categories": dict(
            operation.impact_summary.get("purge", {}).get("summary", {}).get("delete_counts", {})
        ),
        "retained_categories": dict(
            operation.impact_summary.get("purge", {}).get("summary", {}).get("retain_counts", {})
        ),
        "verification": verification,
        "completed_at": completed_at.isoformat(),
    }
    operation.impact_summary = {"purge_ledger": ledger}
    operation.revision_id = None
    operation.status = DocumentOperationStatus.COMPLETED
    operation.stage = "completed"
    operation.retryable = False
    operation.completed_at = completed_at
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error = None
    document.lifecycle_status = DocumentLifecycleStatus.PURGED
    session.add_all([operation, document])
    await session.flush()
    return operation


async def get_document_purge_operation(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
) -> KbDocumentOperation:
    operation = await session.scalar(
        select(KbDocumentOperation).where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == org_id,
            KbDocumentOperation.operation_type == DocumentOperationType.PURGE.value,
        )
    )
    if operation is None:
        raise DocumentPurgeNotFound("document purge operation does not exist")
    return operation


__all__ = [
    "DocumentPurgeBlocked",
    "DocumentPurgeError",
    "DocumentPurgeExecutionError",
    "DocumentPurgeForbidden",
    "DocumentPurgeInventory",
    "DocumentPurgeNotFound",
    "DocumentPurgePlan",
    "DocumentPurgePlanDrift",
    "PurgeBlocker",
    "PurgeBlockerCode",
    "PurgeObjectCandidate",
    "build_document_purge_plan",
    "execute_document_purge",
    "get_document_purge_operation",
    "preview_document_purge",
    "public_document_purge_operation_detail",
    "submit_document_purge",
]
