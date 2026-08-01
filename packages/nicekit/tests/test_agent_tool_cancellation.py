"""ToolContext 的资源收敛泛化(TF pipeline_run_ids → cancellable_resources)。

TF 在执行边界里直接收敛 PipelineRun;SDK 不认识宿主的任务表,改为工具自己
登记"我起了什么"(cancellable_resources)与"怎么收敛它"(on_cancel)。
同时覆盖 ToolDef.emit_start_progress 取代 image_generate 工具名硬编码特判。
"""

import asyncio
from uuid import uuid4

from nicekit.agent.loop import (
    ToolExecutionStatus,
    execute_native_tool,
    run_loop,
)
from nicekit.agent.tools import ToolContext, ToolDef
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import ToolTurn
from nicekit.models.chat import ChatSession


def _ctx() -> ToolContext:
    return ToolContext(
        session=object(),  # type: ignore[arg-type]
        org_id=uuid4(),
        user_id=uuid4(),
        role="member",
        chat_session=ChatSession(
            org_id=uuid4(), user_id=uuid4(), agent_card_id=uuid4()
        ),
    )


def _turn(*calls: ToolCallRequest, text: str | None = None) -> ToolTurn:
    return ToolTurn(
        text=text,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        tokens_in=1,
        tokens_out=1,
        trace_id=None,
    )


def _script(*turns: ToolTurn):
    iterator = iter(turns)

    async def llm_call(_messages, _schemas):
        return next(iterator)

    return llm_call


def _tool(executor, *, side_effect: str = "write", **overrides) -> ToolDef:
    return ToolDef(
        name=overrides.pop("name", "resourceful"),
        description="tool",
        schema={"type": "object", "properties": {}},
        executor=executor,
        side_effect=side_effect,
        **overrides,
    )


async def test_registered_resources_are_reported_on_success() -> None:
    async def executor(ctx, _args) -> dict:
        ctx.cancellable_resources.add("task-2")
        ctx.cancellable_resources.add("task-1")
        return {"ok": True}

    outcome = await execute_native_tool(
        ToolCallRequest(id="c1", name="resourceful", arguments={}),
        _tool(executor),
        _ctx(),
        timeout_seconds=5,
    )

    assert outcome.status is ToolExecutionStatus.SUCCEEDED
    assert outcome.cancellable_resources == ("task-1", "task-2")


async def test_context_is_isolated_per_call() -> None:
    shared = _ctx()
    shared.cancellable_resources.add("pre-existing")

    async def executor(ctx, _args) -> dict:
        ctx.cancellable_resources.add("call-scoped")
        return {"ok": True}

    outcome = await execute_native_tool(
        ToolCallRequest(id="c1", name="resourceful", arguments={}),
        _tool(executor),
        shared,
        timeout_seconds=5,
    )

    assert outcome.cancellable_resources == ("call-scoped",)
    assert shared.cancellable_resources == {"pre-existing"}


async def test_timeout_runs_cancel_hooks_and_reports_resources() -> None:
    finalized: list[tuple[str, ...]] = []

    async def executor(ctx, _args) -> dict:
        ctx.cancellable_resources.add("job-1")
        ctx.on_cancel.append(_hook)
        await asyncio.Future()
        return {}

    async def _hook(ctx) -> None:
        finalized.append(tuple(sorted(ctx.cancellable_resources)))

    outcome = await execute_native_tool(
        ToolCallRequest(id="c1", name="resourceful", arguments={}),
        _tool(executor),
        _ctx(),
        timeout_seconds=0.01,
    )

    assert outcome.status is ToolExecutionStatus.TIMED_OUT
    assert finalized == [("job-1",)]
    assert outcome.output["cancellable_resources"] == ["job-1"]
    assert outcome.output["reason_code"] == "tool_timeout"


async def test_cancellation_runs_cancel_hooks() -> None:
    started = asyncio.Event()
    finalized: list[str] = []

    async def executor(ctx, _args) -> dict:
        ctx.cancellable_resources.add("job-9")
        ctx.on_cancel.append(_hook)
        started.set()
        await asyncio.Future()
        return {}

    async def _hook(_ctx) -> None:
        finalized.append("converged")

    async def is_cancelled() -> bool:
        return started.is_set()

    outcome = await execute_native_tool(
        ToolCallRequest(id="c1", name="resourceful", arguments={}),
        _tool(executor),
        _ctx(),
        timeout_seconds=5,
        is_cancelled=is_cancelled,
        cancellation_poll_interval_seconds=0.001,
    )

    assert outcome.status is ToolExecutionStatus.CANCELLED
    assert finalized == ["converged"]
    assert outcome.output["cancellable_resources"] == ["job-9"]


async def test_failing_cancel_hook_does_not_change_the_terminal_state() -> None:
    async def executor(ctx, _args) -> dict:
        ctx.on_cancel.append(_hook)
        await asyncio.Future()
        return {}

    async def _hook(_ctx) -> None:
        raise RuntimeError("收尾失败")

    outcome = await execute_native_tool(
        ToolCallRequest(id="c1", name="resourceful", arguments={}),
        _tool(executor),
        _ctx(),
        timeout_seconds=0.01,
    )

    assert outcome.status is ToolExecutionStatus.TIMED_OUT


async def test_emit_start_progress_flag_replaces_tool_name_special_casing() -> None:
    async def executor(_ctx, _args) -> dict:
        return {"ok": True}

    async def _run(emit_start_progress: bool) -> list[dict]:
        outcome = await run_loop(
            _script(
                _turn(ToolCallRequest(id="c1", name="writer", arguments={})),
                _turn(text="完成"),
            ),
            tools={
                "writer": _tool(
                    executor,
                    name="writer",
                    emit_start_progress=emit_start_progress,
                )
            },
            allowed=["writer"],
            max_turns=2,
            history=[],
            user_text="执行",
            tool_ctx=None,
        )
        return [event for event in outcome.events if event["type"] == "tool.progress"]

    assert [event["text"] for event in await _run(True)] == ["开始执行"]
    # 自带进度上报的工具(如图片生成)关掉这一条,避免和它自己的进度打架
    assert await _run(False) == []
