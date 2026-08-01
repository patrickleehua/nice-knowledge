"""Lease lifecycle for idempotent knowledge ingestion runs.

All mutation helpers participate in the caller's transaction. Long-running
heartbeats use a dedicated tenant-scoped session through
``maintain_ingest_lease`` so parsing and LLM calls never hold a DB transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.models.kb import (
    DocStatus,
    DocumentRevision,
    IngestRun,
    IngestRunStatus,
    SourceDocument,
)

INGEST_LEASE_EXPIRED_ERROR = "ingest run lease expired before completion"
LEGACY_INGEST_STALE_ERROR = "任务超时未完成,已自动标记失败,请重试"
_RUN_KEY_CONSTRAINT = "uq_ingest_run_stage_segment"
_COMPLETION_STATUSES = frozenset(
    (IngestRunStatus.STAGED, IngestRunStatus.SUCCEEDED)
)


class IngestLeaseLostError(RuntimeError):
    """The worker no longer owns a live lease and must not publish results."""


@dataclass(frozen=True, slots=True)
class IngestRunClaim:
    run_id: UUID
    status: IngestRunStatus
    acquired: bool
    lease_owner: str | None


@dataclass(frozen=True, slots=True)
class IngestRecoveryResult:
    expired_runs: int = 0
    stale_documents: int = 0


async def list_recoverable_ingest_runs(
    session: AsyncSession,
    *,
    org_id: UUID,
    cutoff: datetime,
    limit: int = 100,
) -> list[UUID]:
    """List persisted root intents that were never claimed after dispatch."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    return list(
        (
            await session.execute(
                select(IngestRun.id)
                .where(
                    IngestRun.org_id == org_id,
                    IngestRun.stage == "document",
                    IngestRun.segment_no == 0,
                    IngestRun.status == IngestRunStatus.QUEUED.value,
                    or_(
                        and_(
                            IngestRun.available_at.is_(None),
                            IngestRun.created_at < cutoff,
                        ),
                        and_(
                            IngestRun.available_at.is_not(None),
                            IngestRun.available_at <= func.now(),
                        ),
                    ),
                )
                .order_by(IngestRun.created_at, IngestRun.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )


async def list_recoverable_typed_reextraction_runs(
    session: AsyncSession,
    *,
    org_id: UUID,
    cutoff: datetime,
    limit: int = 100,
) -> list[UUID]:
    """List persisted typed re-extraction roots that were never claimed."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    return list(
        (
            await session.execute(
                select(IngestRun.id)
                .where(
                    IngestRun.org_id == org_id,
                    IngestRun.stage.like("reextract:%"),
                    IngestRun.segment_no == 0,
                    IngestRun.status == IngestRunStatus.QUEUED.value,
                    or_(
                        and_(
                            IngestRun.available_at.is_(None),
                            IngestRun.created_at < cutoff,
                        ),
                        and_(
                            IngestRun.available_at.is_not(None),
                            IngestRun.available_at <= func.now(),
                        ),
                    ),
                )
                .order_by(IngestRun.created_at, IngestRun.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )


def ingest_run_idempotency_key(revision_id: UUID, stage: str, segment_no: int) -> str:
    normalized_stage = stage.strip()
    if not normalized_stage:
        raise ValueError("stage must not be blank")
    if segment_no < 0:
        raise ValueError("segment_no must be non-negative")
    return f"{revision_id}:{normalized_stage}:{segment_no}"


def _lease_duration(lease_seconds: int | None) -> timedelta:
    seconds = (
        lease_seconds
        if lease_seconds is not None
        else get_settings().kb_ingest_lease_seconds
    )
    if seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    return timedelta(seconds=seconds)


def _as_status(value: IngestRunStatus | str) -> IngestRunStatus:
    return value if isinstance(value, IngestRunStatus) else IngestRunStatus(value)


async def _database_now(session: AsyncSession, explicit: datetime | None) -> datetime:
    if explicit is not None:
        return explicit
    current = await session.scalar(select(func.clock_timestamp()))
    if current is None:  # pragma: no cover - PostgreSQL clock_timestamp is never null
        raise RuntimeError("database clock returned null")
    return current


async def claim_ingest_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_id: UUID,
    stage: str,
    segment_no: int,
    lease_owner: str,
    idempotency_key: str | None = None,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> IngestRunClaim:
    """Atomically create or claim one run; a live owner and terminal runs win."""
    normalized_owner = lease_owner.strip()
    if not normalized_owner:
        raise ValueError("lease_owner must not be blank")
    revision_matches_tenant = await session.scalar(
        select(DocumentRevision.id).where(
            DocumentRevision.id == revision_id,
            DocumentRevision.org_id == org_id,
            DocumentRevision.kb_id == kb_id,
        )
    )
    if revision_matches_tenant is None:
        raise ValueError("revision does not belong to the requested org and knowledge base")
    key = (
        ingest_run_idempotency_key(revision_id, stage, segment_no)
        if idempotency_key is None
        else idempotency_key.strip()
    )
    if not key:
        raise ValueError("idempotency_key must not be blank")
    if len(key) > 200:
        raise ValueError("idempotency_key exceeds 200 characters")
    claimed_at = await _database_now(session, now)
    lease_expires_at = claimed_at + _lease_duration(lease_seconds)

    stmt = (
        pg_insert(IngestRun)
        .values(
            org_id=org_id,
            kb_id=kb_id,
            revision_id=revision_id,
            stage=stage.strip(),
            segment_no=segment_no,
            status=IngestRunStatus.RUNNING.value,
            idempotency_key=key,
            lease_owner=normalized_owner,
            lease_expires_at=lease_expires_at,
            heartbeat_at=claimed_at,
            started_at=claimed_at,
        )
        .on_conflict_do_update(
            constraint=_RUN_KEY_CONSTRAINT,
            set_={
                "status": IngestRunStatus.RUNNING.value,
                "idempotency_key": key,
                "lease_owner": normalized_owner,
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": claimed_at,
                "available_at": None,
                "error": None,
                "finished_at": None,
                "started_at": claimed_at,
            },
            where=or_(
                and_(
                    IngestRun.status == IngestRunStatus.QUEUED.value,
                    or_(
                        IngestRun.available_at.is_(None),
                        IngestRun.available_at <= claimed_at,
                    ),
                ),
                IngestRun.status == IngestRunStatus.FAILED.value,
                and_(
                    IngestRun.status == IngestRunStatus.RUNNING.value,
                    IngestRun.lease_expires_at <= claimed_at,
                ),
            ),
        )
        .returning(IngestRun.id, IngestRun.status, IngestRun.lease_owner)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is not None:
        return IngestRunClaim(
            run_id=row.id,
            status=_as_status(row.status),
            acquired=True,
            lease_owner=row.lease_owner,
        )

    existing = (
        await session.execute(
            select(IngestRun.id, IngestRun.status, IngestRun.lease_owner).where(
                IngestRun.revision_id == revision_id,
                IngestRun.stage == stage.strip(),
                IngestRun.segment_no == segment_no,
            )
        )
    ).one()
    return IngestRunClaim(
        run_id=existing.id,
        status=_as_status(existing.status),
        acquired=False,
        lease_owner=existing.lease_owner,
    )


async def heartbeat_ingest_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    lease_owner: str,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> bool:
    heartbeat_at = await _database_now(session, now)
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.id == run_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_owner == lease_owner,
            IngestRun.lease_expires_at > heartbeat_at,
        )
        .values(
            heartbeat_at=heartbeat_at,
            lease_expires_at=heartbeat_at + _lease_duration(lease_seconds),
        )
    )
    return result.rowcount == 1


async def complete_ingest_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    lease_owner: str,
    status: IngestRunStatus = IngestRunStatus.SUCCEEDED,
    stats: dict | None = None,
    now: datetime | None = None,
) -> bool:
    if status not in _COMPLETION_STATUSES:
        raise ValueError("completion status must be staged or succeeded")
    completed_at = await _database_now(session, now)
    values: dict = {
        "status": status.value,
        "lease_owner": None,
        "lease_expires_at": None,
        "error": None,
        "finished_at": completed_at,
    }
    if stats is not None:
        values["stats"] = stats
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.id == run_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_owner == lease_owner,
            IngestRun.lease_expires_at > completed_at,
        )
        .values(**values)
    )
    return result.rowcount == 1


async def fail_ingest_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    lease_owner: str,
    error: str,
    now: datetime | None = None,
) -> bool:
    failed_at = await _database_now(session, now)
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.id == run_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_owner == lease_owner,
            IngestRun.lease_expires_at > failed_at,
        )
        .values(
            status=IngestRunStatus.FAILED.value,
            lease_owner=None,
            lease_expires_at=None,
            error=(error.strip() or "ingest run failed")[:2000],
            finished_at=failed_at,
        )
    )
    return result.rowcount == 1


async def requeue_ingest_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    lease_owner: str,
    reason: str,
    available_at: datetime | None = None,
    delay_seconds: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Return a live owned run to the durable queue without losing its intent."""
    if available_at is not None and delay_seconds is not None:
        raise ValueError("available_at and delay_seconds are mutually exclusive")
    if delay_seconds is not None and delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    requeued_at = await _database_now(session, now)
    next_available_at = (
        requeued_at + timedelta(seconds=delay_seconds)
        if delay_seconds is not None
        else available_at
    )
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.id == run_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_owner == lease_owner,
            IngestRun.lease_expires_at > requeued_at,
        )
        .values(
            status=IngestRunStatus.QUEUED.value,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=next_available_at,
            error=(reason.strip() or "ingest run requeued")[:2000],
            started_at=None,
            finished_at=None,
        )
    )
    return result.rowcount == 1


async def cancel_ingest_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    lease_owner: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Fence and cancel a run only while the caller still owns its live lease."""
    canceled_at = await _database_now(session, now)
    result = await session.execute(
        update(IngestRun)
        .where(
            IngestRun.id == run_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_owner == lease_owner,
            IngestRun.lease_expires_at > canceled_at,
        )
        .values(
            status=IngestRunStatus.CANCELED.value,
            lease_owner=None,
            lease_expires_at=None,
            error=(reason.strip()[:2000] if reason and reason.strip() else None),
            finished_at=canceled_at,
        )
    )
    return result.rowcount == 1


async def _heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    run_id: UUID,
    lease_owner: str,
    stop_event: asyncio.Event,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=heartbeat_seconds)
            return
        except TimeoutError:
            pass
        async with org_session(session_factory, org_id) as session:
            renewed = await heartbeat_ingest_run(
                session,
                run_id=run_id,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
            )
            if not renewed:
                await session.rollback()
                raise IngestLeaseLostError(f"ingest run lease lost: {run_id}")
            await session.commit()


@asynccontextmanager
async def maintain_ingest_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    run_id: UUID,
    lease_owner: str,
    lease_seconds: int | None = None,
    heartbeat_seconds: int | None = None,
) -> AsyncIterator[None]:
    """Renew a lease in a dedicated session for the duration of a long stage."""
    settings = get_settings()
    lease = lease_seconds or settings.kb_ingest_lease_seconds
    heartbeat = heartbeat_seconds or settings.kb_ingest_heartbeat_seconds
    if heartbeat <= 0 or heartbeat >= lease:
        raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            org_id=org_id,
            run_id=run_id,
            lease_owner=lease_owner,
            stop_event=stop_event,
            lease_seconds=lease,
            heartbeat_seconds=heartbeat,
        )
    )
    try:
        yield
    finally:
        stop_event.set()
        await task


async def recover_stale_ingestion(
    session: AsyncSession,
    *,
    org_id: UUID,
    legacy_cutoff: datetime,
    now: datetime | None = None,
) -> IngestRecoveryResult:
    """Fence expired runs and recover old PARSING documents without a live run."""
    recovered_at = await _database_now(session, now)
    expired_rows = (
        await session.execute(
            select(IngestRun, DocumentRevision.doc_id)
            .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
            .where(
                IngestRun.org_id == org_id,
                IngestRun.status == IngestRunStatus.RUNNING.value,
                IngestRun.lease_expires_at <= recovered_at,
            )
            .order_by(IngestRun.lease_expires_at, IngestRun.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    expired_doc_ids: set[UUID] = set()
    for run, doc_id in expired_rows:
        run.status = IngestRunStatus.FAILED
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        run.error = INGEST_LEASE_EXPIRED_ERROR
        run.finished_at = recovered_at
        session.add(run)
        expired_doc_ids.add(doc_id)
    if expired_rows:
        await session.flush()

    stale_documents = 0
    other_live_run = exists(
        select(IngestRun.id)
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .where(
            DocumentRevision.doc_id == SourceDocument.id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
            IngestRun.lease_expires_at > recovered_at,
        )
    )
    if expired_doc_ids:
        result = await session.execute(
            update(SourceDocument)
            .where(
                SourceDocument.id.in_(expired_doc_ids),
                SourceDocument.status == DocStatus.PARSING.value,
                ~other_live_run,
            )
            .values(status=DocStatus.FAILED.value, error=LEGACY_INGEST_STALE_ERROR)
        )
        stale_documents += result.rowcount

    legacy = await session.execute(
        update(SourceDocument)
        .where(
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.PARSING.value,
            SourceDocument.parsing_started_at < legacy_cutoff,
            ~other_live_run,
        )
        .values(status=DocStatus.FAILED.value, error=LEGACY_INGEST_STALE_ERROR)
    )
    stale_documents += legacy.rowcount
    return IngestRecoveryResult(
        expired_runs=len(expired_rows),
        stale_documents=stale_documents,
    )
