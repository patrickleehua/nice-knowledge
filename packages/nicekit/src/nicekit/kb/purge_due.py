"""KB 归档保留期到期清理候选(defensible disposition):只提醒、绝不自动删。

对标 2026 主流处置实践:archived 库过了 `archived_at + retention_days` 后,
系统周期性识别可清理候选、跑一次删除预检(preview),把候选摘要以站内信
推给 org_admin,由管理员到"知识库设置 → 危险操作"里人工审阅并决定是否
提交永久清理。本模块**不触发任何删除**——purge 仍必须走 lifecycle.py 的
name-confirmation + plan_hash + idempotency-key 人工流程。

- 候选口径:lifecycle_status='archived' 且保留期已过、且该库没有未完成的
  purge operation(pending/processing/failed/dead_letter 都算占用中——失败
  操作应在操作详情里重试/处理,而不是再收一份候选提醒)。
- 通知幂等:同一 kb 同一天(UTC)只发一轮,靠查询当天已存在的同 kind+link
  通知判断,不加新表/新列。
- 扫描循环:逐 org 过 FORCE RLS(org_session),单 org 失败只记日志跳过,
  模式与 expiry.run_kb_expiry_sweeper 一致。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb import ports
from nicekit.kb.lifecycle import (
    preview_knowledge_base_deletion,
    public_deletion_preview,
)
from nicekit.models.kb import (
    KnowledgeBase,
    KnowledgeBaseLifecycleOperation,
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecycleStatus,
)
from nicekit.models.tenancy import AuditLog, Notification, Organization

logger = logging.getLogger(__name__)

PURGE_DUE_NOTIFICATION_KIND = "kb.purge_due"
# 未完成 = 除 completed 外全部,与 lifecycle 工作队列索引口径一致
_UNFINISHED_OPERATION_STATUSES = frozenset(
    {
        KnowledgeBaseLifecycleOperationStatus.PENDING.value,
        KnowledgeBaseLifecycleOperationStatus.PROCESSING.value,
        KnowledgeBaseLifecycleOperationStatus.FAILED.value,
        KnowledgeBaseLifecycleOperationStatus.DEAD_LETTER.value,
    }
)


@dataclass(frozen=True, slots=True)
class PurgeDueCandidate:
    """一个保留期已到、可提交人工审批的清理候选(全部为公开口径字段)。"""

    kb_id: UUID
    org_id: UUID
    name: str
    archived_at: datetime
    due_at: datetime
    # 预检公开摘要:blockers 取 public_lifecycle_blockers 口径,不外泄内部清单
    plan_hash: str | None
    preview_complete: bool
    blockers: tuple[dict[str, Any], ...]


def _due_at(archived_at: datetime, retention_days: int) -> datetime:
    """保留期截止时刻;retention_days<=0 视为归档即到期(与 _purge_blockers 口径一致)。"""
    if retention_days <= 0:
        return archived_at
    return archived_at + timedelta(days=retention_days)


async def find_purge_due(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> list[PurgeDueCandidate]:
    """扫描当前会话可见范围内保留期已到期的 archived 库,返回候选清单。

    可见范围由会话的 RLS 上下文决定:org_session 内只扫本 org,
    全租户扫描由 sweep_purge_due_kbs 逐 org 驱动。
    """
    current_time = now or datetime.now(UTC)
    effective_retention_days = (
        get_settings().kb_document_purge_retention_days
        if retention_days is None
        else retention_days
    )
    cutoff = current_time - timedelta(days=max(effective_retention_days, 0))
    archived = (
        (
            await session.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.lifecycle_status
                    == KnowledgeBaseLifecycleStatus.ARCHIVED.value,
                    KnowledgeBase.archived_at.is_not(None),  # type: ignore[union-attr]
                    KnowledgeBase.archived_at <= cutoff,
                )
                .order_by(KnowledgeBase.archived_at)
            )
        )
        .scalars()
        .all()
    )
    # SQL 已按截止时间过滤,这里再用纯函数复核一遍(fail-closed,单测同口径)
    due = [
        kb
        for kb in archived
        if kb.archived_at is not None
        and _due_at(kb.archived_at, effective_retention_days) <= current_time
    ]
    if not due:
        return []
    # 已有未完成 purge operation 的库不再入选:清理流程已在途,提醒只会重复打扰
    busy_kb_ids = set(
        (
            await session.execute(
                select(KnowledgeBaseLifecycleOperation.kb_id).where(
                    KnowledgeBaseLifecycleOperation.kb_id.in_([kb.id for kb in due]),
                    KnowledgeBaseLifecycleOperation.status.in_(
                        _UNFINISHED_OPERATION_STATUSES
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    candidates: list[PurgeDueCandidate] = []
    for kb in due:
        if kb.id in busy_kb_ids:
            continue
        # lock=False:候选扫描只读,不抢 kb 行锁;预检失败(complete=False)也照常
        # 入选——管理员应知道"到期但预检暂不可用",blocker 里已带公开原因
        preview = await preview_knowledge_base_deletion(
            session,
            org_id=kb.org_id,
            kb_id=kb.id,
            now=current_time,
            retention_days=effective_retention_days,
            lock=False,
        )
        public = public_deletion_preview(preview)
        candidates.append(
            PurgeDueCandidate(
                kb_id=kb.id,
                org_id=kb.org_id,
                name=kb.name,
                archived_at=kb.archived_at,  # type: ignore[arg-type]
                due_at=_due_at(kb.archived_at, effective_retention_days),  # type: ignore[arg-type]
                plan_hash=public["plan_hash"],
                preview_complete=bool(public["complete"]),
                blockers=tuple(public["blockers"]),
            )
        )
    return candidates


def _candidate_body(candidate: PurgeDueCandidate, *, now: datetime) -> str:
    """通知正文:库名、归档时长、到期日期、blocker 概况 + 人工处置指引。"""
    archived_days = max((now - candidate.archived_at).days, 0)
    lines = [
        f"知识库「{candidate.name}」已归档 {archived_days} 天,"
        f"保留期于 {candidate.due_at:%Y-%m-%d} 到期。"
    ]
    if not candidate.preview_complete:
        lines.append("删除预检暂不可用,请稍后在页面上重试预检。")
    elif candidate.blockers:
        codes = "、".join(str(item["code"]) for item in candidate.blockers)
        lines.append(f"预检发现 {len(candidate.blockers)} 类阻塞项({codes}),清理前需先处理。")
    else:
        lines.append("预检未发现阻塞项,可以提交永久清理。")
    lines.append(
        "系统不会自动删除任何数据,请到知识库设置 → 危险操作中审阅并决定是否永久清理。"
    )
    return "".join(lines)


async def notify_purge_due(
    session: AsyncSession,
    candidates: list[PurgeDueCandidate],
    *,
    now: datetime | None = None,
) -> int:
    """给候选库所属 org 的 org_admin 发站内信(不发邮件,防批量轰炸)。

    幂等:同一 kb 同一天(UTC)不重复发,靠查询当天同 kind+link 的既有通知判断。
    返回本轮实际新通知的库数;随调用方事务,不 commit。
    """
    if not candidates:
        return 0
    current_time = now or datetime.now(UTC)
    today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    notified = 0
    for candidate in candidates:
        # link 即前端危险操作入口,同时充当"同一 kb"的去重键(不加新表/新列)
        link = f"/org/kb/{candidate.kb_id}?view=settings&tab=danger"
        already_sent_today = int(
            await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.org_id == candidate.org_id,
                    Notification.kind == PURGE_DUE_NOTIFICATION_KIND,
                    Notification.link == link,
                    Notification.created_at >= today_start,  # type: ignore[union-attr]
                )
            )
            or 0
        )
        if already_sent_today:
            continue
        sent = await ports.notify_org_roles(
            session,
            org_id=candidate.org_id,
            kind=PURGE_DUE_NOTIFICATION_KIND,
            title=f"知识库保留期已到期:{candidate.name}",
            body=_candidate_body(candidate, now=current_time),
            link=link,
            email=False,
        )
        if not sent:
            logger.warning(
                "KB 清理候选无收件人可通知(org=%s, kb=%s)",
                candidate.org_id,
                candidate.kb_id,
            )
            continue
        # 审计:系统动作(user_id=None),detail 全部为公开口径
        session.add(
            AuditLog(
                org_id=candidate.org_id,
                user_id=None,
                action="kb.purge_due_notified",
                entity_type="knowledge_base",
                entity_id=str(candidate.kb_id),
                detail={
                    "due_at": candidate.due_at.isoformat(),
                    "plan_hash": candidate.plan_hash,
                    "preview_complete": candidate.preview_complete,
                    "blocker_codes": [
                        str(item["code"]) for item in candidate.blockers
                    ],
                    "recipient_count": sent,
                },
            )
        )
        notified += 1
    return notified


async def sweep_purge_due_kbs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """全租户单轮扫描:逐 org 找候选并通知,返回本轮新通知的库数。

    per-org 隔离:单 org 失败只记日志跳过,不中断整轮(同 expiry 先例)。
    """
    async with session_factory() as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())
    total_candidates = 0
    total_notified = 0
    for org_id in org_ids:
        session = org_session(session_factory, org_id)
        try:
            candidates = await find_purge_due(session)
            total_candidates += len(candidates)
            if candidates:
                total_notified += await notify_purge_due(session, candidates)
                await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("KB 清理候选扫描失败(org=%s),跳过继续", org_id)
        finally:
            await session.close()
    # 每轮固定写一条汇总:有候选升 info,无候选 debug 免刷屏
    logger.log(
        logging.INFO if total_candidates else logging.DEBUG,
        "KB 清理候选扫描:候选 %s 个,新通知 %s 个",
        total_candidates,
        total_notified,
        extra={"candidates": total_candidates, "notified": total_notified},
    )
    return total_notified


async def run_kb_purge_due_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻周期循环(仅 inline 模式 lifespan 托管;celery 模式走 beat,避免双跑):
    启动先跑一次,失败只记日志下轮重试。"""
    # 配置项由主控统一加;未配置时兜底 6 小时
    interval = getattr(get_settings(), "kb_purge_due_interval_seconds", 21600)
    while not stop_event.is_set():
        try:
            await sweep_purge_due_kbs(session_factory)
        except Exception:
            logger.exception("KB 清理候选扫描失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass


__all__ = [
    "PURGE_DUE_NOTIFICATION_KIND",
    "PurgeDueCandidate",
    "find_purge_due",
    "notify_purge_due",
    "run_kb_purge_due_sweeper",
    "sweep_purge_due_kbs",
]
