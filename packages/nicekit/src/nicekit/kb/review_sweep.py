"""KB 事实 AI 审核定时扫描(自动化优先:AI 自动 + 例外升级人工)。

suggested FactClaim 不再等人点按钮:逐 org(过 FORCE RLS)分批调用
ai_review_fact_claims 复用三档分流(auto/ai/human);只挑 reviewed_by 为空的
claim(已 escalate 留人工的不重复送审 → 幂等且不重复烧 LLM)。单 org 失败
记日志跳过,不阻断其他 org。

例外升级:本轮出现 escalate/reject,或存量仍有待人工 claim 时,给
org_admin/operator 发站内信摘要(同日按标题幂等防轰炸,复用履约提醒模式;
不发邮件——批量场景非紧急)。

每轮还对本 org 的 awaiting_review 文档做一次审核收口(review_settlement):
待审事实与待审图片都清空的文档收回 completed,顺带回补历史存量。
"""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb import ports
from nicekit.kb.fact_review_ai import AiReviewSummary, ai_review_fact_claims
from nicekit.kb.review_settlement import settle_org_awaiting_documents
from nicekit.llm.service import LLMService
from nicekit.models.kb import (
    FactClaim,
    FactReviewStatus,
)
from nicekit.models.tenancy import Notification, Organization

logger = logging.getLogger(__name__)

NOTIFY_KIND = "kb.fact_review_pending"


async def _pending_human_count(session: AsyncSession) -> int:
    """存量待人工:AI 已 escalate(reviewed_by 已写)但仍 suggested 的 claim 数。"""
    value = (
        await session.execute(
            select(func.count())
            .select_from(FactClaim)
            .where(
                FactClaim.review_status == FactReviewStatus.SUGGESTED.value,
                FactClaim.reviewed_by.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).scalar_one()
    return int(value or 0)


async def _already_notified_today(
    session: AsyncSession, org_id: UUID, title: str
) -> bool:
    row = (
        await session.execute(
            select(Notification.id)
            .where(
                Notification.org_id == org_id,
                Notification.kind == NOTIFY_KIND,
                Notification.title == title,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _notify_pending_review(
    session: AsyncSession, org_id: UUID, summary: AiReviewSummary, pending_human: int
) -> bool:
    """例外升级人工:同日按标题幂等(履约提醒同款),返回是否新发。"""
    today = datetime.now(UTC).date()
    title = f"KB 事实审核待人工处理({today:%Y-%m-%d})"
    if await _already_notified_today(session, org_id, title):
        return False
    return bool(
        await ports.notify_org_roles(
            session,
            org_id=org_id,
            kind=NOTIFY_KIND,
            title=title,
            body=(
                f"AI 事实审核本轮升级 {summary.escalated} 条、驳回 {summary.rejected} 条,"
                f"当前共 {pending_human} 条事实待人工复核。"
                "请到知识库审核队列处理(升级原因见各条 review_note)。"
            ),
            email=False,
        )
    )


async def sweep_pending_fact_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_limit: int | None = None,
    llm: LLMService | None = None,
) -> dict[str, int]:
    """全租户单轮扫描(celery task / inline 循环共用),返回汇总计数。"""
    limit = batch_limit or get_settings().kb_ai_review_sweep_batch_size
    async with session_factory() as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())
    totals = {
        "orgs": len(org_ids), "reviewed": 0, "confirmed": 0, "rejected": 0,
        "escalated": 0, "failed": 0, "settled_docs": 0,
        "notified_orgs": 0, "failed_orgs": 0,
    }
    for org_id in org_ids:
        session = org_session(session_factory, org_id)
        try:
            summary = await ai_review_fact_claims(
                session, org_id=org_id, limit=limit, llm=llm, unreviewed_only=True
            )
            # 审完最后一条待审项的文档收回 completed(同时回补历史卡住的存量)
            totals["settled_docs"] += await settle_org_awaiting_documents(
                session, org_id=org_id
            )
            await session.commit()
            totals["reviewed"] += summary.reviewed
            totals["confirmed"] += summary.confirmed
            totals["rejected"] += summary.rejected
            totals["escalated"] += summary.escalated
            totals["failed"] += summary.failed
            pending_human = await _pending_human_count(session)
            if (summary.escalated or summary.rejected or pending_human) and (
                await _notify_pending_review(session, org_id, summary, pending_human)
            ):
                await session.commit()
                totals["notified_orgs"] += 1
        except Exception:
            await session.rollback()
            logger.exception("KB 事实 AI 审核扫描失败(org=%s),跳过继续", org_id)
            totals["failed_orgs"] += 1
        finally:
            await session.close()
    return totals


async def run_fact_review_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻循环(仅 inline 模式 lifespan 托管;celery 模式走 beat,避免双跑)。"""
    interval = get_settings().kb_ai_review_sweep_interval_seconds
    while not stop_event.is_set():
        try:
            totals = await sweep_pending_fact_reviews(session_factory)
            if totals["reviewed"] or totals["settled_docs"] or totals["failed_orgs"]:
                logger.info("KB 事实 AI 审核扫描完成", extra=totals)
        except Exception:
            logger.exception("KB 事实 AI 审核扫描失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
