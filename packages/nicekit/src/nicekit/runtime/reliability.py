"""任务可靠性:孤儿任务启动恢复 + 周期超时清扫(MIGRATION-PLAN §4 / §5.7)。

背景(TF services/reliability.py 文件头裁决,原样保留):
inline 派发(BackgroundTasks)的任务随进程死亡,queued/running 永久卡死;
celery 派发的任务在 worker 崩溃后同样可能留下 stale running。

- ``recover_orphan_tasks``:进程启动时执行一次。只回收"执行主体已消亡"的任务
  (celery_task_id 为空 = inline 派发,进程重启即任务消亡)。
- ``sweep_stale_tasks``:周期执行。活跃状态停留超阈值一律回收(不区分执行主体,
  兜住 worker 崩溃)。

**SDK 化改造**:TF 版本深度耦合业务表(pipeline_runs / render_jobs),这里框架化为
``RecoverableTask`` 协议 + 注册表:

    class RecoverableTask(Protocol):
        name: str
        async def list_orphans(session, *, org_id, cutoff) -> Sequence
        async def fail(session, orphans, *, org_id, error, now) -> dict[str, int]
        async def requeue(orphans, *, org_id) -> int

保留的现实约束骨架:无 BYPASSRLS 角色(migrator 同受 FORCE 约束)→ 先读
``organizations``(身份表,无 RLS),再逐 org 走 ``org_session`` 操作业务表。

SDK 自带两个实现并默认注册:``AgentRunRecovery``(卡住的会话 → terminalize)
与 ``KbIngestRunRecovery``(ingest lease 过期 / 存量 parsing 超时 → 重排)。
宿主的业务任务类型自行 ``register_recoverable_task()``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.models.tenancy import Organization

logger = logging.getLogger(__name__)

ORPHAN_ERROR = "服务重启导致任务中断,请重试"
STALE_ERROR = "任务超时未完成,已自动标记失败,请重试"


@runtime_checkable
class RecoverableTask(Protocol):
    """一类可恢复任务。三个动作都在**单个 org 的 org_session 事务**里被调用。

    - ``list_orphans``:``cutoff=None`` = 启动恢复口径(只挑执行主体已消亡的),
      ``cutoff`` 非空 = 周期清扫口径(活跃状态早于 cutoff 的一律算 stale);
    - ``fail``:把它们落终态并按需通知,返回计数字典(键会并入总计数);
    - ``requeue``:``fail`` 提交后调用,重新派发可继续执行的任务,返回重排条数。
      派发在事务外发生(celery send_task 不该被数据库事务卡住)。
    """

    name: str

    async def list_orphans(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        cutoff: datetime | None,
    ) -> Sequence[Any]: ...

    async def fail(
        self,
        session: AsyncSession,
        orphans: Sequence[Any],
        *,
        org_id: UUID,
        error: str,
        now: datetime,
    ) -> dict[str, int]: ...

    async def requeue(self, orphans: Sequence[Any], *, org_id: UUID) -> int: ...


_tasks: dict[str, RecoverableTask] = {}


def register_recoverable_task(task: RecoverableTask) -> None:
    """登记(或按 name 覆盖)一类可恢复任务。"""
    _tasks[task.name] = task


def recoverable_tasks() -> tuple[RecoverableTask, ...]:
    return tuple(_tasks.values())


def reset_recoverable_tasks() -> None:
    """清空并恢复 SDK 内置实现(测试 / 重新装配用)。"""
    _tasks.clear()
    register_recoverable_task(AgentRunRecovery())
    register_recoverable_task(KbIngestRunRecovery())


async def _list_org_ids(session_factory: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """organizations 无 RLS,可用普通会话直读(逐租户扫描的入口)。"""
    async with session_factory() as session:
        return list((await session.execute(select(Organization.id))).scalars().all())


async def _run_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    error: str,
    cutoff: datetime | None,
    now: datetime,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for org_id in await _list_org_ids(session_factory):
        session = org_session(session_factory, org_id)
        pending: list[tuple[RecoverableTask, Sequence[Any]]] = []
        try:
            for task in recoverable_tasks():
                try:
                    orphans = await task.list_orphans(session, org_id=org_id, cutoff=cutoff)
                except Exception:
                    logger.exception("恢复任务扫描失败 task=%s org=%s", task.name, org_id)
                    continue
                if not orphans:
                    continue
                try:
                    result = await task.fail(
                        session, orphans, org_id=org_id, error=error, now=now
                    )
                except Exception:
                    logger.exception("恢复任务落终态失败 task=%s org=%s", task.name, org_id)
                    continue
                for key, value in (result or {}).items():
                    counts[key] = counts.get(key, 0) + int(value)
                pending.append((task, orphans))
            await session.commit()
        finally:
            await session.close()
        # 事务提交后再派发:celery send_task 不该被数据库事务持有
        for task, orphans in pending:
            try:
                requeued = await task.requeue(orphans, org_id=org_id)
            except Exception:
                logger.exception("恢复任务重排失败 task=%s org=%s", task.name, org_id)
                continue
            if requeued:
                counts["requeued"] = counts.get("requeued", 0) + int(requeued)
    return counts


async def recover_orphan_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """启动恢复:执行主体已消亡的活跃任务全部落终态(进程已重启,不可能再执行)。"""
    counts = await _run_cycle(
        session_factory,
        error=ORPHAN_ERROR,
        cutoff=None,
        now=datetime.now(UTC),
    )
    if any(counts.values()):
        logger.warning("启动恢复:孤儿任务已回收", extra=counts)
    return counts


async def sweep_stale_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    """周期清扫:活跃状态停留超过阈值的任务一律回收(含 celery 派发,兜 worker 崩溃)。"""
    settings = get_settings()
    timeout = timeout_seconds or settings.stale_run_timeout_seconds
    now = datetime.now(UTC)
    return await _run_cycle(
        session_factory,
        error=STALE_ERROR,
        cutoff=now - timedelta(seconds=timeout),
        now=now,
    )


async def run_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻清扫循环(lifespan 托管):单轮失败只记日志,下轮重试。"""
    interval = get_settings().stale_sweep_interval_seconds
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event 置位,正常退出
        except TimeoutError:
            pass
        try:
            counts = await sweep_stale_tasks(session_factory)
            if any(counts.values()):
                logger.warning("周期清扫:stale 任务已回收", extra=counts)
        except Exception:
            logger.exception("stale 清扫失败,下轮重试")


# ---------------------------------------------------------------------------
# SDK 自带实现 1:agent run(卡住的会话)
# ---------------------------------------------------------------------------


class AgentRunRecovery:
    """回收卡住的会话:释放互斥标记,并把中断的计划步骤与对话状态同步给用户。

    只清 ``active_run_id`` 的话,plan 会永久停在 in_progress、对话里也看不到失败,
    用户只能从通知里察觉任务挂了(TF 现场反馈的核心问题)。
    """

    name = "agent_run"

    async def list_orphans(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        cutoff: datetime | None,
    ) -> Sequence[Any]:
        from nicekit.models.chat import ChatSession

        conditions = [ChatSession.active_run_id.is_not(None)]
        if cutoff is not None:
            conditions.append(ChatSession.updated_at < cutoff)
        return (
            await session.execute(
                select(
                    ChatSession.id,
                    ChatSession.active_run_id,
                    ChatSession.user_id,
                    ChatSession.title,
                ).where(*conditions)
            )
        ).all()

    async def fail(
        self,
        session: AsyncSession,
        orphans: Sequence[Any],
        *,
        org_id: UUID,
        error: str,
        now: datetime,
    ) -> dict[str, int]:
        from nicekit.agent.terminal import terminalize_agent_run

        recovered = 0
        for chat in orphans:
            if chat.active_run_id is None:
                continue
            terminal = await terminalize_agent_run(
                session,
                chat_session_id=chat.id,
                run_id=chat.active_run_id,
                stop="error",
                reason=error,
                add_terminal_event=True,
            )
            if not terminal.committed:
                continue
            recovered += 1
            await self._notify(session, org_id=org_id, chat=chat, error=error)
        return {"chat_sessions": recovered}

    async def requeue(self, orphans: Sequence[Any], *, org_id: UUID) -> int:
        return 0  # 中断的会话不自动续跑:续跑由用户或 session_goal 决定

    async def _notify(self, session: AsyncSession, *, org_id: UUID, chat: Any, error: str) -> None:
        try:  # 通知是附带效果,不可阻断回收
            from nicekit.capabilities import notify as notifier

            await notifier.notify(
                session,
                org_id=org_id,
                user_ids=[chat.user_id],
                kind="task.recovered",
                title=f"对话已中断:{chat.title}",
                body=error,
                link=f"/app/chat?session={chat.id}",
                email=False,  # 批量回收不发邮件,防轰炸
            )
        except Exception:
            logger.warning("会话回收通知发送失败,已跳过", exc_info=True)


# ---------------------------------------------------------------------------
# SDK 自带实现 2:KB ingest run(lease 过期 / 存量 parsing 超时)
# ---------------------------------------------------------------------------


class KbIngestRunRecovery:
    """KB 摄入恢复:过期 lease 与存量 PARSING 文档统一按 ingest_run 口径处理。

    与 agent run 不同,摄入是**可续跑**的(root run 幂等、分段有 lease),因此
    ``requeue`` 会把可恢复的 run 重新派发回 dispatch。
    """

    name = "kb_ingest_run"

    async def list_orphans(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        cutoff: datetime | None,
    ) -> Sequence[Any]:
        from nicekit.kb.ingest_runs import (
            list_recoverable_ingest_runs,
            list_recoverable_typed_reextraction_runs,
            recover_stale_ingestion,
        )

        settings = get_settings()
        effective_cutoff = cutoff or (
            datetime.now(UTC) - timedelta(seconds=settings.stale_run_timeout_seconds)
        )
        ingestion = await recover_stale_ingestion(
            session, org_id=org_id, legacy_cutoff=effective_cutoff
        )
        ingest_run_ids = await list_recoverable_ingest_runs(
            session, org_id=org_id, cutoff=effective_cutoff
        )
        reextract_run_ids = await list_recoverable_typed_reextraction_runs(
            session, org_id=org_id, cutoff=effective_cutoff
        )
        if not (
            ingestion.expired_runs
            or ingestion.stale_documents
            or ingest_run_ids
            or reextract_run_ids
        ):
            return ()
        return (
            {
                "expired_runs": ingestion.expired_runs,
                "stale_documents": ingestion.stale_documents,
                "ingest_run_ids": list(ingest_run_ids),
                "reextract_run_ids": list(reextract_run_ids),
            },
        )

    async def fail(
        self,
        session: AsyncSession,
        orphans: Sequence[Any],
        *,
        org_id: UUID,
        error: str,
        now: datetime,
    ) -> dict[str, int]:
        # recover_stale_ingestion 已在 list_orphans 里落终态(它同时是扫描与回收),
        # 这里只汇总计数,避免重复执行同一段 UPDATE。
        payload = orphans[0]
        return {
            "ingest_runs": payload["expired_runs"],
            "source_documents": payload["stale_documents"],
        }

    async def requeue(self, orphans: Sequence[Any], *, org_id: UUID) -> int:
        from nicekit.runtime.dispatch import (
            dispatch_kb_ingest_run,
            dispatch_kb_reextract_run,
        )

        payload = orphans[0]
        requeued = 0
        for run_id in payload["ingest_run_ids"]:
            if await dispatch_kb_ingest_run(run_id, org_id):
                requeued += 1
        for run_id in payload["reextract_run_ids"]:
            if await dispatch_kb_reextract_run(run_id, org_id):
                requeued += 1
        return requeued


reset_recoverable_tasks()


__all__ = [
    "ORPHAN_ERROR",
    "STALE_ERROR",
    "AgentRunRecovery",
    "KbIngestRunRecovery",
    "RecoverableTask",
    "recover_orphan_tasks",
    "recoverable_tasks",
    "register_recoverable_task",
    "reset_recoverable_tasks",
    "run_sweeper",
    "sweep_stale_tasks",
]
