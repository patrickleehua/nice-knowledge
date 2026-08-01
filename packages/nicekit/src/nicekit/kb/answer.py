"""Grounded, read-only answers over permission-scoped knowledge search results."""

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nicekit.kb.search import SearchHit
from nicekit.llm.providers import DeltaFn, ProviderError
from nicekit.llm.service import (
    AllProvidersFailedError,
    LlmBudgetExceededError,
    LLMService,
    NoRouteError,
    get_llm_service,
)

# 独立任务名:问答是单跳生成,不该蹭 agent.workbench 的长超时与降级配置
KB_ANSWER_TASK = "kb.answer"

_MAX_SOURCES = 12
_MAX_FIELD_CHARS = 800
_MAX_SOURCE_CHARS = 3_500
_CITATION_PATTERN = re.compile(r"\[(\d{1,3})\]")
_SYSTEM_DATA_KEYS = frozenset(
    {
        "id",
        "scores",
        "snapshot_id",
        "revision_id",
        "source_doc_id",
        "image_asset_id",
        "via",
        "sibling_count",
    }
)

#: 中性默认系统提示(MIGRATION-PLAN B22):只保留"有据回答 + 引用编号 + 缺口如实说"
#: 这套领域无关的诚实性规则;宿主要换口径或加行业约束,调
#: :func:`set_answer_system_prompt` 覆盖。
DEFAULT_ANSWER_SYSTEM_PROMPT = """\
你是一个知识问答助手,只回答有内部证据支持的问题。
你只能依据本轮提供的"内部知识证据"回答,不使用模型记忆补充事实,也不执行任何写操作。

回答规则:
1. 先直接回答用户问题,再补充必要的条件、差异或风险;语言简洁准确。
2. 每个事实性结论紧跟来源编号,例如 [1];同一句有多个依据时写 [1][2]。
3. 只能引用证据清单中真实存在的编号,不得伪造来源。
4. 资料不足、互相冲突或已经标记可能过期时,明确说出缺口或冲突,不要猜。
5. 数值、单位、生效范围一律按原文精确引用;带时效的信息不得说成实时确认值。
6. 不要输出"参考资料"列表,界面会单独展示来源;不要描述检索分数、实体节点、RRF 或重排。
7. 使用 Markdown 正文,不要输出 JSON。
"""

_answer_system_prompt = DEFAULT_ANSWER_SYSTEM_PROMPT


def set_answer_system_prompt(prompt: str | None) -> None:
    """覆盖知识问答的系统提示;None/空串回到 :data:`DEFAULT_ANSWER_SYSTEM_PROMPT`。"""
    global _answer_system_prompt
    _answer_system_prompt = (
        prompt.strip() if prompt and prompt.strip() else DEFAULT_ANSWER_SYSTEM_PROMPT
    )


def answer_system_prompt() -> str:
    """当前生效的问答系统提示。"""
    return _answer_system_prompt


class KnowledgeAnswerGenerationError(RuntimeError):
    """The model did not produce a complete answer with valid evidence links."""


@dataclass(frozen=True)
class GroundedKnowledgeAnswer:
    answer: str | None
    sources: tuple[tuple[int, SearchHit], ...]
    reason: str | None = None


def _clean_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, (list, dict)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return text[:_MAX_FIELD_CHARS] if text else None


def _source_title(hit: SearchHit) -> str:
    for key in ("name", "title", "canonical_name", "heading_path"):
        value = _clean_text(hit.data.get(key))
        if value:
            return value
    content = _clean_text(hit.data.get("content"))
    return content[:80] if content else hit.source


def _source_context(ref: int, hit: SearchHit) -> str:
    citation = hit.citation or {}
    rows = [
        f"[{ref}] 标题：{_source_title(hit)}",
        f"类型：{hit.kind}",
        f"知识层：{hit.layer}",
    ]
    fields: list[str] = []
    for key, raw_value in hit.data.items():
        if key in _SYSTEM_DATA_KEYS:
            continue
        value = _clean_text(raw_value)
        if value:
            fields.append(f"{key}={value}")
    if fields:
        rows.append("业务字段：" + "；".join(fields))

    quote = _clean_text(citation.get("quote_text"))
    if quote:
        rows.append(f"原文证据：{quote}")

    locations: list[str] = []
    for key, label in (
        ("page", "页"),
        ("slide", "幻灯片"),
        ("start_line", "起始行"),
        ("end_line", "结束行"),
        ("cell_ref", "单元格"),
    ):
        value = citation.get(key)
        if value is not None:
            locations.append(f"{label}={value}")
    if locations:
        rows.append("原文位置：" + "；".join(locations))
    if hit.data.get("stale") is True:
        rows.append("时效提示：该资料可能过期")
    return "\n".join(rows)[:_MAX_SOURCE_CHARS]


def cited_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Only evidence-backed hits may be exposed to the answer model."""

    return [
        hit
        for hit in hits
        if hit.citation is not None
        and isinstance(hit.citation.get("quote_text"), str)
        and hit.citation["quote_text"].strip()
    ][:_MAX_SOURCES]


def build_answer_context(hits: list[SearchHit]) -> str:
    return "\n\n".join(
        _source_context(ref, hit) for ref, hit in enumerate(hits, start=1)
    )


def validate_answer_citations(answer: str, source_count: int) -> tuple[int, ...]:
    refs = [int(value) for value in _CITATION_PATTERN.findall(answer)]
    if not refs:
        raise KnowledgeAnswerGenerationError("AI answer did not cite any source")
    invalid = sorted({ref for ref in refs if ref < 1 or ref > source_count})
    if invalid:
        raise KnowledgeAnswerGenerationError(
            f"AI answer cited unavailable sources: {invalid}"
        )
    return tuple(sorted(set(refs)))


def _citation_repair_instruction(answer: str, source_count: int) -> str:
    """把校验失败原因翻译成模型能照做的重写指令(自动修复一轮的回喂内容)。"""

    refs = [int(value) for value in _CITATION_PATTERN.findall(answer)]
    if not refs:
        return (
            "上一版答案没有标注任何来源编号。请重写答案，"
            f"为每个事实性结论紧跟标注证据清单中的编号，可用编号 1..{source_count}。"
        )
    invalid = sorted({ref for ref in refs if ref < 1 or ref > source_count})
    return (
        f"上一版答案引用了不存在的编号 {invalid}，可用编号 1..{source_count}。"
        "请重写答案，只引用证据清单中真实存在的编号。"
    )


async def _generate_validated_answer(
    service: LLMService,
    *,
    query: str,
    evidence: list[SearchHit],
    org_id: UUID,
    on_delta: DeltaFn | None = None,
    on_restart: DeltaFn | None = None,
) -> tuple[str, tuple[int, ...]]:
    """非流式与流式共用的生成核心:引用校验失败自动修复一轮。

    只有引用类失败(validate_answer_citations 抛错)才回喂重试;答案不完整
    (空文本 / 意外工具调用 / max_tokens 截断)与 LLM 传输层异常直接上抛。
    on_restart 在重试前通知调用方丢弃第一轮已流出的文本。"""

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"用户问题：{query}\n\n"
                f"内部知识证据：\n{build_answer_context(evidence)}"
            ),
        }
    ]
    for attempt in range(2):
        turn = await service.generate_with_tools(
            task=KB_ANSWER_TASK,
            system=answer_system_prompt(),
            messages=messages,
            tools=[],
            org_id=org_id,
            on_delta=on_delta,
        )
        answer = (turn.text or "").strip()
        if not answer or turn.tool_calls or turn.stop_reason == "max_tokens":
            raise KnowledgeAnswerGenerationError("AI answer was incomplete")
        try:
            return answer, validate_answer_citations(answer, len(evidence))
        except KnowledgeAnswerGenerationError:
            if attempt == 1:
                raise
            if on_restart is not None:
                await on_restart(None)
            # 带着上一版答案与失败原因重新生成,让模型只修引用而不是重猜问题
            messages = [
                *messages,
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": _citation_repair_instruction(answer, len(evidence)),
                },
            ]
    raise AssertionError("unreachable")  # pragma: no cover


async def generate_grounded_answer(
    *,
    query: str,
    hits: list[SearchHit],
    org_id: UUID,
    llm: LLMService | None = None,
) -> GroundedKnowledgeAnswer:
    evidence = cited_hits(hits)
    if not evidence:
        return GroundedKnowledgeAnswer(
            answer=None,
            sources=(),
            reason="no_evidence",
        )

    service = llm or get_llm_service()
    answer, used_refs = await _generate_validated_answer(
        service, query=query, evidence=evidence, org_id=org_id
    )
    return GroundedKnowledgeAnswer(
        answer=answer,
        sources=tuple((ref, evidence[ref - 1]) for ref in used_refs),
    )


# 流式 error 帧文案(面向业务人员),与 /kb/answer 的 HTTP 错误文案同口径
_STREAM_ERROR_MESSAGES = {
    "budget": "组织今日 AI 额度已用完，请稍后再试或切换到查原文",
    "unavailable": "AI 知识解答暂时不可用，请稍后再试或切换到查原文",
    "invalid_citations": "AI 知识解答未能生成可核验的答案，请重试或切换到查原文",
}


async def stream_grounded_answer(
    *,
    query: str,
    hits: list[SearchHit],
    org_id: UUID,
    llm: LLMService | None = None,
) -> AsyncIterator[dict]:
    """SSE 语义帧流。sources 帧携带 (ref, SearchHit) 元组,由 API 层负责序列化;
    其余帧(no_evidence/delta/restart/done/error)可直接 JSON 下发。
    异常在此归一为 error 帧:流一旦开始,HTTP 层已无法再改状态码。"""

    evidence = cited_hits(hits)
    if not evidence:
        yield {"type": "no_evidence"}
        return
    # 增量文本引用的是编号,所以编号→证据的映射必须先于任何 delta 到达前端
    yield {
        "type": "sources",
        "sources": [(ref, hit) for ref, hit in enumerate(evidence, start=1)],
    }

    service = llm or get_llm_service()
    # 生成核心在独立任务里跑,provider 的 on_delta 回调经队列转成本生成器的帧;
    # restart 有两个来源:降级换 provider(on_delta 收到 None)与引用修复重试
    queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

    async def on_delta(chunk: str | None) -> None:
        await queue.put(("restart", None) if chunk is None else ("delta", chunk))

    async def run() -> tuple[str, tuple[int, ...]]:
        try:
            return await _generate_validated_answer(
                service,
                query=query,
                evidence=evidence,
                org_id=org_id,
                on_delta=on_delta,
                on_restart=on_delta,
            )
        finally:
            # 无论成败都放行消费循环,结果/异常统一从 task 里取
            await queue.put(("finished", None))

    task = asyncio.create_task(run())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "finished":
                break
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            else:
                yield {"type": "restart"}
        try:
            _, used_refs = await task
        except LlmBudgetExceededError:
            code = "budget"
        except (AllProvidersFailedError, NoRouteError, ProviderError):
            code = "unavailable"
        except KnowledgeAnswerGenerationError:
            code = "invalid_citations"
        else:
            yield {"type": "done", "used_refs": list(used_refs)}
            return
        yield {"type": "error", "code": code, "message": _STREAM_ERROR_MESSAGES[code]}
    finally:
        # 消费方提前关闭(客户端断连)时不能让生成任务泄漏
        if not task.done():
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
