"""运行中输入队列。

职责边界:队列的查询/入队/编辑/删除/重排/消费/跳过全部收敛在本模块,
API(chat.py)与 loop 接线(service.py)只调用这里,不各写一份队列 SQL。

核心规则:
- 只在安全检查点消费:run_loop 以 end_turn 收尾后,由 service 逐条取用;
- 消费时以 consumed_message_id 幂等转正式 user message(重复消费不再写行);
- run 以 cancelled/error 终止时剩余项标 skipped,不静默丢弃;
- queued 状态可编辑/删除/重排,consumed/skipped 为终态只读。

纯函数(next_position/reorder_positions/needs_consumption/queue_event_payload)
承载规则判断,便于无 DB 单测;async 函数只做薄的取数与落库。
"""

import logging
from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.db import org_session
from nicekit.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatRunInput,
    ChatRunInputStatus,
    ChatSession,
)

logger = logging.getLogger(__name__)

# API 侧队列事件(input.queued/updated/deleted)没有活跃的 run SSE emit 可用,
# 直接落 chat_events。run 内 emit 的 seq 从 1 递增(单 run 事件量远小于该基数),
# API 侧用独立高位段分配 seq,避免与在跑 run 的事件撞 (session,run,seq) 唯一键。
QUEUE_EVENT_SEQ_BASE = 1_000_000


# ---------------------------------------------------------------------------
# 纯函数:规则判断(无 DB,可直接单测)
# ---------------------------------------------------------------------------


def next_position(existing_positions: Iterable[int]) -> int:
    """入队位置 = 当前队列 max+1;空队列从 1 开始。"""
    return max(existing_positions, default=0) + 1


def is_editable(item: ChatRunInput) -> bool:
    """仅 queued 状态可编辑/删除/重排;consumed/skipped 是终态。"""
    return item.status == ChatRunInputStatus.QUEUED.value


def needs_consumption(item: ChatRunInput) -> bool:
    """幂等判定:已带 consumed_message_id 的项不再重复写正式消息。"""
    return not (
        item.status == ChatRunInputStatus.CONSUMED.value
        and item.consumed_message_id is not None
    )


def reorder_positions(
    items: Sequence[ChatRunInput], ordered_ids: Sequence[UUID]
) -> list[tuple[UUID, int]]:
    """按用户给定顺序重排队列,返回 (id, new_position) 列表。

    要求 ids 恰好是当前全部 queued 项的一个排列(多给/少给/重复都拒绝),
    避免并发消费后基于过期快照的重排悄悄丢项。
    """
    queued = [item for item in items if is_editable(item)]
    current_ids = {item.id for item in queued}
    if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != current_ids:
        raise ValueError("ids 必须恰好包含当前全部排队中的输入")
    return [(input_id, index + 1) for index, input_id in enumerate(ordered_ids)]


def queue_event_payload(
    event_type: str,
    item: ChatRunInput,
    *,
    message_id: UUID | None = None,
    reason: str | None = None,
) -> dict:
    """input.* 事件统一载荷:前端据此维护排队卡片状态。"""
    payload = {
        "type": event_type,
        "queued_input_id": str(item.id),
        "session_id": str(item.session_id),
        "run_id": str(item.run_id),
        "position": item.position,
        "status": item.status,
        "content": item.content,
        # 这条输入自带的本轮临时工具(空列表 = 没勾),前端排队卡片据此显示徽标
        "runtime_tools": list(item.runtime_tools or []),
    }
    if message_id is not None:
        payload["message_id"] = str(message_id)
    if reason is not None:
        payload["reason"] = reason
    return payload


# ---------------------------------------------------------------------------
# DB 操作:API 层(入队/列表/编辑/删除/重排)
# ---------------------------------------------------------------------------


async def enqueue_input(
    session: AsyncSession,
    *,
    chat: ChatSession,
    run_id: UUID,
    user_id: UUID,
    content: str,
    runtime_tools: Sequence[str] | None = None,
) -> ChatRunInput:
    """把运行中收到的用户消息写入队列(不 commit,调用方掌控事务)。

    runtime_tools 是这条输入自带的"本轮临时工具"(见 runtime_tools.py):
    随行落库,消费到它时才解析放行,只作用于它那一轮。
    """
    positions = (
        (
            await session.execute(
                select(ChatRunInput.position).where(
                    ChatRunInput.session_id == chat.id,
                    ChatRunInput.run_id == run_id,
                    ChatRunInput.status == ChatRunInputStatus.QUEUED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    item = ChatRunInput(
        org_id=chat.org_id,
        session_id=chat.id,
        run_id=run_id,
        user_id=user_id,
        content=content,
        position=next_position(positions),
        runtime_tools=list(runtime_tools or []),
    )
    session.add(item)
    await session.flush()
    return item


async def list_queued_inputs(
    session: AsyncSession, session_id: UUID
) -> list[ChatRunInput]:
    """会话当前排队中的输入(按 position 升序;终态项不返回)。"""
    return list(
        (
            await session.execute(
                select(ChatRunInput)
                .where(
                    ChatRunInput.session_id == session_id,
                    ChatRunInput.status == ChatRunInputStatus.QUEUED.value,
                )
                .order_by(ChatRunInput.position, ChatRunInput.created_at)
            )
        )
        .scalars()
        .all()
    )


async def get_input(
    session: AsyncSession, session_id: UUID, input_id: UUID
) -> ChatRunInput | None:
    return (
        await session.execute(
            select(ChatRunInput).where(
                ChatRunInput.id == input_id,
                ChatRunInput.session_id == session_id,
            )
        )
    ).scalar_one_or_none()


async def record_queue_event(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    payload: dict,
) -> None:
    """API 侧队列事件 best-effort 落 chat_events(无活跃 SSE 通道时的审计与重放补充)。

    与 run_turn_events.emit 的落库同一口径:失败只记日志,不阻断队列操作本身。
    seq 用独立高位段(QUEUE_EVENT_SEQ_BASE 起),不与在跑 run 的实时事件竞争编号。
    """
    session = org_session(session_factory, org_id)
    try:
        session_id = UUID(str(payload["session_id"]))
        run_id = UUID(str(payload["run_id"]))
        max_seq = (
            await session.execute(
                select(func.max(ChatEvent.seq)).where(
                    ChatEvent.session_id == session_id,
                    ChatEvent.run_id == run_id,
                    ChatEvent.seq >= QUEUE_EVENT_SEQ_BASE,
                )
            )
        ).scalar_one_or_none()
        seq = (max_seq or QUEUE_EVENT_SEQ_BASE) + 1
        session.add(
            ChatEvent(
                org_id=org_id,
                session_id=session_id,
                run_id=run_id,
                seq=seq,
                payload={**payload, "seq": seq},
            )
        )
        await session.commit()
    except Exception:
        logger.exception("队列事件落库失败 payload=%s", payload.get("type"))
        await session.rollback()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# DB 操作:loop 接线(收编/消费/跳过)
# ---------------------------------------------------------------------------


async def adopt_leftover_inputs(
    session: AsyncSession, *, session_id: UUID, run_id: UUID
) -> list[ChatRunInput]:
    """新 run 启动时收编上一 run 遗留的 queued 项(不 commit)。

    上一 run 停在 confirm/ask_user(或 max_turns)时队列被刻意保留;
    实现取"收编进新 run 的队列"而非"启动时立即消费":遗留项改挂新 run_id
    并整体排在本轮新入队项之前,在新 run 的 end_turn 检查点按原顺序消费。
    这样只有一条消费路径(检查点),历史顺序仍是 用户可见消息 -> 排队补充,
    且新 run 若 cancelled/error,遗留项同样走 skipped,不会静默丢失。
    """
    leftovers = (
        (
            await session.execute(
                select(ChatRunInput)
                .where(
                    ChatRunInput.session_id == session_id,
                    ChatRunInput.status == ChatRunInputStatus.QUEUED.value,
                    ChatRunInput.run_id != run_id,
                )
                .order_by(ChatRunInput.position, ChatRunInput.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for index, item in enumerate(leftovers):
        item.run_id = run_id
        item.position = index + 1
        session.add(item)
    return list(leftovers)


async def claim_next_queued_input(
    session: AsyncSession, *, session_id: UUID, run_id: UUID
) -> ChatRunInput | None:
    """安全检查点取队首(行锁至消费 commit,阻住并发的编辑/删除)。"""
    return (
        await session.execute(
            select(ChatRunInput)
            .where(
                ChatRunInput.session_id == session_id,
                ChatRunInput.run_id == run_id,
                ChatRunInput.status == ChatRunInputStatus.QUEUED.value,
            )
            .order_by(ChatRunInput.position, ChatRunInput.created_at)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def consume_queued_input(
    session: AsyncSession,
    *,
    chat: ChatSession,
    item: ChatRunInput,
    sequence: int,
) -> ChatMessage:
    """把队列项幂等转为正式 user message 并 commit(消费即检查点,必须落盘)。

    幂等:重复消费(如客户端重放)直接返回已写入的消息行;
    (session_id, sequence) 唯一约束兜底防双写。
    """
    if not needs_consumption(item):
        existing = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.id == item.consumed_message_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    message = ChatMessage(
        org_id=chat.org_id,
        session_id=chat.id,
        sequence=sequence,
        role="user",
        content=item.content,
        run_id=item.run_id,
    )
    session.add(message)
    await session.flush()
    item.status = ChatRunInputStatus.CONSUMED.value
    item.consumed_message_id = message.id
    session.add(item)
    await session.commit()
    return message


async def skip_queued_inputs(
    session: AsyncSession,
    *,
    session_id: UUID,
    run_id: UUID,
    reason: str,
) -> list[ChatRunInput]:
    """run 终态(cancelled/error)时把剩余 queued 项全部标 skipped 并 commit。"""
    items = (
        (
            await session.execute(
                select(ChatRunInput)
                .where(
                    ChatRunInput.session_id == session_id,
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
    for item in items:
        item.status = ChatRunInputStatus.SKIPPED.value
        session.add(item)
    if items:
        await session.commit()
    return list(items)
