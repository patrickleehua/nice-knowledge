"""Idempotent terminal-state convergence for one Agent chat run.

与 TF(backend/app/services/agent/terminal.py)的唯一差异:TF 在这里直接
UPDATE PipelineRun/PipelineStep 收敛业务任务;SDK 不认识宿主的任务表,改为
`TerminalFinalizer` 注册点——宿主注册"拿到这批 cancellable_resources 该怎么
收敛"的实现,SDK 只负责在同一个事务里、在同一个 compare-and-set 保护下调用它。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.run_inputs import queue_event_payload
from nicekit.domain.agent_plan import mark_plan_interrupted
from nicekit.models.chat import (
    ChatMessage,
    ChatRunInput,
    ChatRunInputStatus,
    ChatSession,
)

logger = logging.getLogger(__name__)

_REASON_MAX_CHARS = 500


@dataclass(frozen=True, slots=True)
class TerminalContext:
    """一次终结的全部事实,交给宿主的 finalizer 决定怎么收敛外部资源。"""

    chat_session_id: UUID
    org_id: UUID
    run_id: UUID
    stop: str  # cancelled / error / …
    reason: str
    cancellable_resources: tuple[str, ...]
    now: datetime


# 宿主注册的终结实现:在 terminalize_agent_run 的同一事务内执行,不要自行 commit。
TerminalFinalizer = Callable[[AsyncSession, TerminalContext], Awaitable[None]]

_finalizers: list[TerminalFinalizer] = []


def register_terminal_finalizer(finalizer: TerminalFinalizer) -> TerminalFinalizer:
    """登记一个终结回调(可多次调用,按注册顺序执行)。"""
    _finalizers.append(finalizer)
    return finalizer


def reset_terminal_finalizers() -> None:
    """清空已注册的终结回调(测试 / 重新装配用)。"""
    _finalizers.clear()


@dataclass(frozen=True, slots=True)
class AgentTerminalization:
    committed: bool
    queue_events: tuple[dict, ...] = ()
    plan: list | None = None


def _visible_message(stop: str, reason: str) -> str:
    if stop == "cancelled":
        return (
            f"本轮执行已取消（{reason}）。未完成的步骤和排队输入已停止；"
            "如需继续，请明确发起下一步。"
        )
    return (
        f"⚠️ 本轮执行意外中断并已终止（{reason}）。未完成的步骤已标记为失败；"
        "请核实外部副作用后再决定是否重试。"
    )


async def terminalize_agent_run(
    session: AsyncSession,
    *,
    chat_session_id: UUID,
    run_id: UUID,
    stop: str,
    reason: str,
    cancellable_resources: tuple[str, ...] = (),
    add_visible_message: bool = True,
) -> AgentTerminalization:
    """Commit the first terminal outcome and make later attempts no-ops.

    ``chat_sessions.active_run_id`` is the compare-and-set guard for the Agent
    run. Host-owned resources converge through their registered finalizers,
    which carry their own active-state predicates. This lets worker cleanup,
    explicit cancellation, and stale recovery safely call the same operation
    without overwriting an already committed terminal state.
    """

    chat = (
        await session.execute(
            select(ChatSession)
            .where(ChatSession.id == chat_session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if chat is None or chat.active_run_id != run_id:
        await session.rollback()
        return AgentTerminalization(committed=False)

    terminal_reason = reason.strip()[:_REASON_MAX_CHARS] or stop
    now = datetime.now(UTC)
    if _finalizers:
        context = TerminalContext(
            chat_session_id=chat_session_id,
            org_id=chat.org_id,
            run_id=run_id,
            stop=stop,
            reason=terminal_reason,
            cancellable_resources=tuple(cancellable_resources),
            now=now,
        )
        for finalizer in list(_finalizers):
            try:
                await finalizer(session, context)
            except Exception:  # noqa: BLE001 - 宿主收尾失败不能挡住会话终结
                logger.exception(
                    "终结回调失败 session=%s run=%s", chat_session_id, run_id
                )

    queued = (
        (
            await session.execute(
                select(ChatRunInput)
                .where(
                    ChatRunInput.session_id == chat_session_id,
                    ChatRunInput.run_id == run_id,
                    ChatRunInput.status == ChatRunInputStatus.QUEUED.value,
                )
                .order_by(ChatRunInput.position, ChatRunInput.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    queue_events: list[dict] = []
    for item in queued:
        item.status = ChatRunInputStatus.SKIPPED.value
        session.add(item)
        queue_events.append(
            queue_event_payload(
                "input.skipped",
                item,
                reason=f"run_{stop}",
            )
        )

    plan = mark_plan_interrupted(chat.plan, reason=terminal_reason)
    if plan is not None:
        chat.plan = plan
    chat.pending_confirmation = None
    chat.pending_user_input = None
    chat.active_run_id = None
    chat.cancel_requested = False
    session.add(chat)

    if add_visible_message:
        next_sequence = (
            (
                await session.execute(
                    select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.session_id == chat_session_id
                    )
                )
            ).scalar()
            or 0
        ) + 1
        session.add(
            ChatMessage(
                org_id=chat.org_id,
                session_id=chat.id,
                sequence=next_sequence,
                role="assistant",
                content=_visible_message(stop, terminal_reason),
                run_id=run_id,
            )
        )

    await session.commit()
    return AgentTerminalization(
        committed=True,
        queue_events=tuple(queue_events),
        plan=plan,
    )
