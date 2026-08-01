"""KB 文档过期提醒(prod-readiness-4 D4):天级扫描,复用通知系统。

逐 org(过 FORCE RLS)扫 expires_at 已过且未提醒过的文档,给 org 内 KB 治理
角色(ports.kb_notify_roles(),默认 org_admin)发站内信(不发邮件——文档过期
不紧急,且批量场景防轰炸),置 expiry_notified_at 防重复;PATCH 文档重设有效期
会清掉该标记,允许再次提醒。通知走 Notifier 协议,不可用时降级为不通知。
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb import ports
from nicekit.models.kb import SourceDocument
from nicekit.models.tenancy import Organization

logger = logging.getLogger(__name__)

EXPIRY_NOTIFY_KIND = "kb.doc_expired"


async def _sweep_org(session: AsyncSession, org_id) -> int:
    now = datetime.now(UTC)
    docs = (
        await session.execute(
            select(SourceDocument).where(
                SourceDocument.expires_at < now,
                SourceDocument.expiry_notified_at.is_(None),  # type: ignore[union-attr]
            )
        )
    ).scalars().all()
    if not docs:
        return 0
    for doc in docs:
        await ports.notify_org_roles(
            session,
            org_id=org_id,
            kind=EXPIRY_NOTIFY_KIND,
            title=f"知识文档已过期:{doc.filename}",
            body=(
                f"文档「{doc.filename}」的有效期已过"
                f"({doc.expires_at:%Y-%m-%d}),其派生条目在检索中"
                "已标注为陈旧(stale)。请上传新版或调整有效期。"
            ),
            email=False,
        )
        doc.expiry_notified_at = now
        session.add(doc)
    await session.commit()
    return len(docs)


async def sweep_expired_kb_docs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """全租户单轮过期扫描(celery task / inline 循环共用),返回本轮新提醒的文档数。

    per-org 隔离:单 org 失败只记日志跳过,不中断整轮。
    """
    async with session_factory() as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())
    total = 0
    for org_id in org_ids:
        session = org_session(session_factory, org_id)
        try:
            total += await _sweep_org(session, org_id)
        except Exception:
            await session.rollback()
            logger.exception("KB 过期扫描失败(org=%s),跳过继续", org_id)
        finally:
            await session.close()
    return total


async def run_kb_expiry_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻天级循环(仅 inline 模式 lifespan 托管;celery 模式走 beat,避免双跑):
    启动先跑一次,失败只记日志下轮重试。"""
    interval = get_settings().kb_expiry_sweep_interval_seconds
    while not stop_event.is_set():
        try:
            count = await sweep_expired_kb_docs(session_factory)
            if count:
                logger.warning("KB 过期扫描:已提醒 %s 个文档", count, extra={"count": count})
        except Exception:
            logger.exception("KB 过期扫描失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
