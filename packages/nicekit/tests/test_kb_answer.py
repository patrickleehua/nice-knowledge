from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.kb.answer import (
    KB_ANSWER_TASK,
    KnowledgeAnswerGenerationError,
    build_answer_context,
    generate_grounded_answer,
    stream_grounded_answer,
    validate_answer_citations,
)
from nicekit.kb.answer_seed import KB_ANSWER_TIMEOUT, build_kb_answer_route
from nicekit.kb.search import SearchHit
from nicekit.llm.service import ToolTurn


def _hit(name: str, quote: str | None) -> SearchHit:
    return SearchHit(
        kind="hotel",
        layer="tenant",
        kb_id=str(uuid4()),
        source=f"hotel/{name}",
        confidence=0.8,
        data={
            "id": str(uuid4()),
            "name": name,
            "city": "巴黎",
            "price_ref": 220,
            "currency": "EUR",
            "scores": {"rrf": 0.1},
        },
        citation=(
            {
                "kind": "fact_evidence",
                "revision_id": str(uuid4()),
                "source_doc_id": str(uuid4()),
                "source_sha256": "a" * 64,
                "quote_text": quote,
                "fact_claim_id": str(uuid4()),
                "evidence_span_id": str(uuid4()),
                "chunk_id": str(uuid4()),
                "page": 2,
                "start_line": 4,
                "end_line": 5,
                "cell_ref": None,
            }
            if quote is not None
            else None
        ),
    )


class FakeLlm:
    """多次调用按顺序吐出不同文本;提供 on_delta 时模拟 provider 流式分段回调。"""

    def __init__(self, text: str | list[str]) -> None:
        self.texts = [text] if isinstance(text, str) else list(text)
        self.calls: list[dict] = []

    async def generate_with_tools(self, **kwargs) -> ToolTurn:
        self.calls.append(kwargs)
        text = self.texts[min(len(self.calls), len(self.texts)) - 1]
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            for start in range(0, len(text), 4):
                await on_delta(text[start : start + 4])
        return ToolTurn(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            tokens_in=10,
            tokens_out=10,
            trace_id=None,
        )


async def _collect(frames) -> list[dict]:
    return [frame async for frame in frames]


def test_answer_context_excludes_internal_search_scores() -> None:
    context = build_answer_context([_hit("巴黎酒店", "四星酒店参考价 220 欧元")])

    assert "[1] 标题：巴黎酒店" in context
    assert "原文证据：四星酒店参考价 220 欧元" in context
    assert "scores" not in context
    assert "rrf" not in context


def test_validate_answer_citations_rejects_missing_or_unknown_refs() -> None:
    with pytest.raises(KnowledgeAnswerGenerationError):
        validate_answer_citations("没有引用的答案", 2)
    with pytest.raises(KnowledgeAnswerGenerationError):
        validate_answer_citations("错误来源 [3]", 2)


async def test_grounded_answer_returns_only_sources_used_by_model() -> None:
    llm = FakeLlm("巴黎酒店参考价约 220 EUR [2]。")
    first = _hit("左岸酒店", "左岸酒店参考价 180 欧元")
    second = _hit("右岸酒店", "右岸酒店参考价 220 欧元")

    result = await generate_grounded_answer(
        query="巴黎酒店多少钱",
        hits=[first, second],
        org_id=uuid4(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.answer == "巴黎酒店参考价约 220 EUR [2]。"
    assert result.sources == ((2, second),)
    assert llm.calls[0]["tools"] == []
    assert llm.calls[0]["task"] == KB_ANSWER_TASK


async def test_grounded_answer_skips_model_when_no_cited_evidence() -> None:
    llm = FakeLlm("不应调用")

    result = await generate_grounded_answer(
        query="没有资料的问题",
        hits=[_hit("无证据酒店", None)],
        org_id=uuid4(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.answer is None
    assert result.reason == "no_evidence"
    assert result.sources == ()
    assert llm.calls == []


# ---- 引用校验失败自动修复一轮 ---------------------------------------------


async def test_grounded_answer_repairs_invalid_citation_once() -> None:
    llm = FakeLlm(["酒店参考价 220 欧元 [5]。", "酒店参考价 220 欧元 [1]。"])
    hit = _hit("巴黎酒店", "四星酒店参考价 220 欧元")

    result = await generate_grounded_answer(
        query="巴黎酒店多少钱",
        hits=[hit],
        org_id=uuid4(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.answer == "酒店参考价 220 欧元 [1]。"
    assert result.sources == ((1, hit),)
    assert len(llm.calls) == 2
    # 修复轮必须带上上一版答案与可执行的失败原因,而不是从头重问
    repair_messages = llm.calls[1]["messages"]
    assert repair_messages[1]["role"] == "assistant"
    assert repair_messages[1]["content"] == "酒店参考价 220 欧元 [5]。"
    assert "[5]" in repair_messages[2]["content"]
    assert "1..1" in repair_messages[2]["content"]


async def test_grounded_answer_raises_after_two_invalid_rounds() -> None:
    llm = FakeLlm(["错误来源 [7]。", "还是错误 [9]。"])

    with pytest.raises(KnowledgeAnswerGenerationError):
        await generate_grounded_answer(
            query="巴黎酒店多少钱",
            hits=[_hit("巴黎酒店", "四星酒店参考价 220 欧元")],
            org_id=uuid4(),
            llm=llm,  # type: ignore[arg-type]
        )

    assert len(llm.calls) == 2


# ---- SSE 流式帧序 ----------------------------------------------------------


async def test_stream_answer_emits_sources_then_deltas_then_done() -> None:
    llm = FakeLlm("巴黎酒店参考价约 220 EUR [2]。")
    first = _hit("左岸酒店", "左岸酒店参考价 180 欧元")
    second = _hit("右岸酒店", "右岸酒店参考价 220 欧元")

    frames = await _collect(
        stream_grounded_answer(
            query="巴黎酒店多少钱",
            hits=[first, second],
            org_id=uuid4(),
            llm=llm,  # type: ignore[arg-type]
        )
    )

    assert frames[0]["type"] == "sources"
    assert frames[0]["sources"] == [(1, first), (2, second)]
    deltas = [frame for frame in frames[1:-1] if frame["type"] == "delta"]
    assert deltas == frames[1:-1]  # 中段只允许增量帧
    assert "".join(frame["text"] for frame in deltas) == "巴黎酒店参考价约 220 EUR [2]。"
    assert frames[-1] == {"type": "done", "used_refs": [2]}


async def test_stream_answer_without_evidence_only_signals_no_evidence() -> None:
    llm = FakeLlm("不应调用")

    frames = await _collect(
        stream_grounded_answer(
            query="没有资料的问题",
            hits=[_hit("无证据酒店", None)],
            org_id=uuid4(),
            llm=llm,  # type: ignore[arg-type]
        )
    )

    assert frames == [{"type": "no_evidence"}]
    assert llm.calls == []


async def test_stream_answer_restarts_on_repair_and_errors_after_two_rounds() -> None:
    llm = FakeLlm(["错误来源 [7]。", "还是错误 [9]。"])

    frames = await _collect(
        stream_grounded_answer(
            query="巴黎酒店多少钱",
            hits=[_hit("巴黎酒店", "四星酒店参考价 220 欧元")],
            org_id=uuid4(),
            llm=llm,  # type: ignore[arg-type]
        )
    )

    assert frames[0]["type"] == "sources"
    restarts = [index for index, frame in enumerate(frames) if frame["type"] == "restart"]
    assert len(restarts) == 1
    # restart 之后是第二轮的增量,前端此刻应清空已渲染文本
    assert frames[restarts[0] + 1]["type"] == "delta"
    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "invalid_citations"
    assert frames[-1]["message"]
    assert len(llm.calls) == 2


# ---- kb.answer 模型路由种子 ------------------------------------------------


def test_build_kb_answer_route_uses_platform_defaults() -> None:
    settings = SimpleNamespace(
        llm_default_provider="openai",
        llm_default_model="gpt-main",
        llm_fallback_provider="anthropic",
        llm_fallback_model="claude-backup",
        llm_default_max_tokens=4096,
    )

    route = build_kb_answer_route(settings)  # type: ignore[arg-type]

    assert route is not None
    assert route.task == KB_ANSWER_TASK == "kb.answer"
    assert route.org_id is None
    assert route.primary_provider == "openai"
    assert route.primary_model == "gpt-main"
    assert route.fallback_chain == [{"provider": "anthropic", "model": "claude-backup"}]
    assert route.max_tokens == 4096
    assert route.timeout_seconds == KB_ANSWER_TIMEOUT == 120.0


def test_build_kb_answer_route_skips_when_default_model_missing() -> None:
    settings = SimpleNamespace(
        llm_default_provider="openai",
        llm_default_model="",
        llm_fallback_provider="",
        llm_fallback_model="",
        llm_default_max_tokens=8192,
    )

    assert build_kb_answer_route(settings) is None  # type: ignore[arg-type]
