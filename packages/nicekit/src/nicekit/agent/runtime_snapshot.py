"""运行时上下文快照(audit-only):记录一次 agent run 实际加载了什么。

排障时最常见的问题是"这一轮模型到底看到了什么"——system prompt 是哪个版本、
拼了哪些块、放行了哪些工具、带了多少历史、生效的是哪份权限策略。这些信息
散落在启动日志与各类事件里,难以还原全貌,故在每个 run 的首轮把它们压成一条
run.context_snapshot 事件,随现有 emit 通道落 chat_events,管理端按 run_id 查询。

定位与边界(对齐 TripBoss docs/agent-loop-architecture.md §6.4 的裁决):
- 快照是 audit-only 数据,只服务运行日志排障,不是新的记忆源;
- 任何后续轮次的上下文都必须从业务真相表 / chat_messages 重新派生,
  禁止把历史快照读回并注入 prompt,避免 stale 快照反馈循环。
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from uuid import UUID

from nicekit.core.config import get_settings
from nicekit.llm.runtime_config import runtime_overrides

# 事件类型常量:service.py 落库与 admin.py 查询共用,避免字符串漂移
CONTEXT_SNAPSHOT_EVENT_TYPE = "run.context_snapshot"

# 预览只为"一眼确认拼的是哪份 prompt",500 字符足够定位版本;
# 全文不落快照——system prompt 可能上万字符,重复落库只会膨胀 chat_events
_SYSTEM_PROMPT_PREVIEW_CHARS = 500


class _CardLike(Protocol):
    """service.ResolvedAgentCard 的最小读取面(鸭子类型,避免反向 import)。"""

    card_id: UUID
    version_id: UUID
    model_task: str


@dataclass
class RuntimeContextSnapshot:
    """一次 agent run 首轮的上下文全貌(全部字段可 JSON 序列化后落 chat_events)。"""

    agent_card_id: str | None
    card_version: str | None  # AgentCardVersion.id(能力版本不可变,据此可回查全文)
    model_task: str | None
    thinking_level: str | None
    model_override: dict[str, str] | None
    system_prompt_chars: int
    system_prompt_preview: str  # 前 500 字符,只为肉眼确认版本,不是全文
    # system prompt 组装块清单 [{"id", "chars"}],顺序即拼接顺序、字符数求和守恒。
    # 由 prompts.build_system_prompt_blocks 直接给出(全局段/角色段/运行时上下文段/
    # 能力段);调用方未传时退回"卡片提示词 + capability_context"两块的粗略推导,
    # 兼容尚未接线的调用点。
    system_prompt_blocks: list[dict]
    history_messages: int  # 本轮注入的历史消息条数(不含本轮新用户输入)
    history_chars: int  # 历史 content 字符总量,粗略衡量上下文水位
    tools_allowed: list[dict]  # [{"name", "side_effect"}],白名单顺序
    tools_total: int
    policy_profile: str | None
    policy_version: int | None
    policy_id: str | None
    websearch_provider: str | None  # 仅在 web_search 放行时有值(auto/tavily/bocha)
    notes: list[str] = field(default_factory=list)  # 人读备注:降级、能力裁剪等
    # 本轮"运行时临时工具"请求的判定结果(见 runtime_tools.py):
    # [{"name","label","source","reason","accepted","reject_reason","hint"}]。
    # 被拒项也要留下:审计要能回答"用户要开这个工具,为什么没给"。
    runtime_tool_requests: list[dict] = field(default_factory=list)
    # 本轮生效的会话目标(见 session_goal.py):{"status","objective_preview",
    # "tokens_used","token_budget","auto_runs","injected"}。goal_context 消息本身
    # 注入在快照之后(它必须排在确认续跑的 tool_call 拼接之后,否则破坏 assistant
    # 与 tool 行的相邻关系),所以这里记的是"本轮会不会注入、目标处于什么状态",
    # 排障时要能回答"这轮 agent 是被目标驱动的吗、预算还剩多少"。
    session_goal: dict | None = None

    def to_event_payload(self) -> dict:
        """转成可直接 json.dumps 的事件载荷(UUID 已在构建期转 str)。"""
        return asdict(self)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def build_runtime_context_snapshot(
    *,
    card: _CardLike | None,
    thinking_level: str | None,
    model_override: Mapping[str, str] | None,
    system_prompt: str,
    capability_context: str | None,
    history: Sequence[Mapping[str, Any]],
    run_tools: Mapping[str, Any],
    allowed_tools: Sequence[str],
    policy_snapshot: Mapping[str, Any],
    system_prompt_blocks: Sequence[Mapping[str, Any]] | None = None,
    extra_notes: Sequence[str] | None = None,
    runtime_tool_requests: Sequence[Mapping[str, Any]] | None = None,
    session_goal: Mapping[str, Any] | None = None,
) -> RuntimeContextSnapshot:
    """由 _execute_turn 已就绪的组装产物构建快照(纯函数,不做任何 IO)。

    参数取"最终生效值":system_prompt 是拼完 capability_context 之后的版本,
    run_tools/allowed_tools 是 _build_tool_snapshot 的 per-run 冻结视图,
    policy_snapshot 是 policy_snapshot_payload 的落库口径——快照如实转录,
    不再二次推导,保证与模型实际看到的一致。
    system_prompt_blocks 同理由组装方(build_system_prompt_blocks)原样传入;
    extra_notes 收集组装期的降级说明(如工作上下文派生失败);
    runtime_tool_requests 是本轮临时工具开关的判定结果(_build_tool_snapshot 产出)。
    """
    if system_prompt_blocks is not None:
        blocks = [dict(block) for block in system_prompt_blocks]
    else:
        # 兼容路径:调用方未接线块清单时,按 capability_context 是追加块粗略推导
        blocks = []
        capability_chars = 0
        if capability_context:
            # 与 service.py 的拼接格式一致:"\n\n【运行时能力】" + capability_context
            capability_chars = len(f"\n\n【运行时能力】{capability_context}")
        blocks.append(
            {"id": "card_system_prompt", "chars": len(system_prompt) - capability_chars}
        )
        if capability_context:
            blocks.append({"id": "capability_context", "chars": capability_chars})

    notes: list[str] = list(extra_notes or [])
    if capability_context:
        # 出现 capability_context 即意味着某项能力被运行时裁剪(如生图服务未就绪)
        notes.append("system prompt 追加了运行时能力降级说明(capability_context)")

    # websearch provider 只在 web_search 实际放行时才有意义;
    # 读的是进程级 runtime_overrides 缓存(service_configs 覆盖 .env),无 DB IO——
    # 此处正处于 _execute_turn 提交事务之后,不能再让主 session 悬挂新事务
    websearch_provider: str | None = None
    if "web_search" in allowed_tools:
        websearch_provider = str(
            runtime_overrides("websearch").get("provider")
            or get_settings().websearch_provider
        )

    return RuntimeContextSnapshot(
        agent_card_id=_as_str(getattr(card, "card_id", None)),
        card_version=_as_str(getattr(card, "version_id", None)),
        model_task=getattr(card, "model_task", None),
        thinking_level=thinking_level,
        model_override=dict(model_override) if model_override else None,
        system_prompt_chars=len(system_prompt),
        system_prompt_preview=system_prompt[:_SYSTEM_PROMPT_PREVIEW_CHARS],
        system_prompt_blocks=blocks,
        history_messages=len(history),
        history_chars=sum(len(msg.get("content") or "") for msg in history),
        tools_allowed=[
            {
                "name": name,
                "side_effect": getattr(run_tools.get(name), "side_effect", None),
            }
            for name in allowed_tools
        ],
        tools_total=len(allowed_tools),
        policy_profile=policy_snapshot.get("profile"),
        policy_version=policy_snapshot.get("policy_version"),
        policy_id=policy_snapshot.get("policy_id"),
        websearch_provider=websearch_provider,
        notes=notes,
        runtime_tool_requests=[dict(item) for item in (runtime_tool_requests or [])],
        session_goal=dict(session_goal) if session_goal else None,
    )


async def emit_runtime_context_snapshot(
    emit: Callable[[dict], Awaitable[None]],
    *,
    card: _CardLike | None,
    thinking_level: str | None,
    model_override: Mapping[str, str] | None,
    system_prompt: str,
    capability_context: str | None,
    history: Sequence[Mapping[str, Any]],
    run_tools: Mapping[str, Any],
    allowed_tools: Sequence[str],
    policy_snapshot: Mapping[str, Any],
    is_resume: bool,
    system_prompt_blocks: Sequence[Mapping[str, Any]] | None = None,
    extra_notes: Sequence[str] | None = None,
    runtime_tool_requests: Sequence[Mapping[str, Any]] | None = None,
    session_goal: Mapping[str, Any] | None = None,
) -> dict | None:
    """构建并 emit 一条 run.context_snapshot 事件;返回载荷(续跑轮返回 None)。

    为什么只记每个 run 的首轮:确认截停(tool.confirm / approval bundle /
    reviewer override)之后的续跑轮,prompt / 工具 / 策略与截停前同源,重复
    快照只会稀释排障信号并膨胀 chat_events,故由调用方以 is_resume 标记跳过。
    事件本身不落 ChatMessage,前端事件投影对未知类型自然忽略,不影响会话渲染。
    """
    if is_resume:
        return None
    snapshot = build_runtime_context_snapshot(
        card=card,
        thinking_level=thinking_level,
        model_override=model_override,
        system_prompt=system_prompt,
        capability_context=capability_context,
        history=history,
        run_tools=run_tools,
        allowed_tools=allowed_tools,
        policy_snapshot=policy_snapshot,
        system_prompt_blocks=system_prompt_blocks,
        extra_notes=extra_notes,
        runtime_tool_requests=runtime_tool_requests,
        session_goal=session_goal,
    )
    payload = snapshot.to_event_payload()
    await emit({"type": CONTEXT_SNAPSHOT_EVENT_TYPE, **payload})
    return payload
