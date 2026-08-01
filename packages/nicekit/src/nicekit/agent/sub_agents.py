"""子 agent 委派:主 agent 通过 "Agent" 工具把自包含任务派给专员卡。

设计边界(与 TripBoss agent_registry.go / sub_agent_loop.go 同构):

1. **禁递归**——专员的工具集里永远没有 "Agent",子 agent 不能再往下派。
   委派树深度恒为 1,否则一次对话可能炸出指数级的模型调用。
2. **不直接面对用户**——子 agent 跑独立消息上下文(不带主会话 history),
   产出只作为一条 tool result 回给主 agent,由主 agent 决定怎么讲给用户。
   因此 ask_user 与任何需要人工确认的路径在子 agent 内一律不可用:
   子 agent 无法暂停等人,截停只会让整轮悬空。
3. **治理不因委派而绕过**——专员工具集是主轮工具快照的子集(隔离 session、
   联网能力开关、MCP 绑定全部继承),每次工具调用照样过主轮的 preflight /
   reviewer;唯一的改写是把"需要人工审批"的判定降级为拒绝(见 1/2)。
   委派因此只能缩小权限面,不可能扩权。

迁移自 TF backend/app/services/agent/sub_agents.py。SDK 化改造(§5.4):
TF 在 task 末尾注入 `[当前项目 ID:{project_id}]`,SDK 不认识宿主的项目表,
改为从 `ToolContext.chat_session` 读通用作用域绑定注入
`[当前作用域 {scope_type}:{scope_id}]`;会话未绑定作用域时什么都不注入。
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.loop import PreflightFn, ReviewFn, ToolPreflight, run_loop
from nicekit.agent.tools import ToolContext, ToolDef
from nicekit.core.config import get_settings
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
from nicekit.models.chat import AgentCard

if TYPE_CHECKING:  # 仅类型:运行时导入见 list_delegatable_cards 里的说明
    from nicekit.agent.service import ResolvedAgentCard
    from nicekit.llm.service import LLMService

AGENT_TOOL_NAME = "Agent"


def _compose_system_prompt(role_prompt: str) -> str:
    """组装子 agent 的 system prompt。

    延迟 import `nicekit.agent.prompts`:prompt 资源包随 §5.4"prompts 模板化"
    单独交付,未就位时退化为只用角色段(子 agent 仍可跑,只是少了全局纪律段)。
    """
    try:
        from nicekit.agent.prompts import build_system_prompt
    except ImportError:
        return role_prompt or ""
    return build_system_prompt(role_prompt, None)


def scope_marker(chat_session: object | None) -> str:
    """会话绑定的宿主作用域标识行;未绑定返回空串(不注入任何内容)。

    替代 TF 的 `[当前项目 ID:{project_id}]`:SDK 只认识 scope_type/scope_id
    这一对通用字段,具体是什么业务对象由宿主自己解释。
    """
    scope_type = str(getattr(chat_session, "scope_type", None) or "").strip()
    scope_id = getattr(chat_session, "scope_id", None)
    if not scope_type or scope_id is None:
        return ""
    return f"[当前作用域 {scope_type}:{scope_id}]"


# 子 agent 内不可用的工具:委派工具本身(禁递归)与直接面向用户的提问工具
# (子 agent 不直接面对用户,ask_user 只会让子 loop 提前截停且没人回答)。
_FORBIDDEN_SUB_TOOLS = frozenset({AGENT_TOOL_NAME, "ask_user"})

_HUMAN_APPROVAL_DENIED = (
    "子 agent 内不支持需人工审批的操作,请在主对话执行"
)
_SUB_AGENT_DENY_REASON_CODE = "sub_agent_approval_unavailable"

# 委派本身不碰业务数据:真正的读写发生在子 agent 内部逐个工具上,各自过治理。
# 因此这里标 READ/ROUTINE/LOCAL_DATA —— 既让 request_approval 档不会为"派个活"
# 弹审批,也让 loop 把它归进 read 分支的 asyncio.gather,同一跳的多个委派并发跑。
AGENT_TOOL_PERMISSION = ToolPermissionSpec(
    effect=ToolEffect.READ,
    risk=ToolRisk.ROUTINE,
    categories=frozenset({ToolCategory.LOCAL_DATA}),
    reversibility=ToolReversibility.REVERSIBLE,
    delegation=ToolDelegation.AUTOMATIC,
    scope=PermissionScope.SESSION,
)


@dataclass(frozen=True, slots=True)
class DelegatableAgent:
    """一张可委派专员卡的运行时视图(卡本体的展示字段 + active 版本的能力)。"""

    card_id: UUID
    name: str
    description: str
    system_prompt: str
    model_task: str
    max_turns: int
    timeout_seconds: int
    tools: tuple[str, ...]


async def list_delegatable_cards(
    session: AsyncSession,
    org_id: UUID,
    *,
    exclude_card_id: UUID | None = None,
) -> list[DelegatableAgent]:
    """列出本 org 可见、可委派且有 active 版本的专员卡(按 sort_order/名字稳定排序)。

    可见性与 resolve_card 同规则:本 org 卡 + 平台卡(org_id 为平台 org 或 NULL)。
    exclude_card_id 用于排除当前会话卡自身——主卡若也被标成 delegatable,不能派给自己。
    """
    # service 在运行时才需要:service 模块导入本模块做工具注入,顶层互相导入会成环
    from nicekit.agent.service import resolve_card

    platform_org = get_settings().platform_org_id
    rows = (
        (
            await session.execute(
                select(AgentCard)
                .where(
                    AgentCard.is_active,
                    AgentCard.delegatable,
                    or_(
                        AgentCard.org_id == org_id,
                        AgentCard.org_id == platform_org,
                        AgentCard.org_id.is_(None),  # type: ignore[union-attr]
                    ),
                )
                .order_by(AgentCard.sort_order, AgentCard.name)
            )
        )
        .scalars()
        .all()
    )
    # 同名卡本 org 覆盖平台:target_agent 是枚举,重名会让模型无从区分派给谁。
    # dict 保留首次插入位置,替换同名 key 不打乱 sort_order 排序。
    by_name: dict[str, AgentCard] = {}
    for row in rows:
        if exclude_card_id is not None and row.id == exclude_card_id:
            continue
        existing = by_name.get(row.name)
        if existing is None or (existing.org_id != org_id and row.org_id == org_id):
            by_name[row.name] = row

    agents: list[DelegatableAgent] = []
    for row in by_name.values():
        try:
            resolved: ResolvedAgentCard = await resolve_card(session, org_id, row.id)
        except LookupError:
            # 没有 active 版本的卡不构成可委派能力,静默跳过(不打断主对话)
            continue
        agents.append(
            DelegatableAgent(
                card_id=row.id,
                name=row.name,
                description=(row.description or "").strip() or row.name,
                system_prompt=resolved.system_prompt,
                model_task=resolved.model_task,
                max_turns=resolved.max_turns,
                timeout_seconds=resolved.timeout_seconds,
                tools=resolved.tools,
            )
        )
    return agents


def build_agent_tool_schema(cards: Sequence[DelegatableAgent]) -> dict:
    """构造 "Agent" 工具的下发定义 {name, description, schema}。

    target_agent 用 enum 而不是自由文本:模型写错专员名会被 provider 的 schema
    校验直接挡下,不必等到执行期才报"专员不存在"。description 逐卡列出
    名字 + 职责,模型据此选人。
    """
    names = [card.name for card in cards]
    roster = "\n".join(f"- {card.name}:{card.description}" for card in cards)
    description = (
        "把一个自包含的子任务委派给专员 agent。专员跑自己的独立推理循环与工具集,"
        "完成后把结论作为本次工具结果返回。专员看不到当前对话历史,"
        "task 必须自带全部必要上下文(关键实体标识、时间范围、数量与约束、"
        "已知结论等),只写「继续上面那个」专员无法执行。"
        "专员只做只读调研与分析,不能再往下委派,也无法向用户提问;"
        "需要用户拍板或需要写数据的动作请在主对话里自己做。\n\n"
        f"可用专员:\n{roster}"
    )
    return {
        "name": AGENT_TOOL_NAME,
        "description": description,
        # strict 交集:全字段 required + additionalProperties false(同 tools.py 约定)
        "schema": {
            "type": "object",
            "properties": {
                "target_agent": {
                    "type": "string",
                    "enum": names,
                    "description": "承接任务的专员名",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "交给专员的完整任务说明,必须自包含:目标、已知事实、"
                        "约束条件与期望的产出形式"
                    ),
                },
            },
            "required": ["target_agent", "task"],
            "additionalProperties": False,
        },
    }


def build_sub_agent_tools(
    agent: DelegatableAgent,
    parent_tools: Mapping[str, ToolDef],
) -> dict[str, ToolDef]:
    """专员工具集 = 主轮工具快照 ∩ 专员白名单 − 需确认工具 − 禁用工具。

    取主轮快照而不是全局 TOOLS 注册表,是为了继承主轮已经做过的裁剪:
    隔离 session 包装、联网能力开关(用户本轮关了联网就没有 web_search)、
    MCP 绑定、生图就绪判定。委派因此只会缩小可用面,不会绕过主轮的能力边界。
    confirm=True 的工具整体剔除:那类工具的设计前提就是人在环,而子 agent
    没有可以截停等待的用户。
    """
    return {
        name: tool
        for name in agent.tools
        if name not in _FORBIDDEN_SUB_TOOLS
        and (tool := parent_tools.get(name)) is not None
        and not tool.confirm
    }


def _deny_human_approval(preflight: ToolPreflight) -> ToolPreflight:
    """把"需要人工确认"的判定改写成拒绝(附可操作理由)。

    子 agent 无法暂停等人:ASK_USER 若原样返回,loop 会以 stop=confirm 收尾,
    子任务半途而废且没有任何人能答复。降级为 DENY 后,模型至少能换条只读路径
    或如实回报"这步要主对话批准"。
    """
    if preflight.decision in (PermissionDecision.ASK_USER, PermissionDecision.AUTO_REVIEW):
        return replace(
            preflight,
            decision=PermissionDecision.DENY,
            reason_code=_SUB_AGENT_DENY_REASON_CODE,
            reviewer_rationale=_HUMAN_APPROVAL_DENIED,
            override_eligible=False,
        )
    return preflight


def wrap_preflight(
    preflight: PreflightFn, lock: asyncio.Lock | None = None
) -> PreflightFn:
    """复用主轮 preflight,只把 ASK_USER 兜成 DENY;AUTO_REVIEW 仍交 reviewer。

    lock 串行化调用:主轮 preflight 闭包用的是会话的那一个 AsyncSession(动作
    规范化要查项目/资源归属),而多个子 agent 是并发跑的——同一 session 并发
    执行会直接炸在驱动层。主 loop 自身不需要这把锁:它的 preflight 在 gather
    之前顺序做完,与子 agent 的执行期不重叠。
    """

    async def _preflight(call, tool: ToolDef) -> ToolPreflight:
        if lock is None:
            evaluation = await preflight(call, tool)
        else:
            async with lock:
                evaluation = await preflight(call, tool)
        if evaluation.decision is PermissionDecision.ASK_USER:
            return _deny_human_approval(evaluation)
        return evaluation

    return _preflight


def wrap_reviewer(reviewer: ReviewFn | None) -> ReviewFn:
    """复用主轮 reviewer;它转交人工(escalate)或不可用时,子 agent 内一律拒绝。"""

    async def _reviewer(call, tool: ToolDef, routed: ToolPreflight) -> ToolPreflight:
        if reviewer is None:
            return _deny_human_approval(replace(routed, decision=PermissionDecision.ASK_USER))
        try:
            reviewed = await reviewer(call, tool, routed)
        except Exception:
            # reviewer 故障在主轮会升级给用户确认;子 agent 没这条路,只能拒
            return _deny_human_approval(replace(routed, decision=PermissionDecision.ASK_USER))
        return _deny_human_approval(reviewed)

    return _reviewer


async def run_sub_agent(
    agent: DelegatableAgent,
    task: str,
    *,
    llm: "LLMService",
    org_id: UUID,
    parent_tools: Mapping[str, ToolDef],
    tool_ctx: ToolContext,
    preflight: PreflightFn,
    reviewer: ReviewFn | None = None,
    run_id: str | None = None,
    policy_snapshot: dict | None = None,
    progress: Callable[[str], Awaitable[None]] | None = None,
    preflight_lock: asyncio.Lock | None = None,
) -> dict:
    """跑一次专员委派,返回 {output, turns, input_tokens, output_tokens, stop}。

    失败/超时返回 {"error": ...} 而不抛异常:委派结果要作为一条 tool result 回给
    主 agent,炸掉整轮反而让主对话丢失上下文。
    """
    sub_tools = build_sub_agent_tools(agent, parent_tools)
    if not sub_tools:
        return {
            "error": f"专员「{agent.name}」在本轮没有任何可用工具,无法执行委派",
            "agent": agent.name,
        }

    # 独立消息上下文:不带主会话 history。专员只应看到 task 里显式给出的事实,
    # 主对话里的旁支讨论既不该影响它,也没必要重复付这份 token。
    system = _compose_system_prompt(agent.system_prompt)
    user_text = task.strip()
    scope_hint = scope_marker(tool_ctx.chat_session)
    if scope_hint:
        # 会话绑定宿主作用域时补一行标识:宿主工具缺省的作用域参数会回退到会话
        # 绑定值,让专员知道自己在哪个作用域下工作,不至于反复追问
        user_text = f"{user_text}\n\n{scope_hint}"

    turns = 0

    async def llm_call(messages: list[dict], tool_schemas: list[dict]):
        nonlocal turns
        turns += 1
        return await llm.generate_with_tools(
            task=agent.model_task,
            system=system,
            messages=messages,
            tools=tool_schemas,
            org_id=org_id,
            timeout_override=float(agent.timeout_seconds),
            # 无 on_delta / on_thinking:子 agent 的中间产物不直接流给用户,
            # 只以 tool.progress 汇报节奏(见 sub_emit)
        )

    async def sub_emit(event: dict) -> None:
        if progress is None:
            return
        if event.get("type") == "tool.call":
            name = str(event.get("name") or "")
            tool = sub_tools.get(name)
            await progress(f"[{agent.name}] 调用 {tool.label or name if tool else name}…")

    try:
        outcome = await asyncio.wait_for(
            run_loop(
                llm_call,
                tools=sub_tools,
                allowed=list(sub_tools),
                max_turns=agent.max_turns,
                history=[],
                user_text=user_text,
                # 复用主轮 ToolContext:隔离执行器只从中取 progress/引用编号/联网源,
                # 引用编号沿用同一游标,子 agent 检索出的 [ref] 才不会与主轮撞号
                tool_ctx=replace(tool_ctx, progress=None),
                emit=sub_emit,
                preflight=wrap_preflight(preflight, preflight_lock),
                reviewer=wrap_reviewer(reviewer),
                run_id=run_id,
                policy_snapshot=policy_snapshot,
            ),
            timeout=float(agent.timeout_seconds),
        )
    except TimeoutError:
        if progress is not None:
            await progress(f"[{agent.name}] 超时中止")
        return {
            "error": f"专员「{agent.name}」执行超时({agent.timeout_seconds}s),未产出结论",
            "agent": agent.name,
            "turns": turns,
        }
    except Exception as exc:  # 子 agent 崩溃不炸主轮
        if progress is not None:
            await progress(f"[{agent.name}] 执行失败")
        return {
            "error": f"专员「{agent.name}」执行失败 {type(exc).__name__}: {exc}",
            "agent": agent.name,
            "turns": turns,
        }

    if progress is not None:
        await progress(
            f"[{agent.name}] 执行失败"
            if outcome.stop == "error"
            else f"[{agent.name}] 完成({turns} 轮)"
        )
    if outcome.stop == "error":
        return {
            "error": outcome.final_text or f"专员「{agent.name}」执行失败",
            "agent": agent.name,
            "turns": turns,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
        }
    return {
        "agent": agent.name,
        "output": outcome.final_text or "",
        "turns": turns,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "stop": outcome.stop,
    }


def build_delegation_tool(
    agents: Sequence[DelegatableAgent],
    *,
    llm: "LLMService",
    org_id: UUID,
    parent_tools: Mapping[str, ToolDef],
    preflight: Callable[[], PreflightFn],
    reviewer: Callable[[], ReviewFn | None],
    run_id: str | None = None,
    policy_snapshot: dict | None = None,
) -> ToolDef:
    """把可委派名单包成一个运行时 ToolDef,交给 service 注入本轮工具快照。

    preflight/reviewer 传的是取值函数而非值本身:service 在解析工具快照时就要
    注入本工具,而那两个闭包在其后才定义,延迟取值可以避免为此重排既有代码。
    """
    by_name = {agent.name: agent for agent in agents}
    schema = build_agent_tool_schema(agents)
    # 同一 run 内所有子 agent 共用一把锁:preflight 背后是会话那一个 AsyncSession
    preflight_lock = asyncio.Lock()

    async def execute(ctx: ToolContext, args: dict) -> dict:
        name = str(args.get("target_agent") or "").strip()
        task = str(args.get("task") or "").strip()
        agent = by_name.get(name)
        if agent is None:
            return {
                "error": f"专员 {name!r} 不存在或未开放委派,可选:{', '.join(by_name) or '无'}"
            }
        if not task:
            return {"error": "task 不能为空:请写清目标、已知事实与期望产出"}
        return await run_sub_agent(
            agent,
            task,
            llm=llm,
            org_id=org_id,
            parent_tools=parent_tools,
            tool_ctx=ctx,
            preflight=preflight(),
            reviewer=reviewer(),
            run_id=run_id,
            policy_snapshot=policy_snapshot,
            progress=ctx.progress,
            preflight_lock=preflight_lock,
        )

    return ToolDef(
        name=AGENT_TOOL_NAME,
        description=schema["description"],
        schema=schema["schema"],
        executor=execute,
        permission=AGENT_TOOL_PERMISSION,
        label="委派专员",
        category="协作",
    )
