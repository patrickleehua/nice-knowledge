"""Deterministic knowledge snapshot build and pointer transitions.

The caller owns the transaction and must commit it. This keeps the active pointer,
snapshot state, and outbox event in one database transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb import ports
from nicekit.llm.runtime_config import runtime_overrides
from nicekit.models.kb import (
    EMBEDDING_DIM,
    DocumentLifecycleStatus,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    KnowledgeBase,
    KnowledgeSnapshot,
    OutboxEvent,
    RevisionStatus,
    SnapshotFactSupport,
    SnapshotStatus,
    SourceDocument,
)


def _snapshot_media():
    """媒体投影模块(kb/snapshot_media.py)属媒体波次,未装配时整体降级。

    降级语义:没有媒体投影 = 没有媒体门禁,快照构建/激活/回滚照常;媒体波次
    搬入后本函数自然返回模块,所有校验点无需再改。
    """
    try:
        import nicekit.kb.snapshot_media as snapshot_media
    except (ImportError, ModuleNotFoundError):
        return None
    return snapshot_media


_ELIGIBLE_REVISION_STATUSES = (
    RevisionStatus.STAGED.value,
    RevisionStatus.ACTIVE.value,
)
logger = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    """Base error for an invalid snapshot operation."""


class SnapshotOwnershipError(SnapshotError):
    """The requested KB is not owned by the explicit organization."""


class SnapshotStateError(SnapshotError):
    """The requested snapshot transition is not valid."""


class SnapshotTransitionBlocked(SnapshotStateError):
    """A predictable snapshot transition rejection safe to expose via the API."""

    def __init__(self, code: str, message: str, *, public_message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = public_message


class RevisionManifestError(SnapshotError):
    """The requested revision set is invalid."""


SNAPSHOT_NOT_RETIRED = "SNAPSHOT_NOT_RETIRED"
SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED = "SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED"
SNAPSHOT_MANIFEST_INVALID = "SNAPSHOT_MANIFEST_INVALID"
SNAPSHOT_CONTAINS_WITHDRAWN_DOCUMENT = "SNAPSHOT_CONTAINS_WITHDRAWN_DOCUMENT"
SNAPSHOT_PROJECTIONS_UNAVAILABLE = "SNAPSHOT_PROJECTIONS_UNAVAILABLE"
SNAPSHOT_MEDIA_UNAVAILABLE = "SNAPSHOT_MEDIA_UNAVAILABLE"

_ROLLBACK_PUBLIC_MESSAGES = {
    SNAPSHOT_NOT_RETIRED: "该版本当前不是可回滚的历史版本。",
    SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED: (
        "该版本创建后知识范围发生过资料撤回或治理变更，不能直接回滚。"
    ),
    SNAPSHOT_MANIFEST_INVALID: "该历史版本的资料清单不完整，不能直接回滚。",
    SNAPSHOT_CONTAINS_WITHDRAWN_DOCUMENT: (
        "该历史版本包含已撤回的资料，不能直接回滚。"
    ),
    SNAPSHOT_PROJECTIONS_UNAVAILABLE: "该历史版本的运行数据已清理，不能直接回滚。",
    SNAPSHOT_MEDIA_UNAVAILABLE: "该历史版本的媒体数据已不可用，不能直接回滚。",
}


def _rollback_blocked(code: str, message: str) -> SnapshotTransitionBlocked:
    return SnapshotTransitionBlocked(
        code,
        message,
        public_message=_ROLLBACK_PUBLIC_MESSAGES[code],
    )


@dataclass(frozen=True, slots=True)
class SnapshotRollbackCapability:
    allowed: bool
    code: str | None = None
    message: str | None = None

    @classmethod
    def permitted(cls) -> SnapshotRollbackCapability:
        return cls(allowed=True)

    @classmethod
    def blocked(cls, error: SnapshotTransitionBlocked) -> SnapshotRollbackCapability:
        return cls(
            allowed=False,
            code=error.code,
            message=error.public_message,
        )


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def canonical_json(value: object) -> str:
    """Return stable JSON suitable for content-addressed fingerprints."""

    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def current_embedding_fingerprint() -> dict[str, Any]:
    settings = get_settings()
    overrides = runtime_overrides("embedding")
    return {
        "provider": overrides.get("provider") or settings.embedding_provider,
        "model": overrides.get("model") or settings.embedding_model,
        "dim": EMBEDDING_DIM,
    }


def current_snapshot_config(kb: KnowledgeBase) -> dict[str, Any]:
    settings = get_settings()
    return {
        "schema_version": "kb-snapshot-v1",
        "parser_backend": settings.kb_parser_backend,
        "ingest_profile": kb.ingest_profile or {},
        "consumption_epoch": kb.consumption_epoch,
    }


def _normalize_json_object(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    normalized = json.loads(canonical_json(value))
    if not isinstance(normalized, dict):
        raise TypeError(f"{field} must be a JSON object")
    return normalized


def _status_value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _projection_gc_completed(snapshot: KnowledgeSnapshot) -> bool:
    marker = snapshot.build_stats.get("projection_gc")
    return isinstance(marker, dict) and marker.get("status") == "completed"


@dataclass(frozen=True, slots=True)
class SnapshotBuildContext:
    snapshot_id: UUID
    org_id: UUID
    kb_id: UUID
    revision_manifest: tuple[dict[str, Any], ...]
    fact_claim_ids: tuple[UUID, ...]
    embedding_fingerprint: dict[str, Any]
    config_manifest: dict[str, Any]


class ProjectionBuilder(Protocol):
    """Adapter contract for a snapshot-scoped projection materializer."""

    name: str
    version: str

    async def build(
        self, session: AsyncSession, context: SnapshotBuildContext
    ) -> Mapping[str, Any]: ...


class ProjectionBuilderRegistry:
    """Small deterministic registry shared by current and future projections."""

    def __init__(self) -> None:
        self._builders: dict[str, ProjectionBuilder] = {}
        self._required: set[str] = set()

    def register(self, builder: ProjectionBuilder, *, required: bool = False) -> None:
        name = builder.name.strip()
        if not name:
            raise ValueError("projection builder name must not be empty")
        version = builder.version.strip()
        if not version:
            raise ValueError("projection builder version must not be empty")
        if name in self._builders:
            raise ValueError(f"projection builder already registered: {name}")
        self._builders[name] = builder
        if required:
            self._required.add(name)

    def unregister(self, name: str) -> None:
        self._builders.pop(name, None)
        self._required.discard(name)

    def manifest(self) -> list[dict[str, str]]:
        return [
            {"name": name, "version": self._builders[name].version.strip()}
            for name in sorted(self._builders)
        ]

    def required_manifest(self) -> list[dict[str, str]]:
        return [
            {"name": name, "version": self._builders[name].version.strip()}
            for name in sorted(self._required)
        ]

    async def build_all(
        self, session: AsyncSession, context: SnapshotBuildContext
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for name in sorted(self._builders):
            result = await self._builders[name].build(session, context)
            results[name] = {
                "status": "succeeded",
                "version": self._builders[name].version.strip(),
                "stats": _normalize_json_object(result, field=f"projection {name}"),
            }
        return results


projection_builders = ProjectionBuilderRegistry()


async def _require_owned_kb(
    session: AsyncSession, *, org_id: UUID, kb_id: UUID, for_update: bool
) -> KnowledgeBase:
    statement = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.org_id == org_id,
    )
    if for_update:
        statement = statement.with_for_update()
    kb = (await session.execute(statement)).scalar_one_or_none()
    if kb is None:
        raise SnapshotOwnershipError(
            f"knowledge base {kb_id} is not owned by organization {org_id}"
        )
    if kb.lifecycle_status != "active":
        raise SnapshotStateError(
            f"knowledge base {kb_id} is not active: {kb.lifecycle_status}"
        )
    return kb


def _revision_entry(revision: DocumentRevision) -> dict[str, Any]:
    return {
        "doc_id": str(revision.doc_id),
        "revision_id": str(revision.id),
        "revision_no": revision.revision_no,
        "sha256": revision.sha256,
    }


async def _latest_tombstoned_doc_ids(
    session: AsyncSession, *, org_id: UUID, kb_id: UUID
) -> set[UUID]:
    rank = (
        func.row_number()
        .over(
            partition_by=DocumentRevision.doc_id,
            order_by=(
                DocumentRevision.revision_no.desc(),
                DocumentRevision.created_at.desc(),
                DocumentRevision.id.desc(),
            ),
        )
        .label("revision_rank")
    )
    ranked = (
        select(
            DocumentRevision.doc_id.label("doc_id"),
            DocumentRevision.status.label("status"),
            rank,
        )
        .where(
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == kb_id,
        )
        .subquery()
    )
    return set(
        (
            await session.execute(
                select(ranked.c.doc_id).where(
                    ranked.c.revision_rank == 1,
                    ranked.c.status == RevisionStatus.TOMBSTONED.value,
                )
            )
        )
        .scalars()
        .all()
    )


async def _select_revision_manifest(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_ids: Sequence[UUID] | None,
) -> list[dict[str, Any]]:
    tombstoned_doc_ids = await _latest_tombstoned_doc_ids(
        session, org_id=org_id, kb_id=kb_id
    )
    if revision_ids is not None:
        requested = list(revision_ids)
        if len(set(requested)) != len(requested):
            raise RevisionManifestError("revision_ids must not contain duplicates")
        if not requested:
            return []
        revisions = list(
            (
                await session.execute(
                    select(DocumentRevision).where(DocumentRevision.id.in_(requested))
                )
            )
            .scalars()
            .all()
        )
        if len(revisions) != len(requested):
            raise RevisionManifestError(
                "every explicit revision must exist and be visible to the organization"
            )
        if any(row.org_id != org_id or row.kb_id != kb_id for row in revisions):
            raise RevisionManifestError(
                "every explicit revision must belong to the requested organization and KB"
            )
        if any(_status_value(row.status) not in _ELIGIBLE_REVISION_STATUSES for row in revisions):
            raise RevisionManifestError("explicit revisions must have staged or active status")
        if len({row.doc_id for row in revisions}) != len(revisions):
            raise RevisionManifestError("a manifest may contain only one revision per document")
        if any(row.doc_id in tombstoned_doc_ids for row in revisions):
            raise RevisionManifestError(
                "explicit revisions must not revive a tombstoned document"
            )
        active_doc_ids = set(
            (
                await session.scalars(
                    select(SourceDocument.id).where(
                        SourceDocument.org_id == org_id,
                        SourceDocument.kb_id == kb_id,
                        SourceDocument.id.in_({row.doc_id for row in revisions}),
                        SourceDocument.lifecycle_status
                        == DocumentLifecycleStatus.ACTIVE.value,
                    )
                )
            ).all()
        )
        if active_doc_ids != {row.doc_id for row in revisions}:
            raise RevisionManifestError(
                "explicit revisions must belong to active source documents"
            )
    else:
        rank = (
            func.row_number()
            .over(
                partition_by=DocumentRevision.doc_id,
                order_by=(
                    DocumentRevision.revision_no.desc(),
                    DocumentRevision.created_at.desc(),
                    DocumentRevision.id.desc(),
                ),
            )
            .label("revision_rank")
        )
        ranked = (
            select(DocumentRevision.id.label("revision_id"), rank)
            .where(
                DocumentRevision.org_id == org_id,
                DocumentRevision.kb_id == kb_id,
                DocumentRevision.status.in_(_ELIGIBLE_REVISION_STATUSES),
                DocumentRevision.doc_id.in_(
                    select(SourceDocument.id).where(
                        SourceDocument.org_id == org_id,
                        SourceDocument.kb_id == kb_id,
                        SourceDocument.lifecycle_status
                        == DocumentLifecycleStatus.ACTIVE.value,
                    )
                ),
            )
            .subquery()
        )
        revisions = list(
            (
                await session.execute(
                    select(DocumentRevision)
                    .join(ranked, ranked.c.revision_id == DocumentRevision.id)
                    .where(ranked.c.revision_rank == 1)
                    .where(DocumentRevision.doc_id.not_in(tombstoned_doc_ids))
                )
            )
            .scalars()
            .all()
        )

    revisions.sort(key=lambda row: (str(row.doc_id), row.revision_no, str(row.id)))
    return [_revision_entry(row) for row in revisions]


async def build_revision_manifest(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_ids: Sequence[UUID] | None = None,
) -> list[dict[str, Any]]:
    """Build the stable latest-per-document manifest, including an empty manifest."""

    await _require_owned_kb(session, org_id=org_id, kb_id=kb_id, for_update=False)
    return await _select_revision_manifest(
        session,
        org_id=org_id,
        kb_id=kb_id,
        revision_ids=revision_ids,
    )


def _validate_embedding_fingerprint(value: Mapping[str, Any]) -> dict[str, Any]:
    fingerprint = _normalize_json_object(value, field="embedding_fingerprint")
    provider = fingerprint.get("provider")
    model = fingerprint.get("model")
    dim = fingerprint.get("dim")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("embedding_fingerprint.provider must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("embedding_fingerprint.model must be a non-empty string")
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise ValueError("embedding_fingerprint.dim must be a positive integer")
    return fingerprint


async def _fact_set_manifest(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_ids: Sequence[UUID],
) -> list[dict[str, Any]]:
    """Describe exactly the confirmed, evidenced facts eligible for this release."""

    if not revision_ids:
        return []
    rows = (
        await session.execute(
            select(FactClaim, EvidenceSpan)
            .join(EvidenceSpan, EvidenceSpan.fact_claim_id == FactClaim.id)
            .where(
                FactClaim.org_id == org_id,
                FactClaim.kb_id == kb_id,
                FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                EvidenceSpan.revision_id.in_(revision_ids),
            )
            .order_by(FactClaim.id, EvidenceSpan.id)
            .with_for_update(of=(FactClaim, EvidenceSpan))
        )
    ).all()
    claims: dict[UUID, dict[str, Any]] = {}
    for claim, evidence in rows:
        entry = claims.setdefault(
            claim.id,
            {
                "claim_id": str(claim.id),
                "subject_type": claim.subject_type,
                "subject_id": str(claim.subject_id),
                "subject_entity_id": (
                    str(claim.subject_entity_id) if claim.subject_entity_id else None
                ),
                "object_entity_id": (
                    str(claim.object_entity_id) if claim.object_entity_id else None
                ),
                "predicate": claim.predicate,
                "effective_payload": (
                    claim.corrected_payload
                    if claim.corrected_payload is not None
                    else claim.value_json
                ),
                "value_json": claim.value_json,
                "raw_payload": claim.raw_payload,
                "corrected_payload": claim.corrected_payload,
                "valid_from": claim.valid_from,
                "valid_to": claim.valid_to,
                "confidence": claim.confidence,
                "review_status": _status_value(claim.review_status),
                "model_name": claim.model_name,
                "prompt_version": claim.prompt_version,
                "evidence": [],
            },
        )
        entry["evidence"].append(
            {
                "evidence_id": str(evidence.id),
                "revision_id": str(evidence.revision_id),
                "chunk_id": str(evidence.chunk_id) if evidence.chunk_id else None,
                "page": evidence.page,
                "start_line": evidence.start_line,
                "end_line": evidence.end_line,
                "cell_ref": evidence.cell_ref,
                "quote_text": evidence.quote_text,
            }
        )
    return list(claims.values())


async def _materialize_snapshot_fact_supports(
    session: AsyncSession,
    context: SnapshotBuildContext,
) -> int:
    """Freeze every eligible claim/evidence pair used by this snapshot.

    This ledger is the shared boundary for graph/SQL/retrieval projection
    building and post-withdrawal settlement. A source must be both present in
    the candidate manifest and live at build time.
    """

    await session.execute(
        delete(SnapshotFactSupport).where(
            SnapshotFactSupport.snapshot_id == context.snapshot_id,
            SnapshotFactSupport.org_id == context.org_id,
            SnapshotFactSupport.kb_id == context.kb_id,
        )
    )
    if not context.fact_claim_ids or not context.revision_manifest:
        return 0

    revision_ids = {
        UUID(str(item["revision_id"]))
        for item in context.revision_manifest
        if isinstance(item, dict) and item.get("revision_id")
    }
    if not revision_ids:
        return 0
    rows = (
        await session.execute(
            select(FactClaim.id, EvidenceSpan.id, DocumentRevision.id, SourceDocument.id)
            .join(
                EvidenceSpan,
                and_(
                    EvidenceSpan.fact_claim_id == FactClaim.id,
                    EvidenceSpan.org_id == FactClaim.org_id,
                    EvidenceSpan.kb_id == FactClaim.kb_id,
                ),
            )
            .join(
                DocumentRevision,
                and_(
                    DocumentRevision.id == EvidenceSpan.revision_id,
                    DocumentRevision.org_id == EvidenceSpan.org_id,
                    DocumentRevision.kb_id == EvidenceSpan.kb_id,
                ),
            )
            .join(
                SourceDocument,
                and_(
                    SourceDocument.id == DocumentRevision.doc_id,
                    SourceDocument.org_id == DocumentRevision.org_id,
                    SourceDocument.kb_id == DocumentRevision.kb_id,
                ),
            )
            .where(
                FactClaim.id.in_(context.fact_claim_ids),
                FactClaim.org_id == context.org_id,
                FactClaim.kb_id == context.kb_id,
                FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                EvidenceSpan.revision_id.in_(revision_ids),
                DocumentRevision.status.in_(_ELIGIBLE_REVISION_STATUSES),
                DocumentRevision.tombstoned_at.is_(None),
                SourceDocument.lifecycle_status
                == DocumentLifecycleStatus.ACTIVE.value,
            )
            .order_by(FactClaim.id, EvidenceSpan.id)
        )
    ).all()
    supports = [
        SnapshotFactSupport(
            id=uuid5(
                context.snapshot_id,
                f"fact_support:{claim_id}:{evidence_id}",
            ),
            org_id=context.org_id,
            kb_id=context.kb_id,
            snapshot_id=context.snapshot_id,
            fact_claim_id=claim_id,
            evidence_span_id=evidence_id,
            revision_id=revision_id,
            doc_id=doc_id,
        )
        for claim_id, evidence_id, revision_id, doc_id in rows
    ]
    session.add_all(supports)
    await session.flush()
    return len(supports)


async def _fact_evidence_stats(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_ids: Sequence[UUID],
) -> tuple[int, int, list[UUID]]:
    confirmed = FactReviewStatus.CONFIRMED.value
    if revision_ids:
        counts = (
            await session.execute(
                select(
                    func.count(FactClaim.id.distinct()).label("claim_count"),
                    func.count(EvidenceSpan.id.distinct()).label("evidence_count"),
                )
                .select_from(FactClaim)
                .join(EvidenceSpan, EvidenceSpan.fact_claim_id == FactClaim.id)
                .where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == kb_id,
                    FactClaim.review_status == confirmed,
                    EvidenceSpan.revision_id.in_(revision_ids),
                )
            )
        ).one()
        claim_count = int(counts.claim_count or 0)
        evidence_count = int(counts.evidence_count or 0)
    else:
        claim_count = 0
        evidence_count = 0
    evidence_in_manifest = (
        select(EvidenceSpan.id)
        .where(
            EvidenceSpan.fact_claim_id == FactClaim.id,
            EvidenceSpan.revision_id.in_(revision_ids),
        )
        .exists()
    )
    claim_in_manifest = FactClaim.ingest_run_id.in_(
        select(IngestRun.id).where(IngestRun.revision_id.in_(revision_ids))
    )
    legacy_claim_in_manifest = (
        FactClaim.ingest_run_id.is_(None)
        & (FactClaim.subject_type == "source_document")
        & FactClaim.subject_id.in_(
            select(DocumentRevision.doc_id).where(DocumentRevision.id.in_(revision_ids))
        )
    )
    missing = list(
        (
            await session.execute(
                select(FactClaim.id).where(
                    FactClaim.org_id == org_id,
                    FactClaim.kb_id == kb_id,
                    FactClaim.review_status == confirmed,
                    (legacy_claim_in_manifest | claim_in_manifest),
                    ~evidence_in_manifest,
                )
            )
        )
        .scalars()
        .all()
    )
    return claim_count, evidence_count, missing


async def _emit_snapshot_event(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    snapshot_id: UUID,
    event_type: str,
    from_snapshot_id: UUID | None,
    to_snapshot_id: UUID,
    reason: str,
    actor_id: UUID | None,
    operation_id: UUID,
) -> OutboxEvent:
    idempotency_key = f"snapshot:{org_id.hex}:{kb_id.hex}:{event_type}:{operation_id.hex}"
    payload = {
        "from": str(from_snapshot_id) if from_snapshot_id else None,
        "to": str(to_snapshot_id),
        "reason": reason,
        "actor": str(actor_id) if actor_id else None,
    }
    existing = (
        await session.execute(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.org_id != org_id
            or existing.kb_id != kb_id
            or existing.aggregate_type != "knowledge_snapshot"
            or existing.aggregate_id != snapshot_id
            or existing.event_type != event_type
            or existing.payload != payload
        ):
            raise SnapshotStateError(
                "operation_id is already bound to a different snapshot transition"
            )
        return existing
    event = OutboxEvent(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        aggregate_type="knowledge_snapshot",
        aggregate_id=snapshot_id,
        event_type=event_type,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(event)
    return event


async def build_snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    embedding_fingerprint: Mapping[str, Any] | None = None,
    config_manifest: Mapping[str, Any] | None = None,
    revision_ids: Sequence[UUID] | None = None,
    actor_id: UUID | None = None,
    reason: str = "snapshot build completed",
    registry: ProjectionBuilderRegistry | None = None,
) -> KnowledgeSnapshot:
    """Build or return the idempotent snapshot for the three fingerprints."""

    kb = await _require_owned_kb(session, org_id=org_id, kb_id=kb_id, for_update=True)
    manifest = await _select_revision_manifest(
        session,
        org_id=org_id,
        kb_id=kb_id,
        revision_ids=revision_ids,
    )
    embedding = _validate_embedding_fingerprint(
        embedding_fingerprint or current_embedding_fingerprint()
    )
    builders = registry or projection_builders
    manifest_revision_ids = [UUID(item["revision_id"]) for item in manifest]
    fact_manifest = await _fact_set_manifest(
        session,
        org_id=org_id,
        kb_id=kb_id,
        revision_ids=manifest_revision_ids,
    )
    claim_count, evidence_count, missing_claims = await _fact_evidence_stats(
        session,
        org_id=org_id,
        kb_id=kb_id,
        revision_ids=manifest_revision_ids,
    )
    config = _normalize_json_object(
        config_manifest if config_manifest is not None else current_snapshot_config(kb),
        field="config_manifest",
    )
    config["consumption_epoch"] = kb.consumption_epoch
    media = _snapshot_media()
    config["image_projection"] = (
        await media.image_projection_source_manifest(
            session,
            org_id=org_id,
            kb_id=kb_id,
            revision_ids=manifest_revision_ids,
        )
        if media is not None
        else {}
    )
    config["projection_builders"] = builders.manifest()
    config["required_projection_builders"] = builders.required_manifest()
    config["fact_set_hash"] = canonical_sha256(fact_manifest)
    config["evidence_gate_hash"] = canonical_sha256(sorted(map(str, missing_claims)))
    revision_set_hash = canonical_sha256(manifest)
    config_fingerprint = canonical_sha256(config)

    snapshot = (
        await session.execute(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.org_id == org_id,
                KnowledgeSnapshot.kb_id == kb_id,
                KnowledgeSnapshot.revision_set_hash == revision_set_hash,
                KnowledgeSnapshot.embedding_fingerprint == embedding,
                KnowledgeSnapshot.config_fingerprint == config_fingerprint,
            )
        )
    ).scalar_one_or_none()
    snapshot_status = _status_value(snapshot.status) if snapshot is not None else None
    rebuild_collected = (
        snapshot is not None
        and snapshot_status == SnapshotStatus.RETIRED.value
        and _projection_gc_completed(snapshot)
    )
    ready_operation_id = snapshot.id if snapshot is not None else None
    if rebuild_collected and snapshot is not None:
        gc_marker = snapshot.build_stats["projection_gc"]
        ready_operation_id = uuid5(
            snapshot.id,
            f"projection-rebuild:{gc_marker.get('event_id', 'unknown')}",
        )
    if (
        snapshot is not None
        and snapshot_status != SnapshotStatus.FAILED.value
        and not rebuild_collected
    ):
        return snapshot

    if snapshot is None:
        snapshot = KnowledgeSnapshot(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            revision_set_hash=revision_set_hash,
            embedding_fingerprint=embedding,
            config_fingerprint=config_fingerprint,
            revision_manifest=manifest,
            config_manifest=config,
        )
        session.add(snapshot)
        ready_operation_id = snapshot.id
    else:
        snapshot.status = SnapshotStatus.BUILDING
        snapshot.revision_manifest = manifest
        snapshot.config_manifest = config
        snapshot.build_stats = {}
        snapshot.ready_at = None
        snapshot.activated_at = None
        snapshot.retired_at = None
        snapshot.failed_at = None
        snapshot.error = None
    await session.flush()

    now = datetime.now(UTC)
    try:
        if missing_claims:
            sample = ", ".join(str(claim_id) for claim_id in missing_claims[:20])
            raise SnapshotStateError(
                f"{len(missing_claims)} confirmed fact claim(s) have no evidence: {sample}"
            )

        context = SnapshotBuildContext(
            snapshot_id=snapshot.id,
            org_id=org_id,
            kb_id=kb_id,
            revision_manifest=tuple(manifest),
            fact_claim_ids=tuple(UUID(item["claim_id"]) for item in fact_manifest),
            embedding_fingerprint=embedding,
            config_manifest=config,
        )
        async with session.begin_nested():
            await session.execute(
                select(
                    func.set_config(
                        "app.build_snapshot_id", str(context.snapshot_id), True
                    )
                )
            )
            fact_support_count = await _materialize_snapshot_fact_supports(
                session,
                context,
            )
            projection_stats = await builders.build_all(session, context)

        snapshot.build_stats = {
            "revision_count": len(manifest),
            "confirmed_claim_count": claim_count,
            "evidence_count": evidence_count,
            "fact_support_count": fact_support_count,
            "projection_count": len(projection_stats),
            "projections": projection_stats,
        }
        snapshot.status = SnapshotStatus.READY
        snapshot.ready_at = now
        snapshot.failed_at = None
        snapshot.error = None
        await _emit_snapshot_event(
            session,
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot.id,
            event_type="knowledge_snapshot.ready",
            from_snapshot_id=kb.active_snapshot_id,
            to_snapshot_id=snapshot.id,
            reason=reason,
            actor_id=actor_id,
            operation_id=ready_operation_id or snapshot.id,
        )
    except Exception as exc:
        snapshot.status = SnapshotStatus.FAILED
        snapshot.failed_at = now
        snapshot.error = str(exc) or type(exc).__name__
        media = _snapshot_media()
        if media is not None and isinstance(exc, media.SnapshotMediaProjectionError):
            try:
                async with session.begin_nested():
                    await ports.record_incident(
                        session,
                        org_id=org_id,
                        kb_id=kb_id,
                        image_asset_id=exc.asset_id,
                        category="media_projection_failure",
                        code=exc.code,
                    )
            except Exception:
                # Incident persistence is diagnostic. The bounded projection
                # failure remains the authoritative build result.
                logger.error(
                    "failed to persist media projection build incident",
                    extra={
                        "snapshot_id": str(snapshot.id),
                        "failure_code": exc.code,
                    },
                )
    await session.flush()
    return snapshot


async def _lock_transition_snapshots(
    session: AsyncSession, snapshot_ids: Sequence[UUID]
) -> dict[UUID, KnowledgeSnapshot]:
    ids = sorted(set(snapshot_ids), key=str)
    if not ids:
        return {}
    snapshots = list(
        (
            await session.execute(
                select(KnowledgeSnapshot)
                .where(KnowledgeSnapshot.id.in_(ids))
                .order_by(KnowledgeSnapshot.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return {snapshot.id: snapshot for snapshot in snapshots}


def _validate_required_projection_results(snapshot: KnowledgeSnapshot) -> None:
    required = snapshot.config_manifest.get("required_projection_builders")
    projections = snapshot.build_stats.get("projections")
    if not isinstance(required, list) or not isinstance(projections, dict):
        raise SnapshotStateError("snapshot projection build metadata is incomplete")
    configured = snapshot.config_manifest.get("projection_builders")
    if not isinstance(configured, list):
        raise SnapshotStateError("snapshot projection builder manifest is incomplete")

    configured_versions = {
        item.get("name"): item.get("version")
        for item in configured
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("version"), str)
    }
    required_versions: dict[str, str] = {}
    for item in required:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise SnapshotStateError("required projection builder manifest is invalid")
        required_versions[item["name"]] = item["version"]

    for name, version in required_versions.items():
        if configured_versions.get(name) != version:
            raise SnapshotStateError(f"required projection builder {name!r} is not configured")
        result = projections.get(name)
        if (
            not isinstance(result, dict)
            or result.get("status") != "succeeded"
            or result.get("version") != version
        ):
            raise SnapshotStateError(
                f"required projection builder {name!r} did not succeed"
            )


async def _validate_rollback_projection_retention(
    session: AsyncSession, snapshot: KnowledgeSnapshot
) -> None:
    if _projection_gc_completed(snapshot):
        raise _rollback_blocked(
            SNAPSHOT_PROJECTIONS_UNAVAILABLE,
            "target snapshot projections have been garbage collected",
        )

    try:
        manifest_doc_ids = {
            UUID(item["doc_id"])
            for item in snapshot.revision_manifest
            if isinstance(item, dict) and "doc_id" in item
        }
    except (TypeError, ValueError, AttributeError) as exc:
        raise _rollback_blocked(
            SNAPSHOT_MANIFEST_INVALID,
            "target snapshot revision manifest is invalid",
        ) from exc
    if not manifest_doc_ids:
        return
    tombstoned_doc_ids = await _latest_tombstoned_doc_ids(
        session,
        org_id=snapshot.org_id,
        kb_id=snapshot.kb_id,
    )
    if manifest_doc_ids & tombstoned_doc_ids:
        raise _rollback_blocked(
            SNAPSHOT_CONTAINS_WITHDRAWN_DOCUMENT,
            "target snapshot contains a tombstoned document",
        )


async def _validate_snapshot_document_fence(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    snapshot: KnowledgeSnapshot,
) -> None:
    captured_epoch = snapshot.config_manifest.get("consumption_epoch", 0)
    if (
        isinstance(captured_epoch, bool)
        or not isinstance(captured_epoch, int)
        or captured_epoch != kb.consumption_epoch
    ):
        raise _rollback_blocked(
            SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED,
            "target snapshot was built before the current document withdrawal fence",
        )
    try:
        manifest_doc_ids = {
            UUID(item["doc_id"])
            for item in snapshot.revision_manifest
            if isinstance(item, dict) and item.get("doc_id")
        }
    except (TypeError, ValueError, AttributeError) as exc:
        raise _rollback_blocked(
            SNAPSHOT_MANIFEST_INVALID,
            "target snapshot revision manifest is invalid",
        ) from exc
    if not manifest_doc_ids:
        return
    fenced_doc_ids = set(
        (
            await session.scalars(
                select(SourceDocument.id).where(
                    SourceDocument.org_id == kb.org_id,
                    SourceDocument.kb_id == kb.id,
                    SourceDocument.id.in_(manifest_doc_ids),
                    SourceDocument.lifecycle_status != "active",
                )
            )
        ).all()
    )
    fenced_doc_ids.update(
        await _latest_tombstoned_doc_ids(
            session,
            org_id=kb.org_id,
            kb_id=kb.id,
        )
    )
    if manifest_doc_ids & fenced_doc_ids:
        raise _rollback_blocked(
            SNAPSHOT_CONTAINS_WITHDRAWN_DOCUMENT,
            "target snapshot contains a withdrawn document",
        )


async def snapshot_rollback_capability(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    snapshot: KnowledgeSnapshot,
) -> SnapshotRollbackCapability:
    """Return the current public rollback capability without mutating state."""

    if _status_value(snapshot.status) != SnapshotStatus.RETIRED.value:
        return SnapshotRollbackCapability.blocked(
            _rollback_blocked(
                SNAPSHOT_NOT_RETIRED,
                (
                    "target snapshot must be retired, "
                    f"got {_status_value(snapshot.status)}"
                ),
            )
        )

    try:
        await _validate_snapshot_document_fence(session, kb=kb, snapshot=snapshot)
        await _validate_rollback_projection_retention(session, snapshot)
    except SnapshotTransitionBlocked as exc:
        return SnapshotRollbackCapability.blocked(exc)

    media = _snapshot_media()
    if media is not None:
        try:
            await media.validate_snapshot_media_projection(
                session,
                snapshot,
                require_current_acceptance=False,
            )
        except media.SnapshotMediaProjectionError as exc:
            return SnapshotRollbackCapability.blocked(
                _rollback_blocked(SNAPSHOT_MEDIA_UNAVAILABLE, str(exc))
            )
    return SnapshotRollbackCapability.permitted()


async def _switch_active_snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    target_snapshot_id: UUID,
    allowed_target_status: str,
    event_type: str,
    actor_id: UUID | None,
    reason: str,
    operation_id: UUID | None,
    validate_projections: bool,
) -> KnowledgeSnapshot:
    kb = await _require_owned_kb(session, org_id=org_id, kb_id=kb_id, for_update=True)
    if kb.active_snapshot_id == target_snapshot_id:
        target = await session.get(KnowledgeSnapshot, target_snapshot_id)
        if (
            target is None
            or target.org_id != org_id
            or target.kb_id != kb_id
            or _status_value(target.status) != SnapshotStatus.ACTIVE.value
        ):
            raise SnapshotStateError("active snapshot pointer is inconsistent")
        await _validate_snapshot_document_fence(session, kb=kb, snapshot=target)
        return target

    lock_ids = [target_snapshot_id]
    if kb.active_snapshot_id is not None:
        lock_ids.append(kb.active_snapshot_id)
    snapshots = await _lock_transition_snapshots(session, lock_ids)
    target = snapshots.get(target_snapshot_id)
    if target is None or target.org_id != org_id or target.kb_id != kb_id:
        raise SnapshotStateError("target snapshot does not belong to the requested KB")
    if _status_value(target.status) != allowed_target_status:
        message = (
            f"target snapshot must be {allowed_target_status}, "
            f"got {_status_value(target.status)}"
        )
        if event_type == "knowledge_snapshot.rolled_back":
            raise _rollback_blocked(SNAPSHOT_NOT_RETIRED, message)
        raise SnapshotStateError(message)
    await _validate_snapshot_document_fence(session, kb=kb, snapshot=target)
    if validate_projections:
        _validate_required_projection_results(target)
        media = _snapshot_media()
        if media is not None:
            try:
                await media.validate_snapshot_media_projection(session, target)
            except media.SnapshotMediaProjectionError as exc:
                await ports.record_incident(
                    session,
                    org_id=org_id,
                    kb_id=kb_id,
                    image_asset_id=exc.asset_id,
                    category="media_projection_failure",
                    code=exc.code,
                )
                raise SnapshotStateError(str(exc)) from None
    elif event_type == "knowledge_snapshot.rolled_back":
        await _validate_rollback_projection_retention(session, target)
        media = _snapshot_media()
        if media is not None:
            try:
                await media.validate_snapshot_media_projection(
                    session,
                    target,
                    require_current_acceptance=False,
                )
            except media.SnapshotMediaProjectionError as exc:
                await ports.record_incident(
                    session,
                    org_id=org_id,
                    kb_id=kb_id,
                    image_asset_id=exc.asset_id,
                    category="media_projection_failure",
                    code=exc.code,
                )
                raise _rollback_blocked(SNAPSHOT_MEDIA_UNAVAILABLE, str(exc)) from None

    current: KnowledgeSnapshot | None = None
    if kb.active_snapshot_id is not None:
        current = snapshots.get(kb.active_snapshot_id)
        if (
            current is None
            or current.org_id != org_id
            or current.kb_id != kb_id
            or _status_value(current.status) != SnapshotStatus.ACTIVE.value
        ):
            raise SnapshotStateError("current active snapshot is inconsistent")

    now = datetime.now(UTC)
    from_snapshot_id = current.id if current else None
    if current is not None:
        current.status = SnapshotStatus.RETIRED
        current.retired_at = now
        await session.flush()

    target.status = SnapshotStatus.ACTIVE
    target.activated_at = now
    target.retired_at = None
    await session.flush()
    kb.active_snapshot_id = target.id
    await _emit_snapshot_event(
        session,
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=target.id,
        event_type=event_type,
        from_snapshot_id=from_snapshot_id,
        to_snapshot_id=target.id,
        reason=reason,
        actor_id=actor_id,
        operation_id=operation_id or uuid4(),
    )
    await session.flush()
    return target


async def activate_snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    snapshot_id: UUID,
    actor_id: UUID | None,
    reason: str = "snapshot activated",
    operation_id: UUID | None = None,
) -> KnowledgeSnapshot:
    return await _switch_active_snapshot(
        session,
        org_id=org_id,
        kb_id=kb_id,
        target_snapshot_id=snapshot_id,
        allowed_target_status=SnapshotStatus.READY.value,
        event_type="knowledge_snapshot.activated",
        actor_id=actor_id,
        reason=reason,
        operation_id=operation_id,
        validate_projections=True,
    )


async def rollback_snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    target_snapshot_id: UUID,
    actor_id: UUID | None,
    reason: str,
    operation_id: UUID | None = None,
) -> KnowledgeSnapshot:
    return await _switch_active_snapshot(
        session,
        org_id=org_id,
        kb_id=kb_id,
        target_snapshot_id=target_snapshot_id,
        allowed_target_status=SnapshotStatus.RETIRED.value,
        event_type="knowledge_snapshot.rolled_back",
        actor_id=actor_id,
        reason=reason,
        operation_id=operation_id,
        validate_projections=False,
    )


# SQL, Wiki, retrieval, and graph are release gates.
from nicekit.kb.graph_projection import GraphProjectionBuilder  # noqa: E402
from nicekit.kb.projections import SqlProjectionBuilder, WikiProjectionBuilder  # noqa: E402
from nicekit.kb.retrieval_projection import RetrievalProjectionBuilder  # noqa: E402

projection_builders.register(GraphProjectionBuilder(), required=True)
projection_builders.register(SqlProjectionBuilder(), required=True)
projection_builders.register(WikiProjectionBuilder(), required=True)
projection_builders.register(RetrievalProjectionBuilder(), required=True)

# 媒体投影是媒体波次的发布门禁:模块装配后即注册为必需 builder,未装配时
# 快照 config_manifest 里就不含该 builder(与"没有媒体资产"等价)。
_media_module = _snapshot_media()
if _media_module is not None:
    projection_builders.register(
        _media_module.SnapshotMediaProjectionBuilder(), required=True
    )
