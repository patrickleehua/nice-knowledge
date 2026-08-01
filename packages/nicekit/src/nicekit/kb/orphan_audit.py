"""KB 孤儿数据定期巡检(对标 OWASP RAG Security:审计"源已删、派生残留")。

purge 流程自带残留检查(lifecycle.execute_knowledge_base_purge 的 residuals
兜底),但只覆盖"这一次 purge";平时因异常中断、bug 悬挂引用产生的孤儿
没有人巡。本模块提供 report-only 的周期巡检,只报告不删除:

a. 已 purge 文档仍挂着 chunk —— 文档级 purge(document_purge)应删光其
   chunk,残留即孤儿。注:kb_chunks.source_doc_id 是指向 source_documents.id
   的真外键(无 ON DELETE,数据库拒绝悬挂),"文档行已不存在"的硬孤儿在
   DB 层面不可能出现,故不查;kb_chunk_embeddings.chunk_id 的复合外键带
   ON DELETE CASCADE,chunk 删除时向量行随删,同样天然无孤儿,一并跳过。
b. lifecycle_status='purged' 的知识库仍有子表行 —— 口径与 lifecycle 的
   _kb_child_tables() / _PRESERVED_KB_TABLES 完全一致:purged 库除保留的
   审计壳表外不应有任何子表行。注:kb_chunk_embeddings 的 kb_id 无外键、
   不在 _kb_child_tables() 里,但其行必挂在 kb_chunks 上(CASCADE),
   chunk 残留会先被本类别扫出,无需单列。
c. 对象登记表 knowledge_base_purge_objects 中,所属 KB 已 purged 但状态
   仍是 pending/failed 的条目 —— purge 完成后所有登记对象应为
   deleted/skipped_shared,残留意味着对象存储里可能还有实体对象。

发现孤儿时写一条 AuditLog(action="kb.orphan_audit")并打 warning;
零发现只打 debug,不落审计。周期循环模式与 expiry.run_kb_expiry_sweeper
一致(inline 模式 lifespan 托管,per-org 隔离失败不中断整轮)。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb.lifecycle import (
    _PRESERVED_KB_TABLES,
    _delete_order,
    _identity_columns,
    _kb_child_tables,
    _serialize_identity,
)
from nicekit.models.kb import (
    DocumentLifecycleStatus,
    KbChunk,
    KnowledgeBase,
    KnowledgeBaseLifecycleStatus,
    KnowledgeBasePurgeObject,
    SourceDocument,
)
from nicekit.models.tenancy import AuditLog, Organization

logger = logging.getLogger(__name__)

AUDIT_ACTION = "kb.orphan_audit"
CLEANUP_AUDIT_ACTION = "kb.orphan_cleanup"
# 每个巡检类别最多带回的样本 ID 数:够定位问题,又不让审计 detail 无限膨胀
SAMPLE_LIMIT = 20
# purge 完成后登记对象应为 deleted/skipped_shared,以下状态即残留
_STALE_PURGE_OBJECT_STATUSES = ("pending", "failed")


@dataclass(slots=True)
class OrphanCategory:
    """单个巡检类别的结果:总数 + 样本 + 细分(如按表)计数。"""

    count: int = 0
    sample_ids: list[str] = field(default_factory=list)
    detail: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sample_ids": list(self.sample_ids),
            "detail": dict(self.detail),
        }


@dataclass(slots=True)
class OrphanReport:
    """一轮孤儿巡检报告(单 org 视角,受 RLS 约束)。"""

    generated_at: datetime
    # a 类:已 purge 文档仍挂着的 chunk
    purged_document_chunks: OrphanCategory = field(default_factory=OrphanCategory)
    # b 类:purged 知识库残留的子表行(保留审计壳表除外)
    purged_kb_residual_rows: OrphanCategory = field(default_factory=OrphanCategory)
    # c 类:purged 知识库的对象登记条目仍未确认删除
    purge_registry_stale_objects: OrphanCategory = field(default_factory=OrphanCategory)

    @property
    def total(self) -> int:
        return (
            self.purged_document_chunks.count
            + self.purged_kb_residual_rows.count
            + self.purge_registry_stale_objects.count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "total": self.total,
            "categories": {
                "purged_document_chunks": self.purged_document_chunks.to_dict(),
                "purged_kb_residual_rows": self.purged_kb_residual_rows.to_dict(),
                "purge_registry_stale_objects": (
                    self.purge_registry_stale_objects.to_dict()
                ),
            },
        }


async def _scan_purged_document_chunks(session: AsyncSession) -> OrphanCategory:
    """a 类:lifecycle_status='purged' 的 source_documents 名下残留的 kb_chunks。"""
    purged_condition = (
        SourceDocument.lifecycle_status == DocumentLifecycleStatus.PURGED.value
    )
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(KbChunk)
            .join(SourceDocument, KbChunk.source_doc_id == SourceDocument.id)
            .where(purged_condition)
        )
        or 0
    )
    category = OrphanCategory(count=count)
    if count:
        category.detail["kb_chunks"] = count
        category.sample_ids = [
            str(chunk_id)
            for chunk_id in (
                await session.execute(
                    select(KbChunk.id)
                    .join(
                        SourceDocument,
                        KbChunk.source_doc_id == SourceDocument.id,
                    )
                    .where(purged_condition)
                    .order_by(KbChunk.id)
                    .limit(SAMPLE_LIMIT)
                )
            )
            .scalars()
            .all()
        ]
    return category


async def _scan_purged_kb_residual_rows(
    session: AsyncSession, purged_kb_ids: list[UUID]
) -> OrphanCategory:
    """b 类:purged 知识库残留的子表行,口径同 lifecycle 的 purge 残留检查。"""
    category = OrphanCategory()
    if not purged_kb_ids:
        return category
    for table in _kb_child_tables():
        # 保留表是 purge 后刻意留下的审计壳(操作/对象台账/outbox),不算孤儿
        if table.name in _PRESERVED_KB_TABLES:
            continue
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.kb_id.in_(purged_kb_ids))
            )
            or 0
        )
        if not count:
            continue
        category.count += count
        category.detail[table.name] = count
        remaining = SAMPLE_LIMIT - len(category.sample_ids)
        if remaining <= 0:
            continue
        rows = (
            await session.execute(
                select(*_identity_columns(table))
                .where(table.c.kb_id.in_(purged_kb_ids))
                .limit(remaining)
            )
        ).all()
        category.sample_ids.extend(
            f"{table.name}:{_serialize_identity(tuple(row))}" for row in rows
        )
    return category


async def _scan_purge_registry_stale_objects(
    session: AsyncSession, purged_kb_ids: list[UUID]
) -> OrphanCategory:
    """c 类:purged 知识库名下仍未确认删除(pending/failed)的对象登记条目。"""
    category = OrphanCategory()
    if not purged_kb_ids:
        return category
    conditions = (
        KnowledgeBasePurgeObject.kb_id.in_(purged_kb_ids),
        KnowledgeBasePurgeObject.status.in_(_STALE_PURGE_OBJECT_STATUSES),
    )
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(KnowledgeBasePurgeObject)
            .where(*conditions)
        )
        or 0
    )
    category.count = count
    if count:
        category.detail["knowledge_base_purge_objects"] = count
        category.sample_ids = [
            str(object_id)
            for object_id in (
                await session.execute(
                    select(KnowledgeBasePurgeObject.id)
                    .where(*conditions)
                    .order_by(KnowledgeBasePurgeObject.id)
                    .limit(SAMPLE_LIMIT)
                )
            )
            .scalars()
            .all()
        ]
    return category


async def scan_orphans(session: AsyncSession) -> OrphanReport:
    """执行一轮孤儿扫描(只读,report-only)。可见范围由会话的 RLS 上下文决定。"""
    purged_kb_ids = list(
        (
            await session.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.lifecycle_status
                    == KnowledgeBaseLifecycleStatus.PURGED.value
                )
            )
        )
        .scalars()
        .all()
    )
    return OrphanReport(
        generated_at=datetime.now(UTC),
        purged_document_chunks=await _scan_purged_document_chunks(session),
        purged_kb_residual_rows=await _scan_purged_kb_residual_rows(
            session, purged_kb_ids
        ),
        purge_registry_stale_objects=await _scan_purge_registry_stale_objects(
            session, purged_kb_ids
        ),
    )


# ---------------------------------------------------------------------------
# 清理执行(管理员显式触发)
# ---------------------------------------------------------------------------
#
# 安全模型:只删"已获批删除却残留"的数据。a/b 类行都挂在已 purged 的文档 /
# 知识库上——purge 决策此前已经过预检 + 名称确认 + 幂等操作流程批准,删除这些
# 残留只是把先前已确认的删除做完,不构成新的破坏性决策;任何非 purged 归属的
# 数据一概不碰(所有 DELETE 的 WHERE 都锚定在 purged 状态上)。
#
# c 类(对象登记条目 pending/failed)不在此直接删对象存储:对象删除阶段内嵌在
# lifecycle.execute_knowledge_base_purge 中,与 operation 状态机 / outbox 租约 /
# 逐对象 commit 强耦合,且其前置校验要求 KB 处于 purge_pending——已 purged 的库
# 无法重入;既有的重试通道是 lifecycle.retry_lifecycle_operation(重置 outbox
# 事件重新驱动幂等删除)。为避免自造第二套对象删除逻辑,c 类在此标记 skipped,
# 由管理员走 operation 重试通道处理。

# c 类跳过原因(结果与审计中原样携带,前端可直接展示)
_REGISTRY_SKIP_REASON = (
    "对象登记条目不在此直接删除对象存储,"
    "需走 operation 重试通道(lifecycle.retry_lifecycle_operation)重新驱动幂等删除"
)


class OrphanCleanupError(RuntimeError):
    """清理失败的结构化错误:code 稳定可编程,detail 携带上下文。"""

    def __init__(
        self, code: str, message: str, *, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


@dataclass(slots=True)
class OrphanCleanupExpectation:
    """调用方(前端展示的巡检报告)对每类孤儿数量的期望,用于过期检测。"""

    purged_document_chunks: int = 0
    purged_kb_residual_rows: int = 0
    purge_registry_stale_objects: int = 0


@dataclass(slots=True)
class OrphanCleanupCategoryResult:
    """单类清理结果:删除数 + 细分(按表)计数;skipped 时携带原因。"""

    deleted_count: int = 0
    detail: dict[str, int] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "deleted_count": self.deleted_count,
            "detail": dict(self.detail),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


@dataclass(slots=True)
class OrphanCleanupResult:
    """一次清理执行的整体结果(单 org 视角,受 RLS 约束)。"""

    executed_at: datetime
    purged_document_chunks: OrphanCleanupCategoryResult = field(
        default_factory=OrphanCleanupCategoryResult
    )
    purged_kb_residual_rows: OrphanCleanupCategoryResult = field(
        default_factory=OrphanCleanupCategoryResult
    )
    purge_registry_stale_objects: OrphanCleanupCategoryResult = field(
        default_factory=OrphanCleanupCategoryResult
    )

    @property
    def total_deleted(self) -> int:
        return (
            self.purged_document_chunks.deleted_count
            + self.purged_kb_residual_rows.deleted_count
            + self.purge_registry_stale_objects.deleted_count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed_at": self.executed_at.isoformat(),
            "total_deleted": self.total_deleted,
            "categories": {
                "purged_document_chunks": self.purged_document_chunks.to_dict(),
                "purged_kb_residual_rows": self.purged_kb_residual_rows.to_dict(),
                "purge_registry_stale_objects": (
                    self.purge_registry_stale_objects.to_dict()
                ),
            },
        }


def _require_fresh_report(
    report: OrphanReport, expected: OrphanCleanupExpectation
) -> None:
    """任一类实际数 > 期望数即视为报告过期(打开页面后又产生了新孤儿),拒绝执行;
    实际数 ≤ 期望数放行——孤儿被其他流程顺手清掉是好事,不该挡住剩余清理。"""
    stale: dict[str, dict[str, int]] = {}
    for name, actual, wanted in (
        (
            "purged_document_chunks",
            report.purged_document_chunks.count,
            expected.purged_document_chunks,
        ),
        (
            "purged_kb_residual_rows",
            report.purged_kb_residual_rows.count,
            expected.purged_kb_residual_rows,
        ),
        (
            "purge_registry_stale_objects",
            report.purge_registry_stale_objects.count,
            expected.purge_registry_stale_objects,
        ),
    ):
        if actual > wanted:
            stale[name] = {"actual": actual, "expected": wanted}
    if stale:
        raise OrphanCleanupError(
            "orphan_report_stale",
            "孤儿数量已超出巡检报告,请重新巡检后再清理",
            detail={"categories": stale},
        )


async def _cleanup_purged_document_chunks(
    session: AsyncSession,
) -> OrphanCleanupCategoryResult:
    """a 类:删 purged 文档名下的 kb_chunks;kb_chunk_embeddings 有
    ON DELETE CASCADE 随删,无需单独处理。WHERE 锚定在文档 lifecycle_status
    ='purged' 上,非 purged 文档的 chunk 不可能被波及。"""
    result = await session.execute(
        delete(KbChunk).where(
            KbChunk.source_doc_id.in_(
                select(SourceDocument.id).where(
                    SourceDocument.lifecycle_status
                    == DocumentLifecycleStatus.PURGED.value
                )
            )
        )
    )
    deleted = int(result.rowcount or 0)
    category = OrphanCleanupCategoryResult(deleted_count=deleted)
    if deleted:
        category.detail["kb_chunks"] = deleted
    return category


async def _cleanup_purged_kb_residual_rows(
    session: AsyncSession, *, org_id: UUID, purged_kb_ids: list[UUID]
) -> OrphanCleanupCategoryResult:
    """b 类:按 lifecycle._delete_order 的外键拓扑序删除 purged 库残留子表行,
    与 execute_knowledge_base_purge 的元数据删除顺序一致,避免外键违规;
    保留的审计壳表(_PRESERVED_KB_TABLES)跳过。"""
    category = OrphanCleanupCategoryResult()
    if not purged_kb_ids:
        return category
    # knowledge_bases.active_snapshot_id 指向 knowledge_snapshots:purge 完成时
    # 已置空,这里再兜底置空一次,防异常路径下删残留快照行时触发外键违规。
    await session.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id.in_(purged_kb_ids))
        .values(active_snapshot_id=None)
    )
    tables = [
        table
        for table in _kb_child_tables()
        if table.name not in _PRESERVED_KB_TABLES
    ]
    for table in _delete_order(tables):
        conditions = [table.c.kb_id.in_(purged_kb_ids)]
        if "org_id" in table.c:
            conditions.append(table.c.org_id == org_id)
        result = await session.execute(delete(table).where(*conditions))
        deleted = int(result.rowcount or 0)
        if deleted:
            category.deleted_count += deleted
            category.detail[table.name] = deleted
    return category


async def _verify_no_residuals(
    session: AsyncSession, *, org_id: UUID, purged_kb_ids: list[UUID]
) -> None:
    """删完后在同一事务内复核 a/b 类归零(口径同 purge 的 residuals 兜底);
    仍有残留说明删除不完整,抛错让整个事务回滚。"""
    residuals: dict[str, int] = {}
    chunk_count = int(
        await session.scalar(
            select(func.count())
            .select_from(KbChunk)
            .join(SourceDocument, KbChunk.source_doc_id == SourceDocument.id)
            .where(
                SourceDocument.lifecycle_status
                == DocumentLifecycleStatus.PURGED.value
            )
        )
        or 0
    )
    if chunk_count:
        residuals["kb_chunks(purged_documents)"] = chunk_count
    for table in _kb_child_tables():
        if table.name in _PRESERVED_KB_TABLES or not purged_kb_ids:
            continue
        conditions = [table.c.kb_id.in_(purged_kb_ids)]
        if "org_id" in table.c:
            conditions.append(table.c.org_id == org_id)
        count = int(
            await session.scalar(
                select(func.count()).select_from(table).where(*conditions)
            )
            or 0
        )
        if count:
            residuals[table.name] = count
    if residuals:
        raise OrphanCleanupError(
            "orphan_cleanup_residuals",
            "清理后仍存在残留,已整体回滚",
            detail={"residuals": residuals},
        )


async def cleanup_orphans(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    expected: OrphanCleanupExpectation,
) -> OrphanCleanupResult:
    """管理员显式触发的孤儿清理:重扫比对期望 → 单事务删除 a/b 类 → 复核归零
    → 落审计 → commit;任何一步失败整体回滚并抛 OrphanCleanupError。"""
    try:
        # 执行前重跑扫描拿新鲜报告,与前端展示的期望比对,防误删新产生的孤儿
        report = await scan_orphans(session)
        _require_fresh_report(report, expected)

        result = OrphanCleanupResult(executed_at=datetime.now(UTC))
        # a 类:purged 文档残留 chunk
        result.purged_document_chunks = await _cleanup_purged_document_chunks(
            session
        )
        # b 类:purged 知识库残留子表行
        purged_kb_ids = list(
            (
                await session.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.lifecycle_status
                        == KnowledgeBaseLifecycleStatus.PURGED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        result.purged_kb_residual_rows = await _cleanup_purged_kb_residual_rows(
            session, org_id=org_id, purged_kb_ids=purged_kb_ids
        )
        # 同事务复核 a/b 归零,失败即回滚
        await _verify_no_residuals(
            session, org_id=org_id, purged_kb_ids=purged_kb_ids
        )
        # c 类:不直接删对象存储,标记 skipped 走 operation 重试通道(见模块头注释)
        if report.purge_registry_stale_objects.count:
            result.purge_registry_stale_objects = OrphanCleanupCategoryResult(
                skipped=True, skip_reason=_REGISTRY_SKIP_REASON
            )

        session.add(
            AuditLog(
                org_id=org_id,
                user_id=actor_id,
                action=CLEANUP_AUDIT_ACTION,
                entity_type="knowledge_base",
                entity_id=None,
                detail={
                    **result.to_dict(),
                    "expected": {
                        "purged_document_chunks": expected.purged_document_chunks,
                        "purged_kb_residual_rows": expected.purged_kb_residual_rows,
                        "purge_registry_stale_objects": (
                            expected.purge_registry_stale_objects
                        ),
                    },
                },
            )
        )
        await session.commit()
    except OrphanCleanupError:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        raise OrphanCleanupError(
            "orphan_cleanup_failed", "孤儿清理执行失败,已整体回滚"
        ) from exc
    logger.info(
        "KB 孤儿清理:org=%s 共删除 %s 行",
        org_id,
        result.total_deleted,
        extra={"org_id": str(org_id), "deleted": result.total_deleted},
    )
    return result


async def audit_org_orphans(session: AsyncSession, *, org_id: UUID) -> OrphanReport:
    """单 org 巡检:发现孤儿写 AuditLog + warning;零发现只 debug,不落审计。"""
    report = await scan_orphans(session)
    if report.total == 0:
        logger.debug("KB 孤儿巡检:org=%s 无发现", org_id)
        return report
    session.add(
        AuditLog(
            org_id=org_id,
            user_id=None,  # 系统巡检,无操作人
            action=AUDIT_ACTION,
            entity_type="knowledge_base",
            entity_id=None,
            detail=report.to_dict(),
        )
    )
    await session.commit()
    logger.warning(
        "KB 孤儿巡检:org=%s 发现 %s 条孤儿残留",
        org_id,
        report.total,
        extra={"org_id": str(org_id), "count": report.total},
    )
    return report


async def sweep_kb_orphans(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """全租户单轮孤儿巡检,返回本轮发现的孤儿总数。

    per-org 隔离:单 org 失败只记日志跳过,不中断整轮(同 expiry 口径)。
    """
    async with session_factory() as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())
    total = 0
    for org_id in org_ids:
        session = org_session(session_factory, org_id)
        try:
            report = await audit_org_orphans(session, org_id=org_id)
            total += report.total
        except Exception:
            await session.rollback()
            logger.exception("KB 孤儿巡检失败(org=%s),跳过继续", org_id)
        finally:
            await session.close()
    return total


async def run_kb_orphan_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻天级循环(仅 inline 模式 lifespan 托管;celery 模式走 beat,避免双跑):
    启动先跑一次,失败只记日志下轮重试。"""
    # 配置项由主控统一补进 config.py,补上前用 getattr 兜底默认一天一轮
    interval = getattr(get_settings(), "kb_orphan_audit_interval_seconds", 86400)
    while not stop_event.is_set():
        try:
            count = await sweep_kb_orphans(session_factory)
            if count:
                logger.warning(
                    "KB 孤儿巡检:本轮共发现 %s 条孤儿残留", count, extra={"count": count}
                )
        except Exception:
            logger.exception("KB 孤儿巡检失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass


__all__ = [
    "AUDIT_ACTION",
    "CLEANUP_AUDIT_ACTION",
    "OrphanCategory",
    "OrphanCleanupCategoryResult",
    "OrphanCleanupError",
    "OrphanCleanupExpectation",
    "OrphanCleanupResult",
    "OrphanReport",
    "audit_org_orphans",
    "cleanup_orphans",
    "run_kb_orphan_sweeper",
    "scan_orphans",
    "sweep_kb_orphans",
]
