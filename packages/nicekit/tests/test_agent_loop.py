"""共享 agent loop 单测(纯逻辑,mock LLM 与工具,无 DB):
白名单拦截、max_turns 截停、ask_user 截停、结果截断、消息序列正确。"""

import asyncio
import json

from nicekit.agent.loop import TOOL_OUTPUT_MAX_CHARS, LoopOutcome, run_loop
from nicekit.agent.tools import ToolDef, ToolError
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import ToolTurn


def _turn(text=None, calls=None, stop="end_turn") -> ToolTurn:
    return ToolTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason="tool_use" if calls else stop,
        tokens_in=10,
        tokens_out=10,
        trace_id=None,
    )


def _call(name: str, args: dict | None = None, cid: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=cid, name=name, arguments=args or {})


def _scripted_llm(turns: list[ToolTurn]):
    """按脚本依次吐 turn 的假 LLM。"""
    it = iter(turns)

    async def llm_call(messages, tool_schemas):
        return next(it)

    return llm_call


def _echo_tool(name: str = "echo") -> ToolDef:
    async def executor(ctx, args) -> dict:
        return {"echo": args}

    return ToolDef(name=name, description="回显", schema={"type": "object"}, executor=executor)


def _boom_tool() -> ToolDef:
    async def executor(ctx, args) -> dict:
        raise ToolError("业务拒绝:测试")

    return ToolDef(name="boom", description="失败", schema={"type": "object"}, executor=executor)


def _big_tool() -> ToolDef:
    async def executor(ctx, args) -> dict:
        return {"data": "x" * (TOOL_OUTPUT_MAX_CHARS * 2)}

    return ToolDef(name="big", description="大结果", schema={"type": "object"}, executor=executor)


def _nullable_error_tool() -> ToolDef:
    async def executor(ctx, args) -> dict:
        return {"status": "ok", "error": None}

    return ToolDef(
        name="nullable_error",
        description="空错误字段结果",
        schema={"type": "object"},
        executor=executor,
    )


async def _run(turns, tools, allowed, max_turns=4, user="你好") -> LoopOutcome:
    return await run_loop(
        _scripted_llm(turns),
        tools=tools,
        allowed=allowed,
        max_turns=max_turns,
        history=[],
        user_text=user,
        tool_ctx=None,
    )


async def test_plain_reply() -> None:
    outcome = await _run([_turn(text="你好,有什么可以帮你?")], {}, [])
    assert outcome.stop == "end_turn"
    assert outcome.final_text == "你好,有什么可以帮你?"
    roles = [m["role"] for m in outcome.new_messages]
    assert roles == ["user", "assistant"]


async def test_tool_call_then_reply() -> None:
    tools = {"echo": _echo_tool()}
    turns = [
        _turn(calls=[_call("echo", {"q": "东京"})]),
        _turn(text="查到了。"),
    ]
    outcome = await _run(turns, tools, ["echo"])
    assert outcome.stop == "end_turn"
    types = [e["type"] for e in outcome.events]
    assert types == ["tool.call", "tool.progress", "tool.result", "text"]
    tool_msg = next(m for m in outcome.new_messages if m["role"] == "tool")
    assert tool_msg["tool_output"] == {"echo": {"q": "东京"}}


async def test_whitelist_blocks_unlisted_tool() -> None:
    tools = {"echo": _echo_tool()}
    turns = [_turn(calls=[_call("echo", {})]), _turn(text="好的")]
    outcome = await _run(turns, tools, allowed=[])  # 白名单为空
    result_ev = next(e for e in outcome.events if e["type"] == "tool.result")
    assert "不在本 agent 的白名单" in result_ev["output"]["error"]


async def test_ask_user_stops_loop() -> None:
    tools = {"echo": _echo_tool()}
    questions = [
        {
            "id": "departure_date",
            "header": "出发日期",
            "question": "什么时候开始?",
            "options": [
                {"label": "本周", "description": None},
                {"label": "下周", "description": None},
            ],
            "multi_select": False,
        }
    ]
    turns = [_turn(calls=[_call("ask_user", {"questions": questions})])]
    outcome = await _run(turns, tools, ["echo", "ask_user"])
    assert outcome.stop == "ask_user"
    assert outcome.pending_user_input is not None
    assert outcome.pending_user_input["questions"] == questions
    assert [event["type"] for event in outcome.events] == ["user.input.required"]
    assert [message["role"] for message in outcome.new_messages] == [
        "user",
        "assistant",
    ]
    assert outcome.final_text is None
    # 不应再消费第二个 turn(脚本只给了一个,没抛 StopIteration 即证明)


async def test_invalid_ask_user_returns_tool_error_for_model_correction() -> None:
    turns = [
        _turn(calls=[_call("ask_user", {"questions": []})]),
        _turn(text="我换一种方式确认。"),
    ]
    outcome = await _run(turns, {}, ["ask_user"])

    assert outcome.stop == "end_turn"
    result = next(
        event for event in outcome.events if event["type"] == "tool.result"
    )
    assert result["ok"] is False
    assert "1-4" in result["output"]["error"]
    assert outcome.pending_user_input is None


async def test_max_turns_stops() -> None:
    tools = {"echo": _echo_tool()}
    # 每轮都发工具调用,永不收敛
    turns = [_turn(calls=[_call("echo", {}, cid=f"c{i}")]) for i in range(10)]
    outcome = await _run(turns, tools, ["echo"], max_turns=3)
    assert outcome.stop == "max_turns"
    assert len([e for e in outcome.events if e["type"] == "tool.call"]) == 3


async def test_tool_error_returned_to_model() -> None:
    tools = {"boom": _boom_tool()}
    turns = [_turn(calls=[_call("boom", {})]), _turn(text="换个方式")]
    outcome = await _run(turns, tools, ["boom"])
    result_ev = next(e for e in outcome.events if e["type"] == "tool.result")
    assert result_ev["output"]["error"] == "业务拒绝:测试"
    assert result_ev["ok"] is False
    assert outcome.stop == "end_turn"  # loop 未被业务错误打断


async def test_null_error_field_is_a_successful_tool_result() -> None:
    tools = {"nullable_error": _nullable_error_tool()}
    turns = [
        _turn(calls=[_call("nullable_error", {})]),
        _turn(text="完成"),
    ]
    outcome = await _run(turns, tools, ["nullable_error"])
    result_ev = next(event for event in outcome.events if event["type"] == "tool.result")
    assert result_ev["ok"] is True


async def test_big_output_compacted_for_model_but_full_in_row() -> None:
    tools = {"big": _big_tool()}
    turns = [_turn(calls=[_call("big", {})]), _turn(text="done")]
    outcome = await _run(turns, tools, ["big"])
    tool_msg = next(m for m in outcome.new_messages if m["role"] == "tool")
    # 模型可见 content 是紧凑视图:限幅正文前缀 + 结构概要 + 审计说明
    compacted = json.loads(tool_msg["content"])
    assert compacted["result_compacted"] is True
    assert len(compacted["content_prefix"]) == TOOL_OUTPUT_MAX_CHARS
    assert "审计" in compacted["note"]
    # 完整结果保留在 tool_output(落库供前端展示)
    assert len(json.dumps(tool_msg["tool_output"])) > TOOL_OUTPUT_MAX_CHARS


async def test_llm_failure_becomes_error_event() -> None:
    async def failing(messages, tool_schemas):
        raise RuntimeError("全 provider 失败")

    outcome = await run_loop(
        failing,
        tools={},
        allowed=[],
        max_turns=3,
        history=[],
        user_text="hi",
        tool_ctx=None,
    )
    assert outcome.stop == "error"
    assert any(e["type"] == "error" for e in outcome.events)


async def test_read_tools_overlap_and_write_runs_after_reads() -> None:
    running = 0
    max_running = 0
    order: list[str] = []

    async def read_executor(ctx, args) -> dict:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        order.append(f"read-start-{args['id']}")
        await asyncio.sleep(0.02)
        order.append(f"read-end-{args['id']}")
        running -= 1
        return {"id": args["id"]}

    async def write_executor(ctx, args) -> dict:
        assert running == 0
        order.append("write")
        return {"ok": True}

    tools = {
        "read": ToolDef(
            name="read",
            description="read",
            schema={},
            executor=read_executor,
            side_effect="read",
        ),
        "write": ToolDef(
            name="write",
            description="write",
            schema={},
            executor=write_executor,
            side_effect="write",
        ),
    }
    turns = [
        _turn(
            calls=[
                _call("write", cid="w"),
                _call("read", {"id": 1}, cid="r1"),
                _call("read", {"id": 2}, cid="r2"),
            ]
        ),
        _turn(text="done"),
    ]
    await _run(turns, tools, ["read", "write"])
    assert max_running == 2
    assert order[-1] == "write"


async def test_thinking_emitted_as_thought_event() -> None:
    """turn.thinking(thinking block/reasoning summary)→ thought 终值事件,
    工具轮与终答轮都发;thinking 不进 messages 历史。"""
    tools = {"echo": _echo_tool()}
    turns = [
        ToolTurn(
            text=None,
            tool_calls=[_call("echo", {})],
            stop_reason="tool_use",
            tokens_in=10,
            tokens_out=10,
            trace_id=None,
            thinking="先查一下数据",
        ),
        ToolTurn(
            text="查到了。",
            tool_calls=[],
            stop_reason="end_turn",
            tokens_in=10,
            tokens_out=10,
            trace_id=None,
            thinking="结果符合预期",
        ),
    ]
    outcome = await _run(turns, tools, ["echo"])
    thought_events = [e for e in outcome.events if e["type"] == "thought"]
    assert [e["text"] for e in thought_events] == ["先查一下数据", "结果符合预期"]
    # thinking 只进事件流,不进待持久化消息
    assert all("先查一下" not in (m.get("content") or "") for m in outcome.new_messages)


async def test_tool_turn_public_text_is_not_mislabeled_as_thought() -> None:
    """带工具调用的公开模型文本属于过程说明，不得冒充 reasoning。"""
    tools = {"echo": _echo_tool()}
    turns = [
        ToolTurn(
            text="我先查一下资料。",
            tool_calls=[_call("echo", {})],
            stop_reason="tool_use",
            tokens_in=10,
            tokens_out=10,
            trace_id=None,
            thinking="需要先读取权威数据",
        ),
        _turn(text="查询完成。"),
    ]

    outcome = await _run(turns, tools, ["echo"])

    assert [event["type"] for event in outcome.events] == [
        "thought",
        "assistant.message",
        "tool.call",
        "tool.progress",
        "tool.result",
        "text",
    ]
    assert outcome.events[0]["text"] == "需要先读取权威数据"
    assert outcome.events[1]["text"] == "我先查一下资料。"


async def test_cancel_checked_before_llm() -> None:
    called = False

    async def llm_call(messages, tool_schemas):
        nonlocal called
        called = True
        return _turn(text="unexpected")

    async def cancelled() -> bool:
        return True

    outcome = await run_loop(
        llm_call,
        tools={},
        allowed=[],
        max_turns=3,
        history=[],
        user_text="hi",
        tool_ctx=None,
        is_cancelled=cancelled,
    )
    assert outcome.stop == "cancelled"
    assert not called


async def test_native_tool_total_deadline_cancels_and_awaits_executor() -> None:
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def executor(_ctx, _args) -> dict:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned_up.set()

    outcome = await run_loop(
        _scripted_llm([_turn(calls=[_call("slow")])]),
        tools={
            "slow": ToolDef(
                name="slow",
                description="slow",
                schema={},
                executor=executor,
            )
        },
        allowed=["slow"],
        max_turns=2,
        history=[],
        user_text="run",
        tool_ctx=None,
        tool_timeout_seconds=0.01,
    )

    assert started.is_set()
    assert cleaned_up.is_set()
    assert outcome.stop == "error"
    assert outcome.terminal_tool is not None
    assert outcome.terminal_tool.status == "timed_out"
    result = next(event for event in outcome.events if event["type"] == "tool.result")
    assert result["output"]["reason_code"] == "tool_timeout"


async def test_cancellation_during_native_tool_cancels_and_awaits_executor() -> None:
    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def executor(_ctx, _args) -> dict:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned_up.set()

    async def is_cancelled() -> bool:
        return cancel_requested.is_set()

    async def request_cancel() -> None:
        await started.wait()
        cancel_requested.set()

    cancellation = asyncio.create_task(request_cancel())
    outcome = await run_loop(
        _scripted_llm([_turn(calls=[_call("slow")])]),
        tools={
            "slow": ToolDef(
                name="slow",
                description="slow",
                schema={},
                executor=executor,
            )
        },
        allowed=["slow"],
        max_turns=2,
        history=[],
        user_text="run",
        tool_ctx=None,
        is_cancelled=is_cancelled,
        tool_timeout_seconds=1,
        cancellation_poll_interval_seconds=0.001,
    )
    await cancellation

    assert cleaned_up.is_set()
    assert outcome.stop == "cancelled"
    assert outcome.terminal_tool is not None
    assert outcome.terminal_tool.status == "cancelled"
    result = next(event for event in outcome.events if event["type"] == "tool.result")
    assert result["output"]["reason_code"] == "run_cancelled"
