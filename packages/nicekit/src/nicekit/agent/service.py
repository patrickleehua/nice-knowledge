"""chat 会话服务:历史重建 → run_loop → 消息持久化 → 事件落库 → SSE 事件流。

AgentEvent 契约(SSE data 即此 JSON,均带自增 seq 并先写 chat_events 再推送,
供断线重放):
  text.delta    {text, reset}                                 token 级增量,仅 SSE 不落库;
                                                              reset=true 表示丢弃已收增量(降级重试)
  thought.delta {text, reset}                                 思考过程增量(thinking/reasoning),
                                                              仅 SSE 不落库,规则同 text.delta
  thought       {text}                                        思考过程终值(thinking block/reasoning
                                                              summary)
  assistant.message {text}                                    带工具调用的模型公开过程说明
  tool.call     {tool_call_id, name, input}
  tool.progress {parent_tool_call_id, text}
  tool.result   {tool_call_id, name, ok, output}
  text          {text}                                        最终回答
  user.input.required {request}                               结构化澄清请求
  user.input.resolved {request_id,answers}                    澄清答复已恢复执行
  tool.confirm  {tool_call_id, name, input, summary}          高危工具截停等用户批准
  plan.update   {steps: [{id,title,status,note}]}             agent 执行计划全量更新
  websearch.builtin {query, results:[{title,url,page_age}]}   模型内置联网搜索的过程与来源
                                                              (provider 服务端执行,无 tool.call)
  stage.update  {stage, tool}                                 write 工具成功后的阶段变化。
                                                              stage 取值由 `ToolDef.stage`
                                                              自声明,SDK 不推导、不解释;
                                                              工具没声明就不发该事件。
  turn.done     {reason: end_turn/ask_user/confirm/max_turns/cancelled/error, usage}
  error         {message}

确认机制:loop 遇 confirm=True 工具 → 发 tool.confirm 并以 stop=confirm 收尾,
待批调用持久化到 ChatSession.pending_confirmation;下一次发消息时按三种方式恢复:
批准(执行工具)/ 拒绝 / 普通消息覆盖(视为未批准),结果作为 tool result 回给模型续跑。
恢复前先清 pending 并提交,即使本轮中途崩溃也不会二次执行已批准的高危工具。
结构化澄清独立持久化到 ChatSession.pending_user_input,只能由匹配 request_id
的一次性完整答复恢复,不会被普通消息覆盖。

与 TF(backend/app/services/agent/service.py)的差异(MIGRATION-PLAN §5.4):
- `stage.update` 不再按工具名前缀推导业务阶段,改读 `ToolDef.stage`;直读业务
  状态表的那段一并删除(SDK 不认识宿主的业务表)。
- 运行时上下文段(TF 硬接线的 working_context)改为遍历注册的 `ContextProvider`,
  带字符预算与超时,失败不阻塞对话。
- 记忆召回的 scope 解析走 `memory.ScopeResolver`(宿主注册)。
- 独立审批 reviewer 的 `business_facts` 改由宿主注入(`set_business_facts_provider`)。
- 缺省 agent 卡名可配(默认 `assistant`,TF 是写死的 workbench)。
- 能力就绪度探测改 `CapabilityProbe` 注册表(SDK 默认注册 websearch/imagegen)。
- 工具来源从模块级 TOOLS dict 换成 `ToolRegistry`;MCP 工具的权限元数据
  走 `mcp_manager.mcp_tool_permission`(优先读协议原生 read_only_hint),
  不再一律按 write 处理。
"""

import asyncio
import logging
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.agent import builtin_tools, run_inputs, runtime_tools, session_goal
from nicekit.agent.compression import (
    latest_success_summary,
    rows_after_summary,
    schedule_compression,
    summary_user_message,
)
from nicekit.agent.loop import (
    LoopOutcome,
    ToolExecutionOutcome,
    ToolExecutionStatus,
    ToolPreflight,
    execute_native_tool,
    public_approval_bundle,
    public_reviewer_override,
    run_loop,
    tool_result_succeeded,
    truncate_output,
)
from nicekit.agent.mcp_manager import (
    McpConnectionError,
    mcp_client_manager,
    mcp_tool_permission,
)
from nicekit.agent.memory import (
    MEMORY_RECALL_BLOCK_ID,
    record_recall_hits,
    render_memory_section,
    resolve_scope_refs,
    schedule_memory_extraction,
)
from nicekit.agent.memory import (
    recall as recall_memories,
)
from nicekit.agent.permissions import (
    ApprovalDecisionError,
    CapabilityBoundary,
    EffectivePermissionPolicy,
    PermissionAuditAction,
    active_reviewer_overrides,
    add_permission_audit,
    apply_approval_decisions,
    canonical_action_payload,
    canonicalize_tool_action,
    consume_reviewer_override,
    evaluate_tool_action_with_shadow,
    is_approval_bundle,
    load_effective_policy,
    permission_event_audit_detail,
    policy_snapshot_payload,
    review_tool_action,
    serialize_reviewer_context,
    supersede_pending_approval_bundle,
)
from nicekit.agent.prompts import build_system_prompt_blocks
from nicekit.agent.runtime_snapshot import emit_runtime_context_snapshot
from nicekit.agent.runtime_tools import RuntimeToolOverride
from nicekit.agent.sub_agents import (
    AGENT_TOOL_NAME,
    build_delegation_tool,
    list_delegatable_cards,
)
from nicekit.agent.sub_agents import wrap_preflight as wrap_unattended_preflight
from nicekit.agent.sub_agents import wrap_reviewer as wrap_unattended_reviewer
from nicekit.agent.terminal import terminalize_agent_run
from nicekit.agent.tool_trace import (
    TOOL_TRACE_BLOCK_ID,
    TOOL_TRACE_TRUNCATED_NOTE,
    build_tool_trace,
)
from nicekit.agent.tools import (
    ToolContext,
    ToolDef,
    ToolError,
    ToolRegistry,
    default_registry,
)
from nicekit.agent.user_input import (
    UserInputValidationError,
    normalize_answers,
)
from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.domain.agent_permission import PermissionDecision, PermissionScope
from nicekit.domain.agent_plan import continue_failed_plan_step
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import LLMService, get_llm_service
from nicekit.models.agent_permission import AgentPermissionGrant
from nicekit.models.chat import (
    AgentCard,
    AgentCardVersion,
    ChatEvent,
    ChatMessage,
    ChatSession,
)
from nicekit.models.mcp import McpServer

_TITLE_MAX = 50
# 计划步骤 note 里保留的失败因由长度上限(原始报错可能很长)
_PLAN_REASON_MAX = 120
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 扩展点 1:缺省 agent 卡名(TF 写死 workbench)
# ---------------------------------------------------------------------------

DEFAULT_AGENT_CARD_NAME = "assistant"
_default_card_name = DEFAULT_AGENT_CARD_NAME


def set_default_card_name(name: str | None) -> None:
    """改会话未指定卡时使用的缺省卡名(传 None 恢复 `assistant`)。"""
    global _default_card_name
    _default_card_name = (name or DEFAULT_AGENT_CARD_NAME).strip() or DEFAULT_AGENT_CARD_NAME


def default_card_name() -> str:
    return _default_card_name


# ---------------------------------------------------------------------------
# 扩展点 2:运行时上下文段(ContextProvider,替代 TF 的 working_context 硬接线)
# ---------------------------------------------------------------------------

# 单个上下文段的缺省字符预算与派生超时。定得紧是刻意的:上下文段只是让 agent
# 少查几次工具的导航信息,不是对话的必要输入——真相随时能用工具读到。宁可这一轮
# 不注入,也不能让派生异常或慢查询拖垮对话(参照 TF working_context.py 的三铁律)。
DEFAULT_CONTEXT_BUDGET_CHARS = 2500
DEFAULT_CONTEXT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """一次上下文派生的输入事实(只读;provider 不应改动其中任何对象)。"""

    org_id: UUID
    user_id: UUID
    chat_session: ChatSession
    # 本轮用户输入(相关性检索类 provider 用作查询词)
    query_text: str
    budget_chars: int


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """一个待注入 system prompt 的运行时上下文段。"""

    # 块 id:进 system_prompt_blocks 元信息与运行时快照,排障时据此定位来源
    block_id: str
    text: str
    # 人读备注,随快照落库(如"派生结果被截断")
    notes: tuple[str, ...] = ()


@runtime_checkable
class ContextProvider(Protocol):
    """宿主注册的运行时上下文派生器。

    契约:
    - `derive` 只读、不 commit(命中计数一类的写入自行结清,见 memory 的做法);
    - 超出 `budget_chars` 的产物整段丢弃(截断一半的上下文比没有更糟);
    - 超过 `timeout_seconds` 或抛异常一律降级为"这轮不注入",绝不冒泡。
    """

    name: str
    budget_chars: int
    timeout_seconds: float

    async def derive(
        self, session: AsyncSession, ctx: ContextRequest
    ) -> ContextBlock | None: ...


ContextDeriveFn = Callable[
    [AsyncSession, ContextRequest], Awaitable[ContextBlock | None]
]


@dataclass(frozen=True, slots=True)
class FunctionContextProvider:
    """把一个 async 函数包成 ContextProvider(注册函数式实现的便捷形态)。"""

    name: str
    _derive: ContextDeriveFn
    budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS
    timeout_seconds: float = DEFAULT_CONTEXT_TIMEOUT_SECONDS

    async def derive(
        self, session: AsyncSession, ctx: ContextRequest
    ) -> ContextBlock | None:
        return await self._derive(session, ctx)


_context_providers: list[ContextProvider] = []


def register_context_provider(provider: ContextProvider) -> ContextProvider:
    """登记一个上下文派生器(按注册顺序注入,排在记忆段与工具痕迹段之前)。"""
    if not hasattr(provider, "derive"):
        raise TypeError("ContextProvider 必须实现 async derive(session, ctx)")
    _context_providers.append(provider)
    return provider


def context_provider(
    name: str,
    *,
    budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS,
    timeout_seconds: float = DEFAULT_CONTEXT_TIMEOUT_SECONDS,
) -> Callable[[ContextDeriveFn], ContextDeriveFn]:
    """装饰器形态的注册入口。"""

    def _wrap(fn: ContextDeriveFn) -> ContextDeriveFn:
        register_context_provider(
            FunctionContextProvider(
                name=name,
                _derive=fn,
                budget_chars=budget_chars,
                timeout_seconds=timeout_seconds,
            )
        )
        return fn

    return _wrap


def registered_context_providers() -> tuple[ContextProvider, ...]:
    return tuple(_context_providers)


def reset_context_providers() -> None:
    """清空已注册的上下文派生器(测试 / 重新装配用)。"""
    _context_providers.clear()


# ---------------------------------------------------------------------------
# 扩展点 3:能力就绪度探测(CapabilityProbe,替代 TF 对 websearch/imagegen 的硬编码)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    session: AsyncSession
    # 当前工具快照里的工具名(探针据此判断自己关心的能力是否被放行)
    tool_names: frozenset[str]
    # 本轮用户对联网方式的选择(off / builtin / provider id / None=跟随管理端)
    web_search_choice: str | None


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """探测结论:撤掉哪些工具、给模型什么说明、往本轮运行时里写什么。"""

    drop_tools: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    runtime: Mapping[str, object] = MappingProxyType({})


@runtime_checkable
class CapabilityProbe(Protocol):
    name: str
    # 本探针关心的工具名;快照里一个都没有就跳过(不做无谓的配置查询)
    tools: frozenset[str]

    async def probe(self, request: CapabilityRequest) -> CapabilityOutcome | None: ...


_capability_probes: list[CapabilityProbe] = []


def register_capability_probe(probe: CapabilityProbe) -> CapabilityProbe:
    _capability_probes.append(probe)
    return probe


def registered_capability_probes() -> tuple[CapabilityProbe, ...]:
    return tuple(_capability_probes)


def reset_capability_probes() -> None:
    """清空并重新装上 SDK 默认探针(测试 / 重新装配用)。"""
    _capability_probes.clear()
    _register_default_capability_probes()


@dataclass(frozen=True, slots=True)
class _WebSearchProbe:
    """自建联网搜索 / 模型内置搜索的就绪度与本轮开关。"""

    name: str = "websearch"
    tools: frozenset[str] = frozenset({"web_search", "web_fetch"})

    async def probe(self, request: CapabilityRequest) -> CapabilityOutcome:
        # 局部 import:capabilities 子包不进 agent 的导入链(与 A2 同一裁决)
        from nicekit.capabilities.websearch.service import (
            WEBSEARCH_BUILTIN,
            WEBSEARCH_OFF,
            resolve_websearch_readiness,
        )

        choice = request.web_search_choice
        readiness = await resolve_websearch_readiness(request.session)
        if choice == WEBSEARCH_OFF:
            return CapabilityOutcome(
                drop_tools=("web_search", "web_fetch"),
                notes=(
                    "本轮联网搜索已被用户关闭。需要时效信息时请如实说明本轮无法联网，"
                    "只依据知识库与已有资料回答，不要臆测最新信息。",
                ),
            )
        if choice == WEBSEARCH_BUILTIN:
            # 内置搜索由模型服务端执行,自建搜索工具要撤掉——两条通路同时开着,
            # 模型会重复检索同一件事。web_fetch 保留:内置搜索只给摘要,
            # 深入原文仍要靠它。
            return CapabilityOutcome(
                drop_tools=("web_search",),
                notes=(
                    "本轮使用模型内置联网搜索:你可以直接检索最新信息并在回答中标注来源，"
                    "无需调用 web_search 工具(本轮未提供)。需要看网页原文时用 web_fetch。"
                    "诚实边界同样适用:检索没有返回结果时，必须如实说明"
                    "「本次联网未检索到相关结果」并说明你的依据来自何处，"
                    "严禁凭训练记忆补全具体日期、事件细节或来源链接——"
                    "没有真实来源支撑的时效性结论，宁可不给。",
                ),
                runtime={"builtin": readiness.builtin},
            )
        if not readiness.allows(choice):
            # 用户选完之后管理端撤了 key 也走这条:当作没开,不打断对话
            return CapabilityOutcome(
                drop_tools=("web_search", "web_fetch"),
                notes=(
                    "当前运行未提供联网搜索。用户要求查最新信息时请如实说明暂不可用，"
                    "不要编造来源或链接。",
                ),
            )
        # None 表示跟随管理端配置,不构成 provider 覆盖
        return CapabilityOutcome(runtime={"provider": choice})


@dataclass(frozen=True, slots=True)
class _ImageGenProbe:
    name: str = "imagegen"
    tools: frozenset[str] = frozenset({"image_generate"})

    async def probe(self, request: CapabilityRequest) -> CapabilityOutcome | None:
        from nicekit.capabilities.imagegen.service import (
            resolve_image_service_readiness,
        )

        readiness = await resolve_image_service_readiness(request.session)
        if readiness.ready:
            return None
        return CapabilityOutcome(
            drop_tools=("image_generate",),
            notes=(
                "当前运行未提供图片生成功能。用户要求生图时请如实说明暂不可用，"
                "不要请求确认、伪造图片或虚构生成进度。",
            ),
        )


def _register_default_capability_probes() -> None:
    """SDK 默认探针。

    只有 websearch 与 imagegen 需要探测:它们依赖外部 API 密钥,没配就该把工具
    撤掉并让模型如实说明。weather 的 provider 链自带无密钥兜底源(open_meteo),
    notify 是出站通道不进工具快照,两者都没有"就绪度"这一说。
    """
    register_capability_probe(_WebSearchProbe())
    register_capability_probe(_ImageGenProbe())


_register_default_capability_probes()


# ---------------------------------------------------------------------------
# 扩展点 4:独立审批 reviewer 的业务事实(TF 写死会话模式与绑定业务对象 id)
# ---------------------------------------------------------------------------

BusinessFactsProvider = Callable[[ChatSession], Mapping[str, object]]


def _default_business_facts(chat: ChatSession) -> dict:
    """SDK 默认:只给 SDK 自己认识的两件事——会话来源模式与绑定的作用域。"""
    origin = chat.origin_mode
    return {
        "conversation_mode": getattr(origin, "value", origin),
        "bound_scope": {
            "scope_type": chat.scope_type,
            "scope_id": str(chat.scope_id) if chat.scope_id else None,
        },
    }


_business_facts_provider: BusinessFactsProvider = _default_business_facts


def set_business_facts_provider(provider: BusinessFactsProvider | None) -> None:
    """注入宿主的业务事实(传 None 恢复 SDK 默认的会话模式 + 绑定作用域)。"""
    global _business_facts_provider
    _business_facts_provider = provider or _default_business_facts


def business_facts_for(chat: ChatSession) -> dict:
    """取本会话的业务事实;宿主实现异常时降级为默认值,不打断审批评审。"""
    try:
        facts = _business_facts_provider(chat)
    except Exception:  # noqa: BLE001 - 宿主事实派生失败不阻塞审批链路
        logger.warning("业务事实派生失败 session=%s", chat.id, exc_info=True)
        return _default_business_facts(chat)
    return dict(facts or {})


# ---------------------------------------------------------------------------
# agent 卡解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedAgentCard:
    card_id: UUID
    version_id: UUID
    name: str
    system_prompt: str
    model_task: str
    max_turns: int
    timeout_seconds: int
    tools: tuple[str, ...]
    mcp_bindings: tuple[dict, ...]
    skills: tuple[str, ...]


def agent_card_cache_key(card_id: UUID) -> str:
    return f"agent_card:{card_id}"


def _resolved_from_dict(data: dict) -> ResolvedAgentCard:
    return ResolvedAgentCard(
        card_id=UUID(data["card_id"]),
        version_id=UUID(data["version_id"]),
        name=data["name"],
        system_prompt=data["system_prompt"],
        model_task=data["model_task"],
        max_turns=data["max_turns"],
        timeout_seconds=data["timeout_seconds"],
        tools=tuple(data.get("tools") or []),
        mcp_bindings=tuple(data.get("mcp_bindings") or []),
        skills=tuple(data.get("skills") or []),
    )


async def _resolve_version(session: AsyncSession, card: AgentCard) -> ResolvedAgentCard:
    key = agent_card_cache_key(card.id)
    cached = await cache.get_json(key)
    if cached is not None:
        return _resolved_from_dict(cached)
    version = (
        await session.execute(
            select(AgentCardVersion).where(
                AgentCardVersion.card_id == card.id,
                AgentCardVersion.is_active,
                AgentCardVersion.status == "active",
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise LookupError("agent 卡没有 active 版本")
    result = ResolvedAgentCard(
        card_id=card.id,
        version_id=version.id,
        name=card.name,
        system_prompt=version.system_prompt,
        model_task=version.model_task,
        max_turns=version.max_turns,
        timeout_seconds=version.timeout_seconds,
        tools=tuple(version.tools or []),
        mcp_bindings=tuple(version.mcp_bindings or []),
        skills=tuple(version.skills or []),
    )
    payload = asdict(result)
    payload["card_id"] = str(result.card_id)
    payload["version_id"] = str(result.version_id)
    await cache.set_json(key, payload, 60)
    return result


async def resolve_card(
    session: AsyncSession, org_id: UUID, card_id: UUID | None
) -> ResolvedAgentCard:
    """取 agent 卡:指定 id,或缺省用默认卡名(本 org 覆盖优先,平台兜底)。"""
    if card_id is not None:
        card = (
            await session.execute(
                select(AgentCard).where(AgentCard.id == card_id, AgentCard.is_active)
            )
        ).scalar_one_or_none()
        if card is None:
            raise LookupError("agent 卡不存在或已停用")
        return await _resolve_version(session, card)
    platform_org = get_settings().platform_org_id
    name = default_card_name()
    cards = (
        (
            await session.execute(
                select(AgentCard).where(AgentCard.name == name, AgentCard.is_active)
            )
        )
        .scalars()
        .all()
    )
    card = next((c for c in cards if c.org_id == org_id), None) or next(
        (c for c in cards if c.org_id in (platform_org, None)), None
    )
    if card is None:
        raise LookupError(
            f"平台默认 {name} agent 卡未 seed,先调用 agent.seed.ensure_default_agent_card"
        )
    return await _resolve_version(session, card)


# ---------------------------------------------------------------------------
# 历史重建与续跑拼接
# ---------------------------------------------------------------------------


def rows_to_history(rows: list[ChatMessage]) -> list[dict]:
    """Rebuild legal provider history from ordered semantic message rows.

    One assistant turn may own several consecutive tool rows. Empty assistant
    rows are retained only when a tool row actually attaches to them; historical
    orphan placeholders are skipped instead of becoming invalid Anthropic text.
    """
    history: list[dict] = []
    active_assistant: dict | None = None
    assistant_appended = False
    for row in rows:
        if row.role == "user":
            active_assistant = None
            assistant_appended = False
            history.append({"role": "user", "content": row.content or ""})
        elif row.role == "assistant":
            active_assistant = {
                "role": "assistant",
                "content": row.content,
                "tool_calls": [],
            }
            assistant_appended = bool(row.content and row.content.strip())
            if assistant_appended:
                history.append(active_assistant)
        else:  # tool
            if not row.tool_call_id or not row.tool_name:
                logger.warning("skipping malformed historical tool message id=%s", row.id)
                continue
            if active_assistant is None:
                active_assistant = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                }
                assistant_appended = False
            if not assistant_appended:
                history.append(active_assistant)
                assistant_appended = True
            active_assistant.setdefault("tool_calls", []).append(
                {"id": row.tool_call_id, "name": row.tool_name, "arguments": row.tool_input or {}}
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": row.tool_call_id,
                    "name": row.tool_name,
                    "content": row.content or "{}",
                }
            )
    return history


def splice_pending_call(
    history: list[dict],
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    result: dict,
) -> list[dict]:
    """恢复确认截停:把待批 tool_call 挂回最后一条 assistant 并追加结果消息。

    与 rows_to_history 的重建口径一致(assistant 行的 tool_calls 全靠 tool 行回挂,
    暂停时未执行的调用在历史里不存在),故这里补挂后模型看到的对话合法闭合。
    返回需要持久化的新消息(仅 tool 行;assistant 行已在暂停轮持久化)。
    """
    if not history or history[-1].get("role") != "assistant":
        history.append({"role": "assistant", "content": None, "tool_calls": []})
    history[-1].setdefault("tool_calls", []).append(
        {"id": tool_call_id, "name": name, "arguments": args}
    )
    tool_msg = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": truncate_output(result),
        "tool_input": args,
        "tool_output": result,
    }
    history.append(tool_msg)
    return [tool_msg]


def splice_pending_bundle(
    history: list[dict],
    *,
    items: list[dict],
    results: list[dict],
) -> list[dict]:
    """Attach every stored model-hop call and result in its original order."""

    if len(items) != len(results):
        raise ValueError("approval bundle item/result count mismatch")
    if not history or history[-1].get("role") != "assistant":
        history.append({"role": "assistant", "content": None, "tool_calls": []})
    assistant = history[-1]
    assistant["tool_calls"] = [
        {
            "id": str(item["tool_call_id"]),
            "name": str(item["name"]),
            "arguments": dict(item.get("input") or {}),
        }
        for item in items
    ]
    persisted: list[dict] = []
    for item, result in zip(items, results, strict=True):
        tool_message = {
            "role": "tool",
            "tool_call_id": str(item["tool_call_id"]),
            "name": str(item["name"]),
            "content": truncate_output(result),
            "tool_input": dict(item.get("input") or {}),
            "tool_output": result,
        }
        history.append(tool_message)
        persisted.append(tool_message)
    return persisted


def _create_session_tool_grant(
    session: AsyncSession,
    *,
    policy: EffectivePermissionPolicy,
    chat: ChatSession,
    item: dict,
    now: datetime,
) -> AgentPermissionGrant | None:
    if (
        item.get("status") != "approved"
        or item.get("decision_scope") != "session_tool"
        or "session_tool" not in item.get("eligible_scopes", [])
    ):
        return None
    fingerprint = str(item.get("material_fingerprint") or "")
    if len(fingerprint) != 64:
        raise ApprovalDecisionError("approval item has no valid material fingerprint")
    action_scope = item.get("scope") or {}
    target_scope = action_scope.get("scope_id")
    scope_id = UUID(str(target_scope)) if target_scope else None
    resource_id = action_scope.get("resource_id")
    grant = AgentPermissionGrant(
        id=uuid4(),
        org_id=policy.org_id,
        user_id=policy.user_id,
        session_id=policy.session_id,
        # scope_type 只有会话绑定这一处来源:动作作用域解析产出的是 scope_id,
        # 种类名由会话携带(宿主的 ResourceResolver 只在同一种作用域内解析)
        scope_type=chat.scope_type if scope_id else None,
        scope_id=scope_id,
        tool_name=str(item["name"]),
        scope=PermissionScope.RESOURCE if scope_id else PermissionScope.SESSION,
        resource_type=action_scope.get("resource_type"),
        resource_id=str(resource_id) if resource_id else None,
        action_fingerprint=fingerprint,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        created_by=policy.user_id,
        expires_at=now + timedelta(seconds=policy.max_grant_ttl_seconds),
    )
    session.add(grant)
    item["grant_id"] = str(grant.id)
    add_permission_audit(
        session,
        org_id=policy.org_id,
        actor_user_id=policy.user_id,
        action=PermissionAuditAction.GRANT_CREATED,
        entity_id=grant.id,
        detail={
            "session_id": policy.session_id,
            "grant_id": grant.id,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "scope": grant.scope,
            "tool_name": grant.tool_name,
            "categories": item.get("categories") or [],
            "scope_type": grant.scope_type,
            "scope_id": grant.scope_id,
            "resource_type": grant.resource_type,
            "resource_id": grant.resource_id,
            "argument_hash": item.get("argument_hash"),
            "action_hash": item.get("action_hash"),
            "expires_at": grant.expires_at,
        },
    )
    return grant


# ---------------------------------------------------------------------------
# 运行时上下文段派生
# ---------------------------------------------------------------------------


async def _resolve_context_blocks(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    chat: ChatSession,
    query_text: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """遍历注册的 ContextProvider,返回 (extra_sections, 快照备注)。

    为什么整段可丢弃:上下文段只是让 agent 少查几个工具的导航信息,不是对话的
    必要输入——真相表随时能用工具读到。所以宁可这一轮不注入,也不能让派生异常、
    慢查询或超预算的产物拖垮对话;失败/超时/超预算都只在运行时快照留一条 note
    供排障(参照 TF working_context.py:7-12 的三铁律)。
    """
    sections: list[tuple[str, str]] = []
    notes: list[str] = []
    for provider in registered_context_providers():
        name = getattr(provider, "name", provider.__class__.__name__)
        budget = int(getattr(provider, "budget_chars", DEFAULT_CONTEXT_BUDGET_CHARS))
        timeout = float(
            getattr(provider, "timeout_seconds", DEFAULT_CONTEXT_TIMEOUT_SECONDS)
        )
        request = ContextRequest(
            org_id=org_id,
            user_id=user_id,
            chat_session=chat,
            query_text=query_text,
            budget_chars=budget,
        )
        try:
            block = await asyncio.wait_for(
                provider.derive(session, request), timeout=timeout
            )
        except TimeoutError:
            notes.append(f"{name}_skipped:上下文派生超出 {timeout:g}s 预算,本轮未注入")
            # 慢查询多半仍挂在这个连接上,回滚掉免得后续语句被拖住
            with suppress(Exception):
                await session.rollback()
            continue
        except Exception:  # noqa: BLE001 - 上下文派生绝不冒泡影响对话
            logger.warning("上下文段派生失败 provider=%s session=%s", name, chat.id, exc_info=True)
            # 回滚只读事务:失败语句会让 PG 事务转 aborted,不清掉后续工具快照查询会连坐
            with suppress(Exception):
                await session.rollback()
            notes.append(f"{name}_failed:上下文派生失败,本轮未注入")
            continue
        if block is None:
            continue
        text = (block.text or "").strip()
        notes.extend(block.notes)
        if not text:
            continue
        if len(text) > budget:
            # 截断一半的上下文比没有更糟(模型会把残缺当全貌),整段丢弃
            notes.append(
                f"{name}_skipped:派生结果 {len(text)} 字符超出 {budget} 预算,本轮未注入"
            )
            continue
        sections.append((block.block_id, text))
    return sections, notes


def _resolve_tool_trace(rows: list[ChatMessage]) -> tuple[list[tuple[str, str]], list[str]]:
    """派生"本会话已查过"注入段;返回 (extra_sections, 快照备注)。

    与 ContextProvider 段并列且同样可整段丢弃:它只让模型少重复调一次工具,
    真要数据随时能重查。rows 传全量消息行(不用 rows_after_summary 过滤的结果)
    ——被摘要覆盖的工具调用同样该进痕迹,理由见 tool_trace.py 模块注释。
    纯函数、不查库,故这里只需要防"脏数据把渲染搞崩",不需要回滚事务。
    """
    if not rows:
        return [], []
    try:
        text = build_tool_trace(rows)
    except Exception:  # noqa: BLE001 - 上下文派生绝不冒泡影响对话
        logger.warning("工具调用痕迹派生失败 session=%s", rows[0].session_id, exc_info=True)
        return [], ["tool_trace_failed:工具调用痕迹派生失败,本轮未注入"]
    if not text:
        return [], []
    notes = (
        ["tool_trace_truncated:痕迹超出字符预算,较早的调用已省略"]
        if TOOL_TRACE_TRUNCATED_NOTE in text
        else []
    )
    return [(TOOL_TRACE_BLOCK_ID, text)], notes


async def _resolve_memory_recall(
    session: AsyncSession,
    *,
    org_id: UUID,
    session_id: UUID,
    query_text: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """召回长期记忆注入段;返回 (extra_sections, 快照备注)。

    与 ContextProvider 段并列、同一容错口径:记忆是"这个作用域长期什么样"
    的背景,不是对话的必要输入——召回异常宁可这一轮不注入,也绝不打断对话。
    选中项与被丢弃项(含丢弃原因)都进快照备注,排障时要能回答"为什么这条
    记忆没进 prompt"。

    作用域 ref 走 memory 的 ScopeResolver 扩展点(宿主注册);未注册时只召回
    org 范围的记忆。命中计数在这里就地提交:本函数的调用点在 LLM 流式调用
    之前,把这笔写入结清才不会让主 session 带着未提交事务挂上几分钟
    (idle-in-transaction)。
    """
    try:
        scope_refs = await resolve_scope_refs(session, org_id, session_id)
        result = await recall_memories(
            session,
            org_id,
            None,
            query_text,
            extra_scope_refs=sorted(scope_refs.values()),
        )
    except Exception:  # noqa: BLE001 - 记忆召回绝不冒泡影响对话
        logger.warning("长期记忆召回失败 org=%s session=%s", org_id, session_id, exc_info=True)
        # 回滚只读事务:失败语句会让 PG 事务转 aborted,不清掉后续查询会连坐
        with suppress(Exception):
            await session.rollback()
        return [], ["memory_recall_failed:长期记忆召回失败,本轮未注入"]

    notes = result.snapshot_notes()
    if not result.selected:
        return [], notes
    try:
        await record_recall_hits(session, [item.id for item in result.selected])
        await session.commit()
    except Exception:  # noqa: BLE001 - 命中计数只是统计,失败不影响注入
        logger.warning("记忆命中计数写入失败 org=%s", org_id, exc_info=True)
        with suppress(Exception):
            await session.rollback()
        notes.append("memory_hit_count_failed:命中计数未记录(不影响本轮注入)")
    return [(MEMORY_RECALL_BLOCK_ID, render_memory_section(result.selected))], notes


# ---------------------------------------------------------------------------
# 工具快照
# ---------------------------------------------------------------------------


async def _run_capability_probes(
    session: AsyncSession,
    tool_names: Iterable[str],
    *,
    web_search_choice: str | None,
) -> tuple[set[str], list[str], dict]:
    """跑一遍注册的能力探针,返回 (要撤掉的工具, 能力说明, 运行时配置)。

    探针失败按 fail-closed 处理:撤掉它关心的工具并给模型一条"暂不可用"的说明。
    这比放行一个连配置都读不出来的能力安全——模型至少会如实告知,而不是把
    调用失败当成偶发错误反复重试。
    """
    names = frozenset(tool_names)
    drop: set[str] = set()
    notes: list[str] = []
    runtime: dict = {"provider": None, "builtin": None}
    for probe in registered_capability_probes():
        probe_name = getattr(probe, "name", probe.__class__.__name__)
        watched = frozenset(getattr(probe, "tools", frozenset()))
        if watched and not (names & watched):
            continue
        request = CapabilityRequest(
            session=session,
            tool_names=names,
            web_search_choice=web_search_choice,
        )
        try:
            outcome = await probe.probe(request)
        except Exception:  # noqa: BLE001 - 能力探测失败不打断对话
            logger.warning("能力探测失败 probe=%s", probe_name, exc_info=True)
            with suppress(Exception):
                await session.rollback()
            drop.update(watched)
            notes.append(
                f"当前运行未能确认「{probe_name}」能力是否可用,本轮已停用相关工具。"
                "涉及该能力的请求请如实说明暂不可用,不要伪造结果。"
            )
            continue
        if outcome is None:
            continue
        drop.update(outcome.drop_tools)
        notes.extend(outcome.notes)
        runtime.update(outcome.runtime)
    return drop, notes, runtime


async def _build_tool_snapshot(
    session: AsyncSession,
    card: ResolvedAgentCard,
    emit,
    *,
    session_factory: async_sessionmaker,
    org_id: UUID,
    user_id: UUID,
    role: str,
    chat_session_id: UUID,
    registry: ToolRegistry,
    web_search_choice: str | None = None,
    runtime_overrides: Sequence[str] | None = None,
    runtime_source: str = runtime_tools.SOURCE_USER_INPUT,
) -> tuple[dict[str, ToolDef], list[str], str | None, dict, list[RuntimeToolOverride]]:
    """Resolve native and MCP tools once; the loop receives an immutable per-run view.

    第四个返回值是本轮能力运行时:{"provider": 自建搜索源覆盖, "builtin": 内置搜索配置},
    两者互斥——选内置就不给自建工具,否则模型会把同一件事检索两遍。
    第五个返回值是运行时临时工具开关的逐项判定(含被拒项),透传给事件流与快照审计。

    runtime_overrides 是用户为本轮临时点开的工具名:只在这一次调用里并入快照,
    绝不写回 agent 卡白名单;放行面由 runtime_tools.resolve_overrides 把关(只读)。
    """
    catalog = registry.as_dict()
    skill_tools = {"skills_list", "skill_view"}
    snapshot = {
        name: catalog[name]
        for name in card.tools
        if name in catalog and (card.skills or name not in skill_tools)
    }
    if card.skills:
        snapshot.update({name: catalog[name] for name in skill_tools if name in catalog})
    # 数据源没接好的工具不进快照(与上面 skills 未配时不给 skill 工具同一个道理):
    # 暴露一个必然返回 empty 的工具只会诱导模型白跑一轮往返。
    if not builtin_tools.retrieval_snapshot_available():
        snapshot.pop("retrieval_get", None)

    # 运行时临时工具:在就绪度裁剪与执行器隔离之前并入,让它和卡白名单工具走
    # 完全同一条通路(临时开的 web_search 一样要过联网就绪检查、一样被隔离执行),
    # 不给它开任何后门。
    runtime_granted, runtime_decisions = runtime_tools.resolve_overrides(
        list(runtime_overrides or []),
        list(card.tools),
        catalog,
        source=runtime_source,
    )
    snapshot.update({name: catalog[name] for name in runtime_granted})

    # 能力就绪度:多条能力说明要能共存(既没配生图又关了联网时,两条都得让模型知道)
    dropped, capability_notes, capability_runtime = await _run_capability_probes(
        session, snapshot.keys(), web_search_choice=web_search_choice
    )
    for name in dropped:
        snapshot.pop(name, None)

    def _isolated_native_executor(bound_tool: ToolDef):
        async def execute(_ctx: ToolContext, args: dict) -> dict:
            call_session = org_session(session_factory, org_id)
            try:
                call_chat = (
                    await call_session.execute(
                        select(ChatSession).where(ChatSession.id == chat_session_id)
                    )
                ).scalar_one()
                call_ctx = ToolContext(
                    session=call_session,
                    org_id=org_id,
                    user_id=user_id,
                    role=role,
                    chat_session=call_chat,
                    skills=card.skills,
                    progress=_ctx.progress,
                    # 隔离执行器每次调用新建 ctx,引用编号游标必须沿用同一实例,
                    # 否则同一轮里的多次联网调用又会各自从 1 开始编号
                    citations=_ctx.citations,
                    websearch_provider=_ctx.websearch_provider,
                    # 可取消资源与终结回调是"本次调用"的登记簿(execute_native_tool
                    # 每次调用都换新容器),隔离执行器必须原样传下去,否则工具登记的
                    # 收敛动作在超时/取消时找不到
                    cancellable_resources=_ctx.cancellable_resources,
                    on_cancel=_ctx.on_cancel,
                )
                return await bound_tool.executor(call_ctx, args)
            finally:
                await call_session.close()

        return execute

    snapshot = {
        name: ToolDef(
            name=tool.name,
            description=tool.description,
            schema=tool.schema,
            executor=_isolated_native_executor(tool),
            permission=tool.permission,
            label=tool.label,
            category=tool.category,
            timeout_seconds=tool.timeout_seconds,
            confirm=tool.confirm,
            emit_start_progress=tool.emit_start_progress,
            stage=tool.stage,
            runtime_grantable=tool.runtime_grantable,
        )
        for name, tool in snapshot.items()
    }
    allowed = list(snapshot)
    binding_by_id: dict[UUID, dict] = {}
    for binding in card.mcp_bindings:
        try:
            binding_by_id[UUID(str(binding["server_id"]))] = binding
        except (KeyError, ValueError):
            continue
    if binding_by_id:
        servers = (
            (
                await session.execute(
                    select(McpServer).where(McpServer.id.in_(binding_by_id), McpServer.enabled)
                )
            )
            .scalars()
            .all()
        )
        for server in servers:
            binding = binding_by_id[server.id]
            binding_tools = binding.get("enabled_tools")
            server_tools = server.enabled_tools
            try:
                specs = await mcp_client_manager.list_tools(server)
            except Exception as exc:
                await emit({"type": "error", "message": str(exc)})
                continue
            for spec in specs:
                # 双层白名单求交:服务器级 enabled_tools ∩ 卡绑定 enabled_tools
                if server_tools is not None and spec.name not in server_tools:
                    continue
                if binding_tools is not None and spec.name not in binding_tools:
                    continue
                public_name = f"mcp__{server.name}__{spec.name}"

                def _executor(bound_server: McpServer, bound_name: str):
                    async def execute(ctx: ToolContext, args: dict) -> dict:
                        try:
                            # 超时走服务器级配置(call_timeout_seconds,缺省 60s)
                            content = await mcp_client_manager.call_tool(
                                bound_server, bound_name, args
                            )
                        except McpConnectionError as exc:
                            raise ToolError(str(exc)) from exc
                        return {"content": content}

                    return execute

                snapshot[public_name] = ToolDef(
                    name=public_name,
                    description=spec.description,
                    schema=spec.schema,
                    executor=_executor(server, spec.name),
                    # A4:权限元数据优先读协议原生 annotations.read_only_hint,
                    # 其次服务器配置里的 tool_metadata;都没有才 fail-closed 按 write
                    permission=mcp_tool_permission(server, spec),
                )
                allowed.append(public_name)
    return (
        MappingProxyType(snapshot),
        allowed,
        "\n".join(capability_notes) or None,
        capability_runtime,
        runtime_decisions,
    )


def _persist_messages(
    session: AsyncSession,
    chat: ChatSession,
    new_messages: list[dict],
    *,
    run_id: UUID,
    start_sequence: int,
) -> None:
    sequence = start_sequence
    for msg in new_messages:
        if msg["role"] == "tool":
            session.add(
                ChatMessage(
                    org_id=chat.org_id,
                    session_id=chat.id,
                    sequence=sequence,
                    role="tool",
                    content=msg.get("content"),
                    tool_name=msg.get("name"),
                    tool_call_id=msg.get("tool_call_id"),
                    run_id=run_id,
                    tool_input=msg.get("tool_input"),
                    tool_output=msg.get("tool_output"),
                )
            )
        else:
            # assistant 带 tool_calls 的行只存文本;调用参数由后续 tool 行承载,重建时回挂
            session.add(
                ChatMessage(
                    org_id=chat.org_id,
                    session_id=chat.id,
                    sequence=sequence,
                    role=msg["role"],
                    content=msg.get("content"),
                    run_id=run_id,
                    trace_id=UUID(msg["trace_id"]) if msg.get("trace_id") else None,
                )
            )
        sequence += 1


async def _execute_turn(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    user_id: UUID,
    role: str,
    session_id: UUID,
    run_id: UUID,
    user_text: str,
    emit,
    llm: LLMService | None = None,
    confirm_action: dict | None = None,
    override_action: dict | None = None,
    user_input_action: dict | None = None,
    continue_plan_action: dict | None = None,
    thinking_level: str | None = None,
    model_override: dict[str, str] | None = None,
    web_search_choice: str | None = None,
    runtime_tool_requests: Sequence[str] | None = None,
    unattended: bool = False,
    registry: ToolRegistry | None = None,
) -> LoopOutcome:
    session = org_session(session_factory, org_id)
    tool_registry = registry or default_registry
    # 轮次是否走完(含正常错误收尾);False 表示异常/断连中断,finally 里兜底收敛
    turn_settled = False
    interrupt_reason = "连接中断"
    # 运行中输入队列是否已走到明确终局(消费完/终态跳过/刻意保留);
    # False 时在 finally 兜底跳过,保证异常中止的 run 不静默吞掉排队输入。
    queue_resolved = False
    try:
        chat = (
            await session.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one_or_none()
        if chat is None:
            raise LookupError("会话不存在")
        # 收编上一 run 遗留的 queued 项(上一 run 停在 confirm/ask_user/max_turns
        # 时队列被保留):改挂本 run,在本轮 end_turn 检查点按原顺序消费。
        await run_inputs.adopt_leftover_inputs(
            session, session_id=session_id, run_id=run_id
        )
        card = await resolve_card(session, org_id, chat.agent_card_id)
        effective_policy = await load_effective_policy(
            session,
            org_id=org_id,
            user_id=user_id,
            chat_session=chat,
        )
        policy_snapshot = policy_snapshot_payload(effective_policy)
        chat.permission_profile = effective_policy.profile
        chat.permission_scope = effective_policy.scope
        chat.permission_expires_at = effective_policy.expires_at
        chat.permission_policy_id = effective_policy.policy_id
        chat.permission_policy_version = effective_policy.policy_version
        chat.permission_policy_snapshot = policy_snapshot
        session.add(chat)
        rows = (
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.sequence)
                )
            )
            .scalars()
            .all()
        )
        next_message_sequence = max((row.sequence for row in rows), default=0) + 1
        # 会话压缩注入:最新成功摘要覆盖的旧消息不进模型历史,由一条摘要消息
        # 代替。摘要用 user 而非 system 角色——多条 system 在各 provider 的
        # 兼容性不一(Anthropic 仅接受单条顶层 system);消息行本身不删,
        # 前端展示与审计不受影响。
        history_summary = await latest_success_summary(session, session_id)
        history = rows_to_history(rows_after_summary(rows, history_summary))
        if history_summary is not None:
            history.insert(0, summary_user_message(history_summary))
        pending_user_input = dict(chat.pending_user_input or {})
        resume_user_input: dict | None = None
        if user_input_action is not None:
            if not pending_user_input or str(
                user_input_action.get("request_id")
            ) != str(pending_user_input.get("request_id")):
                raise UserInputValidationError("当前没有匹配的待答澄清请求")
            resume_user_input = {
                "request": pending_user_input,
                "result": {
                    "request_id": str(pending_user_input["request_id"]),
                    "answers": normalize_answers(
                        pending_user_input,
                        user_input_action,
                    ),
                },
            }
            # 先清后续跑：客户端重试同一提交不会重复恢复同一工具调用。
            chat.pending_user_input = None
            session.add(chat)

        continued_plan_step: dict | None = None
        if continue_plan_action is not None:
            step_id = str(continue_plan_action.get("step_id") or "")
            chat.plan, continued_plan_step = continue_failed_plan_step(
                chat.plan,
                step_id=step_id,
            )
            session.add(chat)
        resume_override: dict | None = None
        active_overrides = active_reviewer_overrides(
            chat.reviewer_override_candidates or []
        )
        if override_action is not None:
            active_overrides, resume_override = consume_reviewer_override(
                active_overrides,
                override_action,
            )
        if active_overrides != (chat.reviewer_override_candidates or []):
            chat.reviewer_override_candidates = active_overrides
            session.add(chat)
        pending = dict(chat.pending_confirmation or {})
        resume_mode: str | None = None
        resume_bundle: dict | None = None
        bundle_decision_events: list[dict] = []
        legacy_decision_event: dict | None = None
        if pending:
            if is_approval_bundle(pending):
                bundle_update = (
                    apply_approval_decisions(pending, confirm_action)
                    if confirm_action is not None
                    else supersede_pending_approval_bundle(pending)
                )
                pending = bundle_update.bundle
                bundle_decision_events = [
                    {
                        "type": "approval.bundle.decision",
                        "bundle_id": pending["bundle_id"],
                        "tool_call_id": item["tool_call_id"],
                        "name": item.get("name"),
                        "decision": item["decision"],
                        "decision_source": "user",
                        "scope": item["decision_scope"],
                        "note": item.get("user_note"),
                        "reason_code": item.get("decision_reason_code"),
                        "policy_reason_code": item.get("reason_code"),
                        "risk": item.get("risk"),
                        "categories": item.get("categories") or [],
                        "argument_hash": item.get("argument_hash"),
                        "action_hash": item.get("action_hash"),
                        "action_scope": item.get("scope"),
                        "policy_id": (
                            str(effective_policy.policy_id)
                            if effective_policy.policy_id
                            else None
                        ),
                        "policy_version": effective_policy.policy_version,
                        "profile": effective_policy.profile.value,
                        "permission_scope": effective_policy.scope.value,
                    }
                    for item in bundle_update.changed_items
                ]
                if not bundle_update.resolved:
                    chat.pending_confirmation = pending
                    session.add(chat)
                    await session.commit()
                    await emit({"type": "policy.snapshot", **policy_snapshot})
                    for event in bundle_decision_events:
                        await emit(event)
                    await emit(
                        {
                            "type": "approval.bundle.updated",
                            "bundle": public_approval_bundle(pending),
                        }
                    )
                    # 继续等审批:队列刻意保留(不消费不跳过),由下一 run 收编
                    queue_resolved = True
                    return LoopOutcome(
                        stop="confirm",
                        final_text=None,
                        pending_confirmation=pending,
                    )
                decision_time = datetime.now(UTC)
                for item in pending["items"]:
                    grant = _create_session_tool_grant(
                        session,
                        policy=effective_policy,
                        chat=chat,
                        item=item,
                        now=decision_time,
                    )
                    if grant is not None:
                        for event in bundle_decision_events:
                            if event["tool_call_id"] == item["tool_call_id"]:
                                event["grant_id"] = str(grant.id)
                resume_bundle = pending
                chat.pending_confirmation = None
                session.add(chat)
            else:
                if confirm_action is not None and str(confirm_action.get("tool_call_id")) == str(
                    pending.get("tool_call_id")
                ):
                    resume_mode = "approved" if confirm_action.get("approved") else "denied"
                else:
                    resume_mode = "superseded"  # 带着 pending 发普通消息 = 未批准
                legacy_decision_event = {
                    "type": "approval.legacy.decision",
                    "tool_call_id": pending.get("tool_call_id"),
                    "name": pending.get("name"),
                    "decision": "approve" if resume_mode == "approved" else "deny",
                    "decision_source": "user",
                    "scope": "once",
                    "reason_code": (
                        "user_approved"
                        if resume_mode == "approved"
                        else (
                            "user_denied"
                            if resume_mode == "denied"
                            else "user_superseded"
                        )
                    ),
                    "risk": pending.get("risk"),
                    "categories": pending.get("categories") or [],
                    "argument_hash": pending.get("argument_hash"),
                    "action_hash": pending.get("action_hash"),
                    "action_scope": pending.get("scope"),
                    "policy_id": (
                        str(effective_policy.policy_id)
                        if effective_policy.policy_id
                        else None
                    ),
                    "policy_version": effective_policy.policy_version,
                    "profile": effective_policy.profile.value,
                    "permission_scope": effective_policy.scope.value,
                }
                # 旧单项确认仍采用先清后执行，防止客户端重试导致重复调用。
                chat.pending_confirmation = None
                session.add(chat)
        # 结束启动阶段的事务:LLM 流式一跳可达数分钟,
        # 不让主 session 悬挂 idle-in-transaction(锁与快照都无必要)
        await session.commit()
        await emit({"type": "policy.snapshot", **policy_snapshot})
        if resume_user_input is not None:
            await emit(
                {
                    "type": "user.input.resolved",
                    "request_id": resume_user_input["request"]["request_id"],
                    "answers": resume_user_input["result"]["answers"],
                }
            )
        if continued_plan_step is not None:
            await emit({"type": "plan.update", "steps": chat.plan or []})
            await emit(
                {
                    "type": "plan.step.continued",
                    "step_id": continued_plan_step["id"],
                }
            )
        for event in bundle_decision_events:
            await emit(event)
        if legacy_decision_event is not None:
            await emit(legacy_decision_event)
        if resume_override is not None:
            await emit(
                {
                    "type": "reviewer.override.consumed",
                    "override": public_reviewer_override(resume_override),
                }
            )

        service = llm or get_llm_service()
        # 宿主上下文段:让 agent 开口即知当前作用域的现状,不必连查几个工具才
        # 拼出全貌(纯只读派生,失败/超时/超预算都只降级不打断)
        provider_sections, provider_notes = await _resolve_context_blocks(
            session,
            org_id=org_id,
            user_id=user_id,
            chat=chat,
            query_text=user_text,
        )
        # 长期记忆召回:与宿主上下文并列的第二段运行时上下文。宿主上下文回答
        # "这个作用域现在到哪一步",记忆回答"它长期是什么样";用本轮用户输入
        # 作查询词,只注入与当下话题相关的几条。
        memory_sections, memory_notes = await _resolve_memory_recall(
            session,
            org_id=org_id,
            session_id=session_id,
            query_text=user_text,
        )
        # 工具调用痕迹:复用上面已查出的全量 rows(不再查库),让模型知道自己查过什么,
        # 免得把同一份数据来回检索——被摘要覆盖、或被双层预算清成占位符的调用也在其中
        trace_sections, trace_notes = _resolve_tool_trace(list(rows))
        # 段序:宿主现状 → 长期背景 → 本轮已查过什么(由业务事实到导航信息)
        context_sections = [*provider_sections, *memory_sections, *trace_sections]
        context_notes = [*provider_notes, *memory_notes, *trace_notes]
        # 分层组装:global_* 全局段永远前置,card.system_prompt 只作为角色段注入,
        # 防止业务方改卡 prompt 覆盖全局工具纪律/输出规则(能力段拿到快照后再补)
        runtime_system_prompt, system_prompt_blocks = build_system_prompt_blocks(
            card.system_prompt, None, context_sections
        )

        async def on_delta(chunk: str | None) -> None:
            # None = 降级链换 provider,通知前端丢弃已流出的半截文本
            await emit({"type": "text.delta", "text": chunk or "", "reset": chunk is None})

        async def on_thinking(chunk: str | None) -> None:
            await emit({"type": "thought.delta", "text": chunk or "", "reset": chunk is None})

        async def llm_call(messages: list[dict], tool_schemas: list[dict]):
            return await service.generate_with_tools(
                task=card.model_task,
                system=runtime_system_prompt,
                messages=messages,
                tools=tool_schemas,
                org_id=org_id,
                timeout_override=card.timeout_seconds,
                on_delta=on_delta,
                on_thinking=on_thinking,
                thinking_level=thinking_level,
                model_override=model_override,
                # 闭包在 run_loop 里才执行,那时 capability_runtime 已由下面的
                # 工具快照解析赋值
                builtin_web_search=capability_runtime["builtin"],
            )

        # 先解析工具快照:本轮的联网 provider 覆盖是它的产物,ToolContext 要用
        (
            run_tools,
            allowed_tools,
            capability_context,
            capability_runtime,
            runtime_tool_decisions,
        ) = await _build_tool_snapshot(
            session,
            card,
            emit,
            web_search_choice=web_search_choice,
            runtime_overrides=runtime_tool_requests,
            runtime_source=runtime_tools.SOURCE_USER_INPUT,
            session_factory=session_factory,
            org_id=org_id,
            user_id=user_id,
            role=role,
            chat_session_id=session_id,
            registry=tool_registry,
        )
        if runtime_tool_decisions:
            # 本轮临时工具的判定结果即时广播:被拒项(写工具/不存在)要让用户当场
            # 看到原因,否则他只会发现"我明明勾了却没用上"
            await emit(
                {
                    "type": runtime_tools.RUNTIME_TOOLS_EVENT_TYPE,
                    "source": runtime_tools.SOURCE_USER_INPUT,
                    "requests": runtime_tools.request_payloads(runtime_tool_decisions),
                }
            )
        # 子 agent 委派:组织里有可委派专员卡时,运行时注入 "Agent" 工具。
        # 运行时注入而不是写进卡白名单/seed:委派名单随专员卡的增删启停自动生效,
        # 主卡不必为此发新版本;卡的 tools 白名单仍只描述它自己的直接能力。
        # 专员工具集取自本轮快照(见 sub_agents.build_sub_agent_tools),委派只能
        # 缩小权限面;工具治理仍走下方的 permission_preflight / permission_reviewer。
        delegatable = await list_delegatable_cards(
            session, org_id, exclude_card_id=card.card_id
        )
        if delegatable:
            # 委派的 parent_tools 剔掉本轮临时工具:临时开关是"用户为这一轮自己开的",
            # 不该顺着委派扩散到子 agent 的可用面。排队输入那条路径同样沿用基线实例
            # (见下方 base_run_tools 的接线),两条路径口径一致。
            granted_runtime = {
                decision.tool_name
                for decision in runtime_tool_decisions
                if decision.reason == runtime_tools.OUTCOME_GRANTED
            }
            delegation_parent_tools = MappingProxyType(
                {name: tool for name, tool in run_tools.items() if name not in granted_runtime}
            )
            run_tools = MappingProxyType(
                {
                    **run_tools,
                    AGENT_TOOL_NAME: build_delegation_tool(
                        delegatable,
                        llm=service,
                        org_id=org_id,
                        parent_tools=delegation_parent_tools,
                        # 两个策略闭包在下方才定义,这里传取值函数延迟绑定,
                        # 免得为一次注入把既有代码顺序整体重排
                        preflight=lambda: permission_preflight,
                        reviewer=lambda: permission_reviewer,
                        run_id=str(run_id),
                        policy_snapshot=policy_snapshot,
                    ),
                }
            )
            allowed_tools = [*allowed_tools, AGENT_TOOL_NAME]
        tool_ctx = ToolContext(
            session=session,
            org_id=org_id,
            user_id=user_id,
            role=role,
            chat_session=chat,
            skills=card.skills,
            websearch_provider=capability_runtime["provider"],
        )
        if capability_context:
            # 能力快照就绪后整体重组(而非在旧串上追加),保证段序恒为 全局→角色→上下文→能力
            runtime_system_prompt, system_prompt_blocks = build_system_prompt_blocks(
                card.system_prompt, capability_context, context_sections
            )

        # 会话目标在此提前读出:goal_context 消息本身要等到确认续跑的 tool_call
        # 拼接之后才能插(见下方注入点),但快照必须记下"本轮目标是什么状态、
        # 会不会注入、预算烧到哪"。读一次复用,不为审计多查一遍库。
        active_goal = await session_goal.get_goal(session, session_id)

        # 运行时上下文快照(audit-only,详见 runtime_snapshot.py):system prompt /
        # 工具快照 / 历史 / 策略此刻全部就绪,压成一条 run.context_snapshot 事件随
        # 现有链路落 chat_events,供运行日志还原"本轮实际加载了什么"。只在每个 run
        # 的首轮记录:确认/审批/覆盖的续跑轮与截停前同源,不重复记;该载荷禁止被
        # 读回注入任何后续轮次的上下文(不是记忆源)。
        await emit_runtime_context_snapshot(
            emit,
            card=card,
            thinking_level=thinking_level,
            model_override=model_override,
            system_prompt=runtime_system_prompt,
            capability_context=capability_context,
            history=history,
            run_tools=run_tools,
            allowed_tools=allowed_tools,
            policy_snapshot=policy_snapshot,
            is_resume=(
                resume_mode is not None
                or resume_bundle is not None
                or resume_override is not None
            ),
            # 组装方给出的真实块清单(全局/角色/上下文段/工具痕迹/能力),不再由快照二次推导
            system_prompt_blocks=system_prompt_blocks,
            # 宿主上下文 / 记忆召回 / 工具痕迹的派生备注合流
            extra_notes=context_notes,
            # 临时工具的逐项判定(含被拒项与原因)随快照落库,供事后审计
            runtime_tool_requests=runtime_tools.request_payloads(runtime_tool_decisions),
            # 目标状态与预算(消息体注入在后面,这里只记状态供排障)
            session_goal=session_goal.goal_snapshot_payload(active_goal),
        )

        async def permission_preflight(call, tool: ToolDef) -> ToolPreflight:
            action = await canonicalize_tool_action(
                session,
                org_id=org_id,
                chat_session=chat,
                tool=tool,
                arguments=call.arguments,
            )
            comparison = evaluate_tool_action_with_shadow(
                effective_policy,
                tool,
                action,
                capability=CapabilityBoundary(
                    agent_tool_allowed=call.name in allowed_tools,
                    service_ready=call.name in run_tools,
                    session_owned=chat.user_id == user_id,
                ),
            )
            evaluation = comparison.enforced
            shadow = comparison.shadow
            return ToolPreflight(
                decision=evaluation.decision,
                reason_code=evaluation.reason_code,
                source=evaluation.source,
                action=canonical_action_payload(action),
                matched_rule=evaluation.matched_rule,
                grant_id=str(evaluation.grant_id) if evaluation.grant_id else None,
                shadow_decision=shadow.decision if shadow else None,
                shadow_reason_code=shadow.reason_code if shadow else None,
                shadow_source=shadow.source if shadow else None,
                shadow_matched_rule=shadow.matched_rule if shadow else None,
            )

        async def permission_reviewer(
            call: ToolCallRequest,
            tool: ToolDef,
            routed: ToolPreflight,
        ) -> ToolPreflight:
            permission = tool.permission
            context = serialize_reviewer_context(
                history=[*history, {"role": "user", "content": effective_text}],
                tool_name=tool.name,
                tool_label=tool.label or tool.name,
                action=routed.action or {},
                permission={
                    "effect": permission.effect.value,
                    "risk": permission.risk.value,
                    "categories": sorted(
                        category.value for category in permission.categories
                    ),
                    "reversibility": permission.reversibility.value,
                    "delegation": permission.delegation.value,
                    "scope": permission.scope.value,
                },
                reason_code=routed.reason_code,
                source=routed.source,
                matched_rule=routed.matched_rule,
                # 业务事实由宿主注入(SDK 默认只给会话模式与绑定作用域)
                business_facts=business_facts_for(chat),
            )
            review = await review_tool_action(
                service,
                org_id=org_id,
                context=context,
            )
            decision = {
                "approve": PermissionDecision.ALLOW,
                "deny": PermissionDecision.DENY,
                "escalate": PermissionDecision.ASK_USER,
            }[review.decision.value]
            return ToolPreflight(
                decision=decision,
                reason_code=review.reason_code,
                source="reviewer",
                action=routed.action,
                matched_rule=routed.matched_rule,
                reviewer_rationale=review.rationale,
                reviewer_decision=review.decision.value,
                reviewer_risk_flags=tuple(flag.value for flag in review.risk_flags),
                reviewer_available=review.available,
                override_eligible=bool(
                    decision is PermissionDecision.DENY
                    and permission.categories
                    & effective_policy.reviewer_overridable_categories
                ),
            )

        # 无人值守 run(iCron 定时任务):没有任何人守在另一端回答确认弹窗。
        # 若原样保留 ASK_USER,loop 会以 stop=confirm 收尾并把会话钉在
        # pending_confirmation 上,直到有人偶然打开这个会话——定时任务因此
        # 会悄悄堵死后续每一次调度。与子 agent 同一裁决(sub_agents.wrap_preflight):
        # ASK_USER/AUTO_REVIEW 一律降级 DENY,模型拿到"这步需人工批准"的拒绝
        # 理由后可以换条路或如实收尾。降级只会缩小权限面,不可能扩权。
        turn_preflight = permission_preflight
        turn_reviewer = permission_reviewer
        if unattended:
            turn_preflight = wrap_unattended_preflight(permission_preflight)
            turn_reviewer = wrap_unattended_reviewer(permission_reviewer)

        async def loop_emit(event: dict) -> None:
            await emit(event)
            if event.get("type") != "tool.result" or not event.get("ok"):
                return
            if event.get("name") == "plan_update":
                # 计划只是意图声明:发 plan.update 供前端渲染,不触发阶段刷新
                output = event.get("output") or {}
                await emit({"type": "plan.update", "steps": output.get("steps") or []})
                return
            if event.get("name") == "update_goal":
                # 会话目标状态变化同样只影响本会话,发 goal.updated 供前端横幅刷新;
                # 工具在独立 session 里执行,拿不到 emit,只能在这里转发它的产物
                output = event.get("output") or {}
                goal_payload = output.get("goal") if isinstance(output, dict) else None
                if goal_payload:
                    await emit(dict(goal_payload))
                return
            tool = run_tools.get(str(event.get("name")))
            if tool is None or tool.side_effect != "write":
                return
            # 阶段事件由工具自声明(ToolDef.stage)。SDK 既不按工具名前缀猜业务
            # 阶段,也不去读宿主的状态表——没声明就不发,前端据此不渲染时间线。
            if not tool.stage:
                return
            await emit(
                {
                    "type": "stage.update",
                    "stage": tool.stage,
                    "tool": tool.name,
                }
            )

        async def is_cancelled() -> bool:
            return bool(
                (
                    await session.execute(
                        select(ChatSession.cancel_requested).where(
                            ChatSession.id == session_id,
                            ChatSession.active_run_id == run_id,
                        )
                    )
                ).scalar_one_or_none()
            )

        native_settings = get_settings()

        async def execute_approved_tool(
            *,
            call_id: str,
            tool_name: str,
            tool: ToolDef,
            args: dict,
            progress,
        ) -> ToolExecutionOutcome:
            return await execute_native_tool(
                ToolCallRequest(id=call_id, name=tool_name, arguments=args),
                tool,
                replace(tool_ctx, progress=progress),
                timeout_seconds=native_settings.agent_native_tool_timeout_seconds,
                is_cancelled=is_cancelled,
                cancellation_poll_interval_seconds=(
                    native_settings.agent_cancellation_poll_interval_seconds
                ),
            )

        terminal_tool: ToolExecutionOutcome | None = None

        def remember_terminal(execution: ToolExecutionOutcome) -> dict:
            nonlocal terminal_tool
            if execution.is_terminal and terminal_tool is None:
                terminal_tool = execution
            return execution.output

        resume_messages: list[dict] = []
        effective_text = user_text
        if resume_user_input is not None:
            request = resume_user_input["request"]
            result = resume_user_input["result"]
            resume_messages = splice_pending_call(
                history,
                tool_call_id=str(request["tool_call_id"]),
                name="ask_user",
                args={"questions": request["questions"]},
                result=result,
            )
            if not effective_text.strip():
                effective_text = "(用户已回答澄清问题)"
        elif continued_plan_step is not None:
            note = str(continued_plan_step.get("note") or "").strip()
            effective_text = (
                f"(继续处理计划步骤：{continued_plan_step['title']}"
                f"{f'；上次失败：{note}' if note else ''})"
            )
        elif resume_override is not None:
            original_call_id = str(resume_override["tool_call_id"])
            call_id = f"override-{uuid4()}"
            tool_name = str(resume_override["name"])
            args = dict(resume_override.get("input") or {})
            await loop_emit(
                {
                    "type": "tool.call",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "input": resume_override.get("display_input") or {},
                    "override_of": original_call_id,
                }
            )
            if await is_cancelled():
                result = {
                    "error": "运行已取消，该覆盖操作未执行",
                    "executed": False,
                    "reason_code": "run_cancelled",
                }
            else:
                tool = run_tools.get(tool_name)
                if tool is None or tool_name not in allowed_tools:
                    result = {
                        "error": f"工具 {tool_name} 不在本 agent 的白名单内",
                        "executed": False,
                        "reason_code": "agent_tool_not_allowed",
                    }
                else:
                    call = ToolCallRequest(id=call_id, name=tool_name, arguments=args)
                    current_preflight = await permission_preflight(call, tool)
                    expected_hash = resume_override.get("action_hash")
                    actual_hash = (current_preflight.action or {}).get("action_hash")
                    overridable = bool(
                        tool.permission.categories
                        & effective_policy.reviewer_overridable_categories
                    )
                    if not expected_hash or expected_hash != actual_hash:
                        result = {
                            "error": "独立审批覆盖与当前动作指纹不一致",
                            "executed": False,
                            "reason_code": "reviewer_override_action_mismatch",
                        }
                    elif current_preflight.decision is PermissionDecision.DENY:
                        result = {
                            "error": "当前能力或组织策略已禁止该操作",
                            "executed": False,
                            "reason_code": current_preflight.reason_code,
                        }
                    elif not overridable:
                        result = {
                            "error": "组织策略不再允许覆盖该类独立审批拒绝",
                            "executed": False,
                            "reason_code": "reviewer_override_not_allowed",
                        }
                    else:
                        if tool.side_effect == "write" and tool.emit_start_progress:
                            await loop_emit(
                                {
                                    "type": "tool.progress",
                                    "parent_tool_call_id": call_id,
                                    "text": "用户已覆盖独立审批拒绝，开始执行",
                                }
                            )

                        async def report_override_progress(text: str) -> None:
                            if text.strip():
                                await loop_emit(
                                    {
                                        "type": "tool.progress",
                                        "parent_tool_call_id": call_id,
                                        "text": text.strip(),
                                    }
                                )

                        result = remember_terminal(
                            await execute_approved_tool(
                                call_id=call_id,
                                tool_name=tool_name,
                                tool=tool,
                                args=args,
                                progress=report_override_progress,
                            )
                        )
            await loop_emit(
                {
                    "type": "tool.result",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "ok": tool_result_succeeded(result),
                    "decision": "approve",
                    "decision_source": "user_override",
                    "output": result,
                    "override_of": original_call_id,
                }
            )
            resume_messages = splice_pending_call(
                history,
                tool_call_id=call_id,
                name=tool_name,
                args=args,
                result=result,
            )
            await loop_emit(
                {
                    "type": "reviewer.override.completed",
                    "candidate_id": resume_override["candidate_id"],
                    "tool_call_id": call_id,
                    "override_of": original_call_id,
                    "name": tool_name,
                    "ok": tool_result_succeeded(result),
                    "reason_code": result.get("reason_code"),
                    "risk": resume_override.get("risk"),
                    "categories": resume_override.get("categories") or [],
                    "argument_hash": resume_override.get("argument_hash"),
                    "action_hash": resume_override.get("action_hash"),
                    "scope": resume_override.get("scope"),
                    "policy_id": (
                        str(effective_policy.policy_id)
                        if effective_policy.policy_id
                        else None
                    ),
                    "policy_version": effective_policy.policy_version,
                    "profile": effective_policy.profile.value,
                    "permission_scope": effective_policy.scope.value,
                }
            )
            if not effective_text.strip():
                effective_text = "(已处理独立审批覆盖)"
        elif resume_bundle is not None:
            bundle_results: list[dict] = []
            for item in sorted(resume_bundle["items"], key=lambda value: value["order"]):
                call_id = str(item["tool_call_id"])
                tool_name = str(item["name"])
                args = dict(item.get("input") or {})
                tool_execution_attempted = False
                await loop_emit(
                    {
                        "type": "tool.call",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "input": item.get("display_input") or {},
                    }
                )
                if terminal_tool is not None:
                    reason_code = (
                        "run_cancelled"
                        if terminal_tool.status is ToolExecutionStatus.CANCELLED
                        else "prior_tool_timeout"
                    )
                    result = {
                        "error": "本轮已进入终态，该操作未执行",
                        "executed": False,
                        "reason_code": reason_code,
                    }
                elif await is_cancelled():
                    result = {
                        "error": "运行已取消，该操作未执行",
                        "executed": False,
                        "reason_code": "run_cancelled",
                    }
                elif item.get("status") == "denied":
                    decision_reason = item.get("decision_reason_code") or "user_denied"
                    default_error = (
                        "用户未批准该操作，直接发送了新消息"
                        if decision_reason == "user_superseded"
                        else "用户拒绝执行该操作"
                    )
                    result = {
                        "error": item.get("user_note") or default_error,
                        "executed": False,
                        "reason_code": decision_reason,
                        "policy_reason_code": item.get("reason_code"),
                    }
                else:
                    tool = run_tools.get(tool_name)
                    if tool is None or tool_name not in allowed_tools:
                        result = {
                            "error": f"工具 {tool_name} 不在本 agent 的白名单内",
                            "executed": False,
                            "reason_code": "agent_tool_not_allowed",
                        }
                    else:
                        call = ToolCallRequest(id=call_id, name=tool_name, arguments=args)
                        current_preflight = await permission_preflight(call, tool)
                        expected_hash = item.get("action_hash")
                        actual_hash = (current_preflight.action or {}).get("action_hash")
                        if expected_hash and expected_hash != actual_hash:
                            result = {
                                "error": "待批动作与当前动作指纹不一致",
                                "executed": False,
                                "reason_code": "approval_action_mismatch",
                            }
                        elif current_preflight.decision is PermissionDecision.DENY:
                            result = {
                                "error": "当前能力或组织策略已禁止该操作",
                                "executed": False,
                                "reason_code": current_preflight.reason_code,
                            }
                        elif (
                            item.get("status") == "allowed"
                            and current_preflight.decision is not PermissionDecision.ALLOW
                        ):
                            result = {
                                "error": "审批等待期间策略已变化，未执行该操作",
                                "executed": False,
                                "reason_code": "policy_changed_before_resume",
                            }
                        else:
                            if tool.side_effect == "write" and tool.emit_start_progress:
                                await loop_emit(
                                    {
                                        "type": "tool.progress",
                                        "parent_tool_call_id": call_id,
                                        "text": "审批已完成，开始执行",
                                    }
                                )

                            async def report_progress(
                                text: str, parent_call_id: str = call_id
                            ) -> None:
                                if text.strip():
                                    await loop_emit(
                                        {
                                            "type": "tool.progress",
                                            "parent_tool_call_id": parent_call_id,
                                            "text": text.strip(),
                                        }
                                    )

                            tool_execution_attempted = True
                            result = remember_terminal(
                                await execute_approved_tool(
                                    call_id=call_id,
                                    tool_name=tool_name,
                                    tool=tool,
                                    args=args,
                                    progress=report_progress,
                                )
                            )
                bundle_results.append(result)
                await loop_emit(
                    {
                        "type": "tool.result",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "ok": tool_result_succeeded(result),
                        "output": result,
                    }
                )
                if (
                    tool_execution_attempted
                    and item.get("reason_code") == "profile_full_access"
                ):
                    item_scope = item.get("scope") or {}
                    add_permission_audit(
                        session,
                        org_id=org_id,
                        actor_user_id=user_id,
                        action=PermissionAuditAction.FULL_ACCESS_EXECUTED,
                        entity_id=session_id,
                        detail={
                            "session_id": session_id,
                            "run_id": run_id,
                            "bundle_id": resume_bundle.get("bundle_id"),
                            "tool_call_id": call_id,
                            "policy_id": effective_policy.policy_id,
                            "policy_version": effective_policy.policy_version,
                            "profile": effective_policy.profile,
                            "scope": effective_policy.scope,
                            "tool_name": tool_name,
                            "decision": "allow",
                            "decision_source": "profile",
                            "reason_code": "profile_full_access",
                            "risk": item.get("risk"),
                            "categories": item.get("categories") or [],
                            "scope_id": item_scope.get("scope_id"),
                            "resource_type": item_scope.get("resource_type"),
                            "resource_id": item_scope.get("resource_id"),
                            "argument_hash": item.get("argument_hash"),
                            "action_hash": item.get("action_hash"),
                            "successful": tool_result_succeeded(result),
                        },
                    )
            ordered_items = sorted(
                resume_bundle["items"], key=lambda value: value["order"]
            )
            resume_messages = splice_pending_bundle(
                history,
                items=ordered_items,
                results=bundle_results,
            )
            await loop_emit(
                {
                    "type": "approval.bundle.completed",
                    "bundle": public_approval_bundle(
                        {**resume_bundle, "status": "completed"}
                    ),
                }
            )
            if not effective_text.strip():
                if len(ordered_items) == 1:
                    effective_text = (
                        "(已批准执行)"
                        if ordered_items[0].get("status") == "approved"
                        else "(已拒绝执行)"
                    )
                else:
                    effective_text = "(已处理审批决定)"
        elif resume_mode is not None:
            call_id = str(pending.get("tool_call_id"))
            tool_name = str(pending.get("name"))
            args = dict(pending.get("input") or {})
            # 恢复轮的事件流自包含:补发 tool.call,前端按 tool_call_id 归并出完整卡片
            await loop_emit(
                {"type": "tool.call", "tool_call_id": call_id, "name": tool_name, "input": args}
            )
            if resume_mode == "approved":
                tool = run_tools.get(tool_name)
                if tool is None or tool_name not in allowed_tools:
                    result = {"error": f"工具 {tool_name} 不在本 agent 的白名单内"}
                else:
                    if tool.side_effect == "write" and tool.emit_start_progress:
                        await loop_emit(
                            {
                                "type": "tool.progress",
                                "parent_tool_call_id": call_id,
                                "text": "用户已批准,开始执行",
                            }
                        )

                    async def report_progress(
                        text: str, parent_tool_call_id: str = call_id
                    ) -> None:
                        if text.strip():
                            await loop_emit(
                                {
                                    "type": "tool.progress",
                                    "parent_tool_call_id": parent_tool_call_id,
                                    "text": text.strip(),
                                }
                            )

                    result = remember_terminal(
                        await execute_approved_tool(
                            call_id=call_id,
                            tool_name=tool_name,
                            tool=tool,
                            args=args,
                            progress=report_progress,
                        )
                    )
                if not effective_text.strip():
                    effective_text = "(已批准执行)"
            else:
                result = {
                    "error": "用户拒绝执行该操作"
                    if resume_mode == "denied"
                    else "用户未批准该操作,直接发送了新消息"
                }
                if not effective_text.strip():
                    effective_text = "(已拒绝执行)"
            await loop_emit(
                {
                    "type": "tool.result",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "ok": tool_result_succeeded(result),
                    "output": result,
                }
            )
            resume_messages = splice_pending_call(
                history, tool_call_id=call_id, name=tool_name, args=args, result=result
            )

        # 会话目标注入:目标上下文追加在历史末尾、本轮 user 消息之前。
        # 位置在确认/审批的续跑拼接之后——那些拼接依赖 history 末尾的 assistant
        # 行来回挂 tool_call,先插一条 user 会破坏相邻关系。
        # 复用快照前读出的那次查询(active_goal),不重复查库
        goal_context = session_goal.build_goal_context_message(active_goal)
        if goal_context is not None:
            history.append(goal_context)

        if terminal_tool is not None:
            outcome = LoopOutcome(
                stop=(
                    "cancelled"
                    if terminal_tool.status is ToolExecutionStatus.CANCELLED
                    else "error"
                ),
                final_text=str(terminal_tool.output.get("error") or ""),
                terminal_tool=terminal_tool,
            )
        else:
            outcome = await run_loop(
                llm_call,
                tools=run_tools,
                allowed=allowed_tools,
                max_turns=card.max_turns,
                history=history,
                user_text=effective_text,
                tool_ctx=tool_ctx,
                emit=loop_emit,
                is_cancelled=is_cancelled,
                preflight=turn_preflight,
                reviewer=turn_reviewer,
                run_id=str(run_id),
                policy_snapshot=policy_snapshot,
                tool_timeout_seconds=(
                    native_settings.agent_native_tool_timeout_seconds
                ),
                cancellation_poll_interval_seconds=(
                    native_settings.agent_cancellation_poll_interval_seconds
                ),
            )

        if outcome.builtin_web_search_requests:
            # 内置搜索由模型侧按次计费,不走 websearch service 的计量,这里补记;
            # provider 名固定为 builtin,便于与自建搜索的用量分开看
            from nicekit.tenancy.usage import record_usage

            await record_usage(
                session, org_id=org_id, task="web.search",
                provider="builtin", model="",
                calls=outcome.builtin_web_search_requests,
            )

        persisted_outcome_messages = list(outcome.new_messages)
        if not user_text.strip() and (
            resume_bundle is not None
            or resume_mode is not None
            or resume_override is not None
            or resume_user_input is not None
            or continued_plan_step is not None
        ):
            # Continuation markers exist only to give the model an explicit
            # resume turn. They are not user-authored chat content and must not
            # reappear as a fake bubble after history refresh.
            for index, message in enumerate(persisted_outcome_messages):
                if (
                    message.get("role") == "user"
                    and message.get("content") == effective_text
                ):
                    del persisted_outcome_messages[index]
                    break

        _persist_messages(
            session,
            chat,
            [*resume_messages, *persisted_outcome_messages],
            run_id=run_id,
            start_sequence=next_message_sequence,
        )
        next_message_sequence += len(resume_messages) + len(persisted_outcome_messages)

        # —— 运行中输入队列:安全检查点消费 ——
        # 只有模型自然收尾(end_turn)才是安全检查点:此时上下文闭合、无待批
        # 工具、无未答提问,追加一条 user 输入再跑一轮是合法对话延续。
        # confirm/ask_user/max_turns 保留队列(下一 run 收编,见 adopt_leftover_inputs),
        # cancelled/error 走下方终态跳过。
        turn_history = [*history, *outcome.new_messages]
        # 本 run 的工具基线:排队输入可能自带"本轮临时工具",消费它时在基线上
        # 叠加,消费完立刻回到基线——临时开关的作用域是一条输入,不是整个 run
        base_run_tools, base_allowed_tools = run_tools, allowed_tools
        while outcome.stop == "end_turn":
            queued_item = await run_inputs.claim_next_queued_input(
                session, session_id=session_id, run_id=run_id
            )
            if queued_item is None:
                break
            await emit(run_inputs.queue_event_payload("input.consuming", queued_item))
            queued_text = queued_item.content
            # 上一条排队输入的临时工具不能顺延给下一条,先无条件回到基线
            run_tools, allowed_tools = base_run_tools, base_allowed_tools
            queued_runtime_tools = list(queued_item.runtime_tools or [])
            if queued_runtime_tools:
                (
                    queued_tools,
                    queued_allowed,
                    _queued_capability,
                    _queued_runtime,
                    queued_decisions,
                ) = await _build_tool_snapshot(
                    session,
                    card,
                    emit,
                    web_search_choice=web_search_choice,
                    runtime_overrides=queued_runtime_tools,
                    runtime_source=runtime_tools.SOURCE_QUEUED_INPUT,
                    session_factory=session_factory,
                    org_id=org_id,
                    user_id=user_id,
                    role=role,
                    chat_session_id=session_id,
                    registry=tool_registry,
                )
                if AGENT_TOOL_NAME in base_run_tools:
                    # 委派工具沿用本 run 基线实例:临时工具刻意不下放给子 agent,
                    # 委派只能缩小权限面(见上方 build_delegation_tool 的接线注释)
                    queued_tools = MappingProxyType(
                        {
                            **queued_tools,
                            AGENT_TOOL_NAME: base_run_tools[AGENT_TOOL_NAME],
                        }
                    )
                    queued_allowed = [*queued_allowed, AGENT_TOOL_NAME]
                # 重绑局部名:permission_preflight / loop_emit 闭包读的就是它们,
                # 权限守门与阶段事件随之落在这一轮的真实工具面上
                run_tools, allowed_tools = queued_tools, queued_allowed
                await emit(
                    {
                        "type": runtime_tools.RUNTIME_TOOLS_EVENT_TYPE,
                        "source": runtime_tools.SOURCE_QUEUED_INPUT,
                        "queued_input_id": str(queued_item.id),
                        "requests": runtime_tools.request_payloads(queued_decisions),
                    }
                )
            # 幂等转正式 user message 并 commit(消费即检查点,先落盘再续跑)
            consumed_message = await run_inputs.consume_queued_input(
                session, chat=chat, item=queued_item, sequence=next_message_sequence
            )
            next_message_sequence += 1
            await emit(
                run_inputs.queue_event_payload(
                    "input.consumed", queued_item, message_id=consumed_message.id
                )
            )
            outcome = await run_loop(
                llm_call,
                tools=run_tools,
                allowed=allowed_tools,
                max_turns=card.max_turns,
                history=turn_history,
                user_text=queued_text,
                tool_ctx=tool_ctx,
                emit=loop_emit,
                is_cancelled=is_cancelled,
                preflight=turn_preflight,
                reviewer=turn_reviewer,
                run_id=str(run_id),
                policy_snapshot=policy_snapshot,
                tool_timeout_seconds=native_settings.agent_native_tool_timeout_seconds,
                cancellation_poll_interval_seconds=(
                    native_settings.agent_cancellation_poll_interval_seconds
                ),
            )
            round_messages = list(outcome.new_messages)
            if round_messages and round_messages[0].get("role") == "user":
                # 首条 user 行已由 consume_queued_input 以自身 sequence 落库,不重复持久化
                round_messages = round_messages[1:]
            _persist_messages(
                session,
                chat,
                round_messages,
                run_id=run_id,
                start_sequence=next_message_sequence,
            )
            next_message_sequence += len(round_messages)
            turn_history = [*turn_history, *outcome.new_messages]

        if outcome.stop in ("cancelled", "error"):
            # Timeout/cancellation/LLM failure share one compare-and-set
            # terminalizer. It commits staged messages, host-owned resources,
            # queued inputs, plan, pending approval and active_run_id together.
            if chat.title == "新对话" and user_text.strip():
                chat.title = user_text.strip()[:_TITLE_MAX]
                session.add(chat)
            if active_goal is not None:
                await session_goal.record_turn_usage(
                    session,
                    session_id=session_id,
                    tokens=outcome.input_tokens + outcome.output_tokens,
                )
            terminal = await terminalize_agent_run(
                session,
                chat_session_id=session_id,
                run_id=run_id,
                stop=outcome.stop,
                reason=(outcome.final_text or f"run_{outcome.stop}"),
                cancellable_resources=(
                    outcome.terminal_tool.cancellable_resources
                    if outcome.terminal_tool is not None
                    else ()
                ),
            )
            queue_resolved = True
            turn_settled = True
            if terminal.committed:
                for event in terminal.queue_events:
                    await emit(event)
                if terminal.plan is not None:
                    await emit({"type": "plan.update", "steps": terminal.plan})
                schedule_compression(
                    session_factory,
                    org_id=org_id,
                    session_id=session_id,
                    llm=service,
                )
                schedule_memory_extraction(
                    session_factory,
                    org_id=org_id,
                    session_id=session_id,
                    llm=service,
                )
            return outcome
        queue_resolved = True

        if outcome.pending_confirmation is not None:
            chat.pending_confirmation = {
                **outcome.pending_confirmation,
                "run_id": str(run_id),
                "requested_at": datetime.now(UTC).isoformat(),
            }
            session.add(chat)
        if outcome.pending_user_input is not None:
            chat.pending_user_input = {
                **outcome.pending_user_input,
                "run_id": str(run_id),
                "requested_at": datetime.now(UTC).isoformat(),
            }
            session.add(chat)
        if outcome.reviewer_override_candidates:
            chat.reviewer_override_candidates = active_reviewer_overrides(
                [
                    *(chat.reviewer_override_candidates or []),
                    *outcome.reviewer_override_candidates,
                ]
            )
            session.add(chat)
        if chat.title == "新对话" and user_text.strip():
            chat.title = user_text.strip()[:_TITLE_MAX]
        if active_goal is not None:
            # 本轮 token 计入目标预算,与 turn 数据同事务落盘(记账不能因崩溃丢失)
            await session_goal.record_turn_usage(
                session,
                session_id=session_id,
                tokens=outcome.input_tokens + outcome.output_tokens,
            )
        await session.commit()
        turn_settled = True
        # turn 完成后异步评估会话压缩:水位判断与摘要生成都在后台任务里做,
        # 不阻塞本轮响应;任务内部吞异常只记日志(缺一次摘要无碍,下轮再评估)。
        schedule_compression(
            session_factory, org_id=org_id, session_id=session_id, llm=service
        )
        # 长期记忆抽取同样后台跑(同一模式):从刚结束的对话沉淀偏好/约束/决策。
        # 抽取产物不直接落库,先过 memory.ingest_candidate 的防污染与去重管道;
        # 少沉淀一次无碍,下一个 turn 会再抽,故任务内部吞异常只记日志。
        schedule_memory_extraction(
            session_factory, org_id=org_id, session_id=session_id, llm=service
        )
        # 会话目标自动续跑:只有模型自然收尾(end_turn)才考虑起新 run——
        # confirm/ask_user 还等着用户,cancelled/error 说明这轮已经出事,
        # 续跑都会抢跑。其余否决条件在后台任务里复查(session_goal.can_continue)。
        if active_goal is not None and outcome.stop == "end_turn":
            session_goal.schedule_continuation(
                session_factory,
                org_id=org_id,
                user_id=user_id,
                role=role,
                session_id=session_id,
                llm=service,
            )
        return outcome
    except BaseException as exc:  # 断连/取消/未捕获异常:记录中断因由后原样抛出
        detail = str(exc).strip()
        interrupt_reason = f"{type(exc).__name__}:{detail}" if detail else type(exc).__name__
        raise
    finally:
        # active_run_id 是跨 worker 的会话互斥标记，任何退出路径都必须释放。
        try:
            await session.rollback()
            if not turn_settled:
                terminal = await terminalize_agent_run(
                    session,
                    chat_session_id=session_id,
                    run_id=run_id,
                    stop="error",
                    reason=interrupt_reason[:_PLAN_REASON_MAX],
                )
                if terminal.committed:
                    queue_resolved = True
                    for event in terminal.queue_events:
                        await emit(event)
                    if terminal.plan is not None:
                        await emit({"type": "plan.update", "steps": terminal.plan})
            else:
                chat = (
                    await session.execute(
                        select(ChatSession)
                        .where(ChatSession.id == session_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if chat is not None and chat.active_run_id == run_id:
                    chat.active_run_id = None
                    chat.cancel_requested = False
                    session.add(chat)
                await session.commit()
        except Exception:
            logger.exception("释放会话 active_run_id 失败 session=%s run=%s", session_id, run_id)
            await session.rollback()
        if not queue_resolved:
            # run 异常中止:剩余排队输入兜底标 skipped(best-effort),不静默丢弃
            try:
                for skipped_item in await run_inputs.skip_queued_inputs(
                    session, session_id=session_id, run_id=run_id, reason="run_error"
                ):
                    await emit(
                        run_inputs.queue_event_payload(
                            "input.skipped", skipped_item, reason="run_error"
                        )
                    )
            except Exception:
                logger.exception(
                    "兜底跳过排队输入失败 session=%s run=%s", session_id, run_id
                )
                await session.rollback()
        await session.close()


async def run_turn_events(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    user_id: UUID,
    role: str,
    session_id: UUID,
    run_id: UUID,
    user_text: str,
    llm: LLMService | None = None,
    confirm_action: dict | None = None,
    override_action: dict | None = None,
    user_input_action: dict | None = None,
    continue_plan_action: dict | None = None,
    thinking_level: str | None = None,
    model_override: dict[str, str] | None = None,
    web_search_choice: str | None = None,
    runtime_tool_requests: Sequence[str] | None = None,
    unattended: bool = False,
    registry: ToolRegistry | None = None,
) -> AsyncIterator[dict]:
    """SSE 事件流:边执行边产出事件,末尾 turn.done。异常转 error 事件不上抛。

    unattended=True 表示这一轮没有在线用户(iCron 定时任务):工具治理照旧,
    但"需人工确认"的判定降级为拒绝,防止 run 停在确认弹窗上把会话钉死。
    registry 缺省用 `tools.default_registry`(SDK 内置工具 + 宿主注册的工具)。
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    seq = 0
    active_policy_snapshot: dict = {}
    full_access_decisions: dict[str, dict] = {}

    async def emit(ev: dict) -> None:
        nonlocal seq
        seq += 1
        payload = {**ev, "seq": seq}
        event_type = str(ev.get("type") or "")
        audit_action: PermissionAuditAction | None = None
        audit_event = ev
        if event_type == "policy.snapshot":
            active_policy_snapshot.clear()
            active_policy_snapshot.update(ev)
        elif event_type == "reviewer.decision":
            audit_action = PermissionAuditAction.REVIEWER_DECIDED
        elif event_type in {
            "approval.bundle.decision",
            "approval.legacy.decision",
        }:
            audit_action = PermissionAuditAction.MANUAL_DECIDED
        elif event_type == "reviewer.override.completed":
            audit_action = PermissionAuditAction.REVIEWER_OVERRIDE_COMPLETED
            audit_event = {**ev, "successful": bool(ev.get("ok"))}
        elif event_type == "policy.decision" and ev.get("phase") == "final":
            call_id = str(ev.get("tool_call_id") or "")
            if (
                call_id
                and ev.get("decision") == PermissionDecision.ALLOW.value
                and ev.get("reason_code") == "profile_full_access"
            ):
                full_access_decisions[call_id] = dict(ev)
            elif (
                ev.get("decision") == PermissionDecision.DENY.value
                and ev.get("source") != "reviewer"
            ):
                audit_action = PermissionAuditAction.POLICY_DENIED
        elif event_type == "tool.result":
            full_access = full_access_decisions.pop(
                str(ev.get("tool_call_id") or ""),
                None,
            )
            if full_access is not None:
                audit_action = PermissionAuditAction.FULL_ACCESS_EXECUTED
                audit_event = {**full_access, "successful": bool(ev.get("ok"))}
        # text.delta / thought.delta 是瞬时传输增量,只推 SSE 不落库;
        # 断线重放靠随后的 thought/assistant.message/text 语义终值事件。
        # 落库是重放的 best-effort 补充,失败不得阻断正在推送的回合(消息持久化仍是硬保证)。
        if ev.get("type") not in ("text.delta", "thought.delta"):
            event_session = org_session(session_factory, org_id)
            try:
                event_session.add(
                    ChatEvent(
                        org_id=org_id,
                        session_id=session_id,
                        run_id=run_id,
                        seq=seq,
                        payload=payload,
                    )
                )
                if audit_action is not None:
                    try:
                        add_permission_audit(
                            event_session,
                            org_id=org_id,
                            actor_user_id=user_id,
                            action=audit_action,
                            entity_id=session_id,
                            detail=permission_event_audit_detail(
                                audit_event,
                                session_id=session_id,
                                run_id=run_id,
                                policy_snapshot=active_policy_snapshot,
                            ),
                        )
                    except ValueError:
                        logger.exception(
                            "Agent 权限审计投影失败 session=%s run=%s seq=%s",
                            session_id,
                            run_id,
                            seq,
                        )
                await event_session.commit()
            except Exception:
                logger.exception(
                    "chat_events 落库失败 session=%s run=%s seq=%s", session_id, run_id, seq
                )
            finally:
                await event_session.close()
        await queue.put(payload)

    async def _work() -> None:
        try:
            outcome = await _execute_turn(
                session_factory,
                org_id=org_id,
                user_id=user_id,
                role=role,
                session_id=session_id,
                run_id=run_id,
                user_text=user_text,
                emit=emit,
                llm=llm,
                confirm_action=confirm_action,
                override_action=override_action,
                user_input_action=user_input_action,
                continue_plan_action=continue_plan_action,
                thinking_level=thinking_level,
                model_override=model_override,
                web_search_choice=web_search_choice,
                runtime_tool_requests=runtime_tool_requests,
                unattended=unattended,
                registry=registry,
            )
            await emit(
                {
                    "type": "turn.done",
                    "reason": outcome.stop,
                    "usage": {
                        "input_tokens": outcome.input_tokens,
                        "output_tokens": outcome.output_tokens,
                    },
                }
            )
        except Exception as exc:
            logger.exception("agent turn 执行失败 session=%s run=%s", session_id, run_id)
            await emit({"type": "error", "message": str(exc)})
            await emit(
                {
                    "type": "turn.done",
                    "reason": "error",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(_work())
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
    finally:
        await task
