"""Versioned embedding rebuild campaigns with resumable, fenced workers."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb.embedding import (
    EmbeddingFingerprint,
    EmbeddingService,
    EmbeddingUnavailableError,
    chunk_context_text,
    chunk_embedding_text,
    embedding_content_hash,
    normalize_embedding_config,
)
from nicekit.llm.capability_routes import provider_model_endpoint
from nicekit.llm.runtime_config import runtime_overrides
from nicekit.models.kb import (
    EMBEDDING_DIM,
    EmbeddingMigrationCampaign,
    EmbeddingReindexJob,
    KbChunk,
    KbChunkEmbedding,
    KnowledgeBase,
)
from nicekit.models.service_config import ServiceConfig
from nicekit.models.tenancy import Organization

_DEFAULT_DUAL_READ_SECONDS = 24 * 60 * 60
_DEFAULT_LEASE_SECONDS = 5 * 60
_DIMENSION_RE = re.compile(r"actual[=: ]+(\d+)|实际\s*(\d+)", re.IGNORECASE)
_CAMPAIGN_CREATE_LOCK_ID = 0x454D4244


class _Embedder(Protocol):
    async def embed(
        self, texts: list[str], *, org_id: UUID, task: str | None = None
    ) -> list[list[float]]: ...


EmbeddingServiceFactory = Callable[[Mapping[str, Any]], _Embedder]
SessionFactory = async_sessionmaker[AsyncSession]


class EmbeddingDimensionMismatchError(EmbeddingUnavailableError):
    code = "embedding_dimension_mismatch"

    def __init__(self, *, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        self.offline_migration = (
            "alter vector column dimension, rebuild all embeddings, then rebuild "
            "the pgvector index before switching configuration"
        )
        super().__init__(
            f"embedding dimension mismatch: expected={expected}, actual={actual}; "
            f"offline migration required: {self.offline_migration}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "expected_dim": self.expected,
            "actual_dim": self.actual,
            "offline_migration": self.offline_migration,
        }


class EmbeddingCampaignConflictError(RuntimeError):
    pass


class EmbeddingLeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingCampaignStatus:
    campaign_id: UUID
    status: str
    total_jobs: int
    ready_jobs: int
    failed_jobs: int
    total_chunks: int
    embedded_chunks: int
    coverage: float
    dual_read_until: datetime | None
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": str(self.campaign_id),
            "status": self.status,
            "total_jobs": self.total_jobs,
            "ready_jobs": self.ready_jobs,
            "failed_jobs": self.failed_jobs,
            "total_chunks": self.total_chunks,
            "embedded_chunks": self.embedded_chunks,
            "coverage": self.coverage,
            "dual_read_until": (self.dual_read_until.isoformat() if self.dual_read_until else None),
            "error": self.error,
        }


@dataclass(frozen=True)
class _ChunkWork:
    chunk_id: UUID
    text: str
    content_hash: str


async def _organization_ids(session_factory: SessionFactory) -> list[UUID]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(Organization.id).order_by(Organization.id))).scalars()
        )


async def _campaign_job_counts(
    campaign_id: UUID, session_factory: SessionFactory
) -> tuple[int, int, int, int, int]:
    async with org_session(session_factory, get_settings().platform_org_id) as session:
        row = (
            await session.execute(
                select(
                    func.count(EmbeddingReindexJob.id),
                    func.count(EmbeddingReindexJob.id).filter(
                        EmbeddingReindexJob.status == "ready"
                    ),
                    func.count(EmbeddingReindexJob.id).filter(
                        EmbeddingReindexJob.status == "failed"
                    ),
                    func.coalesce(func.sum(EmbeddingReindexJob.total_chunks), 0),
                    func.coalesce(func.sum(EmbeddingReindexJob.embedded_chunks), 0),
                ).where(EmbeddingReindexJob.campaign_id == campaign_id)
            )
        ).one()
    return tuple(int(value) for value in row)  # type: ignore[return-value]


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _default_embedding_config() -> dict[str, Any]:
    override = runtime_overrides("embedding")
    return _embedding_config_from_payload(override)


def _embedding_config_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    settings = get_settings()
    values = payload or {}
    raw_dim = values.get("dim")
    return normalize_embedding_config(
        {
            "provider": values.get("provider") or settings.embedding_provider,
            "model": values.get("model") or settings.embedding_model,
            "dim": int(raw_dim) if raw_dim not in (None, "") else EMBEDDING_DIM,
        }
    )


async def _tenant_knowledge_bases(
    session_factory: SessionFactory,
) -> list[KnowledgeBase]:
    kbs: list[KnowledgeBase] = []
    for org_id in await _organization_ids(session_factory):
        async with org_session(session_factory, org_id) as session:
            kbs.extend(
                (
                    await session.execute(
                        select(KnowledgeBase)
                        .where(
                            KnowledgeBase.org_id == org_id,
                            KnowledgeBase.lifecycle_status == "active",
                        )
                        .order_by(KnowledgeBase.id)
                    )
                ).scalars()
            )
    return kbs


async def _ensure_campaign_jobs(
    campaign: EmbeddingMigrationCampaign,
    session_factory: SessionFactory,
) -> None:
    """Repair the non-atomic campaign/job creation boundary after a crash."""
    kbs = await _tenant_knowledge_bases(session_factory)
    for kb in kbs:
        async with org_session(session_factory, kb.org_id) as session:
            stmt = insert(EmbeddingReindexJob).values(
                campaign_id=campaign.id,
                org_id=kb.org_id,
                kb_id=kb.id,
                status="queued",
            )
            await session.execute(
                stmt.on_conflict_do_nothing(constraint="uq_embedding_reindex_job_campaign_kb")
            )
            await session.commit()
    counts = await _campaign_job_counts(campaign.id, session_factory)
    values: dict[str, Any] = {
        "total_jobs": counts[0],
        "ready_jobs": counts[1],
        "failed_jobs": counts[2],
    }
    if counts[1] != counts[0]:
        values.update(
            status="running",
            dual_read_until=None,
            error="campaign jobs reconciled; automatic retry scheduled",
        )
    async with session_factory() as session:
        await session.execute(
            update(EmbeddingMigrationCampaign)
            .where(
                EmbeddingMigrationCampaign.id == campaign.id,
                EmbeddingMigrationCampaign.status.in_(("queued", "running", "dual_read")),
            )
            .values(**values)
        )
        await session.commit()


def _make_embedder(config: Mapping[str, Any]) -> EmbeddingService:
    endpoint = provider_model_endpoint(
        str(config["provider"]),
        str(config["model"]),
        capability="embedding",
    )
    if endpoint is None:
        raise EmbeddingUnavailableError(
            "embedding 模型未配置可用的提供商凭证"
        )
    return EmbeddingService(
        provider=str(config["provider"]),
        model=str(config["model"]),
        dim=int(config["dim"]),
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
    )


async def _close_embedder(embedder: _Embedder) -> None:
    close = getattr(embedder, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def _actual_dimension_from_error(exc: EmbeddingUnavailableError) -> int | None:
    match = _DIMENSION_RE.search(str(exc))
    if match is None:
        return None
    return int(next(group for group in match.groups() if group is not None))


async def _probe_target(
    config: Mapping[str, Any],
    *,
    org_id: UUID,
    embedding_service_factory: EmbeddingServiceFactory,
) -> None:
    fingerprint = EmbeddingFingerprint.from_config(config)
    if fingerprint.dim != EMBEDDING_DIM:
        raise EmbeddingDimensionMismatchError(expected=EMBEDDING_DIM, actual=fingerprint.dim)
    embedder = embedding_service_factory(config)
    try:
        try:
            vectors = await embedder.embed(
                ["embedding migration dimension probe"],
                org_id=org_id,
                task="kb.embedding_migration.probe",
            )
        except EmbeddingUnavailableError as exc:
            actual = _actual_dimension_from_error(exc)
            if actual is not None:
                raise EmbeddingDimensionMismatchError(
                    expected=EMBEDDING_DIM, actual=actual
                ) from exc
            raise
        if len(vectors) != 1:
            raise EmbeddingUnavailableError("embedding probe returned an invalid batch")
        actual = len(vectors[0])
        if actual != EMBEDDING_DIM:
            raise EmbeddingDimensionMismatchError(expected=EMBEDDING_DIM, actual=actual)
    finally:
        await _close_embedder(embedder)


async def create_embedding_campaign(
    session_factory: SessionFactory,
    target_config: Mapping[str, Any],
    dual_read_seconds: int = _DEFAULT_DUAL_READ_SECONDS,
    *,
    embedding_service_factory: EmbeddingServiceFactory | None = None,
) -> EmbeddingMigrationCampaign:
    """Probe a same-dimension target and create one idempotent job per KB."""
    if dual_read_seconds <= 0:
        raise ValueError("dual_read_seconds must be positive")
    target = normalize_embedding_config(target_config)
    target_fingerprint = EmbeddingFingerprint.from_config(target).as_dict()

    async with session_factory() as session:
        active = (
            await session.execute(
                select(EmbeddingMigrationCampaign)
                .where(EmbeddingMigrationCampaign.status.in_(("queued", "running", "dual_read")))
                .order_by(EmbeddingMigrationCampaign.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if active is not None and active.target_fingerprint != target_fingerprint:
            raise EmbeddingCampaignConflictError("another embedding migration campaign is active")

        service_config = (
            await session.execute(select(ServiceConfig).where(ServiceConfig.name == "embedding"))
        ).scalar_one_or_none()
        active_config = _embedding_config_from_payload(
            service_config.payload if service_config is not None else None
        )
        source_fingerprint = EmbeddingFingerprint.from_config(active_config).as_dict()
        source = dict(source_fingerprint)
    kbs = await _tenant_knowledge_bases(session_factory)

    probe_org_id = kbs[0].org_id if kbs else get_settings().platform_org_id
    await _probe_target(
        target,
        org_id=probe_org_id,
        embedding_service_factory=embedding_service_factory or _make_embedder,
    )

    async with session_factory() as session:
        # Serialize competing creators after the network probe without holding a DB lock.
        await session.execute(select(func.pg_advisory_xact_lock(_CAMPAIGN_CREATE_LOCK_ID)))
        competing = (
            (
                await session.execute(
                    select(EmbeddingMigrationCampaign)
                    .where(
                        EmbeddingMigrationCampaign.status.in_(("queued", "running", "dual_read"))
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if competing is not None:
            if competing.target_fingerprint == target_fingerprint:
                competing.target_config = target
                session.add(competing)
                await session.commit()
                await _ensure_campaign_jobs(competing, session_factory)
                return competing
            raise EmbeddingCampaignConflictError("another embedding migration campaign is active")

        campaign = EmbeddingMigrationCampaign(
            source_config=source,
            target_config=target,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
            status="queued" if kbs else "dual_read",
            total_jobs=len(kbs),
            ready_jobs=0,
            failed_jobs=0,
            dual_read_seconds=dual_read_seconds,
            dual_read_until=(
                datetime.now(UTC) + timedelta(seconds=dual_read_seconds) if not kbs else None
            ),
        )
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
    try:
        await _ensure_campaign_jobs(campaign, session_factory)
    except Exception as exc:
        async with session_factory() as session:
            await session.execute(
                update(EmbeddingMigrationCampaign)
                .where(EmbeddingMigrationCampaign.id == campaign.id)
                .values(status="failed", error=str(exc)[:2000])
            )
            await session.commit()
        raise
    return campaign


async def _claim_job(
    campaign_id: UUID,
    session_factory: SessionFactory,
    *,
    worker_id: str,
    lease_seconds: int,
) -> tuple[UUID, UUID, UUID, str] | None:
    now = datetime.now(UTC)
    for org_id in await _organization_ids(session_factory):
        async with org_session(session_factory, org_id) as session:
            job = (
                await session.execute(
                    select(EmbeddingReindexJob)
                    .join(
                        EmbeddingMigrationCampaign,
                        EmbeddingMigrationCampaign.id == EmbeddingReindexJob.campaign_id,
                    )
                    .where(
                        EmbeddingReindexJob.campaign_id == campaign_id,
                        EmbeddingReindexJob.org_id == org_id,
                        EmbeddingMigrationCampaign.status.in_(("queued", "running")),
                        or_(
                            EmbeddingReindexJob.status == "queued",
                            and_(
                                EmbeddingReindexJob.status == "running",
                                EmbeddingReindexJob.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .order_by(EmbeddingReindexJob.created_at, EmbeddingReindexJob.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if job is None:
                continue
            token = f"{worker_id[:80]}:{uuid4().hex}"
            job.status = "running"
            job.lease_owner = token
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            job.attempts += 1
            job.error = None
            session.add(job)
            await session.execute(
                update(EmbeddingMigrationCampaign)
                .where(
                    EmbeddingMigrationCampaign.id == campaign_id,
                    EmbeddingMigrationCampaign.status == "queued",
                )
                .values(status="running", error=None, updated_at=now)
            )
            await session.commit()
            return job.id, job.org_id, job.kb_id, token
    return None


async def _load_chunk_page(
    session_factory: SessionFactory,
    *,
    org_id: UUID,
    kb_id: UUID,
    fingerprint: EmbeddingFingerprint,
    after_id: UUID | None,
    page_size: int,
) -> tuple[list[_ChunkWork], UUID | None]:
    async with org_session(session_factory, org_id) as session:
        stmt = (
            select(KbChunk, KbChunkEmbedding.content_hash)
            .outerjoin(
                KbChunkEmbedding,
                and_(
                    KbChunkEmbedding.chunk_id == KbChunk.id,
                    KbChunkEmbedding.provider == fingerprint.provider,
                    KbChunkEmbedding.model == fingerprint.model,
                    KbChunkEmbedding.dim == fingerprint.dim,
                ),
            )
            .where(KbChunk.kb_id == kb_id, KbChunk.quarantined.is_(False))
            .order_by(KbChunk.id)
            .limit(page_size)
        )
        if after_id is not None:
            stmt = stmt.where(KbChunk.id > after_id)
        rows = (await session.execute(stmt)).all()
    work: list[_ChunkWork] = []
    for chunk, stored_hash in rows:
        text = chunk_embedding_text(
            chunk.content, chunk.heading_path, chunk_context_text(chunk.meta)
        )
        content_hash = embedding_content_hash(text)
        if stored_hash != content_hash:
            work.append(_ChunkWork(chunk.id, text, content_hash))
    return work, rows[-1][0].id if rows else None


async def _write_batch(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    org_id: UUID,
    kb_id: UUID,
    claim_token: str,
    fingerprint: EmbeddingFingerprint,
    work: list[_ChunkWork],
    vectors: list[list[float]],
    lease_seconds: int,
) -> None:
    now = datetime.now(UTC)
    expected = {item.chunk_id: item for item in work}
    async with org_session(session_factory, org_id) as session:
        owned = (
            await session.execute(
                select(EmbeddingReindexJob.id)
                .where(
                    EmbeddingReindexJob.id == job_id,
                    EmbeddingReindexJob.status == "running",
                    EmbeddingReindexJob.lease_owner == claim_token,
                    EmbeddingReindexJob.lease_expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if owned is None:
            raise EmbeddingLeaseLostError(f"embedding job lease lost: {job_id}")
        current_chunks = (
            (
                await session.execute(
                    select(KbChunk).where(
                        KbChunk.id.in_(expected),
                        KbChunk.kb_id == kb_id,
                        KbChunk.quarantined.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        vector_by_id = {item.chunk_id: vector for item, vector in zip(work, vectors, strict=True)}
        values = []
        for chunk in current_chunks:
            item = expected[chunk.id]
            current_hash = embedding_content_hash(
                chunk_embedding_text(
                    chunk.content, chunk.heading_path, chunk_context_text(chunk.meta)
                )
            )
            if current_hash != item.content_hash:
                continue
            values.append(
                {
                    "org_id": org_id,
                    "kb_id": kb_id,
                    "chunk_id": chunk.id,
                    "provider": fingerprint.provider,
                    "model": fingerprint.model,
                    "dim": fingerprint.dim,
                    "content_hash": current_hash,
                    "embedding": vector_by_id[chunk.id],
                    "updated_at": now,
                }
            )
        if values:
            stmt = insert(KbChunkEmbedding).values(values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_kb_chunk_embedding_version",
                    set_={
                        "org_id": stmt.excluded.org_id,
                        "kb_id": stmt.excluded.kb_id,
                        "content_hash": stmt.excluded.content_hash,
                        "embedding": stmt.excluded.embedding,
                        "updated_at": now,
                    },
                )
            )
        await session.execute(
            update(EmbeddingReindexJob)
            .where(
                EmbeddingReindexJob.id == job_id,
                EmbeddingReindexJob.lease_owner == claim_token,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        await session.commit()


async def _renew_job_lease(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    org_id: UUID,
    claim_token: str,
    lease_seconds: int,
) -> None:
    now = datetime.now(UTC)
    async with org_session(session_factory, org_id) as session:
        result = await session.execute(
            update(EmbeddingReindexJob)
            .where(
                EmbeddingReindexJob.id == job_id,
                EmbeddingReindexJob.status == "running",
                EmbeddingReindexJob.lease_owner == claim_token,
                EmbeddingReindexJob.lease_expires_at > now,
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            raise EmbeddingLeaseLostError(f"embedding job lease lost: {job_id}")
        await session.commit()


async def _coverage(
    session_factory: SessionFactory,
    *,
    org_id: UUID,
    kb_id: UUID,
    fingerprint: EmbeddingFingerprint,
) -> tuple[int, int]:
    after_id: UUID | None = None
    total = 0
    matching = 0
    while True:
        async with org_session(session_factory, org_id) as session:
            stmt = (
                select(KbChunk, KbChunkEmbedding.content_hash)
                .outerjoin(
                    KbChunkEmbedding,
                    and_(
                        KbChunkEmbedding.chunk_id == KbChunk.id,
                        KbChunkEmbedding.provider == fingerprint.provider,
                        KbChunkEmbedding.model == fingerprint.model,
                        KbChunkEmbedding.dim == fingerprint.dim,
                    ),
                )
                .where(KbChunk.kb_id == kb_id, KbChunk.quarantined.is_(False))
                .order_by(KbChunk.id)
                .limit(1000)
            )
            if after_id is not None:
                stmt = stmt.where(KbChunk.id > after_id)
            rows = (await session.execute(stmt)).all()
        if not rows:
            break
        total += len(rows)
        for chunk, stored_hash in rows:
            current_hash = embedding_content_hash(
                chunk_embedding_text(
                    chunk.content, chunk.heading_path, chunk_context_text(chunk.meta)
                )
            )
            matching += stored_hash == current_hash
        after_id = rows[-1][0].id
    return total, matching


async def _finish_job(
    campaign_id: UUID,
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    org_id: UUID,
    claim_token: str,
    total: int,
    embedded: int,
) -> None:
    now = datetime.now(UTC)
    async with org_session(session_factory, org_id) as session:
        job = (
            await session.execute(
                select(EmbeddingReindexJob)
                .where(
                    EmbeddingReindexJob.id == job_id,
                    EmbeddingReindexJob.status == "running",
                    EmbeddingReindexJob.lease_owner == claim_token,
                    EmbeddingReindexJob.lease_expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise EmbeddingLeaseLostError(f"embedding job lease lost: {job_id}")
        job.total_chunks = total
        job.embedded_chunks = embedded
        job.heartbeat_at = now
        if embedded == total:
            job.status = "ready"
            job.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
        else:
            job.status = "queued"
            job.lease_owner = None
            job.lease_expires_at = None
        session.add(job)
        await session.commit()

    async with session_factory() as session:
        campaign = (
            await session.execute(
                select(EmbeddingMigrationCampaign)
                .where(EmbeddingMigrationCampaign.id == campaign_id)
                .with_for_update()
            )
        ).scalar_one()
        counts = await _campaign_job_counts(campaign_id, session_factory)
        campaign.total_jobs = counts[0]
        campaign.ready_jobs = counts[1]
        campaign.failed_jobs = counts[2]
        if campaign.total_jobs == campaign.ready_jobs:
            campaign.status = "dual_read"
            campaign.dual_read_until = now + timedelta(seconds=campaign.dual_read_seconds)
        session.add(campaign)
        await session.commit()


async def _mark_job_failed(
    session_factory: SessionFactory,
    *,
    campaign_id: UUID,
    job_id: UUID,
    org_id: UUID,
    claim_token: str,
    error: str,
) -> None:
    now = datetime.now(UTC)
    async with org_session(session_factory, org_id) as session:
        await session.execute(
            update(EmbeddingReindexJob)
            .where(
                EmbeddingReindexJob.id == job_id,
                EmbeddingReindexJob.status == "running",
                EmbeddingReindexJob.lease_owner == claim_token,
            )
            .values(
                status="failed",
                error=error[:2000],
                lease_owner=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    counts = await _campaign_job_counts(campaign_id, session_factory)
    async with session_factory() as session:
        await session.execute(
            update(EmbeddingMigrationCampaign)
            .where(EmbeddingMigrationCampaign.id == campaign_id)
            .values(
                status="failed",
                ready_jobs=counts[1],
                failed_jobs=counts[2],
                error=error[:2000],
                updated_at=now,
            )
        )
        await session.commit()


async def _release_budget_paused_job(
    session_factory: SessionFactory,
    *,
    campaign_id: UUID,
    job_id: UUID,
    org_id: UUID,
    claim_token: str,
    error: str,
) -> None:
    now = datetime.now(UTC)
    async with org_session(session_factory, org_id) as session:
        await session.execute(
            update(EmbeddingReindexJob)
            .where(
                EmbeddingReindexJob.id == job_id,
                EmbeddingReindexJob.status == "running",
                EmbeddingReindexJob.lease_owner == claim_token,
            )
            .values(
                status="queued",
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    async with session_factory() as session:
        await session.execute(
            update(EmbeddingMigrationCampaign)
            .where(
                EmbeddingMigrationCampaign.id == campaign_id,
                EmbeddingMigrationCampaign.status.in_(("queued", "running")),
            )
            .values(
                status="running",
                error=f"budget paused; automatic retry scheduled: {error}"[:2000],
                updated_at=now,
            )
        )
        await session.commit()


async def execute_embedding_campaign(
    campaign_id: UUID,
    session_factory: SessionFactory,
    *,
    embedding_service_factory: EmbeddingServiceFactory | None = None,
    worker_id: str | None = None,
    batch_size: int = 64,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> EmbeddingCampaignStatus:
    """Run all currently claimable jobs; persisted version rows are the checkpoint."""
    if batch_size <= 0 or lease_seconds <= 0:
        raise ValueError("batch_size and lease_seconds must be positive")
    async with session_factory() as session:
        campaign = await session.get(EmbeddingMigrationCampaign, campaign_id)
        if campaign is None:
            raise LookupError(f"embedding campaign not found: {campaign_id}")
        if _status_value(campaign.status) not in {"queued", "running"}:
            return await get_embedding_campaign_status(campaign_id, session_factory)
        config = normalize_embedding_config(campaign.target_config)
    fingerprint = EmbeddingFingerprint.from_config(config)
    factory = embedding_service_factory or _make_embedder
    worker = worker_id or f"embedding-reindex-{uuid4().hex[:12]}"

    while claimed := await _claim_job(
        campaign_id,
        session_factory,
        worker_id=worker,
        lease_seconds=lease_seconds,
    ):
        job_id, org_id, kb_id, claim_token = claimed
        embedder: _Embedder | None = None
        try:
            embedder = factory(config)
            after_id: UUID | None = None
            while True:
                work, next_id = await _load_chunk_page(
                    session_factory,
                    org_id=org_id,
                    kb_id=kb_id,
                    fingerprint=fingerprint,
                    after_id=after_id,
                    page_size=batch_size,
                )
                if next_id is None:
                    break
                after_id = next_id
                if not work:
                    continue
                await _renew_job_lease(
                    session_factory,
                    job_id=job_id,
                    org_id=org_id,
                    claim_token=claim_token,
                    lease_seconds=lease_seconds,
                )
                vectors = await embedder.embed(
                    [item.text for item in work],
                    org_id=org_id,
                    task="kb.embedding_migration.batch",
                )
                await _write_batch(
                    session_factory,
                    job_id=job_id,
                    org_id=org_id,
                    kb_id=kb_id,
                    claim_token=claim_token,
                    fingerprint=fingerprint,
                    work=work,
                    vectors=vectors,
                    lease_seconds=lease_seconds,
                )
            total, embedded = await _coverage(
                session_factory,
                org_id=org_id,
                kb_id=kb_id,
                fingerprint=fingerprint,
            )
            await _finish_job(
                campaign_id,
                session_factory,
                job_id=job_id,
                org_id=org_id,
                claim_token=claim_token,
                total=total,
                embedded=embedded,
            )
        except Exception as exc:
            # Budget errors and cancellation remain observable to the dispatcher.
            from nicekit.llm.service import LlmBudgetExceededError

            if isinstance(exc, LlmBudgetExceededError):
                await _release_budget_paused_job(
                    session_factory,
                    campaign_id=campaign_id,
                    job_id=job_id,
                    org_id=org_id,
                    claim_token=claim_token,
                    error=str(exc),
                )
                raise
            if isinstance(exc, EmbeddingLeaseLostError):
                raise
            await _mark_job_failed(
                session_factory,
                campaign_id=campaign_id,
                job_id=job_id,
                org_id=org_id,
                claim_token=claim_token,
                error=str(exc),
            )
            raise
        finally:
            if embedder is not None:
                await _close_embedder(embedder)
    return await get_embedding_campaign_status(campaign_id, session_factory)


async def reconcile_embedding_campaigns(
    session_factory: SessionFactory,
    *,
    embedding_service_factory: EmbeddingServiceFactory | None = None,
    batch_size: int = 64,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> list[EmbeddingCampaignStatus]:
    """Repair campaign jobs and resume every active migration campaign."""
    async with session_factory() as session:
        campaigns = list(
            (
                await session.execute(
                    select(EmbeddingMigrationCampaign)
                    .where(
                        EmbeddingMigrationCampaign.status.in_(
                            ("queued", "running", "dual_read")
                        )
                    )
                    .order_by(EmbeddingMigrationCampaign.created_at)
                )
            ).scalars()
        )
    statuses: list[EmbeddingCampaignStatus] = []
    for campaign in campaigns:
        await _ensure_campaign_jobs(campaign, session_factory)
        statuses.append(
            await execute_embedding_campaign(
                campaign.id,
                session_factory,
                embedding_service_factory=embedding_service_factory,
                batch_size=batch_size,
                lease_seconds=lease_seconds,
            )
        )
    return statuses


async def retry_embedding_campaign(
    campaign_id: UUID, session_factory: SessionFactory
) -> EmbeddingMigrationCampaign:
    now = datetime.now(UTC)
    async with session_factory() as session:
        campaign = (
            await session.execute(
                select(EmbeddingMigrationCampaign)
                .where(EmbeddingMigrationCampaign.id == campaign_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise LookupError(f"embedding campaign not found: {campaign_id}")
        latest_id = (
            await session.execute(
                select(EmbeddingMigrationCampaign.id)
                .order_by(EmbeddingMigrationCampaign.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        if latest_id != campaign.id:
            raise EmbeddingCampaignConflictError(
                "only the latest embedding migration campaign can be retried"
            )
        if _status_value(campaign.status) not in {"failed", "running", "queued"}:
            raise EmbeddingCampaignConflictError(
                f"campaign cannot be retried from {campaign.status}"
            )
        service_config = (
            await session.execute(
                select(ServiceConfig).where(ServiceConfig.name == "embedding")
            )
        ).scalar_one_or_none()
        active_fingerprint = EmbeddingFingerprint.from_config(
            _embedding_config_from_payload(
                service_config.payload if service_config is not None else None
            )
        ).as_dict()
        if active_fingerprint != campaign.source_fingerprint:
            raise EmbeddingCampaignConflictError(
                "active embedding fingerprint changed; create a new campaign"
            )
        campaign.status = "queued"
        campaign.failed_jobs = 0
        campaign.error = None
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
    for org_id in await _organization_ids(session_factory):
        async with org_session(session_factory, org_id) as session:
            await session.execute(
                update(EmbeddingReindexJob)
                .where(
                    EmbeddingReindexJob.campaign_id == campaign_id,
                    EmbeddingReindexJob.org_id == org_id,
                    EmbeddingReindexJob.status == "failed",
                )
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    error=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            await session.commit()
    return campaign


async def _validate_campaign_for_cutover(
    campaign: EmbeddingMigrationCampaign,
    session_factory: SessionFactory,
) -> tuple[bool, int, int, int]:
    """Revalidate jobs while the caller's table lock blocks chunk mutations."""
    fingerprint = EmbeddingFingerprint.from_config(campaign.target_config)
    jobs: list[EmbeddingReindexJob] = []
    coverage_gap = False
    expected_kb_ids = {kb.id for kb in await _tenant_knowledge_bases(session_factory)}
    async with org_session(session_factory, get_settings().platform_org_id) as session:
        job_org_ids = list(
            (
                await session.execute(
                    select(EmbeddingReindexJob.org_id)
                    .where(EmbeddingReindexJob.campaign_id == campaign.id)
                    .distinct()
                    .order_by(EmbeddingReindexJob.org_id)
                )
            ).scalars()
        )
    for org_id in job_org_ids:
        async with org_session(session_factory, org_id) as session:
            org_jobs = list(
                (
                    await session.execute(
                        select(EmbeddingReindexJob)
                        .where(
                            EmbeddingReindexJob.campaign_id == campaign.id,
                            EmbeddingReindexJob.org_id == org_id,
                        )
                        .order_by(EmbeddingReindexJob.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            jobs.extend(org_jobs)
            for job in org_jobs:
                if _status_value(job.status) != "ready":
                    coverage_gap = True
                    continue
                rows = (
                    await session.execute(
                        select(KbChunk, KbChunkEmbedding.content_hash)
                        .outerjoin(
                            KbChunkEmbedding,
                            and_(
                                KbChunkEmbedding.chunk_id == KbChunk.id,
                                KbChunkEmbedding.provider == fingerprint.provider,
                                KbChunkEmbedding.model == fingerprint.model,
                                KbChunkEmbedding.dim == fingerprint.dim,
                            ),
                        )
                        .where(
                            KbChunk.org_id == org_id,
                            KbChunk.kb_id == job.kb_id,
                            KbChunk.quarantined.is_(False),
                        )
                        .order_by(KbChunk.id)
                        .with_for_update(of=KbChunk)
                    )
                ).all()
                matching = sum(
                    stored_hash
                    == embedding_content_hash(
                        chunk_embedding_text(
                            chunk.content,
                            chunk.heading_path,
                            chunk_context_text(chunk.meta),
                        )
                    )
                    for chunk, stored_hash in rows
                )
                job.total_chunks = len(rows)
                job.embedded_chunks = matching
                if matching != len(rows):
                    job.status = "queued"
                    job.finished_at = None
                    job.error = "content changed before embedding cutover"
                    coverage_gap = True
                session.add(job)
            await session.commit()

    if {job.kb_id for job in jobs} != expected_kb_ids:
        coverage_gap = True
    ready_jobs = sum(_status_value(job.status) == "ready" for job in jobs)
    failed_jobs = sum(_status_value(job.status) == "failed" for job in jobs)
    return not coverage_gap, len(jobs), ready_jobs, failed_jobs


async def finalize_due_embedding_campaigns(
    session_factory: SessionFactory, *, now: datetime | None = None
) -> list[UUID]:
    """Revalidate locked current content, then atomically switch active config."""
    effective_now = now or datetime.now(UTC)
    finalized: list[UUID] = []
    async with session_factory() as session:
        campaigns = (
            (
                await session.execute(
                    select(EmbeddingMigrationCampaign)
                    .where(
                        EmbeddingMigrationCampaign.status == "dual_read",
                        EmbeddingMigrationCampaign.dual_read_until <= effective_now,
                    )
                    .order_by(EmbeddingMigrationCampaign.dual_read_until)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not campaigns:
            return finalized
        await session.execute(text("LOCK TABLE kb_chunks IN SHARE MODE"))
        config_row = (
            await session.execute(
                select(ServiceConfig).where(ServiceConfig.name == "embedding").with_for_update()
            )
        ).scalar_one_or_none()
        for campaign in campaigns:
            valid, total_jobs, ready_jobs, failed_jobs = await _validate_campaign_for_cutover(
                campaign, session_factory
            )
            campaign.total_jobs = total_jobs
            campaign.ready_jobs = ready_jobs
            campaign.failed_jobs = failed_jobs
            active_fingerprint = EmbeddingFingerprint.from_config(
                _embedding_config_from_payload(
                    config_row.payload if config_row is not None else None
                )
            ).as_dict()
            if active_fingerprint != campaign.source_fingerprint:
                campaign.status = "failed"
                campaign.error = (
                    "active embedding fingerprint changed before cutover; "
                    "create a new campaign"
                )
                campaign.updated_at = effective_now
                session.add(campaign)
                continue
            if not valid:
                campaign.status = "running"
                campaign.dual_read_until = None
                campaign.error = "embedding coverage changed before cutover"
                campaign.updated_at = effective_now
                session.add(campaign)
                continue
            target_payload = {
                key: value
                for key, value in (config_row.payload if config_row is not None else {}).items()
                if key not in {"provider", "model", "dim", "api_key", "base_url"}
            }
            target_payload.update(
                EmbeddingFingerprint.from_config(campaign.target_config).as_dict()
            )
            if config_row is None:
                config_row = ServiceConfig(name="embedding", payload=target_payload)
                session.add(config_row)
            else:
                config_row.payload = target_payload
                config_row.updated_at = effective_now
                session.add(config_row)
            campaign.status = "completed"
            campaign.updated_at = effective_now
            session.add(campaign)
            finalized.append(campaign.id)
        await session.commit()
    return finalized


async def get_embedding_campaign_status(
    campaign_id: UUID, session_factory: SessionFactory
) -> EmbeddingCampaignStatus:
    async with session_factory() as session:
        campaign = await session.get(EmbeddingMigrationCampaign, campaign_id)
        if campaign is None:
            raise LookupError(f"embedding campaign not found: {campaign_id}")
        counts = await _campaign_job_counts(campaign_id, session_factory)
        total_chunks = counts[3]
        embedded_chunks = counts[4]
        coverage = embedded_chunks / total_chunks if total_chunks else 1.0
        return EmbeddingCampaignStatus(
            campaign_id=campaign.id,
            status=_status_value(campaign.status),
            total_jobs=campaign.total_jobs,
            ready_jobs=campaign.ready_jobs,
            failed_jobs=campaign.failed_jobs,
            total_chunks=total_chunks,
            embedded_chunks=embedded_chunks,
            coverage=round(coverage, 6),
            dual_read_until=campaign.dual_read_until,
            error=campaign.error,
        )
