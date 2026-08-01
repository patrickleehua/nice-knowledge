"""会话上下文压缩:长会话把早前消息压成一条中文摘要,与工具结果双层预算互补。

双层预算解决"单轮工具输出重",这里解决"轮次多"的累积膨胀:turn 完成后在
后台评估水位,超过则把最新成功摘要之后的 older segment 压成新摘要
(增量合并既有摘要,新摘要始终是全量替身),历史注入时只保留
摘要 + covered_until_sequence 之后的消息。

约束(与 memory 既有裁决一致):摘要只是会话上下文的替身,不是业务真相;
消息行与完整工具输出(chat_messages.tool_output)永远保留可审计。
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from nicekit.core.db import org_session
from nicekit.llm.service import LLMService
from nicekit.models.chat import ChatMessage, ChatSessionSummary

logger = logging.getLogger(__name__)

# 水位 45000 token,估算口径 = 字符数/4(英文 JSON 的通用近似)。中文实际约
# 1 字符/1 token,该口径对中文偏低、触发偏晚——可以接受:最重的工具结果已被
# 双层预算先兜住,摘要压缩宁可晚触发也不过度压缩(摘要有信息损耗)。
COMPRESS_TOKEN_WATERMARK = 45000
# 最低消息数门槛:消息很少但字符量大(如单条巨型工具结果)时交给双层预算即可,
# 摘要压缩针对的是多轮累积,轮次不够时压缩收益小、信息损耗不划算。
COMPRESS_MIN_MESSAGES = 20
# 压缩时保留最近 15 条消息作 tail 不压:近期上下文是当前任务的直接依据,
# 必须原样保留;边界会再向前吸附到 user 行,实际保留条数 >= 15。
COMPRESS_KEEP_TAIL = 15
# 最近 3 条摘要全为 failed 时熔断跳过:摘要失败通常是环境性问题(路由/预算),
# 不熔断会让之后每个 turn 都烧一次注定失败的 LLM 调用。
COMPRESS_FAILURE_CIRCUIT = 3

# 复用默认会话路由:摘要与会话同模型域,免去额外 seed 一条 ModelRoute;
# 走 LLMService.generate_with_tools(tools=[])即可拿纯文本输出并复用降级链/审计。
# (TF 里写死 agent.workbench;SDK 默认 agent.default,宿主可经 set_summary_task 改。)
DEFAULT_SUMMARY_TASK = "agent.default"
SUMMARY_TASK = DEFAULT_SUMMARY_TASK

SUMMARY_SYSTEM_PROMPT = (
    "你是会话记录的压缩助手。把给定的早前对话压缩成一份中文备忘。"
    "必须保留:已做出的决策、用户明确提出的约束与偏好、关键事实与数字"
    "(数量/日期/金额/标识符等一律精确引用,不得改写或近似);"
    "以及尚未解决的问题;丢弃寒暄、过程性细节与工具调用噪音。"
    "若输入包含【既有摘要】,把它与新对话合并成一份完整摘要(新摘要将完全取代它)。"
    "输出纯文本,不用 markdown 标题,不超过 800 字。"
)


def set_summary_task(task: str | None) -> None:
    """改摘要用的 LLM task 名(传 None 恢复默认 agent.default)。"""
    global SUMMARY_TASK
    SUMMARY_TASK = task or DEFAULT_SUMMARY_TASK

# 历史注入时摘要消息的前缀。用 user 而非 system 角色:Anthropic 只接受单条
# 顶层 system,OpenAI 协议对多条 system 的兼容也因网关而异;user 角色在
# 所有 provider 都合法,语义上等价于"调用方注入的背景材料"。
SUMMARY_HISTORY_HEADER = "【早前对话摘要】\n"

# fire-and-forget 任务需持强引用,否则可能在完成前被 GC(asyncio 官方告诫)
_background_tasks: set[asyncio.Task] = set()


def estimate_tokens(text: str) -> int:
    """粗估 token:字符数/4(口径说明见 COMPRESS_TOKEN_WATERMARK 注释)。"""
    return len(text) // 4


def should_compress(history_rows: list[ChatMessage]) -> bool:
    """水位判断:消息数达到门槛且模型可见文本(content)总量超水位。"""
    if len(history_rows) < COMPRESS_MIN_MESSAGES:
        return False
    total_chars = sum(len(row.content or "") for row in history_rows)
    return total_chars // 4 >= COMPRESS_TOKEN_WATERMARK


def rows_after_summary(
    rows: list[ChatMessage], summary: ChatSessionSummary | None
) -> list[ChatMessage]:
    """历史注入口径:摘要覆盖的行不再进模型历史(行本身不删,审计仍在)。"""
    if summary is None:
        return rows
    return [row for row in rows if row.sequence > summary.covered_until_sequence]


def summary_user_message(summary: ChatSessionSummary) -> dict:
    """摘要注入消息(user 角色,理由见 SUMMARY_HISTORY_HEADER 注释)。"""
    return {"role": "user", "content": SUMMARY_HISTORY_HEADER + summary.content}


async def latest_success_summary(session, session_id: UUID) -> ChatSessionSummary | None:
    """最新生效摘要:success 且覆盖序列最大的一条。"""
    return (
        (
            await session.execute(
                select(ChatSessionSummary)
                .where(
                    ChatSessionSummary.session_id == session_id,
                    ChatSessionSummary.status == "success",
                )
                .order_by(ChatSessionSummary.covered_until_sequence.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _summary_boundary(rows: list[ChatMessage]) -> int:
    """选压缩边界:保留至少最近 COMPRESS_KEEP_TAIL 条,再向前吸附到 user 行。

    吸附保证被保留段以 user 行开头——tool 行不与它的 assistant 父行分离,
    rows_to_history 重建出的历史对任何 provider 都合法。找不到 user 行时
    返回 0(放弃本次压缩),宁可不压也不产生断裂上下文。
    """
    candidate = len(rows) - COMPRESS_KEEP_TAIL
    for index in range(min(candidate, len(rows) - 1), 0, -1):
        if rows[index].role == "user":
            return index
    return 0


def _segment_text(rows: list[ChatMessage], prior_summary: str | None) -> str:
    """待压缩片段的纯文本视图。tool 行只取截断 content(模型可见口径),
    完整输出本就不该进摘要——摘要面向决策与约束,不是数据备份。"""
    lines: list[str] = []
    if prior_summary:
        lines.append(f"【既有摘要】\n{prior_summary}\n")
    for row in rows:
        if row.role == "tool":
            lines.append(f"[工具 {row.tool_name}] {(row.content or '')[:500]}")
        else:
            lines.append(f"[{row.role}] {row.content or ''}")
    return "\n".join(lines)


async def compress_session(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    session_id: UUID,
    llm: LLMService,
) -> ChatSessionSummary | None:
    """压缩一次会话:水位不足 / 熔断 / 找不到安全边界时返回 None(无副作用)。

    失败也落一行 status=failed:一是保留可观测痕迹,二是给熔断计数用。
    """
    session = org_session(session_factory, org_id)
    try:
        recent = (
            (
                await session.execute(
                    select(ChatSessionSummary)
                    .where(ChatSessionSummary.session_id == session_id)
                    .order_by(ChatSessionSummary.created_at.desc())
                    .limit(COMPRESS_FAILURE_CIRCUIT)
                )
            )
            .scalars()
            .all()
        )
        if len(recent) >= COMPRESS_FAILURE_CIRCUIT and all(
            row.status == "failed" for row in recent
        ):
            logger.warning(
                "会话压缩熔断:最近 %s 次摘要连续失败,跳过 session=%s",
                COMPRESS_FAILURE_CIRCUIT,
                session_id,
            )
            return None

        prior = await latest_success_summary(session, session_id)
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence)
        )
        if prior is not None:
            stmt = stmt.where(ChatMessage.sequence > prior.covered_until_sequence)
        rows = (await session.execute(stmt)).scalars().all()
        if not should_compress(list(rows)):
            return None
        boundary = _summary_boundary(list(rows))
        if boundary <= 0:
            return None
        segment = list(rows)[:boundary]
        covered_until = segment[-1].sequence

        try:
            turn = await llm.generate_with_tools(
                task=SUMMARY_TASK,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _segment_text(
                            segment, prior.content if prior is not None else None
                        ),
                    }
                ],
                tools=[],
                org_id=org_id,
            )
            content = (turn.text or "").strip()
            if not content:
                raise ValueError("摘要模型返回空文本")
            summary = ChatSessionSummary(
                org_id=org_id,
                session_id=session_id,
                content=content,
                covered_until_sequence=covered_until,
                token_estimate=estimate_tokens(content),
                status="success",
            )
        except Exception as exc:
            logger.warning("会话摘要生成失败 session=%s: %s", session_id, exc)
            summary = ChatSessionSummary(
                org_id=org_id,
                session_id=session_id,
                content=f"(摘要生成失败:{exc})",
                covered_until_sequence=covered_until,
                token_estimate=0,
                status="failed",
            )
        session.add(summary)
        await session.commit()
        return summary
    finally:
        await session.close()


def schedule_compression(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    session_id: UUID,
    llm: LLMService,
) -> asyncio.Task:
    """turn 完成后的异步触发入口:不阻塞响应,异常吞掉只记日志。

    摘要缺一次无碍(下一个 turn 会再评估),因此这里绝不向调用方抛错。
    """

    async def _run() -> None:
        try:
            await compress_session(
                session_factory, org_id=org_id, session_id=session_id, llm=llm
            )
        except Exception:
            logger.exception("会话压缩后台任务失败 session=%s", session_id)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
