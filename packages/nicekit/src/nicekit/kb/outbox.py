"""Tenant-scoped, at-least-once consumer for knowledge outbox events.

Handlers receive the same transaction that publishes the event. They must not
commit or roll back it; raising leaves both handler side effects and event state
uncommitted so the event can be retried.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb.metrics import KB_OUTBOX_CLAIMED, KB_OUTBOX_EVENTS, KB_OUTBOX_ROUND_CLAIMED
from nicekit.models.kb import (
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    KbDocumentOperation,
    KnowledgeBaseLifecycleOperation,
    KnowledgeBaseLifecycleOperationStatus,
    OutboxEvent,
    OutboxStatus,
    SourceDocument,
)
from nicekit.models.tenancy import Organization

logger = logging.getLogger(__name__)

OutboxHandler = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: UUID
    org_id: UUID
    claim_token: str


@dataclass(slots=True)
class ConsumeResult:
    recovered: int = 0
    claimed: int = 0
    published: int = 0
    retried: int = 0
    dead_lettered: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "recovered": self.recovered,
            "claimed": self.claimed,
            "published": self.published,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
        }


_handlers: dict[str, list[OutboxHandler]] = {}


def _operation_id(event: OutboxEvent) -> UUID | None:
    raw = event.payload.get("operation_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _document_id(event: OutboxEvent) -> UUID | None:
    raw = event.payload.get("document_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


async def _sync_operation_failure(
    session: AsyncSession,
    event: OutboxEvent,
    *,
    dead_lettered: bool,
    now: datetime,
) -> None:
    if event.event_type == "knowledge_base.purge.requested":
        operation_id = _operation_id(event)
        if (
            operation_id is None
            or event.aggregate_type != "knowledge_base"
            or event.aggregate_id != event.kb_id
        ):
            return
        operation = await session.scalar(
            select(KnowledgeBaseLifecycleOperation)
            .where(
                KnowledgeBaseLifecycleOperation.id == operation_id,
                KnowledgeBaseLifecycleOperation.org_id == event.org_id,
                KnowledgeBaseLifecycleOperation.kb_id == event.kb_id,
            )
            .with_for_update()
        )
        if (
            operation is None
            or operation.status
            == KnowledgeBaseLifecycleOperationStatus.COMPLETED.value
        ):
            return
        operation.status = (
            KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER
            if dead_lettered
            else KnowledgeBaseLifecycleOperationStatus.PENDING
        )
        operation.last_error_code = (
            "purge_dispatch_dead_letter"
            if dead_lettered
            else "purge_dispatch_retry_scheduled"
        )
        operation.last_error_message = (
            "永久清理事件进入死信"
            if dead_lettered
            else "永久清理事件等待重试"
        )
        operation.failed_at = now if dead_lettered else None
        session.add(operation)
        return
    if event.event_type not in {
        "document.reingestion.staged",
        "document.tombstoned",
        "document.purge.requested",
    }:
        return
    operation_id = _operation_id(event)
    document_id = _document_id(event)
    if (
        operation_id is None
        or document_id is None
        or event.aggregate_type != "source_document"
        or event.aggregate_id != document_id
    ):
        return
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
        return
    operation = await session.scalar(
        select(KbDocumentOperation)
        .where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == event.org_id,
            KbDocumentOperation.kb_id == event.kb_id,
            KbDocumentOperation.document_id == document_id,
        )
        .with_for_update()
    )
    if operation is None or operation.status == DocumentOperationStatus.COMPLETED.value:
        return
    operation_type = str(getattr(operation.operation_type, "value", operation.operation_type))
    if operation_type == DocumentOperationType.PURGE.value:
        operation.status = (
            DocumentOperationStatus.FAILED if dead_lettered else DocumentOperationStatus.PENDING
        )
        operation.stage = "dispatch_failed" if dead_lettered else "dispatch_retry_scheduled"
        operation.retryable = dead_lettered
        operation.last_error_code = "purge_dispatch_failed"
        operation.last_error = "permanent purge dispatch failed"
        operation.failed_at = now if dead_lettered else None
        if dead_lettered:
            document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWN
            session.add(document)
    elif operation_type == DocumentOperationType.REINGESTION.value:
        operation.status = (
            DocumentOperationStatus.DEAD_LETTER
            if dead_lettered
            else DocumentOperationStatus.PENDING
        )
        operation.stage = "dead_letter" if dead_lettered else "retry_scheduled"
        operation.attempts = event.attempts
        operation.retryable = True
        operation.last_error_code = "reingestion_snapshot_failed"
        operation.last_error = "reingestion snapshot publication failed"
        operation.failed_at = now if dead_lettered else None
        if dead_lettered:
            document.lifecycle_status = DocumentLifecycleStatus.WITHDRAWN
            session.add(document)
    else:
        operation.status = (
            DocumentOperationStatus.DEAD_LETTER
            if dead_lettered
            else DocumentOperationStatus.PENDING
        )
        operation.stage = "dead_letter" if dead_lettered else "retry_scheduled"
        operation.attempts = event.attempts
        operation.retryable = True
        operation.last_error_code = "withdrawal_snapshot_failed"
        operation.last_error = "withdrawal snapshot publication failed"
        operation.failed_at = now if dead_lettered else None
    session.add(operation)


def register_outbox_handler(event_type: str, handler: OutboxHandler) -> None:
    """Subscribe a handler; subscribers run sequentially in the publish transaction."""
    normalized = event_type.strip()
    if not normalized:
        raise ValueError("event_type must not be blank")
    subscribers = _handlers.setdefault(normalized, [])
    if handler in subscribers:
        raise ValueError(f"outbox handler already registered: {normalized}")
    subscribers.append(handler)


async def _audit_snapshot_event(session: AsyncSession, event: OutboxEvent) -> None:
    """Audit-only hook until concrete snapshot projections register handlers."""
    del session
    logger.info(
        "知识快照 outbox 事件已消费",
        extra={
            "event_id": str(event.id),
            "event_type": event.event_type,
            "org_id": str(event.org_id),
            "kb_id": str(event.kb_id),
            "aggregate_id": str(event.aggregate_id),
        },
    )


for _snapshot_event_type in (
    "knowledge_snapshot.ready",
    "knowledge_snapshot.activated",
    "knowledge_snapshot.rolled_back",
):
    register_outbox_handler(_snapshot_event_type, _audit_snapshot_event)

# Projection GC is part of the transition publish transaction and is therefore
# retried atomically with the outbox event.
from nicekit.kb.projection_gc import gc_retired_snapshot_projections  # noqa: E402

for _snapshot_gc_event_type in (
    "knowledge_snapshot.activated",
    "knowledge_snapshot.rolled_back",
):
    register_outbox_handler(_snapshot_gc_event_type, gc_retired_snapshot_projections)

from nicekit.kb.document_lifecycle import (  # noqa: E402
    rebuild_after_document_tombstone,
)

register_outbox_handler("document.tombstoned", rebuild_after_document_tombstone)

from nicekit.kb.document_reingestion import (  # noqa: E402
    publish_reingested_document,
)

register_outbox_handler("document.reingestion.staged", publish_reingested_document)

from nicekit.kb.document_purge import execute_document_purge  # noqa: E402


async def _execute_document_purge(
    session: AsyncSession,
    event: OutboxEvent,
) -> None:
    if event.event_type != "document.purge.requested":
        raise ValueError("unsupported document purge event")
    operation_id = _operation_id(event)
    document_id = _document_id(event)
    if (
        operation_id is None
        or document_id is None
        or event.aggregate_type != "source_document"
        or event.aggregate_id != document_id
    ):
        raise ValueError("document purge event has invalid aggregate identity")
    operation = await session.scalar(
        select(KbDocumentOperation).where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == event.org_id,
            KbDocumentOperation.kb_id == event.kb_id,
            KbDocumentOperation.document_id == document_id,
            KbDocumentOperation.operation_type == DocumentOperationType.PURGE.value,
        )
    )
    if operation is None:
        raise ValueError("document purge event does not match its operation")
    await execute_document_purge(
        session,
        org_id=event.org_id,
        operation_id=operation_id,
    )


register_outbox_handler("document.purge.requested", _execute_document_purge)

from nicekit.kb.lifecycle import (  # noqa: E402
    ARCHIVED_EVENT_TYPE,
    PURGE_EVENT_TYPE,
    RESTORED_EVENT_TYPE,
    execute_knowledge_base_purge,
)


async def _audit_lifecycle_event(
    session: AsyncSession,
    event: OutboxEvent,
) -> None:
    """Audit-only lifecycle invalidation subscriber."""

    del session
    logger.info(
        "知识库生命周期 outbox 事件已消费",
        extra={
            "event_id": str(event.id),
            "event_type": event.event_type,
            "org_id": str(event.org_id),
            "kb_id": str(event.kb_id),
            "aggregate_id": str(event.aggregate_id),
        },
    )


register_outbox_handler(PURGE_EVENT_TYPE, execute_knowledge_base_purge)
register_outbox_handler(ARCHIVED_EVENT_TYPE, _audit_lifecycle_event)
register_outbox_handler(RESTORED_EVENT_TYPE, _audit_lifecycle_event)


async def _list_org_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[UUID]:
    # organizations is the identity table and deliberately has no RLS policy.
    async with session_factory() as session:
        result = await session.execute(select(Organization.id).order_by(Organization.id))
        return list(result.scalars().all())


async def _recover_expired_leases(
    session: AsyncSession,
    *,
    org_id: UUID,
    now: datetime,
    max_attempts: int,
) -> tuple[int, int]:
    expired_filter = [
        OutboxEvent.org_id == org_id,
        OutboxEvent.status == OutboxStatus.PROCESSING,
        OutboxEvent.claim_expires_at <= now,
    ]
    if not get_settings().kb_lifecycle_purge_worker_enabled:
        expired_filter.append(
            OutboxEvent.event_type != "knowledge_base.purge.requested"
        )
    expired = (
        (
            await session.execute(
                select(OutboxEvent)
                .where(*expired_filter)
                .order_by(OutboxEvent.claim_expires_at, OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    dead_lettered = 0
    for event in expired:
        event.claimed_by = None
        event.claimed_at = None
        event.claim_expires_at = None
        event.last_error = "outbox processing lease expired before publish"
        if event.attempts >= max_attempts:
            event.status = OutboxStatus.DEAD_LETTER
            dead_lettered += 1
            await _sync_operation_failure(
                session,
                event,
                dead_lettered=True,
                now=now,
            )
        else:
            event.status = OutboxStatus.PENDING
            event.available_at = now
            await _sync_operation_failure(
                session,
                event,
                dead_lettered=False,
                now=now,
            )
        session.add(event)
    if expired:
        await session.commit()
    return len(expired), dead_lettered


async def _claim_pending(
    session: AsyncSession,
    *,
    org_id: UUID,
    now: datetime,
    batch_size: int,
    lease_seconds: int,
    consumer_id: str,
) -> list[ClaimedEvent]:
    pending_filter = [
        OutboxEvent.org_id == org_id,
        OutboxEvent.status == OutboxStatus.PENDING,
        OutboxEvent.available_at <= now,
    ]
    if not get_settings().kb_lifecycle_purge_worker_enabled:
        pending_filter.append(
            OutboxEvent.event_type != "knowledge_base.purge.requested"
        )
    events = (
        (
            await session.execute(
                select(OutboxEvent)
                .where(*pending_filter)
                .order_by(
                    OutboxEvent.available_at,
                    OutboxEvent.created_at,
                    OutboxEvent.id,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claimed: list[ClaimedEvent] = []
    for event in events:
        claim_token = f"{consumer_id[:120]}:{uuid4().hex}"
        event.status = OutboxStatus.PROCESSING
        event.attempts += 1
        event.claimed_by = claim_token
        event.claimed_at = now
        event.claim_expires_at = now + timedelta(seconds=lease_seconds)
        event.last_error = None
        session.add(event)
        claimed.append(ClaimedEvent(event.id, org_id, claim_token))
    if claimed:
        await session.commit()
    return claimed


async def _mark_failed_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    *,
    error: str,
    max_attempts: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
) -> bool | None:
    """Return True for dead-letter, False for retry, None for a stale claim."""
    async with org_session(session_factory, claimed.org_id) as session:
        event = (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.id == claimed.event_id,
                    OutboxEvent.org_id == claimed.org_id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.claimed_by == claimed.claim_token,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if event is None:
            await session.rollback()
            return None

        event.claimed_by = None
        event.claimed_at = None
        event.claim_expires_at = None
        event.last_error = error
        now = datetime.now(UTC)
        if event.attempts >= max_attempts:
            event.status = OutboxStatus.DEAD_LETTER
            dead_lettered = True
        else:
            delay = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** max(0, event.attempts - 1)),
            )
            event.status = OutboxStatus.PENDING
            event.available_at = now + timedelta(seconds=delay)
            dead_lettered = False
        await _sync_operation_failure(
            session,
            event,
            dead_lettered=dead_lettered,
            now=now,
        )
        session.add(event)
        await session.commit()
        return dead_lettered


async def _dead_letter_unknown(
    session: AsyncSession,
    event: OutboxEvent,
    claimed: ClaimedEvent,
) -> bool:
    now = datetime.now(UTC)
    result = await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == claimed.event_id,
            OutboxEvent.org_id == claimed.org_id,
            OutboxEvent.status == OutboxStatus.PROCESSING,
            OutboxEvent.claimed_by == claimed.claim_token,
        )
        .values(
            status=OutboxStatus.DEAD_LETTER,
            claimed_by=None,
            claimed_at=None,
            claim_expires_at=None,
            last_error=f"no outbox handler registered for {event.event_type!r}",
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    await _sync_operation_failure(
        session,
        event,
        dead_lettered=True,
        now=now,
    )
    await session.commit()
    return True


async def _process_claimed_event(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedEvent,
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
) -> str:
    session = org_session(session_factory, claimed.org_id)
    try:
        event = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.id == claimed.event_id,
                    OutboxEvent.org_id == claimed.org_id,
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.claimed_by == claimed.claim_token,
                )
            )
        ).scalar_one_or_none()
        if event is None:
            await session.rollback()
            return "stale"

        handlers = _handlers.get(event.event_type)
        if not handlers:
            if await _dead_letter_unknown(session, event, claimed):
                logger.error(
                    "未知 outbox 事件已进入 dead letter",
                    extra={"event_id": str(event.id), "event_type": event.event_type},
                )
                return "dead_lettered"
            return "stale"

        for handler in handlers:
            await handler(session, event)
        result = await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == claimed.event_id,
                OutboxEvent.org_id == claimed.org_id,
                OutboxEvent.status == OutboxStatus.PROCESSING,
                OutboxEvent.claimed_by == claimed.claim_token,
            )
            .values(
                status=OutboxStatus.PUBLISHED,
                claimed_by=None,
                claimed_at=None,
                claim_expires_at=None,
                published_at=datetime.now(UTC),
                last_error=None,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            return "stale"
        await session.commit()
        return "published"
    except Exception as exc:
        await session.rollback()
        error = str(exc).strip() or type(exc).__name__
        dead_lettered = await _mark_failed_attempt(
            session_factory,
            claimed,
            error=error,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
        )
        logger.exception(
            "outbox handler 执行失败",
            extra={"event_id": str(claimed.event_id), "dead_lettered": dead_lettered},
        )
        if dead_lettered is None:
            return "stale"
        return "dead_lettered" if dead_lettered else "retried"
    finally:
        await session.close()


async def consume_outbox_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    consumer_id: str | None = None,
    org_ids: Sequence[UUID] | None = None,
) -> ConsumeResult:
    """Recover, claim, and consume one batch per selected organization."""
    settings = get_settings()
    worker_id = consumer_id or f"{socket.gethostname()}:{os.getpid()}"
    result = ConsumeResult()
    selected_org_ids = (
        list(dict.fromkeys(org_ids))
        if org_ids is not None
        else await _list_org_ids(session_factory)
    )

    for org_id in selected_org_ids:
        now = datetime.now(UTC)
        async with org_session(session_factory, org_id) as session:
            recovered, recovered_dead = await _recover_expired_leases(
                session,
                org_id=org_id,
                now=now,
                max_attempts=settings.kb_outbox_max_attempts,
            )
            result.recovered += recovered
            result.dead_lettered += recovered_dead
            if recovered:
                KB_OUTBOX_EVENTS.labels(outcome="recovered").inc(recovered)
            if recovered_dead:
                KB_OUTBOX_EVENTS.labels(outcome="dead_lettered").inc(recovered_dead)

        remaining = max(1, settings.kb_outbox_batch_size)
        concurrency = max(1, settings.kb_outbox_concurrency)
        while remaining > 0:
            claim_limit = min(remaining, concurrency)
            async with org_session(session_factory, org_id) as session:
                claimed_events = await _claim_pending(
                    session,
                    org_id=org_id,
                    now=datetime.now(UTC),
                    batch_size=claim_limit,
                    lease_seconds=settings.kb_outbox_lease_seconds,
                    consumer_id=worker_id,
                )
            if not claimed_events:
                break
            result.claimed += len(claimed_events)
            remaining -= len(claimed_events)
            KB_OUTBOX_CLAIMED.inc(len(claimed_events))

            outcomes = await asyncio.gather(
                *(
                    _process_claimed_event(
                        session_factory,
                        claimed,
                        max_attempts=settings.kb_outbox_max_attempts,
                        retry_base_seconds=settings.kb_outbox_retry_base_seconds,
                        retry_max_seconds=settings.kb_outbox_retry_max_seconds,
                    )
                    for claimed in claimed_events
                )
            )
            for outcome in outcomes:
                if outcome == "published":
                    result.published += 1
                elif outcome == "retried":
                    result.retried += 1
                elif outcome == "dead_lettered":
                    result.dead_lettered += 1
                if outcome in ("published", "retried", "dead_lettered"):
                    KB_OUTBOX_EVENTS.labels(outcome=outcome).inc()
            if len(claimed_events) < claim_limit:
                break
    KB_OUTBOX_ROUND_CLAIMED.set(result.claimed)
    return result


async def run_outbox_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Run the inline consumer immediately, then poll until shutdown."""
    interval = get_settings().kb_outbox_poll_interval_seconds
    while not stop_event.is_set():
        try:
            result = await consume_outbox_once(session_factory)
            if result.claimed or result.recovered:
                logger.info("outbox 消费轮次完成", extra=result.as_dict())
        except Exception:
            logger.exception("outbox 消费轮次失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
