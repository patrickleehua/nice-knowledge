"""子 agent 委派单测(纯逻辑,mock LLM 与工具,无 DB)。

守四条不可退让的边界:①禁递归(专员工具集里没有 Agent)②confirm 工具在
子 agent 内不可用 ③任何"需要人工确认"的判定在子 agent 内降级为拒绝
④专员 seed 幂等。外加 schema 枚举构建与一次完整委派跑通。
"""

import asyncio
from uuid import uuid4

from nicekit.agent.loop import ToolPreflight, run_loop
from nicekit.agent.sub_agents import (
    AGENT_TOOL_NAME,
    DelegatableAgent,
    build_agent_tool_schema,
    build_delegation_tool,
    build_sub_agent_tools,
    run_sub_agent,
    wrap_preflight,
    wrap_reviewer,
)
from nicekit.agent.tools import ToolContext, ToolDef
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolPermissionSpec,
    ToolReversibility,
    ToolRisk,
)
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import ToolTurn
from nicekit.tenancy.roles import MEMBER

READ_PERMISSION = ToolPermissionSpec(
    effect=ToolEffect.READ,
    risk=ToolRisk.ROUTINE,
    categories=frozenset({ToolCategory.LOCAL_DATA}),
    reversibility=ToolReversibility.REVERSIBLE,
    delegation=ToolDelegation.AUTOMATIC,
    scope=PermissionScope.SESSION,
)
WRITE_PERMISSION = ToolPermissionSpec(
    effect=ToolEffect.WRITE,
    risk=ToolRisk.CRITICAL,
    categories=frozenset({ToolCategory.FINANCIAL}),
    reversibility=ToolReversibility.IRREVERSIBLE,
    delegation=ToolDelegation.REVIEWABLE,
    scope=PermissionScope.RESOURCE,
)


def _agent(name: str = "检索研究员", tools: tuple[str, ...] = ("kb_search",)) -> DelegatableAgent:
    return DelegatableAgent(
        card_id=uuid4(),
        name=name,
        description=f"{name}职责说明",
        system_prompt=f"你是{name}",
        model_task="agent.workbench",
        max_turns=4,
        timeout_seconds=30,
        tools=tools,
    )


def _tool(
    name: str,
    *,
    confirm: bool = False,
    calls: list[str] | None = None,
    permission: ToolPermissionSpec = READ_PERMISSION,
) -> ToolDef:
    async def executor(ctx, args) -> dict:
        if calls is not None:
            calls.append(name)
        return {"tool": name, "args": args}

    return ToolDef(
        name=name,
        description=name,
        schema={"type": "object"},
        executor=executor,
        permission=permission,
        label=f"{name}标签",
        confirm=confirm,
    )


def _turn(text=None, tool_calls=None) -> ToolTurn:
    return ToolTurn(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason="tool_use" if tool_calls else "end_turn",
        tokens_in=7,
        tokens_out=5,
        trace_id=None,
    )


class _ScriptedLLM:
    """按脚本依次吐 ToolTurn 的假 LLMService(只实现 generate_with_tools)。"""

    def __init__(self, turns: list[ToolTurn]):
        self._turns = iter(turns)
        self.calls: list[dict] = []

    async def generate_with_tools(self, **kwargs) -> ToolTurn:
        self.calls.append(kwargs)
        return next(self._turns)


class _FakeChatSession:
    """ToolContext.chat_session 的最小读取面:只有作用域绑定这一对字段。"""

    def __init__(self, scope_type=None, scope_id=None):
        self.id = uuid4()
        self.scope_type = scope_type
        self.scope_id = scope_id


def _ctx(*, scope_type=None, scope_id=None) -> ToolContext:
    return ToolContext(
        session=None,
        org_id=uuid4(),
        user_id=uuid4(),
        role=MEMBER,
        chat_session=_FakeChatSession(scope_type, scope_id),
    )


def _allow_preflight(*, decision=PermissionDecision.ALLOW, reason="test"):
    async def preflight(call, tool) -> ToolPreflight:
        return ToolPreflight(decision=decision, reason_code=reason, source="profile")

    return preflight


# ---------- Agent 工具 schema ----------


def test_agent_tool_schema_lists_每张卡_并用枚举锁定目标() -> None:
    schema = build_agent_tool_schema([_agent(), _agent("报价核算员", ("quote_get",))])
    assert schema["name"] == AGENT_TOOL_NAME
    params = schema["schema"]
    assert params["properties"]["target_agent"]["enum"] == ["检索研究员", "报价核算员"]
    assert set(params["required"]) == {"target_agent", "task"}
    # 双协议 strict 交集:全字段 required + 禁止额外字段
    assert params["additionalProperties"] is False
    assert "- 检索研究员:检索研究员职责说明" in schema["description"]
    assert "- 报价核算员:报价核算员职责说明" in schema["description"]


def test_agent_tool_是只读例程_走并行分支且不触发审批() -> None:
    tool = build_delegation_tool(
        [_agent()],
        llm=_ScriptedLLM([]),
        org_id=uuid4(),
        parent_tools={},
        preflight=lambda: _allow_preflight(),
        reviewer=lambda: None,
    )
    # side_effect=read 是刻意的:loop 的 read 分支才会 asyncio.gather 并发执行,
    # 同一跳里的多个委派必须能并行(委派本身不写数据,内部工具各自过治理)
    assert tool.side_effect == "read"
    assert tool.confirm is False
    assert tool.permission.delegation is ToolDelegation.AUTOMATIC


# ---------- 禁递归与工具裁剪 ----------


def test_子agent工具集不含Agent自身_禁止再委派() -> None:
    parent = {
        "kb_search": _tool("kb_search"),
        AGENT_TOOL_NAME: _tool(AGENT_TOOL_NAME),
    }
    # 即便专员白名单里手写了 Agent,也必须被剔除
    agent = _agent(tools=("kb_search", AGENT_TOOL_NAME))
    sub_tools = build_sub_agent_tools(agent, parent)
    assert set(sub_tools) == {"kb_search"}


def test_子agent工具集剔除confirm工具与ask_user() -> None:
    parent = {
        "kb_search": _tool("kb_search"),
        "quote_submit_review": _tool(
            "quote_submit_review", confirm=True, permission=WRITE_PERMISSION
        ),
        "ask_user": _tool("ask_user"),
    }
    agent = _agent(tools=("kb_search", "quote_submit_review", "ask_user"))
    # confirm 工具的设计前提是人在环,ask_user 直接面向用户,子 agent 两者都不具备
    assert set(build_sub_agent_tools(agent, parent)) == {"kb_search"}


def test_子agent工具集不能超出主轮快照() -> None:
    # 主轮因联网关闭没有 web_search 时,专员也拿不到——委派只能缩小权限面
    parent = {"kb_search": _tool("kb_search")}
    agent = _agent(tools=("kb_search", "web_search"))
    assert set(build_sub_agent_tools(agent, parent)) == {"kb_search"}


# ---------- 人工审批路径降级 ----------


async def test_preflight的ASK_USER在子agent内变成DENY() -> None:
    wrapped = wrap_preflight(_allow_preflight(decision=PermissionDecision.ASK_USER))
    evaluation = await wrapped(
        ToolCallRequest(id="c1", name="kb_search", arguments={}), _tool("kb_search")
    )
    assert evaluation.decision is PermissionDecision.DENY
    assert evaluation.reason_code == "sub_agent_approval_unavailable"
    assert "主对话" in (evaluation.reviewer_rationale or "")


async def test_preflight的AUTO_REVIEW保留_仍交reviewer裁决() -> None:
    wrapped = wrap_preflight(_allow_preflight(decision=PermissionDecision.AUTO_REVIEW))
    evaluation = await wrapped(
        ToolCallRequest(id="c1", name="kb_search", arguments={}), _tool("kb_search")
    )
    assert evaluation.decision is PermissionDecision.AUTO_REVIEW


async def test_reviewer转交人工或故障时子agent一律拒绝() -> None:
    routed = ToolPreflight(
        decision=PermissionDecision.AUTO_REVIEW, reason_code="profile", source="profile"
    )
    call = ToolCallRequest(id="c1", name="quote_submit_review", arguments={})
    tool = _tool("quote_submit_review", permission=WRITE_PERMISSION)

    async def escalating(_call, _tool, _routed) -> ToolPreflight:
        return ToolPreflight(
            decision=PermissionDecision.ASK_USER,
            reason_code="reviewer_escalated",
            source="reviewer",
        )

    async def broken(_call, _tool, _routed) -> ToolPreflight:
        raise RuntimeError("reviewer down")

    for reviewer in (escalating, broken, None):
        evaluation = await wrap_reviewer(reviewer)(call, tool, routed)
        assert evaluation.decision is PermissionDecision.DENY
        assert evaluation.reason_code == "sub_agent_approval_unavailable"
        assert evaluation.override_eligible is False


async def test_需人工审批的工具在子agent内被拒而不是截停() -> None:
    """子 agent 无法暂停等人:ASK_USER 若原样返回,整个子任务会悬空。"""
    calls: list[str] = []
    parent = {"quote_get": _tool("quote_get", calls=calls, permission=WRITE_PERMISSION)}
    llm = _ScriptedLLM(
        [
            _turn(tool_calls=[ToolCallRequest(id="s1", name="quote_get", arguments={})]),
            _turn(text="该步骤需要主对话审批,我改为汇报现状。"),
        ]
    )
    result = await run_sub_agent(
        _agent(tools=("quote_get",)),
        "核一下报价",
        llm=llm,
        org_id=uuid4(),
        parent_tools=parent,
        tool_ctx=_ctx(),
        preflight=_allow_preflight(decision=PermissionDecision.ASK_USER),
    )
    assert result["stop"] == "end_turn"
    assert calls == []  # 被拒 → 工具没有真正执行
    assert result["output"].startswith("该步骤需要主对话审批")


# ---------- 一次完整委派 ----------


async def test_run_sub_agent跑通一次委派并回报进度() -> None:
    calls: list[str] = []
    progress: list[str] = []
    parent = {"kb_search": _tool("kb_search", calls=calls)}
    llm = _ScriptedLLM(
        [
            _turn(tool_calls=[ToolCallRequest(id="s1", name="kb_search", arguments={"q": "巴黎"})]),
            _turn(text="巴黎 6 月适合出行[1]。"),
        ]
    )

    async def report(text: str) -> None:
        progress.append(text)

    scope_id = uuid4()
    result = await run_sub_agent(
        _agent(),
        "查巴黎 6 月天气与签证",
        llm=llm,
        org_id=uuid4(),
        parent_tools=parent,
        tool_ctx=_ctx(scope_type="ticket", scope_id=scope_id),
        preflight=_allow_preflight(),
        progress=report,
    )
    assert result["output"] == "巴黎 6 月适合出行[1]。"
    assert result["turns"] == 2
    assert result["stop"] == "end_turn"
    assert result["input_tokens"] == 14 and result["output_tokens"] == 10
    assert calls == ["kb_search"]
    assert progress == ["[检索研究员] 调用 kb_search标签…", "[检索研究员] 完成(2 轮)"]
    # 独立上下文:system 来自专员卡,首条 user 就是 task(不带主会话 history)
    first = llm.calls[0]
    assert "你是检索研究员" in first["system"]
    assert first["messages"][0]["role"] == "user"
    # 作用域注入泛化:TF 是 [当前项目 ID:...],SDK 是 [当前作用域 type:id]
    assert f"[当前作用域 ticket:{scope_id}]" in first["messages"][0]["content"]
    assert [schema["name"] for schema in first["tools"]] == ["kb_search"]


async def test_run_sub_agent失败与未知专员都返回error而不抛() -> None:
    llm = _ScriptedLLM([])

    async def boom(**_kwargs):
        raise RuntimeError("provider down")

    llm.generate_with_tools = boom  # type: ignore[method-assign]
    failed = await run_sub_agent(
        _agent(),
        "查资料",
        llm=llm,
        org_id=uuid4(),
        parent_tools={"kb_search": _tool("kb_search")},
        tool_ctx=_ctx(),
        preflight=_allow_preflight(),
    )
    # run_loop 自己把模型异常收敛成 stop=error,委派结果只保留错误文本
    assert "error" in failed and "provider down" in failed["error"]

    tool = build_delegation_tool(
        [_agent()],
        llm=llm,
        org_id=uuid4(),
        parent_tools={},
        preflight=lambda: _allow_preflight(),
        reviewer=lambda: None,
    )
    unknown = await tool.executor(_ctx(), {"target_agent": "不存在的人", "task": "x"})
    assert "不存在" in unknown["error"]
    empty_task = await tool.executor(_ctx(), {"target_agent": "检索研究员", "task": "  "})
    assert "task" in empty_task["error"]


async def test_同一跳的多个委派并发执行() -> None:
    """Agent 工具标成 read,才能走 loop 的 asyncio.gather 分支。"""
    started = asyncio.Event()
    both_started = asyncio.Event()
    running = 0

    async def slow_executor(ctx, args) -> dict:
        nonlocal running
        running += 1
        if running >= 2:
            both_started.set()
        started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return {"ok": True}

    slow = ToolDef(
        name="kb_search",
        description="kb_search",
        schema={"type": "object"},
        executor=slow_executor,
        permission=READ_PERMISSION,
    )

    def _llm_for(text: str) -> _ScriptedLLM:
        return _ScriptedLLM(
            [
                _turn(tool_calls=[ToolCallRequest(id="s1", name="kb_search", arguments={})]),
                _turn(text=text),
            ]
        )

    agents = [_agent("检索研究员"), _agent("报价核算员", ("kb_search",))]
    llms = {"检索研究员": _llm_for("甲完成"), "报价核算员": _llm_for("乙完成")}

    class _Router:
        async def generate_with_tools(self, **kwargs):
            return await llms[kwargs["system"].split("你是")[-1].strip()].generate_with_tools(
                **kwargs
            )

    # 主轮 preflight 背后是会话那一个 AsyncSession,并发进入会炸在驱动层;
    # 这里守它在子 agent 之间被串行化
    inflight = 0
    max_inflight = 0

    async def counting_preflight(call, tool) -> ToolPreflight:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0)
        inflight -= 1
        return ToolPreflight(
            decision=PermissionDecision.ALLOW, reason_code="test", source="profile"
        )

    agent_tool = build_delegation_tool(
        agents,
        llm=_Router(),
        org_id=uuid4(),
        parent_tools={"kb_search": slow},
        preflight=lambda: counting_preflight,
        reviewer=lambda: None,
    )
    turns = iter(
        [
            _turn(
                tool_calls=[
                    ToolCallRequest(
                        id="a1",
                        name=AGENT_TOOL_NAME,
                        arguments={"target_agent": "检索研究员", "task": "甲"},
                    ),
                    ToolCallRequest(
                        id="a2",
                        name=AGENT_TOOL_NAME,
                        arguments={"target_agent": "报价核算员", "task": "乙"},
                    ),
                ]
            ),
            _turn(text="两位专员都回来了"),
        ]
    )

    async def main_llm(_messages, _schemas):
        return next(turns)

    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    outcome = await asyncio.wait_for(
        run_loop(
            main_llm,
            tools={AGENT_TOOL_NAME: agent_tool},
            allowed=[AGENT_TOOL_NAME],
            max_turns=3,
            history=[],
            user_text="分头去查",
            tool_ctx=_ctx(),
            emit=emit,
            preflight=_allow_preflight(),
        ),
        timeout=5,
    )
    # 两个子 agent 的工具执行必须重叠,否则 both_started 永远等不到、上面已超时
    assert outcome.stop == "end_turn"
    assert outcome.final_text == "两位专员都回来了"
    results = {
        event["output"]["agent"]: event["output"]
        for event in events
        if event["type"] == "tool.result"
    }
    assert results["检索研究员"]["output"] == "甲完成"
    assert results["报价核算员"]["output"] == "乙完成"
    # 子 agent 进度经 tool.progress 挂在父调用上转发
    progress = [
        event["text"] for event in events if event["type"] == "tool.progress"
    ]
    assert "[检索研究员] 完成(2 轮)" in progress
    assert "[报价核算员] 完成(2 轮)" in progress
    assert {
        event["parent_tool_call_id"]
        for event in events
        if event["type"] == "tool.progress"
    } == {"a1", "a2"}
    assert max_inflight == 1


# ---------- 专员 seed 幂等 ----------
#
# 专员卡 seed(agent/seed.py)随 P2c 交付,TF 里对应的 3 个用例届时搬回。
