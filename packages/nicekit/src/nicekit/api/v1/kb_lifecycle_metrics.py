"""知识库生命周期可观测指标(只读聚合)。

对标 2026 主流生命周期 KPI:
- 知识库按 lifecycle_status 的存量分布(active/archived/purge_pending/purged);
- 近 30/90 天归档与恢复操作次数(AuditLog action='kb.archive' / 'kb.restore',
  action 字符串以 services/kb/lifecycle.py 写入为准);
- purge operation 按 status / phase 的分布、失败与死信明细数、
  已完成操作的平均清理时长(completed_at - created_at,秒)。

本路由不在 router.py 注册,由主控接线。权限口径与 kb.py 生命周期端点一致:
org_admin / platform_admin。RLS 已按 org 隔离,这里仍显式过滤 org_id,
把共享库(RLS 下可见的他 org 库)排除在本 org 指标之外。
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import OrgContext, get_org_session, require_role
from nicekit.core.config import get_settings

# retry 准入的单一真源:与 POST /kb/lifecycle-operations/{id}/retry 完全一致
# (status 为 failed/dead_letter 且 last_error_code 在可重试清单内),不放宽
from nicekit.kb.lifecycle import _RETRYABLE_OPERATION_ERROR_CODES
from nicekit.kb.purge_due import find_purge_due
from nicekit.models.kb import (
    DocumentOperationType,
    KbDocumentOperation,
    KnowledgeBase,
    KnowledgeBaseLifecycleOperation,
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecyclePhase,
    KnowledgeBaseLifecycleStatus,
    SourceDocument,
)
from nicekit.models.tenancy import AuditLog, Role

router = APIRouter(prefix="/kb")

# 归档 / 恢复审计动作(单一真源:services/kb/lifecycle.py 落库的 action 字符串)
ARCHIVE_ACTION = "kb.archive"
RESTORE_ACTION = "kb.restore"


def _value(value: object) -> str:
    """StrEnum 或裸字符串统一取值(测试内存对象持枚举,DB 行持字符串)。"""
    return str(getattr(value, "value", value))


class KbStatusCountsOut(BaseModel):
    """知识库按生命周期状态的存量分布。"""

    active: int = 0
    archived: int = 0
    purge_pending: int = 0
    purged: int = 0
    total: int = 0


class LifecycleActivityOut(BaseModel):
    """近 30/90 天归档与恢复次数(审计日志口径)。"""

    archives_30d: int = 0
    restores_30d: int = 0
    archives_90d: int = 0
    restores_90d: int = 0


class PurgeOperationMetricsOut(BaseModel):
    """purge operation 聚合:状态/阶段分布、失败与死信、平均清理时长。"""

    total: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    phase_counts: dict[str, int] = Field(default_factory=dict)
    failed_count: int = 0
    dead_letter_count: int = 0
    completed_count: int = 0
    # 已完成操作 completed_at - created_at 的平均秒数;无完成记录为 null
    avg_completion_seconds: float | None = None


class KbLifecycleMetricsOut(BaseModel):
    """GET /kb/lifecycle/metrics 响应契约(snake_case)。"""

    generated_at: datetime
    kb_status_counts: KbStatusCountsOut
    lifecycle_activity: LifecycleActivityOut
    purge_operations: PurgeOperationMetricsOut


def _zero_status_counts() -> dict[str, int]:
    return {status.value: 0 for status in KnowledgeBaseLifecycleOperationStatus}


def _zero_phase_counts() -> dict[str, int]:
    return {phase.value: 0 for phase in KnowledgeBaseLifecyclePhase}


async def _kb_status_counts(
    session: AsyncSession, *, org_id: UUID
) -> KbStatusCountsOut:
    """按 lifecycle_status 分组计数(只统计本 org 拥有的库)。"""
    rows = (
        await session.execute(
            select(KnowledgeBase.lifecycle_status, func.count())
            .where(KnowledgeBase.org_id == org_id)
            .group_by(KnowledgeBase.lifecycle_status)
        )
    ).all()
    counts = {status.value: 0 for status in KnowledgeBaseLifecycleStatus}
    for status_value, count in rows:
        counts[_value(status_value)] = int(count)
    return KbStatusCountsOut(**counts, total=sum(counts.values()))


async def _lifecycle_activity(
    session: AsyncSession, *, org_id: UUID, now: datetime
) -> LifecycleActivityOut:
    """近 90 天审计行拉回后在内存里按 30/90 天窗口计数(行数量级很小)。"""
    since_90d = now - timedelta(days=90)
    since_30d = now - timedelta(days=30)
    rows = (
        await session.execute(
            select(AuditLog.action, AuditLog.created_at).where(
                AuditLog.org_id == org_id,
                AuditLog.action.in_((ARCHIVE_ACTION, RESTORE_ACTION)),
                AuditLog.created_at >= since_90d,
            )
        )
    ).all()
    activity = LifecycleActivityOut()
    for action, created_at in rows:
        if created_at is None or created_at < since_90d:
            continue
        in_30d = created_at >= since_30d
        if action == ARCHIVE_ACTION:
            activity.archives_90d += 1
            activity.archives_30d += int(in_30d)
        else:
            activity.restores_90d += 1
            activity.restores_30d += int(in_30d)
    return activity


async def _purge_operation_metrics(
    session: AsyncSession, *, org_id: UUID
) -> PurgeOperationMetricsOut:
    """purge operation 全量拉回聚合(每库一生仅个位数操作,量级可控)。"""
    rows = (
        await session.execute(
            select(
                KnowledgeBaseLifecycleOperation.status,
                KnowledgeBaseLifecycleOperation.phase,
                KnowledgeBaseLifecycleOperation.created_at,
                KnowledgeBaseLifecycleOperation.completed_at,
            ).where(KnowledgeBaseLifecycleOperation.org_id == org_id)
        )
    ).all()
    status_counts = _zero_status_counts()
    phase_counts = _zero_phase_counts()
    durations: list[float] = []
    for status_value, phase_value, created_at, completed_at in rows:
        status = _value(status_value)
        phase = _value(phase_value)
        status_counts[status] = status_counts.get(status, 0) + 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if (
            status == KnowledgeBaseLifecycleOperationStatus.COMPLETED.value
            and created_at is not None
            and completed_at is not None
        ):
            durations.append((completed_at - created_at).total_seconds())
    return PurgeOperationMetricsOut(
        total=len(rows),
        status_counts=status_counts,
        phase_counts=phase_counts,
        failed_count=status_counts[
            KnowledgeBaseLifecycleOperationStatus.FAILED.value
        ],
        dead_letter_count=status_counts[
            KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER.value
        ],
        completed_count=status_counts[
            KnowledgeBaseLifecycleOperationStatus.COMPLETED.value
        ],
        avg_completion_seconds=(
            round(sum(durations) / len(durations), 3) if durations else None
        ),
    )


@router.get("/lifecycle/metrics", response_model=KbLifecycleMetricsOut)
async def kb_lifecycle_metrics(
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> KbLifecycleMetricsOut:
    """聚合当前 org 的知识库生命周期指标(只读,无副作用)。"""
    now = datetime.now(UTC)
    return KbLifecycleMetricsOut(
        generated_at=now,
        kb_status_counts=await _kb_status_counts(session, org_id=ctx.org_id),
        lifecycle_activity=await _lifecycle_activity(
            session, org_id=ctx.org_id, now=now
        ),
        purge_operations=await _purge_operation_metrics(
            session, org_id=ctx.org_id
        ),
    )


# ---------------------------------------------------------------------------
# 生命周期管理台(console):board / operations / purge-due 三个只读聚合端点
# ---------------------------------------------------------------------------

# board 排序的状态优先级:archived > purge_pending > active > purged
_BOARD_STATUS_PRIORITY = {
    KnowledgeBaseLifecycleStatus.ARCHIVED.value: 0,
    KnowledgeBaseLifecycleStatus.PURGE_PENDING.value: 1,
    KnowledgeBaseLifecycleStatus.ACTIVE.value: 2,
    KnowledgeBaseLifecycleStatus.PURGED.value: 3,
}
# 时间字段缺失时的排序兜底(升序排最后 / 倒序排最后)
_SORT_LAST_ASC = datetime.max.replace(tzinfo=UTC)
_SORT_LAST_DESC = datetime.min.replace(tzinfo=UTC)
# operation 终态里可重试的状态集合(kb_purge 与 document 口径一致)
_RETRYABLE_STATUSES = frozenset(
    {
        KnowledgeBaseLifecycleOperationStatus.FAILED.value,
        KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER.value,
    }
)


class KbBoardLatestOperationOut(BaseModel):
    """看板行内嵌的最近一次 KB purge operation 摘要。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str
    phase: str
    requested_at: datetime | None
    completed_at: datetime | None
    last_error_message: str | None


class KbBoardItemOut(BaseModel):
    """看板一行 = 一个知识库的生命周期状态与保留期信息。"""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    name: str
    lifecycle_status: str
    archived_at: datetime | None
    purged_at: datetime | None
    retention_due_at: datetime | None
    purge_due: bool
    latest_operation: KbBoardLatestOperationOut | None


class KbLifecycleBoardOut(BaseModel):
    """GET /kb/lifecycle/board 响应契约。"""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    purge_worker_enabled: bool
    retention_days: int
    items: list[KbBoardItemOut]


class UnifiedOperationOut(BaseModel):
    """统一操作队列一行:KB 永久清理或文档级操作。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: str  # kb_purge | document
    kb_id: UUID
    kb_name: str
    target: str
    status: str
    phase: str | None
    requested_at: datetime | None
    completed_at: datetime | None
    last_error_message: str | None
    retryable: bool


class KbLifecycleOperationsOut(BaseModel):
    """GET /kb/lifecycle/operations 响应契约。"""

    model_config = ConfigDict(extra="forbid")

    purge_worker_enabled: bool
    items: list[UnifiedOperationOut]


class PurgeDueBlockerOut(BaseModel):
    """清理候选的阻塞项摘要:只保留 code 与 count。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    count: int


class PurgeDueItemOut(BaseModel):
    """一个保留期已到期的清理候选。"""

    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    name: str
    archived_at: datetime
    due_at: datetime
    plan_hash: str | None
    preview_complete: bool
    blockers: list[PurgeDueBlockerOut]


class KbPurgeDueOut(BaseModel):
    """GET /kb/lifecycle/purge-due 响应契约。"""

    model_config = ConfigDict(extra="forbid")

    items: list[PurgeDueItemOut]


def _retention_due_at(
    archived_at: datetime | None, retention_days: int
) -> datetime | None:
    """保留期截止时刻;retention_days<=0 视为归档即到期(与 purge_due 口径一致)。"""
    if archived_at is None:
        return None
    if retention_days <= 0:
        return archived_at
    return archived_at + timedelta(days=retention_days)


async def _latest_operations_by_kb(
    session: AsyncSession, *, org_id: UUID
) -> dict[UUID, KbBoardLatestOperationOut]:
    """一次查询取每个 kb 最近一次 lifecycle operation(窗口函数,不做 N+1)。

    模型无 requested_at 列,以 created_at 作为请求时刻(与落库语义一致)。
    """
    row_number = (
        func.row_number()
        .over(
            partition_by=KnowledgeBaseLifecycleOperation.kb_id,
            order_by=KnowledgeBaseLifecycleOperation.created_at.desc(),
        )
        .label("rn")
    )
    ranked = (
        select(
            KnowledgeBaseLifecycleOperation.kb_id,
            KnowledgeBaseLifecycleOperation.id,
            KnowledgeBaseLifecycleOperation.status,
            KnowledgeBaseLifecycleOperation.phase,
            KnowledgeBaseLifecycleOperation.created_at,
            KnowledgeBaseLifecycleOperation.completed_at,
            KnowledgeBaseLifecycleOperation.last_error_message,
            row_number,
        )
        .where(KnowledgeBaseLifecycleOperation.org_id == org_id)
        .subquery()
    )
    rows = (
        await session.execute(select(ranked).where(ranked.c.rn == 1))
    ).all()
    latest: dict[UUID, KbBoardLatestOperationOut] = {}
    for kb_id, op_id, op_status, phase, created_at, completed_at, error, *_ in rows:
        latest[kb_id] = KbBoardLatestOperationOut(
            id=op_id,
            status=_value(op_status),
            phase=_value(phase),
            requested_at=created_at,
            completed_at=completed_at,
            last_error_message=error,
        )
    return latest


@router.get("/lifecycle/board", response_model=KbLifecycleBoardOut)
async def kb_lifecycle_board(
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> KbLifecycleBoardOut:
    """每个知识库一行的生命周期看板(只读,只统计本 org 拥有的库)。"""
    settings = get_settings()
    retention_days = settings.kb_document_purge_retention_days
    now = datetime.now(UTC)
    kb_rows = (
        await session.execute(
            select(
                KnowledgeBase.id,
                KnowledgeBase.name,
                KnowledgeBase.lifecycle_status,
                KnowledgeBase.archived_at,
                KnowledgeBase.purged_at,
                KnowledgeBase.created_at,
            ).where(KnowledgeBase.org_id == ctx.org_id)
        )
    ).all()
    latest_by_kb = await _latest_operations_by_kb(session, org_id=ctx.org_id)

    items: list[tuple[datetime, KbBoardItemOut]] = []
    for kb_id, name, status_value, archived_at, purged_at, created_at in kb_rows:
        lifecycle_status = _value(status_value)
        # retention_due_at 仅对 archived 库有意义;purge_due 同口径
        is_archived = (
            lifecycle_status == KnowledgeBaseLifecycleStatus.ARCHIVED.value
        )
        retention_due_at = (
            _retention_due_at(archived_at, retention_days) if is_archived else None
        )
        purge_due = retention_due_at is not None and retention_due_at <= now
        sort_at = archived_at or created_at or _SORT_LAST_ASC
        items.append(
            (
                sort_at,
                KbBoardItemOut(
                    kb_id=kb_id,
                    name=name,
                    lifecycle_status=lifecycle_status,
                    archived_at=archived_at,
                    purged_at=purged_at,
                    retention_due_at=retention_due_at,
                    purge_due=purge_due,
                    latest_operation=latest_by_kb.get(kb_id),
                ),
            )
        )
    # 排序:purge_due 在前 → 状态优先级 → archived_at/created_at 升序
    items.sort(
        key=lambda entry: (
            not entry[1].purge_due,
            _BOARD_STATUS_PRIORITY.get(entry[1].lifecycle_status, 99),
            entry[0],
        )
    )
    return KbLifecycleBoardOut(
        generated_at=now,
        purge_worker_enabled=settings.kb_lifecycle_purge_worker_enabled,
        retention_days=retention_days,
        items=[item for _, item in items],
    )


def _document_operation_retryable(
    operation_type: str, status: str, retryable_flag: bool
) -> bool:
    """与 POST /kb/document-operations/{id}/retry 的准入完全一致,不放宽:

    - purge:failed/dead_letter 且行上 retryable 标记为真;
    - withdrawal:failed/dead_letter 即可;
    - reingestion:该 retry 端点直接 409(需走重新摄入入口),恒为不可重试。
    """
    if status not in _RETRYABLE_STATUSES:
        return False
    if operation_type == DocumentOperationType.PURGE.value:
        return bool(retryable_flag)
    return operation_type == DocumentOperationType.WITHDRAWAL.value


@router.get("/lifecycle/operations", response_model=KbLifecycleOperationsOut)
async def kb_lifecycle_operations(
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> KbLifecycleOperationsOut:
    """KB 永久清理 + 文档级操作的统一队列,按请求时间倒序截断 limit。"""
    items: list[UnifiedOperationOut] = []

    # kb_purge:lifecycle operation × 库名(target=库名,phase=其 phase)
    kb_operation_rows = (
        await session.execute(
            select(
                KnowledgeBaseLifecycleOperation.id,
                KnowledgeBaseLifecycleOperation.kb_id,
                KnowledgeBase.name,
                KnowledgeBaseLifecycleOperation.status,
                KnowledgeBaseLifecycleOperation.phase,
                KnowledgeBaseLifecycleOperation.created_at,
                KnowledgeBaseLifecycleOperation.completed_at,
                KnowledgeBaseLifecycleOperation.last_error_message,
                KnowledgeBaseLifecycleOperation.last_error_code,
            )
            .join(
                KnowledgeBase,
                KnowledgeBase.id == KnowledgeBaseLifecycleOperation.kb_id,
            )
            .where(KnowledgeBaseLifecycleOperation.org_id == ctx.org_id)
            .order_by(KnowledgeBaseLifecycleOperation.created_at.desc())
            .limit(limit)
        )
    ).all()
    for row in kb_operation_rows:
        (
            op_id,
            kb_id,
            kb_name,
            status_value,
            phase_value,
            created_at,
            completed_at,
            error_message,
            error_code,
        ) = row
        op_status = _value(status_value)
        items.append(
            UnifiedOperationOut(
                id=op_id,
                kind="kb_purge",
                kb_id=kb_id,
                kb_name=kb_name,
                target=kb_name,
                status=op_status,
                phase=_value(phase_value),
                requested_at=created_at,
                completed_at=completed_at,
                last_error_message=error_message,
                # 与 retry 端点准入一致:终态失败且错误码在可重试清单内
                retryable=(
                    op_status in _RETRYABLE_STATUSES
                    and error_code in _RETRYABLE_OPERATION_ERROR_CODES
                ),
            )
        )

    # document:文档操作 × 文档文件名 × 库名(target=文件名,phase=类型:stage)
    document_operation_rows = (
        await session.execute(
            select(
                KbDocumentOperation.id,
                KbDocumentOperation.kb_id,
                KnowledgeBase.name,
                SourceDocument.filename,
                KbDocumentOperation.operation_type,
                KbDocumentOperation.status,
                KbDocumentOperation.stage,
                KbDocumentOperation.created_at,
                KbDocumentOperation.completed_at,
                KbDocumentOperation.last_error,
                KbDocumentOperation.retryable,
            )
            .join(
                SourceDocument,
                SourceDocument.id == KbDocumentOperation.document_id,
            )
            .join(KnowledgeBase, KnowledgeBase.id == KbDocumentOperation.kb_id)
            .where(KbDocumentOperation.org_id == ctx.org_id)
            .order_by(KbDocumentOperation.created_at.desc())
            .limit(limit)
        )
    ).all()
    for row in document_operation_rows:
        (
            op_id,
            kb_id,
            kb_name,
            filename,
            type_value,
            status_value,
            stage,
            created_at,
            completed_at,
            last_error,
            retryable_flag,
        ) = row
        operation_type = _value(type_value)
        op_status = _value(status_value)
        items.append(
            UnifiedOperationOut(
                id=op_id,
                kind="document",
                kb_id=kb_id,
                kb_name=kb_name,
                target=filename,
                status=op_status,
                # 文档操作类型并入 phase(如 "purge:delete_objects"),无 stage 时只留类型
                phase=f"{operation_type}:{stage}" if stage else operation_type,
                requested_at=created_at,
                completed_at=completed_at,
                last_error_message=last_error,
                retryable=_document_operation_retryable(
                    operation_type, op_status, bool(retryable_flag)
                ),
            )
        )

    # 两类合并后按请求时间倒序,统一截断 limit
    items.sort(
        key=lambda item: item.requested_at or _SORT_LAST_DESC, reverse=True
    )
    return KbLifecycleOperationsOut(
        purge_worker_enabled=get_settings().kb_lifecycle_purge_worker_enabled,
        items=items[:limit],
    )


@router.get("/lifecycle/purge-due", response_model=KbPurgeDueOut)
async def kb_lifecycle_purge_due(
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> KbPurgeDueOut:
    """保留期已到期的清理候选清单(直接复用 purge_due.find_purge_due 的判定)。"""
    del ctx  # 权限校验用;候选范围由会话 RLS(org_session)限定
    candidates = await find_purge_due(session)
    return KbPurgeDueOut(
        items=[
            PurgeDueItemOut(
                kb_id=candidate.kb_id,
                name=candidate.name,
                archived_at=candidate.archived_at,
                due_at=candidate.due_at,
                plan_hash=candidate.plan_hash,
                preview_complete=candidate.preview_complete,
                # blocker 只保留 code 与 count,不透出 identifiers 等内部细节
                blockers=[
                    PurgeDueBlockerOut(
                        code=str(blocker["code"]),
                        count=int(blocker.get("count", 0)),
                    )
                    for blocker in candidate.blockers
                ],
            )
            for candidate in candidates
        ]
    )
