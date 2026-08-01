"""Fail-closed knowledge-base archive, deletion preview, and durable purge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Table, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from nicekit.core.config import get_settings
from nicekit.kb import ports, storage
from nicekit.kb.projection_gc import _externally_referenced_snapshot_ids
from nicekit.kb.reference_registry import (
    ReferenceTargets,
    business_reference_counts,
    feedback_reference_count,
)
from nicekit.models.kb import (
    CanonicalEntity,
    EmbeddingReindexJob,
    FactClaim,
    IngestRun,
    KbDocumentOperation,
    KbShare,
    KnowledgeBase,
    KnowledgeBaseLifecycleOperation,
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecyclePhase,
    KnowledgeBaseLifecycleStatus,
    KnowledgeBasePurgeObject,
    KnowledgeBasePurgeObjectStatus,
    KnowledgeSnapshot,
    OutboxEvent,
    OutboxStatus,
    SnapshotStatus,
    SourceDocument,
)
from nicekit.models.tenancy import AuditLog

logger = logging.getLogger(__name__)

PLAN_VERSION = "kb-deletion-v1"
PLAN_TTL = timedelta(minutes=15)
PURGE_EVENT_TYPE = "knowledge_base.purge.requested"
ARCHIVED_EVENT_TYPE = "knowledge_base.archived"
RESTORED_EVENT_TYPE = "knowledge_base.restored"
_PRESERVED_KB_TABLES = frozenset(
    {
        "knowledge_base_lifecycle_operations",
        "knowledge_base_purge_objects",
        "outbox_events",
    }
)
_OBJECT_KEY_COLUMNS = frozenset(
    {
        "object_key",
        "markdown_key",
        "structured_json_key",
        "original_object_key",
        "thumbnail_object_key",
    }
)
_ACTIVE_INGEST_STATUSES = frozenset({"queued", "running"})
_ACTIVE_DOCUMENT_OPERATION_STATUSES = frozenset({"pending", "processing"})
_ACTIVE_REINDEX_STATUSES = frozenset({"queued", "running"})
# 预检 stat 对象身份的并发上限:见 _object_identity_inventory 的耗时说明
_OBJECT_STAT_CONCURRENCY = 64
_RETRYABLE_OPERATION_ERROR_CODES = frozenset(
    {
        "metadata_cleanup_failed",
        "metadata_residuals",
        "object_delete_failed",
        "purge_dispatch_dead_letter",
    }
)

DeleteObject = Callable[
    [str, str | None, str | None, bool],
    Awaitable[None],
]


class KnowledgeBaseLifecycleError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        blockers: Iterable[dict[str, Any]] = (),
        preview: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.blockers = list(blockers)
        self.preview = preview


@dataclass(frozen=True, slots=True)
class _Inventory:
    table_ids: dict[str, list[str]]
    #: 宿主业务对象对本 KB 本身的引用数(ReferenceScanner kind="knowledge_base")
    external_reference_count: int
    object_keys: list[str]
    object_identities: dict[str, storage.ObjectIdentity]
    shared_object_keys: list[str]
    legal_hold_document_ids: list[UUID]
    manual_fact_ids: list[UUID]
    pinned_entity_ids: list[UUID]
    protected_snapshot_ids: list[UUID]
    business_reference_count: int
    feedback_reference_count: int
    active_job_counts: dict[str, int]


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _execution_hash(payload: dict[str, Any]) -> str:
    """Hash references and object keys while per-object identity stays in the ledger."""

    execution_payload = {
        **payload,
        "objects": [item["key"] for item in payload["objects"]],
    }
    return _canonical_hash(execution_payload)


def _plan_expires_at(now: datetime) -> datetime:
    window_seconds = int(PLAN_TTL.total_seconds())
    window_end = ((int(now.timestamp()) // window_seconds) + 1) * window_seconds
    return datetime.fromtimestamp(window_end, tz=UTC)


def _kb_child_tables() -> list[Table]:
    tables: list[Table] = []
    for table in SQLModel.metadata.sorted_tables:
        kb_column = table.c.get("kb_id")
        if kb_column is None:
            continue
        if any(
            foreign_key.column.table.name == "knowledge_bases"
            and foreign_key.column.name == "id"
            for foreign_key in kb_column.foreign_keys
        ):
            tables.append(table)
    return sorted(tables, key=lambda table: table.name)


def registered_kb_reference_tables() -> tuple[str, ...]:
    """Expose the inventory registry for migration and coverage tests."""

    return tuple(table.name for table in _kb_child_tables())


def _identity_columns(table: Table) -> list[Any]:
    columns = list(table.primary_key.columns)
    if columns:
        return columns
    if "id" in table.c:
        return [table.c.id]
    return [table.c.kb_id]


def _serialize_identity(values: tuple[object, ...]) -> str:
    return "|".join(str(value) for value in values)


async def _table_identities(
    session: AsyncSession,
    *,
    table: Table,
    org_id: UUID,
    kb_id: UUID,
) -> list[str]:
    columns = _identity_columns(table)
    conditions = [table.c.kb_id == kb_id]
    if "org_id" in table.c:
        conditions.append(table.c.org_id == org_id)
    rows = (await session.execute(select(*columns).where(*conditions))).all()
    return sorted(_serialize_identity(tuple(row)) for row in rows)


async def _external_kb_references(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
) -> int:
    """宿主业务对本知识库自身的引用数(会话作用域、检索审计、派生资产……)。

    TF 在这里直接查 projects / retrieval_snapshots / route_assets 三张业务表;
    SDK 改走 ReferenceScanner 协议(MIGRATION-PLAN §4):宿主注册实现才会有
    引用,SDK 自身不认识任何业务对象。解除关联必须由宿主完成——框架只负责
    如实报数并在未确认时挡住归档。
    """
    counts = await ports.scan_references(
        session, org_id=org_id, kind="knowledge_base", ids=[kb_id]
    )
    return int(counts.get(kb_id, 0))


async def _object_inventory(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    tables: Iterable[Table],
) -> tuple[list[str], list[str]]:
    sources: list[tuple[Table, Any]] = []
    target_keys: set[str] = set()
    for table in tables:
        for column_name in _OBJECT_KEY_COLUMNS:
            column = table.c.get(column_name)
            if column is None:
                continue
            sources.append((table, column))
            conditions = [table.c.kb_id == kb_id, column.is_not(None)]
            if "org_id" in table.c:
                conditions.append(table.c.org_id == org_id)
            values = (
                await session.execute(select(column).where(*conditions).distinct())
            ).scalars()
            target_keys.update(
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            )
    if not target_keys:
        return [], []

    shared: set[str] = set()
    for table, column in sources:
        conditions = [
            table.c.kb_id != kb_id,
            column.in_(target_keys),
        ]
        if "org_id" in table.c:
            conditions.append(table.c.org_id == org_id)
        shared.update(
            value
            for value in (
                await session.execute(select(column).where(*conditions).distinct())
            ).scalars()
            if isinstance(value, str)
        )
    return sorted(target_keys), sorted(shared)


async def _object_identity_inventory(
    object_keys: Iterable[str],
) -> dict[str, storage.ObjectIdentity]:
    """并发取对象身份(exists/version/etag)。

    每个 key 一次 HEAD 往返,预检耗时基本等于 `ceil(对象数 / 并发上限)` 个 RTT——
    对象存储在公网时单次 RTT 可达数百毫秒(实测 ~625ms),并发上限过低会让常见的
    十几个对象白跑两批。HEAD 很轻,提高上限只增加连接数不增加显著负载。
    """
    semaphore = asyncio.Semaphore(_OBJECT_STAT_CONCURRENCY)

    async def inspect(key: str) -> tuple[str, storage.ObjectIdentity]:
        async with semaphore:
            return key, await storage.stat_object_identity(key)

    return dict(
        await asyncio.gather(*(inspect(key) for key in sorted(object_keys)))
    )


async def _active_job_counts(
    session: AsyncSession, *, org_id: UUID, kb_id: UUID
) -> dict[str, int]:
    async def count(model: Any, statuses: frozenset[str]) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.org_id == org_id,
                    model.kb_id == kb_id,
                    model.status.in_(statuses),
                )
            )
            or 0
        )

    return {
        "ingest_runs": await count(IngestRun, _ACTIVE_INGEST_STATUSES),
        "document_operations": await count(
            KbDocumentOperation, _ACTIVE_DOCUMENT_OPERATION_STATUSES
        ),
        "embedding_reindex_jobs": await count(
            EmbeddingReindexJob, _ACTIVE_REINDEX_STATUSES
        ),
    }


async def _collect_inventory(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
) -> _Inventory:
    tables = _kb_child_tables()
    table_ids = {
        table.name: await _table_identities(
            session,
            table=table,
            org_id=kb.org_id,
            kb_id=kb.id,
        )
        for table in tables
    }
    external_reference_count = await _external_kb_references(
        session,
        org_id=kb.org_id,
        kb_id=kb.id,
    )
    object_keys, shared_object_keys = await _object_inventory(
        session,
        org_id=kb.org_id,
        kb_id=kb.id,
        tables=tables,
    )
    object_identities = await _object_identity_inventory(object_keys)
    legal_hold_ids = sorted(
        (
            await session.execute(
                select(SourceDocument.id).where(
                    SourceDocument.org_id == kb.org_id,
                    SourceDocument.kb_id == kb.id,
                    SourceDocument.legal_hold_at.is_not(None),
                )
            )
        )
        .scalars()
        .all(),
        key=str,
    )
    manual_fact_ids = sorted(
        (
            await session.execute(
                select(FactClaim.id).where(
                    FactClaim.org_id == kb.org_id,
                    FactClaim.kb_id == kb.id,
                    or_(
                        FactClaim.review_status == "suggested",
                        FactClaim.ingest_run_id.is_(None),
                        FactClaim.reviewed_by == "human",
                    ),
                )
            )
        )
        .scalars()
        .all(),
        key=str,
    )
    pinned_entity_ids = sorted(
        (
            await session.execute(
                select(CanonicalEntity.id).where(
                    CanonicalEntity.org_id == kb.org_id,
                    CanonicalEntity.kb_id == kb.id,
                    CanonicalEntity.is_pinned.is_(True),
                )
            )
        )
        .scalars()
        .all(),
        key=str,
    )
    targets = ReferenceTargets.from_ids(
        document_ids=table_ids.get("source_documents", []),
        revision_ids=table_ids.get("document_revisions", []),
        fact_ids=table_ids.get("fact_claims", []),
        evidence_ids=table_ids.get("evidence_spans", []),
        entity_ids=table_ids.get("canonical_entities", []),
        image_ids=table_ids.get("kb_image_assets", []),
        snapshot_ids=table_ids.get("knowledge_snapshots", []),
    )
    _, business_count, _ = await business_reference_counts(
        session,
        org_id=kb.org_id,
        targets=targets,
    )
    feedback_count = await feedback_reference_count(
        session,
        org_id=kb.org_id,
        targets=targets,
    )
    protected_snapshot_ids = sorted(
        await _externally_referenced_snapshot_ids(
            session,
            org_id=kb.org_id,
            kb_id=kb.id,
        ),
        key=str,
    )
    return _Inventory(
        table_ids=table_ids,
        external_reference_count=external_reference_count,
        object_keys=object_keys,
        object_identities=object_identities,
        shared_object_keys=shared_object_keys,
        legal_hold_document_ids=legal_hold_ids,
        manual_fact_ids=manual_fact_ids,
        pinned_entity_ids=pinned_entity_ids,
        protected_snapshot_ids=protected_snapshot_ids,
        business_reference_count=business_count,
        feedback_reference_count=feedback_count,
        active_job_counts=await _active_job_counts(
            session, org_id=kb.org_id, kb_id=kb.id
        ),
    )


def _blocker(
    code: str,
    count: int,
    message: str,
    *,
    ids: Iterable[UUID] = (),
    retry_at: datetime | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "count": count,
        "message": message,
    }
    public_ids = [str(item) for item in ids]
    if public_ids:
        result["ids"] = public_ids
    if retry_at is not None:
        result["retry_at"] = retry_at.isoformat()
    return result


def _purge_blockers(
    kb: KnowledgeBase,
    inventory: _Inventory,
    *,
    now: datetime,
    retention_days: int,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if inventory.business_reference_count:
        blockers.append(
            _blocker(
                "business_artifact_references",
                inventory.business_reference_count,
                "外部业务产物仍引用该知识库资料",
            )
        )
    if inventory.protected_snapshot_ids:
        blockers.append(
            _blocker(
                "protected_knowledge_snapshot_references",
                len(inventory.protected_snapshot_ids),
                "受保护知识快照仍被历史或业务产物引用",
                ids=inventory.protected_snapshot_ids,
            )
        )
    if inventory.feedback_reference_count:
        blockers.append(
            _blocker(
                "feedback_or_citation_references",
                inventory.feedback_reference_count,
                "反馈或引用记录仍保留该知识库资料标识",
            )
        )
    if inventory.legal_hold_document_ids:
        blockers.append(
            _blocker(
                "legal_hold_active",
                len(inventory.legal_hold_document_ids),
                "知识库资料处于法律保留状态",
                ids=inventory.legal_hold_document_ids,
            )
        )
    if inventory.manual_fact_ids:
        blockers.append(
            _blocker(
                "manual_fact_references",
                len(inventory.manual_fact_ids),
                "人工确认事实必须先转移或显式解除",
                ids=inventory.manual_fact_ids,
            )
        )
    if inventory.pinned_entity_ids:
        blockers.append(
            _blocker(
                "pinned_entity_references",
                len(inventory.pinned_entity_ids),
                "人工固定实体必须先转移或解除固定",
                ids=inventory.pinned_entity_ids,
            )
        )
    if inventory.shared_object_keys:
        blockers.append(
            _blocker(
                "shared_object_keys",
                len(inventory.shared_object_keys),
                "对象仍被其他知识库资料使用",
            )
        )
    active_job_count = sum(inventory.active_job_counts.values())
    if active_job_count:
        blockers.append(
            _blocker(
                "active_jobs",
                active_job_count,
                "仍有活动任务，需等待完成或取消",
            )
        )
    if kb.archived_at is not None and retention_days > 0:
        retention_deadline = kb.archived_at + timedelta(days=retention_days)
        if now < retention_deadline:
            blockers.append(
                _blocker(
                    "retention_period_active",
                    1,
                    "知识库归档保留期尚未结束",
                    retry_at=retention_deadline,
                )
            )
    return sorted(blockers, key=lambda item: item["code"])


def _hard_delete_reference_count(inventory: _Inventory) -> int:
    return (
        sum(len(ids) for ids in inventory.table_ids.values())
        + inventory.external_reference_count
        + len(inventory.object_keys)
    )


def _plan_payload(
    kb: KnowledgeBase,
    inventory: _Inventory,
    *,
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    lifecycle = _value(kb.lifecycle_status)
    if lifecycle == KnowledgeBaseLifecycleStatus.PURGE_PENDING.value:
        lifecycle = KnowledgeBaseLifecycleStatus.ARCHIVED.value
    content_tables = {
        name: ids
        for name, ids in inventory.table_ids.items()
        if name not in _PRESERVED_KB_TABLES
    }
    return {
        "version": PLAN_VERSION,
        "kb_id": str(kb.id),
        "org_id": str(kb.org_id),
        "lifecycle_status": lifecycle,
        "consumption_epoch": kb.consumption_epoch,
        "content_tables": content_tables,
        "external_reference_count": inventory.external_reference_count,
        "objects": [
            {
                "key": key,
                "exists": inventory.object_identities[key].exists,
                "version_id": inventory.object_identities[key].version_id,
                "etag": inventory.object_identities[key].etag,
            }
            for key in inventory.object_keys
        ],
        "shared_object_keys": inventory.shared_object_keys,
        "legal_hold_document_ids": [
            str(item) for item in inventory.legal_hold_document_ids
        ],
        "manual_fact_ids": [str(item) for item in inventory.manual_fact_ids],
        "pinned_entity_ids": [str(item) for item in inventory.pinned_entity_ids],
        "protected_snapshot_ids": [
            str(item) for item in inventory.protected_snapshot_ids
        ],
        "business_reference_count": inventory.business_reference_count,
        "feedback_reference_count": inventory.feedback_reference_count,
        "active_job_counts": inventory.active_job_counts,
        "blocker_codes": [item["code"] for item in blockers],
    }


async def preview_knowledge_base_deletion(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    now: datetime | None = None,
    retention_days: int | None = None,
    lock: bool = False,
) -> dict[str, Any]:
    statement = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.org_id == org_id,
    )
    if lock:
        statement = statement.with_for_update()
    kb = await session.scalar(statement)
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    current_time = now or datetime.now(UTC)
    try:
        inventory = await _collect_inventory(session, kb=kb)
    except Exception:
        logger.exception(
            "knowledge-base deletion inventory is incomplete",
            extra={"org_id": str(org_id), "kb_id": str(kb_id)},
        )
        return {
            "complete": False,
            "version": PLAN_VERSION,
            "kb_id": str(kb.id),
            "name": kb.name,
            "owner_org_id": str(kb.org_id),
            "lifecycle_status": _value(kb.lifecycle_status),
            "consumption_epoch": kb.consumption_epoch,
            "allowed_actions": [],
            "hard_delete_eligible": False,
            "content_hash": None,
            "plan_hash": None,
            "expires_at": None,
            "counts": {
                "tables": {},
                "objects": 0,
                "shared_objects": 0,
                "legal_holds": 0,
                "manual_facts": 0,
                "pinned_entities": 0,
                "protected_snapshots": 0,
                "business_references": 0,
                "external_references": 0,
                "feedback_references": 0,
                "ingest_runs": 0,
                "document_operations": 0,
                "embedding_reindex_jobs": 0,
            },
            "latest_operation": None,
            "blockers": [
                _blocker(
                    "reference_registry_unavailable",
                    1,
                    "引用登记或对象清单暂不可用，请稍后重试",
                )
            ],
            "_manifest": {"objects": [], "content_tables": {}},
        }
    effective_retention_days = (
        get_settings().kb_document_purge_retention_days
        if retention_days is None
        else retention_days
    )
    blockers = _purge_blockers(
        kb,
        inventory,
        now=current_time,
        retention_days=effective_retention_days,
    )
    hard_delete_eligible = (
        _value(kb.lifecycle_status) == KnowledgeBaseLifecycleStatus.ACTIVE.value
        and _hard_delete_reference_count(inventory) == 0
    )
    lifecycle = _value(kb.lifecycle_status)
    allowed_actions: list[str] = []
    if lifecycle == KnowledgeBaseLifecycleStatus.ACTIVE.value:
        allowed_actions.append("hard_delete" if hard_delete_eligible else "archive")
    elif lifecycle == KnowledgeBaseLifecycleStatus.ARCHIVED.value:
        allowed_actions.append("restore")
        if not blockers:
            allowed_actions.append("purge")
    payload = _plan_payload(kb, inventory, blockers=blockers)
    content_hash = _canonical_hash(payload)
    execution_hash = _execution_hash(payload)
    expires_at = _plan_expires_at(current_time)
    latest_operation = await session.scalar(
        select(KnowledgeBaseLifecycleOperation)
        .where(
            KnowledgeBaseLifecycleOperation.org_id == org_id,
            KnowledgeBaseLifecycleOperation.kb_id == kb_id,
        )
        .order_by(
            KnowledgeBaseLifecycleOperation.created_at.desc(),
            KnowledgeBaseLifecycleOperation.id.desc(),
        )
        .limit(1)
    )
    return {
        "complete": True,
        "version": PLAN_VERSION,
        "kb_id": str(kb.id),
        "name": kb.name,
        "owner_org_id": str(kb.org_id),
        "lifecycle_status": lifecycle,
        "consumption_epoch": kb.consumption_epoch,
        "allowed_actions": allowed_actions,
        "hard_delete_eligible": hard_delete_eligible,
        "content_hash": content_hash,
        "execution_hash": execution_hash,
        "plan_hash": _canonical_hash(
            {
                "content_hash": content_hash,
                "expires_at": expires_at.isoformat(),
            }
        ),
        "expires_at": expires_at.isoformat(),
        "counts": {
            "tables": {
                name: len(ids) for name, ids in inventory.table_ids.items()
            },
            "external_references": inventory.external_reference_count,
            "objects": len(inventory.object_keys),
            "shared_objects": len(inventory.shared_object_keys),
            "legal_holds": len(inventory.legal_hold_document_ids),
            "manual_facts": len(inventory.manual_fact_ids),
            "pinned_entities": len(inventory.pinned_entity_ids),
            "protected_snapshots": len(inventory.protected_snapshot_ids),
            "business_references": inventory.business_reference_count,
            "feedback_references": inventory.feedback_reference_count,
            **inventory.active_job_counts,
        },
        "latest_operation": (
            public_lifecycle_operation(latest_operation)
            if latest_operation is not None
            else None
        ),
        "blockers": blockers,
        "_manifest": {
            "objects": [
                {
                    "key": key,
                    "exists": inventory.object_identities[key].exists,
                    "version_id": inventory.object_identities[key].version_id,
                    "etag": inventory.object_identities[key].etag,
                }
                for key in inventory.object_keys
                if key not in set(inventory.shared_object_keys)
            ],
            "content_tables": {
                name: len(ids)
                for name, ids in inventory.table_ids.items()
                if name not in _PRESERVED_KB_TABLES
            },
        },
    }


def public_deletion_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Strip execution-only object keys from an API preview."""

    table_counts = preview["counts"]["tables"]

    def total(*names: str) -> int:
        return sum(int(table_counts.get(name, 0)) for name in names)

    known_tables = {
        "source_documents",
        "document_revisions",
        "knowledge_snapshots",
        "canonical_entities",
        "kb_entities",
        "fact_claims",
        "evidence_spans",
        "kb_image_assets",
        "kb_snapshot_image_assets",
        "kb_shares",
        *_PRESERVED_KB_TABLES,
    }
    projections = sum(
        int(count)
        for name, count in table_counts.items()
        if name not in known_tables
    )
    blockers = public_lifecycle_blockers(preview["blockers"])
    return {
        "version": preview["version"],
        "kb_id": preview["kb_id"],
        "kb_name": preview["name"],
        "owner_org_id": preview["owner_org_id"],
        "lifecycle_status": preview["lifecycle_status"],
        "consumption_epoch": preview["consumption_epoch"],
        "complete": preview["complete"],
        "allowed_actions": [
            "empty_delete" if action == "hard_delete" else action
            for action in preview["allowed_actions"]
        ],
        "impact_counts": {
            "documents": total("source_documents"),
            "revisions": total("document_revisions"),
            "snapshots": total("knowledge_snapshots"),
            "projections": projections,
            "entities": total("canonical_entities", "kb_entities"),
            "fact_claims": total("fact_claims", "evidence_spans"),
            "media": total("kb_image_assets", "kb_snapshot_image_assets"),
            "shares": total("kb_shares"),
            "external_references": preview["counts"].get("external_references", 0),
            "active_tasks": (
                preview["counts"]["ingest_runs"]
                + preview["counts"]["document_operations"]
                + preview["counts"]["embedding_reindex_jobs"]
            ),
            "business_artifacts": preview["counts"].get("business_references", 0),
            "feedback_references": preview["counts"].get(
                "feedback_references", 0
            ),
            "pinned_entities": preview["counts"].get("pinned_entities", 0),
            "object_keys": preview["counts"]["objects"],
            "shared_object_keys": preview["counts"]["shared_objects"],
        },
        "blockers": blockers,
        "plan_hash": preview["plan_hash"],
        "expires_at": preview["expires_at"],
        "latest_operation": preview["latest_operation"],
    }


def public_lifecycle_blockers(
    blockers: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return stable, non-sensitive blocker codes and identifiers."""

    blocker_codes = {
        "retention_period_active": "RETENTION_PERIOD_ACTIVE",
        "legal_hold_active": "LEGAL_HOLD_ACTIVE",
        "business_artifact_references": "BUSINESS_ARTIFACT_REFERENCE",
        "protected_knowledge_snapshot_references": "KNOWLEDGE_SNAPSHOT_REFERENCE",
        "feedback_or_citation_references": "FEEDBACK_OR_CITATION_REFERENCE",
        "manual_fact_references": "MANUAL_FACT_REFERENCE",
        "pinned_entity_references": "PINNED_ENTITY_REFERENCE",
        "shared_object_keys": "SHARED_OBJECT_REFERENCE",
        "reference_registry_unavailable": "REFERENCE_REGISTRY_UNAVAILABLE",
        "active_ingest_runs": "ACTIVE_TASK_REFERENCE",
        "active_document_operations": "ACTIVE_TASK_REFERENCE",
        "active_embedding_reindex_jobs": "ACTIVE_TASK_REFERENCE",
        "active_jobs": "ACTIVE_TASK_REFERENCE",
        "external_scope_references": "EXTERNAL_SCOPE_REFERENCE",
        "knowledge_base_references": "KNOWLEDGE_BASE_REFERENCE",
    }
    return [
        {
            "code": blocker_codes.get(
                blocker["code"], str(blocker["code"]).upper()
            ),
            "count": blocker["count"],
            "identifiers": list(blocker.get("ids", [])),
            "resolution_hint": blocker["message"],
            **(
                {"retry_at": blocker["retry_at"]}
                if "retry_at" in blocker
                else {}
            ),
        }
        for blocker in blockers
    ]


def _require_plan(preview: dict[str, Any], expected_plan_hash: str) -> None:
    if not preview["complete"] or preview["plan_hash"] is None:
        raise KnowledgeBaseLifecycleError(
            "deletion_inventory_incomplete",
            "引用登记或对象清单不完整，当前不能执行删除",
            blockers=preview["blockers"],
            preview=public_deletion_preview(preview),
        )
    if expected_plan_hash.strip().strip('"') != preview["plan_hash"]:
        raise KnowledgeBaseLifecycleError(
            "deletion_plan_stale",
            "删除影响已经变化，请重新预检",
            preview=public_deletion_preview(preview),
        )


async def archive_knowledge_base(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    actor_id: UUID,
    reason: str,
    expected_plan_hash: str,
    acknowledge_external_unlink: bool,
) -> KnowledgeBase:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise KnowledgeBaseLifecycleError("archive_reason_required", "归档原因不能为空")
    preview = await preview_knowledge_base_deletion(
        session, org_id=org_id, kb_id=kb_id, lock=True
    )
    _require_plan(preview, expected_plan_hash)
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    if _value(kb.lifecycle_status) != KnowledgeBaseLifecycleStatus.ACTIVE.value:
        raise KnowledgeBaseLifecycleError(
            "invalid_lifecycle_transition", "仅活动知识库可归档"
        )
    external_references = int(preview["counts"].get("external_references", 0))
    if external_references and not acknowledge_external_unlink:
        # 解除关联是宿主的动作(SDK 不认识业务对象),这里只负责如实报数并拦一次
        raise KnowledgeBaseLifecycleError(
            "external_unlink_acknowledgement_required",
            "知识库仍被外部业务对象引用，需确认解除关联",
            blockers=[
                _blocker(
                    "external_scope_references",
                    external_references,
                    "确认后由宿主负责从业务侧解除引用",
                )
            ],
        )
    await session.execute(delete(KbShare).where(KbShare.kb_id == kb.id))
    now = datetime.now(UTC)
    kb.lifecycle_status = KnowledgeBaseLifecycleStatus.ARCHIVED
    kb.consumption_epoch += 1
    kb.archived_at = now
    kb.archived_by = actor_id
    kb.archive_reason = normalized_reason
    session.add_all(
        [
            kb,
            AuditLog(
                org_id=org_id,
                user_id=actor_id,
                action="kb.archive",
                entity_type="knowledge_base",
                entity_id=str(kb.id),
                detail={
                    "plan_hash": preview["plan_hash"],
                    "external_unlink_count": external_references,
                },
            ),
            OutboxEvent(
                org_id=org_id,
                kb_id=kb.id,
                aggregate_type="knowledge_base",
                aggregate_id=kb.id,
                event_type=ARCHIVED_EVENT_TYPE,
                idempotency_key=f"kb:{kb.id}:archived:{kb.consumption_epoch}",
                payload={
                    "consumption_epoch": kb.consumption_epoch,
                    "external_unlink_count": external_references,
                },
            ),
        ]
    )
    await session.flush()
    return kb


async def restore_knowledge_base(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    actor_id: UUID,
    reason: str,
) -> KnowledgeBase:
    kb = await session.scalar(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id, KnowledgeBase.org_id == org_id)
        .with_for_update()
    )
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    if _value(kb.lifecycle_status) != KnowledgeBaseLifecycleStatus.ARCHIVED.value:
        raise KnowledgeBaseLifecycleError(
            "invalid_lifecycle_transition", "仅已归档知识库可恢复"
        )
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise KnowledgeBaseLifecycleError("restore_reason_required", "恢复原因不能为空")
    previous_archive = {
        "archived_at": kb.archived_at.isoformat() if kb.archived_at else None,
        "archived_by": str(kb.archived_by) if kb.archived_by else None,
        "archive_reason": kb.archive_reason,
    }
    kb.lifecycle_status = KnowledgeBaseLifecycleStatus.ACTIVE
    kb.consumption_epoch += 1
    kb.archived_at = None
    kb.archived_by = None
    kb.archive_reason = None
    session.add_all(
        [
            kb,
            AuditLog(
                org_id=org_id,
                user_id=actor_id,
                action="kb.restore",
                entity_type="knowledge_base",
                entity_id=str(kb.id),
                detail={"reason": normalized_reason, "previous_archive": previous_archive},
            ),
            OutboxEvent(
                org_id=org_id,
                kb_id=kb.id,
                aggregate_type="knowledge_base",
                aggregate_id=kb.id,
                event_type=RESTORED_EVENT_TYPE,
                idempotency_key=f"kb:{kb.id}:restored:{kb.consumption_epoch}",
                payload={"consumption_epoch": kb.consumption_epoch},
            ),
        ]
    )
    await session.flush()
    return kb


async def hard_delete_empty_knowledge_base(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    actor_id: UUID,
    expected_plan_hash: str,
) -> None:
    preview = await preview_knowledge_base_deletion(
        session, org_id=org_id, kb_id=kb_id, lock=True
    )
    _require_plan(preview, expected_plan_hash)
    if not preview["hard_delete_eligible"]:
        raise KnowledgeBaseLifecycleError(
            "kb_not_empty",
            "知识库已产生资料或引用，请先归档",
            blockers=[
                _blocker(
                    "knowledge_base_references",
                    sum(preview["counts"]["tables"].values())
                    + preview["counts"].get("external_references", 0)
                    + preview["counts"]["objects"],
                    "知识库不是从未使用的空库",
                )
            ],
            preview=public_deletion_preview(preview),
        )
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    session.add(
        AuditLog(
            org_id=org_id,
            user_id=actor_id,
            action="kb.hard_delete_empty",
            entity_type="knowledge_base",
            entity_id=str(kb.id),
            detail={"name": kb.name, "plan_hash": preview["plan_hash"]},
        )
    )
    await session.delete(kb)
    await session.flush()


# 管理员强制清理可跳过的 blocker:保留期与各类"引用仍存在"拦截——跳过后引用方
# (外部业务产物/受保护快照/人工事实等)会出现死链,由前端确认弹窗明示后果。
# 法律保留(合规红线)、共享对象(删了会物理损坏其他库)、活动任务(与在途摄入互踩)
# 是系统完整性门禁,任何模式都不可跳过。
FORCE_BYPASSABLE_BLOCKER_CODES = frozenset(
    {
        "retention_period_active",
        "historical_retrieval_references",
        "business_artifact_references",
        "protected_knowledge_snapshot_references",
        "feedback_or_citation_references",
        "manual_fact_references",
        "pinned_entity_references",
    }
)


def _split_force_blockers(
    blockers: list[dict[str, Any]], *, force: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 force 模式拆分 blocker:返回 (仍拦截的, 被跳过的)。非 force 全拦。"""
    if not force:
        return blockers, []
    remaining = [
        b for b in blockers if b["code"] not in FORCE_BYPASSABLE_BLOCKER_CODES
    ]
    bypassed = [
        b for b in blockers if b["code"] in FORCE_BYPASSABLE_BLOCKER_CODES
    ]
    return remaining, bypassed


async def submit_knowledge_base_purge(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    actor_id: UUID,
    expected_plan_hash: str,
    name_confirmation: str,
    reason: str,
    idempotency_key: str,
    retention_days: int | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> KnowledgeBaseLifecycleOperation:
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise KnowledgeBaseLifecycleError(
            "idempotency_key_required", "永久清理需要 Idempotency-Key"
        )
    existing = await session.scalar(
        select(KnowledgeBaseLifecycleOperation)
        .where(
            KnowledgeBaseLifecycleOperation.org_id == org_id,
            KnowledgeBaseLifecycleOperation.idempotency_key == normalized_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.kb_id != kb_id:
            raise KnowledgeBaseLifecycleError(
                "idempotency_key_conflict", "幂等键已被其他知识库操作使用"
            )
        return existing
    preview = await preview_knowledge_base_deletion(
        session,
        org_id=org_id,
        kb_id=kb_id,
        now=now,
        retention_days=retention_days,
        lock=True,
    )
    _require_plan(preview, expected_plan_hash)
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    if _value(kb.lifecycle_status) != KnowledgeBaseLifecycleStatus.ARCHIVED.value:
        raise KnowledgeBaseLifecycleError(
            "invalid_lifecycle_transition", "仅已归档知识库可永久清理"
        )
    if name_confirmation != kb.name:
        raise KnowledgeBaseLifecycleError(
            "name_confirmation_mismatch", "知识库名称确认不匹配"
        )
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise KnowledgeBaseLifecycleError("purge_reason_required", "清理原因不能为空")
    remaining_blockers, bypassed_blockers = _split_force_blockers(
        preview["blockers"], force=force
    )
    if remaining_blockers:
        raise KnowledgeBaseLifecycleError(
            "purge_blocked",
            "知识库仍有受保护引用，不能永久清理",
            blockers=remaining_blockers,
            preview=public_deletion_preview(preview),
        )

    operation_id = uuid4()
    event_id = uuid4()
    event = OutboxEvent(
        id=event_id,
        org_id=org_id,
        kb_id=kb.id,
        aggregate_type="knowledge_base",
        aggregate_id=kb.id,
        event_type=PURGE_EVENT_TYPE,
        idempotency_key=f"kb:{kb.id}:purge:{normalized_key}",
        payload={"operation_id": str(operation_id), "plan_hash": preview["plan_hash"]},
    )
    operation = KnowledgeBaseLifecycleOperation(
        id=operation_id,
        org_id=org_id,
        kb_id=kb.id,
        plan_hash=preview["plan_hash"],
        idempotency_key=normalized_key,
        requested_by=actor_id,
        confirmation_summary={
            "name_sha256": hashlib.sha256(
                name_confirmation.encode("utf-8")
            ).hexdigest(),
            "reason": normalized_reason,
            "force": force,
        },
        manifest_summary={
            "version": PLAN_VERSION,
            "content_hash": preview["content_hash"],
            "execution_hash": preview["execution_hash"],
            "content_tables": preview["_manifest"]["content_tables"],
            "object_count": len(preview["_manifest"]["objects"]),
            # 强制清理时记录被跳过的拦截项(公开口径 code+count),供审计与执行复核
            "force_bypassed_blockers": [
                {"code": b["code"], "count": b["count"]}
                for b in bypassed_blockers
            ],
        },
        outbox_event_id=event.id,
    )
    session.add(event)
    await session.flush()
    session.add(operation)
    await session.flush()
    bucket = get_settings().minio_bucket
    session.add_all(
        [
            KnowledgeBasePurgeObject(
                org_id=org_id,
                kb_id=kb.id,
                operation_id=operation.id,
                bucket=bucket,
                object_key=object["key"],
                object_version=object["version_id"] or "",
                etag=object["etag"],
                expected_missing=not object["exists"],
                object_kind="kb_content",
            )
            for object in preview["_manifest"]["objects"]
        ]
    )
    kb.lifecycle_status = KnowledgeBaseLifecycleStatus.PURGE_PENDING
    kb.purge_requested_at = now or datetime.now(UTC)
    session.add_all(
        [
            kb,
            AuditLog(
                org_id=org_id,
                user_id=actor_id,
                action="kb.purge_requested",
                entity_type="knowledge_base",
                entity_id=str(kb.id),
                detail={
                    "operation_id": str(operation.id),
                    "plan_hash": preview["plan_hash"],
                    "force": force,
                    "force_bypassed_blockers": [
                        {"code": b["code"], "count": b["count"]}
                        for b in bypassed_blockers
                    ],
                },
            ),
        ]
    )
    await session.flush()
    return operation


def public_lifecycle_operation(
    operation: KnowledgeBaseLifecycleOperation,
) -> dict[str, Any]:
    status = _value(operation.status)
    return {
        "id": str(operation.id),
        "kb_id": str(operation.kb_id),
        "operation_type": _value(operation.operation_type),
        "status": status,
        "phase": _value(operation.phase),
        "attempt_count": operation.attempt_count,
        "impact_summary": operation.manifest_summary,
        "last_error_code": operation.last_error_code,
        "error_message": operation.last_error_message,
        "retryable": (
            status
            in {
                KnowledgeBaseLifecycleOperationStatus.FAILED.value,
                KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER.value,
            }
            and operation.last_error_code
            in _RETRYABLE_OPERATION_ERROR_CODES
        ),
        "started_at": operation.started_at,
        "completed_at": operation.completed_at,
        "created_at": operation.created_at,
    }


async def get_lifecycle_operation(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
) -> KnowledgeBaseLifecycleOperation:
    operation = await session.scalar(
        select(KnowledgeBaseLifecycleOperation).where(
            KnowledgeBaseLifecycleOperation.id == operation_id,
            KnowledgeBaseLifecycleOperation.org_id == org_id,
        )
    )
    if operation is None:
        raise KnowledgeBaseLifecycleError(
            "lifecycle_operation_not_found", "知识库生命周期操作不存在"
        )
    return operation


def _delete_order(tables: Iterable[Table]) -> list[Table]:
    by_name = {table.name: table for table in tables}
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, int] = {name: 0 for name in by_name}
    for table in by_name.values():
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table.name
            if target == table.name or target not in by_name:
                continue
            if target not in outgoing[table.name]:
                outgoing[table.name].add(target)
                incoming[target] += 1
    ready = sorted(name for name, count in incoming.items() if count == 0)
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for target in sorted(outgoing[name]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort()
    if len(ordered) != len(by_name):
        remaining = sorted(set(by_name) - set(ordered))
        raise KnowledgeBaseLifecycleError(
            "purge_dependency_cycle",
            f"知识库元数据依赖存在未处理环: {remaining}",
        )
    return [by_name[name] for name in ordered]


async def _delete_kb_metadata(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    preserved_outbox_event_id: UUID,
) -> None:
    """删除知识库的全部子表元数据(保留审计壳表)。

    快照投影表(带 snapshot_id 列)上有 RESTRICTIVE 的 snapshot_delete_scope 策略:
    DELETE 必须命中 `app.build_snapshot_id` 且对应快照非 active,否则 **静默删 0 行**
    (RLS 不报错),随后删 evidence_spans / fact_claims 等被引用表就会撞外键。
    因此这里照 projection_gc 的既定做法:先退休快照,再逐快照设置 build_snapshot_id
    分批删投影,最后删 snapshot_id 为空的历史行。
    """
    kb.active_snapshot_id = None
    session.add(kb)
    await session.flush()
    # 策略要求快照非 active;purge 终态会连快照行一起删,这里改状态没有副作用。
    # retired 受 ck_knowledge_snapshot_retired_at 约束,必须同时补 retired_at。
    await session.execute(
        update(KnowledgeSnapshot)
        .where(
            KnowledgeSnapshot.org_id == kb.org_id,
            KnowledgeSnapshot.kb_id == kb.id,
            KnowledgeSnapshot.status == SnapshotStatus.ACTIVE.value,
        )
        .values(status=SnapshotStatus.RETIRED.value, retired_at=datetime.now(UTC))
    )
    await session.flush()
    snapshot_ids = list(
        (
            await session.execute(
                select(KnowledgeSnapshot.id).where(
                    KnowledgeSnapshot.org_id == kb.org_id,
                    KnowledgeSnapshot.kb_id == kb.id,
                )
            )
        )
        .scalars()
        .all()
    )
    tables = [
        table
        for table in _kb_child_tables()
        if table.name not in _PRESERVED_KB_TABLES
    ]
    for table in _delete_order(tables):
        conditions = [table.c.kb_id == kb.id]
        if "org_id" in table.c:
            conditions.append(table.c.org_id == kb.org_id)
        snapshot_column = table.c.get("snapshot_id")
        if snapshot_column is None:
            await session.execute(delete(table).where(*conditions))
            continue
        # 逐快照进入 build 作用域后删除该快照的投影行
        for snapshot_id in snapshot_ids:
            await session.execute(
                select(
                    func.set_config("app.build_snapshot_id", str(snapshot_id), True)
                )
            )
            await session.execute(
                delete(table).where(*conditions, snapshot_column == snapshot_id)
            )
        # snapshot_id 为空的历史行:kb_chunks 靠 active_snapshot_id 已置空的分支放行,
        # 其余表本就不在快照作用域内;legacy_projection_gc 开关兼容旧检索行策略
        await session.execute(
            select(func.set_config("app.legacy_projection_gc", "on", True))
        )
        await session.execute(
            delete(table).where(*conditions, snapshot_column.is_(None))
        )
        await session.execute(
            select(func.set_config("app.legacy_projection_gc", "off", True))
        )
    await session.execute(
        delete(OutboxEvent).where(
            OutboxEvent.org_id == kb.org_id,
            OutboxEvent.kb_id == kb.id,
            OutboxEvent.id != preserved_outbox_event_id,
        )
    )


async def execute_knowledge_base_purge(
    session: AsyncSession,
    event: OutboxEvent,
    *,
    delete_object: DeleteObject = storage.remove_object_if_unchanged,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> KnowledgeBaseLifecycleOperation:
    if (
        event.event_type != PURGE_EVENT_TYPE
        or event.aggregate_type != "knowledge_base"
        or event.aggregate_id != event.kb_id
    ):
        raise KnowledgeBaseLifecycleError(
            "purge_event_invalid", "知识库清理事件身份无效"
        )
    try:
        operation_id = UUID(str(event.payload["operation_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise KnowledgeBaseLifecycleError(
            "purge_event_invalid", "知识库清理事件缺少 operation"
        ) from exc
    operation = await session.scalar(
        select(KnowledgeBaseLifecycleOperation)
        .where(
            KnowledgeBaseLifecycleOperation.id == operation_id,
            KnowledgeBaseLifecycleOperation.org_id == event.org_id,
            KnowledgeBaseLifecycleOperation.kb_id == event.kb_id,
        )
        .with_for_update()
    )
    if operation is None:
        raise KnowledgeBaseLifecycleError(
            "lifecycle_operation_not_found", "知识库清理操作不存在"
        )
    if (
        _value(operation.status)
        == KnowledgeBaseLifecycleOperationStatus.COMPLETED.value
    ):
        return operation
    kb = await session.scalar(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.id == event.kb_id,
            KnowledgeBase.org_id == event.org_id,
        )
        .with_for_update()
    )
    if kb is None:
        raise KnowledgeBaseLifecycleError(
            "knowledge_base_not_found", "知识库不存在"
        )
    if (
        _value(kb.lifecycle_status)
        != KnowledgeBaseLifecycleStatus.PURGE_PENDING.value
    ):
        raise KnowledgeBaseLifecycleError(
            "invalid_lifecycle_transition", "知识库不在永久清理状态"
        )

    operation.status = KnowledgeBaseLifecycleOperationStatus.PROCESSING
    operation.phase = KnowledgeBaseLifecyclePhase.REVALIDATE_AND_QUIESCE
    operation.attempt_count += 1
    operation.started_at = operation.started_at or datetime.now(UTC)
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error_message = None
    session.add(operation)
    await session.flush()

    current_preview = await preview_knowledge_base_deletion(
        session,
        org_id=event.org_id,
        kb_id=event.kb_id,
        now=now,
        retention_days=retention_days,
    )
    # 强制清理的操作在复核时沿用同一跳过口径,否则被跳过的引用会立刻拦下执行
    operation_force = isinstance(operation.confirmation_summary, dict) and bool(
        operation.confirmation_summary.get("force")
    )
    revalidation_blockers, _ = _split_force_blockers(
        current_preview["blockers"], force=operation_force
    )
    expected_execution_hash = operation.manifest_summary.get("execution_hash")
    if (
        not isinstance(expected_execution_hash, str)
        or not expected_execution_hash
        or current_preview.get("execution_hash") != expected_execution_hash
        or revalidation_blockers
    ):
        operation.status = KnowledgeBaseLifecycleOperationStatus.FAILED
        operation.phase = KnowledgeBaseLifecyclePhase.REVALIDATE_AND_QUIESCE
        operation.last_error_code = "purge_plan_drift"
        operation.last_error_message = "永久清理计划已变化，请重新预检"
        operation.failed_at = datetime.now(UTC)
        session.add(operation)
        await session.flush()
        return operation

    operation.phase = KnowledgeBaseLifecyclePhase.DELETE_OBJECTS
    objects = list(
        (
            await session.execute(
                select(KnowledgeBasePurgeObject)
                .where(
                    KnowledgeBasePurgeObject.operation_id == operation.id,
                    KnowledgeBasePurgeObject.org_id == operation.org_id,
                    KnowledgeBasePurgeObject.kb_id == operation.kb_id,
                )
                .order_by(
                    KnowledgeBasePurgeObject.bucket,
                    KnowledgeBasePurgeObject.object_key,
                    KnowledgeBasePurgeObject.object_version,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for item in objects:
        if _value(item.status) in {
            KnowledgeBasePurgeObjectStatus.DELETED.value,
            KnowledgeBasePurgeObjectStatus.SKIPPED_SHARED.value,
        }:
            continue
        item.attempt_count += 1
        try:
            await delete_object(
                item.object_key,
                item.object_version or None,
                item.etag,
                item.expected_missing,
            )
        except Exception as exc:
            error_code = (
                "object_identity_changed"
                if isinstance(exc, storage.ObjectIdentityChanged)
                else "object_delete_failed"
            )
            item.status = KnowledgeBasePurgeObjectStatus.FAILED
            item.last_error_code = error_code
            item.last_error_message = "对象删除失败，可安全重试"
            operation.status = KnowledgeBaseLifecycleOperationStatus.FAILED
            operation.last_error_code = error_code
            operation.last_error_message = "对象删除阶段未完成"
            operation.failed_at = datetime.now(UTC)
            session.add_all([item, operation])
            await session.flush()
            return operation
        item.status = KnowledgeBasePurgeObjectStatus.DELETED
        item.completed_at = datetime.now(UTC)
        item.last_error_code = None
        item.last_error_message = None
        session.add(item)
        await session.flush()
        if _value(event.status) == OutboxStatus.PROCESSING.value:
            event.claim_expires_at = datetime.now(UTC) + timedelta(
                seconds=get_settings().kb_outbox_lease_seconds
            )
            session.add(event)
        await session.commit()

    operation.phase = KnowledgeBaseLifecyclePhase.CLEANUP_METADATA
    session.add(operation)
    await session.flush()
    try:
        async with session.begin_nested():
            await _delete_kb_metadata(
                session,
                kb=kb,
                preserved_outbox_event_id=event.id,
            )
    except Exception as exc:
        operation.status = KnowledgeBaseLifecycleOperationStatus.FAILED
        operation.last_error_code = "metadata_cleanup_failed"
        # 带上根因摘要:外键违反等原因此前被吞掉,只能靠复现才能定位
        operation.last_error_message = (
            f"元数据清理未完成，可安全重试：{type(exc).__name__}: {exc}"[:2000]
        )
        operation.failed_at = datetime.now(UTC)
        logger.exception(
            "KB 永久清理元数据删除失败(kb=%s, operation=%s)", kb.id, operation.id
        )
        session.add(operation)
        await session.flush()
        return operation

    residuals: dict[str, int] = {}
    for table in _kb_child_tables():
        if table.name in _PRESERVED_KB_TABLES:
            continue
        conditions = [table.c.kb_id == kb.id]
        if "org_id" in table.c:
            conditions.append(table.c.org_id == kb.org_id)
        count = int(
            await session.scalar(
                select(func.count()).select_from(table).where(*conditions)
            )
            or 0
        )
        if count:
            residuals[table.name] = count
    if residuals:
        operation.status = KnowledgeBaseLifecycleOperationStatus.FAILED
        operation.last_error_code = "metadata_residuals"
        operation.last_error_message = "元数据清理后仍存在残留"
        operation.manifest_summary = {
            **operation.manifest_summary,
            "residuals": residuals,
        }
        operation.failed_at = datetime.now(UTC)
        session.add(operation)
        await session.flush()
        return operation

    operation.phase = KnowledgeBaseLifecyclePhase.FINALIZE_AUDIT_SHELL
    completed_at = now or datetime.now(UTC)
    for item in objects:
        item.object_key = f"sha256:{hashlib.sha256(item.object_key.encode()).hexdigest()}"
        session.add(item)
    original_name_hash = hashlib.sha256(kb.name.encode("utf-8")).hexdigest()
    kb.name = f"已清理知识库 {kb.id.hex[:8]}"
    kb.description = None
    kb.ingest_profile = None
    kb.active_snapshot_id = None
    kb.lifecycle_status = KnowledgeBaseLifecycleStatus.PURGED
    kb.purged_at = completed_at
    kb.consumption_epoch += 1
    operation.status = KnowledgeBaseLifecycleOperationStatus.COMPLETED
    operation.phase = KnowledgeBaseLifecyclePhase.COMPLETED
    operation.completed_at = completed_at
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error_message = None
    operation.manifest_summary = {
        **operation.manifest_summary,
        "verified": True,
        "original_name_sha256": original_name_hash,
        "completed_at": completed_at.isoformat(),
    }
    session.add_all(
        [
            kb,
            operation,
            AuditLog(
                org_id=kb.org_id,
                user_id=operation.requested_by,
                action="kb.purged",
                entity_type="knowledge_base",
                entity_id=str(kb.id),
                detail={"operation_id": str(operation.id)},
            ),
        ]
    )
    await session.flush()
    return operation


async def retry_lifecycle_operation(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
) -> KnowledgeBaseLifecycleOperation:
    operation = await session.scalar(
        select(KnowledgeBaseLifecycleOperation)
        .where(
            KnowledgeBaseLifecycleOperation.id == operation_id,
            KnowledgeBaseLifecycleOperation.org_id == org_id,
        )
        .with_for_update()
    )
    if operation is None:
        raise KnowledgeBaseLifecycleError(
            "lifecycle_operation_not_found", "知识库生命周期操作不存在"
        )
    if _value(operation.status) not in {
        KnowledgeBaseLifecycleOperationStatus.FAILED.value,
        KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER.value,
    } or operation.last_error_code not in _RETRYABLE_OPERATION_ERROR_CODES:
        raise KnowledgeBaseLifecycleError(
            "operation_not_retryable", "当前操作状态不可重试"
        )
    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.id == operation.outbox_event_id,
            OutboxEvent.org_id == org_id,
        )
        .with_for_update()
    )
    if event is None:
        raise KnowledgeBaseLifecycleError(
            "purge_event_missing", "永久清理事件不存在"
        )
    await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event.id)
        .values(
            status=OutboxStatus.PENDING.value,
            available_at=datetime.now(UTC),
            claimed_by=None,
            claimed_at=None,
            claim_expires_at=None,
            published_at=None,
            last_error=None,
            attempts=0,
        )
    )
    operation.status = KnowledgeBaseLifecycleOperationStatus.PENDING
    operation.failed_at = None
    operation.last_error_code = None
    operation.last_error_message = None
    session.add(operation)
    await session.flush()
    return operation


__all__ = [
    "ARCHIVED_EVENT_TYPE",
    "KnowledgeBaseLifecycleError",
    "PLAN_VERSION",
    "PURGE_EVENT_TYPE",
    "RESTORED_EVENT_TYPE",
    "archive_knowledge_base",
    "execute_knowledge_base_purge",
    "get_lifecycle_operation",
    "hard_delete_empty_knowledge_base",
    "preview_knowledge_base_deletion",
    "public_deletion_preview",
    "public_lifecycle_blockers",
    "public_lifecycle_operation",
    "registered_kb_reference_tables",
    "restore_knowledge_base",
    "retry_lifecycle_operation",
    "submit_knowledge_base_purge",
]
