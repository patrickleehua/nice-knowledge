"""共享 agent loop(架构 §4.2):model + tools + loop,所有场景共用这一个实现。

纯编排、无 DB:LLM 调用与工具执行由调用方注入,产出事件序列 + 待持久化的
新消息(内部格式,见 providers.py 注释)。约束:工具白名单、max_turns、
ask_user 截停、工具结果截断(防上下文爆炸)。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from nicekit.agent.context_budget import (
    apply_tool_result_budget,
    compact_tool_output,
)
from nicekit.agent.tools import ToolContext, ToolDef, ToolError
from nicekit.agent.user_input import (
    UserInputValidationError,
    normalize_questions,
    public_user_input_request,
)
from nicekit.domain.agent_permission import PermissionDecision, ToolDelegation
from nicekit.llm.providers import ToolCallRequest
from nicekit.llm.service import ToolTurn

logger = logging.getLogger(__name__)

TOOL_OUTPUT_MAX_CHARS = 4000
TRUNCATED_MARK = "…(结果过长已截断,完整数据在系统里,可用更精确的参数重查)"
REVIEWER_OVERRIDE_TTL_SECONDS = 600

# llm_call(messages, tool_schemas) -> ToolTurn;由 service 绑定 LLMService/task/org
LlmCall = Callable[[list[dict], list[dict]], Awaitable[ToolTurn]]
# 事件回调(SSE 推送);loop 同时把事件收进 outcome
EmitFn = Callable[[dict], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    """Typed result from the shared native-tool execution boundary."""

    tool_call_id: str
    tool_name: str
    status: ToolExecutionStatus
    output: dict
    # 本次调用登记过、需要在终态收敛的外部资源标识
    # (见 ToolContext.cancellable_resources)
    cancellable_resources: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            ToolExecutionStatus.TIMED_OUT,
            ToolExecutionStatus.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class ToolPreflight:
    decision: PermissionDecision
    reason_code: str
    source: str
    action: dict | None = None
    matched_rule: str | None = None
    grant_id: str | None = None
    reviewer_rationale: str | None = None
    reviewer_decision: str | None = None
    reviewer_risk_flags: tuple[str, ...] = ()
    reviewer_available: bool | None = None
    override_eligible: bool = False
    circuit_breaker: bool = False
    shadow_decision: PermissionDecision | None = None
    shadow_reason_code: str | None = None
    shadow_source: str | None = None
    shadow_matched_rule: str | None = None


PreflightFn = Callable[[ToolCallRequest, ToolDef], Awaitable[ToolPreflight]]
ReviewFn = Callable[
    [ToolCallRequest, ToolDef, ToolPreflight], Awaitable[ToolPreflight]
]


@dataclass
class LoopOutcome:
    stop: str  # end_turn / ask_user / confirm / max_turns / cancelled / error
    final_text: str | None
    events: list[dict] = field(default_factory=list)
    new_messages: list[dict] = field(default_factory=list)  # 内部格式,调用方持久化
    input_tokens: int = 0
    output_tokens: int = 0
    # stop=confirm 时的待批工具调用 {tool_call_id, name, input, summary};
    # 由 service 持久化到会话,用户批准/拒绝后恢复执行。
    pending_confirmation: dict | None = None
    # stop=ask_user 时的结构化澄清请求；由 service 持久化后等待一次性答复。
    pending_user_input: dict | None = None
    reviewer_override_candidates: list[dict] = field(default_factory=list)
    # 模型内置联网搜索的请求次数(provider 按次计费,需单独计量)
    builtin_web_search_requests: int = 0
    terminal_tool: ToolExecutionOutcome | None = None


async def _cancel_and_await(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def execute_native_tool(
    call: ToolCallRequest,
    tool: ToolDef,
    tool_ctx: ToolContext | None,
    *,
    timeout_seconds: float,
    is_cancelled: CancelCheck | None = None,
    cancellation_poll_interval_seconds: float = 0.25,
) -> ToolExecutionOutcome:
    """Run one native tool under a total deadline and cooperative cancellation.

    The executor task and cancellation observer are always awaited after
    cancellation. ``CancelledError`` from the parent remains control flow and
    is never translated into an ordinary tool result.
    """

    effective_timeout = tool.timeout_seconds or timeout_seconds
    call_ctx = (
        replace(tool_ctx, cancellable_resources=set(), on_cancel=[])
        if tool_ctx is not None
        else None
    )

    def created_resources() -> tuple[str, ...]:
        if call_ctx is None:
            return ()
        return tuple(sorted(str(value) for value in call_ctx.cancellable_resources))

    async def run_cancel_hooks() -> None:
        """终态收尾:逐个调用工具登记的终结回调。

        回调是尽力而为的收尾(取消一个下游任务、释放一把锁),异常一律吞掉——
        让收尾失败去炸掉终结路径,会把"已取消"变成"状态不明"。
        """
        if call_ctx is None:
            return
        for hook in list(call_ctx.on_cancel):
            try:
                await hook(call_ctx)
            except Exception:  # noqa: BLE001 - 收尾失败不改变终态
                logger.exception("工具 %s 的取消回调失败", call.name)

    async def observe_cancellation() -> None:
        if is_cancelled is None:
            await asyncio.Future()
            return
        while not await is_cancelled():
            await asyncio.sleep(cancellation_poll_interval_seconds)

    tool_task = asyncio.create_task(tool.executor(call_ctx, call.arguments))
    cancellation_task = (
        asyncio.create_task(observe_cancellation())
        if is_cancelled is not None
        else None
    )
    waiters = {tool_task}
    if cancellation_task is not None:
        waiters.add(cancellation_task)
    try:
        try:
            async with asyncio.timeout(effective_timeout):
                done, _pending = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED
                )
                # Cancellation is authoritative when it races tool completion.
                if cancellation_task is not None and cancellation_task in done:
                    await cancellation_task
                    await _cancel_and_await(tool_task)
                    await run_cancel_hooks()
                    created = created_resources()
                    return ToolExecutionOutcome(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.CANCELLED,
                        output={
                            "error": "运行已取消，工具执行已中止",
                            "reason_code": "run_cancelled",
                            "cancellable_resources": list(created),
                        },
                        cancellable_resources=created,
                    )
                try:
                    output = await tool_task
                except TimeoutError:
                    await run_cancel_hooks()
                    created = created_resources()
                    return ToolExecutionOutcome(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.TIMED_OUT,
                        output={
                            "error": (
                                f"工具 {call.name} 的下游工作超过总执行时限，已中止"
                            ),
                            "reason_code": "tool_timeout",
                            "timeout_seconds": effective_timeout,
                            "cancellable_resources": list(created),
                        },
                        cancellable_resources=created,
                    )
                except ToolError as exc:
                    return ToolExecutionOutcome(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.FAILED,
                        output={"error": str(exc)},
                    )
                except Exception as exc:
                    return ToolExecutionOutcome(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        status=ToolExecutionStatus.FAILED,
                        output={
                            "error": f"工具执行异常 {type(exc).__name__}: {exc}"
                        },
                    )
                return ToolExecutionOutcome(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status=(
                        ToolExecutionStatus.SUCCEEDED
                        if tool_result_succeeded(output)
                        else ToolExecutionStatus.FAILED
                    ),
                    output=output,
                    cancellable_resources=created_resources(),
                )
        except TimeoutError:
            await _cancel_and_await(tool_task)
            await run_cancel_hooks()
            created = created_resources()
            return ToolExecutionOutcome(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolExecutionStatus.TIMED_OUT,
                output={
                    "error": (
                        f"工具 {call.name} 超过总执行时限 "
                        f"{effective_timeout:g}s，已中止"
                    ),
                    "reason_code": "tool_timeout",
                    "timeout_seconds": effective_timeout,
                    "cancellable_resources": list(created),
                },
                cancellable_resources=created,
            )
    except asyncio.CancelledError:
        await _cancel_and_await(tool_task)
        raise
    finally:
        if cancellation_task is not None:
            await _cancel_and_await(cancellation_task)


def truncate_output(output: dict) -> str:
    text = json.dumps(output, ensure_ascii=False, default=str)
    if len(text) > TOOL_OUTPUT_MAX_CHARS:
        return text[:TOOL_OUTPUT_MAX_CHARS] + TRUNCATED_MARK
    return text


def tool_result_succeeded(result: dict) -> bool:
    """Treat a missing, null, or empty error as a successful tool result."""
    return not bool(result.get("error"))


def _approval_bundle_item(
    call: ToolCallRequest,
    tool: ToolDef | None,
    preflight: ToolPreflight,
    *,
    order: int,
) -> dict:
    action = preflight.action or {}
    if preflight.decision is PermissionDecision.ASK_USER:
        status = "pending"
        decision = None
    elif preflight.decision is PermissionDecision.ALLOW:
        status = "allowed"
        decision = "approve"
    else:
        status = "denied"
        decision = "deny"
    eligible_scopes = ["once"]
    if (
        tool is not None
        and tool.permission.delegation is ToolDelegation.REVIEWABLE
        and preflight.reason_code != "organization_user_required"
    ):
        eligible_scopes.append("session_tool")
    label = tool.label or call.name if tool is not None else call.name
    return {
        "order": order,
        "tool_call_id": call.id,
        "name": call.name,
        "label": label,
        "summary": f"执行「{label}」",
        "input": call.arguments,
        "display_input": action.get("sanitized_arguments", call.arguments),
        "effect": tool.permission.effect.value if tool is not None else None,
        "risk": tool.permission.risk.value if tool is not None else "critical",
        "categories": (
            sorted(category.value for category in tool.permission.categories)
            if tool is not None
            else []
        ),
        "reversibility": (
            tool.permission.reversibility.value if tool is not None else None
        ),
        "delegation": (
            tool.permission.delegation.value
            if tool is not None
            else ToolDelegation.AGENT_FORBIDDEN.value
        ),
        "scope": action.get("scope"),
        "argument_hash": action.get("argument_hash"),
        "action_hash": action.get("action_hash"),
        "material_fingerprint": action.get("material_fingerprint"),
        "policy_decision": preflight.decision.value,
        "reason_code": preflight.reason_code,
        "decision_source": preflight.source,
        "matched_rule": preflight.matched_rule,
        "grant_id": preflight.grant_id,
        "reviewer_rationale": preflight.reviewer_rationale,
        "reviewer_decision": preflight.reviewer_decision,
        "reviewer_risk_flags": list(preflight.reviewer_risk_flags),
        "override_eligible": preflight.override_eligible,
        "circuit_breaker": preflight.circuit_breaker,
        "eligible_scopes": eligible_scopes,
        "status": status,
        "decision": decision,
        "decision_scope": None,
        "user_note": None,
    }


def public_approval_bundle(bundle: dict) -> dict:
    """Remove executable/raw arguments before an approval bundle enters events or APIs."""

    return {
        **{key: value for key, value in bundle.items() if key != "items"},
        "items": [
            {
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"input", "display_input"}
                },
                "input": item.get("display_input") or {},
            }
            for item in bundle.get("items", [])
        ],
    }


def public_pending_confirmation(value: dict | None) -> dict | None:
    """Project stored pending state without exposing executable arguments.

    New approval bundles retain a separately sanitized ``display_input`` for the
    UI. Legacy pending calls do not have that field, so their arguments must be
    omitted instead of guessing which values are safe to disclose.
    """

    if not value:
        return None
    if value.get("kind") == "approval_bundle" and isinstance(value.get("items"), list):
        return public_approval_bundle(value)
    return {
        key: item
        for key, item in value.items()
        if key not in {"input", "display_input"}
    } | {"input": value.get("display_input") or {}}


def public_reviewer_override(candidate: dict) -> dict:
    """Remove executable arguments before an override candidate enters events/APIs."""

    return {
        key: value
        for key, value in candidate.items()
        if key not in {"input", "display_input"}
    } | {"input": candidate.get("display_input") or {}}


def _reviewer_override_candidate(
    call: ToolCallRequest,
    tool: ToolDef,
    preflight: ToolPreflight,
    *,
    run_id: str | None,
    model_hop: int,
    policy_snapshot: dict,
) -> dict:
    now = datetime.now(UTC)
    action = preflight.action or {}
    return {
        "kind": "reviewer_override",
        "candidate_id": str(uuid4()),
        "run_id": run_id,
        "model_hop": model_hop,
        "tool_call_id": call.id,
        "name": call.name,
        "label": tool.label or call.name,
        "input": call.arguments,
        "display_input": action.get("sanitized_arguments") or {},
        "argument_hash": action.get("argument_hash"),
        "action_hash": action.get("action_hash"),
        "material_fingerprint": action.get("material_fingerprint"),
        "scope": action.get("scope"),
        "categories": sorted(category.value for category in tool.permission.categories),
        "risk": tool.permission.risk.value,
        "reviewer_rationale": preflight.reviewer_rationale,
        "reviewer_risk_flags": list(preflight.reviewer_risk_flags),
        "reason_code": preflight.reason_code,
        "policy_snapshot": policy_snapshot,
        "created_at": now.isoformat(),
        "expires_at": (
            now + timedelta(seconds=REVIEWER_OVERRIDE_TTL_SECONDS)
        ).isoformat(),
    }


def _policy_event(
    call: ToolCallRequest,
    preflight: ToolPreflight,
    *,
    tool: ToolDef | None = None,
    policy_snapshot: dict | None = None,
    phase: str = "final",
) -> dict:
    action = preflight.action or {}
    snapshot = policy_snapshot or {}
    return {
        "type": "policy.decision",
        "tool_call_id": call.id,
        "name": call.name,
        "decision": preflight.decision.value,
        "reason_code": preflight.reason_code,
        "source": preflight.source,
        "matched_rule": preflight.matched_rule,
        "grant_id": preflight.grant_id,
        "argument_hash": action.get("argument_hash"),
        "action_hash": action.get("action_hash"),
        "scope": action.get("scope"),
        "risk": tool.permission.risk.value if tool is not None else None,
        "categories": (
            sorted(category.value for category in tool.permission.categories)
            if tool is not None
            else []
        ),
        "policy_id": snapshot.get("policy_id"),
        "policy_version": snapshot.get("policy_version"),
        "profile": snapshot.get("profile"),
        "permission_scope": snapshot.get("scope"),
        "phase": phase,
    }


def _shadow_policy_event(
    call: ToolCallRequest,
    preflight: ToolPreflight,
    *,
    tool: ToolDef | None = None,
    policy_snapshot: dict | None = None,
) -> dict:
    action = preflight.action or {}
    snapshot = policy_snapshot or {}
    shadow_decision = preflight.shadow_decision
    return {
        "type": "policy.shadow_evaluation",
        "tool_call_id": call.id,
        "name": call.name,
        "enforced_decision": preflight.decision.value,
        "enforced_reason_code": preflight.reason_code,
        "enforced_source": preflight.source,
        "shadow_decision": shadow_decision.value if shadow_decision else None,
        "shadow_reason_code": preflight.shadow_reason_code,
        "shadow_source": preflight.shadow_source,
        "shadow_matched_rule": preflight.shadow_matched_rule,
        "differs": bool(
            shadow_decision is not None
            and (
                shadow_decision is not preflight.decision
                or preflight.shadow_reason_code != preflight.reason_code
                or preflight.shadow_source != preflight.source
            )
        ),
        "argument_hash": action.get("argument_hash"),
        "action_hash": action.get("action_hash"),
        "scope": action.get("scope"),
        "risk": tool.permission.risk.value if tool is not None else None,
        "categories": (
            sorted(category.value for category in tool.permission.categories)
            if tool is not None
            else []
        ),
        "policy_id": snapshot.get("policy_id"),
        "policy_version": snapshot.get("policy_version"),
        "profile": snapshot.get("profile"),
        "permission_scope": snapshot.get("scope"),
    }


def _reviewer_event(
    call: ToolCallRequest,
    preflight: ToolPreflight,
    *,
    tool: ToolDef | None = None,
    policy_snapshot: dict | None = None,
) -> dict:
    action = preflight.action or {}
    snapshot = policy_snapshot or {}
    return {
        "type": "reviewer.decision",
        "tool_call_id": call.id,
        "name": call.name,
        "decision": preflight.reviewer_decision or "escalate",
        "source": "reviewer",
        "reason_code": preflight.reason_code,
        "rationale": preflight.reviewer_rationale,
        "risk_flags": list(preflight.reviewer_risk_flags),
        "available": preflight.reviewer_available,
        "override_eligible": preflight.override_eligible,
        "circuit_breaker": preflight.circuit_breaker,
        "argument_hash": action.get("argument_hash"),
        "action_hash": action.get("action_hash"),
        "scope": action.get("scope"),
        "risk": tool.permission.risk.value if tool is not None else None,
        "categories": (
            sorted(category.value for category in tool.permission.categories)
            if tool is not None
            else []
        ),
        "policy_id": snapshot.get("policy_id"),
        "policy_version": snapshot.get("policy_version"),
        "profile": snapshot.get("profile"),
        "permission_scope": snapshot.get("scope"),
    }


def _denied_tool_result(preflight: ToolPreflight) -> dict:
    reviewer_denial = preflight.reason_code in {
        "reviewer_denied",
        "reviewer_circuit_breaker",
    }
    result = {
        "error": (
            preflight.reviewer_rationale or "独立审批未批准该操作"
            if reviewer_denial
            else "权限策略未执行该操作"
        ),
        "executed": False,
        "decision": preflight.decision.value,
        "reason_code": preflight.reason_code,
        "reviewer_rationale": preflight.reviewer_rationale,
    }
    if reviewer_denial:
        result["guidance"] = (
            "不要重试或改名绕过相同/实质等价的动作；只能选择风险实质更低的路径，"
            "或停止并向用户说明。"
        )
        result["override_eligible"] = preflight.override_eligible
    return result


async def run_loop(
    llm_call: LlmCall,
    *,
    tools: dict[str, ToolDef],
    allowed: list[str],
    max_turns: int,
    history: list[dict],
    user_text: str,
    tool_ctx: ToolContext,
    emit: EmitFn | None = None,
    is_cancelled: CancelCheck | None = None,
    preflight: PreflightFn | None = None,
    reviewer: ReviewFn | None = None,
    run_id: str | None = None,
    policy_snapshot: dict | None = None,
    tool_timeout_seconds: float = 300.0,
    cancellation_poll_interval_seconds: float = 0.25,
) -> LoopOutcome:
    outcome = LoopOutcome(stop="end_turn", final_text=None)
    consecutive_reviewer_denials = 0

    async def _event(ev: dict) -> None:
        outcome.events.append(ev)
        if emit is not None:
            await emit(ev)

    def _record(msg: dict) -> None:
        outcome.new_messages.append(msg)

    messages = [*history, {"role": "user", "content": user_text}]
    _record({"role": "user", "content": user_text})

    tool_schemas = [
        {"name": t.name, "description": t.description, "schema": t.schema}
        for name in allowed
        if (t := tools.get(name)) is not None
    ]

    async def _cancelled() -> bool:
        if is_cancelled is None or not await is_cancelled():
            return False
        outcome.stop = "cancelled"
        return True

    def _terminalize_execution(execution: ToolExecutionOutcome) -> None:
        if execution.status is ToolExecutionStatus.CANCELLED:
            outcome.stop = "cancelled"
            outcome.final_text = execution.output.get("error")
            outcome.terminal_tool = execution
        elif execution.status is ToolExecutionStatus.TIMED_OUT:
            outcome.stop = "error"
            outcome.final_text = execution.output.get("error")
            outcome.terminal_tool = execution

    async def _execute(call: ToolCallRequest) -> ToolExecutionOutcome:
        if await _cancelled():
            execution = ToolExecutionOutcome(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolExecutionStatus.CANCELLED,
                output={"error": "运行已取消", "reason_code": "run_cancelled"},
            )
            _terminalize_execution(execution)
            return execution
        if call.name not in allowed or call.name not in tools:
            return ToolExecutionOutcome(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolExecutionStatus.FAILED,
                output={"error": f"工具 {call.name} 不在本 agent 的白名单内"},
            )

        async def report_progress(text: str) -> None:
            if text.strip():
                await _event(
                    {
                        "type": "tool.progress",
                        "parent_tool_call_id": call.id,
                        "text": text.strip(),
                    }
                )

        call_ctx = (
            replace(tool_ctx, progress=report_progress)
            if tool_ctx is not None
            else tool_ctx
        )
        execution = await execute_native_tool(
            call,
            tools[call.name],
            call_ctx,
            timeout_seconds=tool_timeout_seconds,
            is_cancelled=is_cancelled,
            cancellation_poll_interval_seconds=cancellation_poll_interval_seconds,
        )
        _terminalize_execution(execution)
        return execution

    for _turn_no in range(max_turns):
        if await _cancelled():
            return outcome
        # 每次模型请求前做工具结果总量预算:较旧的 tool result 换成占位符,
        # 只影响本次请求可见文本;落库消息(outcome.new_messages)不受影响。
        messages = apply_tool_result_budget(messages)
        try:
            turn = await llm_call(messages, tool_schemas)
        except Exception as exc:  # 全 provider 失败等:落错误事件,不炸接口
            outcome.stop = "error"
            outcome.final_text = f"模型调用失败:{exc}"
            await _event({"type": "error", "message": str(exc)})
            _record({"role": "assistant", "content": outcome.final_text})
            return outcome

        outcome.builtin_web_search_requests += turn.web_search_requests
        outcome.input_tokens += turn.tokens_in
        outcome.output_tokens += turn.tokens_out

        # 模型实际返回的 thinking block / reasoning summary:终值事件落库供重放;
        # 不进 messages 历史、不回传模型。只有这一通道可标记为 thought。
        if turn.thinking:
            await _event({"type": "thought", "text": turn.thinking})

        # 模型内置联网搜索由 provider 服务端执行,不经过本地工具循环,因此这里
        # 单独发事件把过程与来源暴露出来——否则用户只看到结论,无从核实出处。
        for search in turn.web_searches:
            await _event(
                {
                    "type": "websearch.builtin",
                    "query": search.get("query") or "",
                    "results": search.get("results") or [],
                }
            )

        if not turn.tool_calls:
            outcome.final_text = turn.text or ""
            _record(
                {
                    "role": "assistant",
                    "content": outcome.final_text,
                    "trace_id": str(turn.trace_id) if turn.trace_id else None,
                }
            )
            await _event({"type": "text", "text": outcome.final_text})
            outcome.stop = "end_turn"
            return outcome

        # 助手消息(可能带说明文字)+ 全部工具调用,先进上下文再逐个执行
        assistant_msg = {
            "role": "assistant",
            "content": turn.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls
            ],
            "trace_id": str(turn.trace_id) if turn.trace_id else None,
        }
        messages.append(assistant_msg)
        _record(assistant_msg)
        if turn.text:
            # 工具轮的公开 assistant 文本是模型过程说明，不是 reasoning。
            # 独立事件使前端能按真实 loop 顺序展示且不会伪装成“思考”。
            await _event({"type": "assistant.message", "text": turn.text})

        ask_call = next((call for call in turn.tool_calls if call.name == "ask_user"), None)
        if ask_call is not None:
            # ask_user 是一个真正的暂停边界：合法请求不补 tool result，等用户
            # 一次性提交后再由 service 回挂。这样不会伪造普通 assistant/user 消息。
            try:
                questions = normalize_questions(ask_call.arguments.get("questions"))
            except UserInputValidationError as exc:
                result = {"error": str(exc)}
                await _event(
                    {
                        "type": "tool.call",
                        "tool_call_id": ask_call.id,
                        "name": ask_call.name,
                        "input": ask_call.arguments,
                    }
                )
                await _event(
                    {
                        "type": "tool.result",
                        "tool_call_id": ask_call.id,
                        "name": ask_call.name,
                        "ok": False,
                        "output": result,
                    }
                )
                _append_tool_result(messages, _record, ask_call, result)
                continue
            request = {
                "kind": "user_input",
                "request_id": str(uuid4()),
                "run_id": run_id,
                "tool_call_id": ask_call.id,
                "requested_at": datetime.now(UTC).isoformat(),
                "questions": questions,
            }
            outcome.stop = "ask_user"
            outcome.pending_user_input = request
            await _event(
                {
                    "type": "user.input.required",
                    "request": public_user_input_request(request),
                }
            )
            return outcome

        if preflight is None:
            # 迁移期兼容旧 confirm bool；真实服务始终传入策略 preflight。
            for call in turn.tool_calls:
                tool = tools.get(call.name)
                if tool is not None and tool.confirm and call.name in allowed:
                    summary = f"Agent 请求执行「{tool.label or call.name}」,等待你的批准"
                    await _event(
                        {
                            "type": "tool.confirm",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                            "summary": summary,
                        }
                    )
                    outcome.stop = "confirm"
                    outcome.pending_confirmation = {
                        "tool_call_id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                        "summary": summary,
                    }
                    return outcome

            pending = list(turn.tool_calls)
            for call in pending:
                await _event(
                    {
                        "type": "tool.call",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            read_calls = [
                call
                for call in pending
                if call.name in tools and tools[call.name].side_effect == "read"
            ]
            read_results = await asyncio.gather(*(_execute(call) for call in read_calls))
            results = dict(zip((call.id for call in read_calls), read_results, strict=True))
            for call in pending:
                if call.id not in results:
                    if (
                        call.name in tools
                        and tools[call.name].side_effect == "write"
                        and tools[call.name].emit_start_progress
                    ):
                        await _event(
                            {
                                "type": "tool.progress",
                                "parent_tool_call_id": call.id,
                                "text": "开始执行",
                            }
                        )
                    results[call.id] = await _execute(call)
                execution = results[call.id]
                result = execution.output
                await _event(
                    {
                        "type": "tool.result",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": tool_result_succeeded(result),
                        "output": result,
                    }
                )
                _append_tool_result(messages, _record, call, result)
                if execution.is_terminal:
                    return outcome
            continue

        evaluated: list[tuple[ToolCallRequest, ToolDef | None, ToolPreflight]] = []
        for call in turn.tool_calls:
            tool = tools.get(call.name)
            if tool is None or call.name not in allowed:
                evaluation = ToolPreflight(
                    decision=PermissionDecision.DENY,
                    reason_code="agent_tool_not_allowed",
                    source="capability",
                )
            else:
                try:
                    evaluation = await preflight(call, tool)
                except Exception as exc:
                    evaluation = ToolPreflight(
                        decision=PermissionDecision.DENY,
                        reason_code="policy_evaluation_failed",
                        source="capability",
                        reviewer_rationale=f"权限策略评估失败：{type(exc).__name__}",
                    )
            await _event(
                {
                    "type": "tool.call",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "input": (
                        (evaluation.action or {}).get("sanitized_arguments", {})
                    ),
                }
            )
            if evaluation.shadow_decision is not None:
                await _event(
                    _shadow_policy_event(
                        call,
                        evaluation,
                        tool=tool,
                        policy_snapshot=policy_snapshot,
                    )
                )
            if evaluation.decision is PermissionDecision.AUTO_REVIEW:
                routed = evaluation
                review_was_called = False
                await _event(
                    _policy_event(
                        call,
                        routed,
                        tool=tool,
                        policy_snapshot=policy_snapshot,
                        phase="route",
                    )
                )
                await _event(
                    {
                        "type": "reviewer.requested",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "reason_code": routed.reason_code,
                        "matched_rule": routed.matched_rule,
                        "action_hash": (routed.action or {}).get("action_hash"),
                        "risk": tool.permission.risk.value if tool is not None else None,
                        "categories": (
                            sorted(
                                category.value
                                for category in tool.permission.categories
                            )
                            if tool is not None
                            else []
                        ),
                    }
                )
                if consecutive_reviewer_denials >= 3:
                    evaluation = replace(
                        routed,
                        decision=PermissionDecision.DENY,
                        reason_code="reviewer_circuit_breaker",
                        source="reviewer",
                        reviewer_decision="deny",
                        reviewer_rationale=(
                            "本轮已连续三次未通过独立审批，已停止继续尝试。"
                        ),
                        reviewer_available=True,
                        circuit_breaker=True,
                    )
                elif reviewer is None or tool is None:
                    evaluation = replace(
                        routed,
                        decision=PermissionDecision.ASK_USER,
                        reason_code="reviewer_unavailable",
                        source="reviewer",
                        reviewer_decision="escalate",
                        reviewer_rationale="自动审批当前不可用，需要你确认该操作。",
                        reviewer_available=False,
                    )
                else:
                    review_was_called = True
                    try:
                        reviewed = await reviewer(call, tool, routed)
                    except Exception:
                        reviewed = replace(
                            routed,
                            decision=PermissionDecision.ASK_USER,
                            reason_code="reviewer_unavailable",
                            source="reviewer",
                            reviewer_decision="escalate",
                            reviewer_rationale=(
                                "自动审批当前不可用，需要你确认该操作。"
                            ),
                            reviewer_available=False,
                        )
                    if reviewed.decision is PermissionDecision.AUTO_REVIEW:
                        reviewed = replace(
                            routed,
                            decision=PermissionDecision.ASK_USER,
                            reason_code="reviewer_malformed",
                            source="reviewer",
                            reviewer_decision="escalate",
                            reviewer_rationale=(
                                "自动审批结果无效，需要你确认该操作。"
                            ),
                            reviewer_available=False,
                        )
                    evaluation = replace(
                        reviewed,
                        action=routed.action,
                        matched_rule=routed.matched_rule,
                        grant_id=None,
                    )

                if evaluation.reviewer_decision == "deny":
                    consecutive_reviewer_denials += 1
                    if consecutive_reviewer_denials >= 3:
                        evaluation = replace(
                            evaluation,
                            reason_code="reviewer_circuit_breaker",
                            circuit_breaker=True,
                        )
                        await _event(
                            {
                                "type": "reviewer.circuit_breaker",
                                "tool_call_id": call.id,
                                "name": call.name,
                                "denial_count": consecutive_reviewer_denials,
                                "action_hash": (
                                    (evaluation.action or {}).get("action_hash")
                                ),
                            }
                        )
                else:
                    consecutive_reviewer_denials = 0
                await _event(
                    _reviewer_event(
                        call,
                        evaluation,
                        tool=tool,
                        policy_snapshot=policy_snapshot,
                    )
                )
                await _event(
                    _policy_event(
                        call,
                        evaluation,
                        tool=tool,
                        policy_snapshot=policy_snapshot,
                    )
                )
                if (
                    review_was_called
                    and evaluation.decision is PermissionDecision.DENY
                    and evaluation.override_eligible
                    and evaluation.reviewer_available
                    and tool is not None
                ):
                    candidate = _reviewer_override_candidate(
                        call,
                        tool,
                        evaluation,
                        run_id=run_id,
                        model_hop=_turn_no + 1,
                        policy_snapshot=policy_snapshot or {},
                    )
                    outcome.reviewer_override_candidates.append(candidate)
                    await _event(
                        {
                            "type": "reviewer.override.available",
                            "override": public_reviewer_override(candidate),
                        }
                    )
            else:
                await _event(
                    _policy_event(
                        call,
                        evaluation,
                        tool=tool,
                        policy_snapshot=policy_snapshot,
                    )
                )
            evaluated.append((call, tool, evaluation))

        if any(
            evaluation.decision is PermissionDecision.ASK_USER
            for _call, _tool, evaluation in evaluated
        ):
            bundle = {
                "kind": "approval_bundle",
                "bundle_id": str(uuid4()),
                "run_id": run_id,
                "model_hop": _turn_no + 1,
                "status": "pending",
                "requested_at": datetime.now(UTC).isoformat(),
                "policy_snapshot": policy_snapshot or {},
                "assistant_text": turn.text,
                "items": [
                    _approval_bundle_item(call, tool, evaluation, order=order)
                    for order, (call, tool, evaluation) in enumerate(evaluated)
                ],
            }
            public_bundle = public_approval_bundle(bundle)
            await _event({"type": "approval.bundle.requested", "bundle": public_bundle})
            pending_items = [
                item for item in public_bundle["items"] if item["status"] == "pending"
            ]
            if len(pending_items) == 1:
                # 旧客户端继续理解 tool.confirm；载荷只含脱敏参数。
                item = pending_items[0]
                # pending_confirmation 仍是 bundle；这些顶层字段只是单项旧客户端的
                # 安全投影。原始可执行参数仅保存在 items[].input 中，不投影到顶层。
                bundle.update(
                    {
                        "tool_call_id": item["tool_call_id"],
                        "name": item["name"],
                        "input": item["input"],
                        "summary": item["summary"],
                    }
                )
                await _event(
                    {
                        "type": "tool.confirm",
                        "tool_call_id": item["tool_call_id"],
                        "name": item["name"],
                        "input": item["input"],
                        "summary": item["summary"],
                        "bundle_id": bundle["bundle_id"],
                        "risk": item["risk"],
                        "categories": item["categories"],
                        "reason_code": item["reason_code"],
                    }
                )
            outcome.stop = "confirm"
            outcome.pending_confirmation = bundle
            return outcome

        allowed_read_calls = [
            call
            for call, tool, evaluation in evaluated
            if tool is not None
            and evaluation.decision is PermissionDecision.ALLOW
            and tool.side_effect == "read"
        ]
        read_results = await asyncio.gather(
            *(_execute(call) for call in allowed_read_calls)
        )
        results = dict(
            zip((call.id for call in allowed_read_calls), read_results, strict=True)
        )
        for call, tool, evaluation in evaluated:
            if evaluation.decision is PermissionDecision.DENY:
                result = _denied_tool_result(evaluation)
                execution = None
            elif call.id in results:
                execution = results[call.id]
                result = execution.output
            else:
                if (
                    tool is not None
                    and tool.side_effect == "write"
                    and tool.emit_start_progress
                ):
                    await _event(
                        {
                            "type": "tool.progress",
                            "parent_tool_call_id": call.id,
                            "text": "开始执行",
                        }
                    )
                execution = await _execute(call)
                result = execution.output
            await _event(
                {
                    "type": "tool.result",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "ok": tool_result_succeeded(result),
                    "output": result,
                }
            )
            _append_tool_result(messages, _record, call, result)
            if execution is not None and execution.is_terminal:
                return outcome

    outcome.stop = "max_turns"
    outcome.final_text = "本轮对话步数已达上限,已停止;请补充信息或换个说法继续。"
    _record({"role": "assistant", "content": outcome.final_text})
    await _event({"type": "text", "text": outcome.final_text})
    return outcome


def _append_tool_result(
    messages: list[dict],
    record: Callable[[dict], None],
    call: ToolCallRequest,
    result: dict,
) -> None:
    msg = {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        # 模型可见文本走单条紧凑视图;完整 result 仍原样进 tool_output 落库审计
        "content": compact_tool_output(call.name, result, TOOL_OUTPUT_MAX_CHARS),
        "tool_input": call.arguments,
        "tool_output": result,
    }
    messages.append(msg)
    record(msg)
