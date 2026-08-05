"""LLMService:结构化输出 + 工具调用统一入口(路由 + 降级链 + trace + usage)。

业务 pipeline 只依赖 generate_structured();AgentRuntime 依赖 generate_with_tools
(system 来自 agent 卡片而非 Prompt Registry,故 trace 的 prompt_version 为空)。
trace 与 usage 写入使用服务自建会话并设置 org 上下文(llm_traces 受 RLS 约束),
落账动作经 TraceSink/UsageSink 协议(见 sinks 模块),默认实现落 SQL 表。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID
from weakref import WeakKeyDictionary

from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory
from nicekit.core.secretbox import get_secret_box
from nicekit.llm.providers import (
    AnthropicProvider,
    DeltaFn,
    LLMProvider,
    OpenAIProvider,
    ProviderError,
    ToolCallRequest,
    classify_provider_exception,
    sanitize_provider_diagnostic,
    sanitize_provider_error,
)
from nicekit.llm.registry import get_active_prompt, resolve_route
from nicekit.llm.runtime_config import runtime_providers
from nicekit.llm.sinks import SqlTraceSink, SqlUsageSink, TraceSink, UsageSink

T = TypeVar("T", bound=BaseModel)
CallT = TypeVar("CallT")

logger = logging.getLogger(__name__)


# 同 hop 断流加试:connection_error 是快速失败(链路被网关/中间层掐断,几乎
# 不消耗超时预算),原地加试一次的代价远低于把失败抛给用户 —— 单跳路由(无降级
# 位)下这就是"一次网络抖动 = 用户可见失败"。timeout 不加试(已烧满整段超时
# 预算),rate_limit 不加试(原地重试只会再撞限流),两者仍立即走降级链。
_SAME_HOP_RETRIES = 1
_SAME_HOP_RETRY_CODES = frozenset({"connection_error"})
_SAME_HOP_RETRY_DELAY_SECONDS = 0.2


class NoRouteError(Exception):
    pass


class AllProvidersFailedError(Exception):
    def __init__(self, task: str, errors: list[str]):
        self.errors = [sanitize_provider_diagnostic(error) for error in errors]
        super().__init__(f"task {task!r} 全部 provider 失败: {'; '.join(self.errors)}")


class LlmBudgetExceededError(Exception):
    """org 当日 token 预算耗尽(prod-readiness-1 D4):不进降级链,直接失败。"""

    def __init__(
        self, org_id: UUID, used: int, budget: int, estimated_tokens: int = 0
    ):
        projected = used + estimated_tokens
        super().__init__(
            f"组织今日 LLM 用量 {used} tokens,本次预计 {estimated_tokens} tokens,"
            f"预计总量 {projected} 已达到或超过预算上限 {budget},"
            "请明日再试或联系管理员调整"
        )
        self.org_id = org_id
        self.used = used
        self.budget = budget
        self.estimated_tokens = estimated_tokens


# SDK 化改造:TF 的 `_org_semaphores`/`_provider_cooldowns` 是模块级全局,
# 这里下沉为 LLMService 实例属性(宿主/测试可完全隔离),同时保留 TF 的
# "勿回退"裁决——LLMService 每回合经 get_llm_service() 新建实例,冷却与
# 限流状态必须进程级共享才有意义,故默认工厂路径注入下面这对进程级共享
# 容器;直接实例化 LLMService(不传参)则各自独立。
# per-event-loop 语义保留:测试里每个 asyncio.run 新开 loop,Semaphore 不能
# 跨 loop 复用,故按 loop 隔离存放(WeakKeyDictionary,loop 回收即释放)。
_shared_org_semaphores: WeakKeyDictionary = WeakKeyDictionary()

# provider 失败冷却:provider:model → 冷却截止时刻(monotonic);成功一次即解除。
_shared_provider_cooldowns: dict[str, float] = {}


def _hop_key(hop: dict) -> str:
    return f"{hop['provider']}:{hop['model']}"


@dataclass(frozen=True)
class StructuredGeneration[OutputT: BaseModel]:
    """Parsed output with the exact successful route and prompt revision."""

    parsed: OutputT
    provider: str
    model: str
    prompt_version: int


@dataclass(frozen=True)
class ToolTurn:
    """一跳工具调用式 LLM 结果(provider 归一化 + 本次成功 trace 的 id)。"""

    text: str | None
    tool_calls: list[ToolCallRequest]
    stop_reason: str
    tokens_in: int
    tokens_out: int
    trace_id: UUID | None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # 模型思考过程(thinking block / reasoning summary),仅展示不回传
    thinking: str | None = None
    # 模型内置联网搜索的过程与来源(provider 服务端执行),仅展示与引用
    web_searches: tuple[dict, ...] = ()
    web_search_requests: int = 0


class LLMService:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        trace_sink: TraceSink | None = None,
        usage_sink: UsageSink | None = None,
        org_semaphores: WeakKeyDictionary | None = None,
        provider_cooldowns: dict[str, float] | None = None,
    ):
        self._providers = providers
        self._session_factory = session_factory or get_session_factory()
        self._trace_sink = trace_sink if trace_sink is not None else SqlTraceSink()
        self._usage_sink = usage_sink if usage_sink is not None else SqlUsageSink()
        # 见模块头注释:默认各实例独立;get_llm_service() 注入进程级共享容器
        self._org_semaphores: WeakKeyDictionary = (
            org_semaphores if org_semaphores is not None else WeakKeyDictionary()
        )
        self._provider_cooldowns: dict[str, float] = (
            provider_cooldowns if provider_cooldowns is not None else {}
        )

    def _active_attempts(self, attempts: list[dict]) -> list[dict]:
        """剔除冷却中的线;全部在冷却时按原链尝试(不能无路可走)。"""
        if get_settings().llm_provider_cooldown_seconds <= 0:
            return attempts
        now = time.monotonic()
        active = [
            h for h in attempts if self._provider_cooldowns.get(_hop_key(h), 0.0) <= now
        ]
        return active or attempts

    def _mark_provider_failed(self, hop: dict) -> None:
        cooldown = get_settings().llm_provider_cooldown_seconds
        if cooldown > 0:
            self._provider_cooldowns[_hop_key(hop)] = time.monotonic() + cooldown

    def _mark_provider_ok(self, hop: dict) -> None:
        self._provider_cooldowns.pop(_hop_key(hop), None)

    def _org_semaphore(self, org_id: UUID) -> asyncio.Semaphore | None:
        limit = get_settings().llm_org_max_concurrency
        if limit <= 0:
            return None
        loop = asyncio.get_running_loop()
        per_loop: dict[UUID, asyncio.Semaphore] = self._org_semaphores.setdefault(
            loop, {}
        )
        sem = per_loop.get(org_id)
        if sem is None:
            sem = per_loop[org_id] = asyncio.Semaphore(limit)
        return sem

    async def run_governed_call(
        self,
        *,
        org_id: UUID,
        task: str,
        provider: str,
        model: str,
        estimated_tokens: int,
        quantity: int,
        call: Callable[[], Awaitable[CallT]],
        token_usage: Callable[[CallT], tuple[int, int]],
        attempt: int = 1,
        fallback_from: str | None = None,
    ) -> CallT:
        """Run one external model API call under the shared org guardrails.

        The caller owns protocol-specific invocation and response parsing. A successful
        parser result supplies actual input/output token usage for trace and daily usage;
        protocol exceptions are traced and re-raised unchanged for the adapter to map.
        """
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        if attempt < 1:
            raise ValueError("attempt must be positive")

        sem = self._org_semaphore(org_id)
        async with sem if sem is not None else nullcontext():
            await self._check_budget(org_id, estimated_tokens=estimated_tokens)
            started = time.monotonic()
            try:
                result = await call()
                tokens_in, tokens_out = token_usage(result)
                if tokens_in < 0 or tokens_out < 0:
                    raise ValueError("token usage must be non-negative")
            except Exception as exc:
                safe_error = classify_provider_exception(exc)
                latency = int((time.monotonic() - started) * 1000)
                try:
                    await self._record(
                        org_id=org_id,
                        task=task,
                        provider=provider,
                        model=model,
                        prompt_version=None,
                        attempt=attempt,
                        fallback_from=fallback_from,
                        status="error",
                        error=safe_error.diagnostic,
                        tokens_in=0,
                        tokens_out=0,
                        latency_ms=latency,
                    )
                except Exception:
                    logger.error(
                        "模型调用失败后的 trace 写入失败: task=%s provider=%s model=%s",
                        task,
                        provider,
                        model,
                    )
                raise

            latency = int((time.monotonic() - started) * 1000)
            await self._record(
                org_id=org_id,
                task=task,
                provider=provider,
                model=model,
                prompt_version=None,
                attempt=attempt,
                fallback_from=fallback_from,
                status="success",
                error=None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                quantity=quantity,
            )
            return result

    async def generate_structured(
        self,
        *,
        task: str,
        messages: list[dict],
        output_model: type[T],
        org_id: UUID,
        model_override: dict[str, str] | None = None,
    ) -> T:
        generation = await self.generate_structured_with_metadata(
            task=task,
            messages=messages,
            output_model=output_model,
            org_id=org_id,
            model_override=model_override,
        )
        return generation.parsed

    async def generate_structured_with_metadata(
        self,
        *,
        task: str,
        messages: list[dict],
        output_model: type[T],
        org_id: UUID,
        model_override: dict[str, str] | None = None,
    ) -> StructuredGeneration[T]:
        """messages 的 content 支持纯文本或多模态内容块(见 providers 模块注释)。
        model_override 与 generate_with_tools 同规则:提供时只尝试该 provider/model,
        不静默进入任务路由的降级位(视觉等能力敏感任务不能落回纯文本模型)。"""
        async with self._session_factory() as session:
            prompt = await get_active_prompt(session, task)
            try:
                route = await resolve_route(session, task, org_id)
            except LookupError as exc:
                raise NoRouteError(str(exc)) from exc

        attempts = self._active_attempts(
            [dict(model_override)]
            if model_override is not None
            else [
                {"provider": route.primary_provider, "model": route.primary_model},
                *route.fallback_chain,
            ]
        )
        errors: list[str] = []
        fallback_from: str | None = None

        await self._check_budget(org_id)
        sem = self._org_semaphore(org_id)
        async with sem if sem is not None else nullcontext():
            for attempt_no, hop in enumerate(attempts, start=1):
                provider = self._providers.get(hop["provider"])
                if provider is None:
                    errors.append("provider_error")
                    continue

                result = None
                for hop_try in range(1 + _SAME_HOP_RETRIES):
                    started = time.monotonic()
                    try:
                        result = await provider.generate_structured(
                            model=hop["model"],
                            system=prompt.content,
                            messages=messages,
                            output_model=output_model,
                            max_tokens=route.max_tokens,
                            timeout_seconds=route.timeout_seconds,
                        )
                        break
                    except ProviderError as exc:
                        safe_error = sanitize_provider_error(exc)
                        latency = int((time.monotonic() - started) * 1000)
                        try:
                            await self._record(
                                org_id=org_id, task=task,
                                provider=hop["provider"], model=hop["model"],
                                prompt_version=prompt.version, attempt=attempt_no,
                                fallback_from=fallback_from, status="error",
                                error=safe_error.diagnostic,
                                tokens_in=0, tokens_out=0, latency_ms=latency,
                            )
                        except Exception:
                            logger.error(
                                "provider 失败后的 trace 写入失败: "
                                "task=%s provider=%s model=%s",
                                task,
                                hop["provider"],
                                hop["model"],
                            )
                        errors.append(safe_error.diagnostic)
                        if not safe_error.retryable:
                            raise safe_error from None
                        if (
                            hop_try < _SAME_HOP_RETRIES
                            and safe_error.code in _SAME_HOP_RETRY_CODES
                        ):
                            await asyncio.sleep(_SAME_HOP_RETRY_DELAY_SECONDS)
                            continue
                        self._mark_provider_failed(hop)
                        fallback_from = f"{hop['provider']}:{hop['model']}"
                        break
                if result is None:
                    continue

                latency = int((time.monotonic() - started) * 1000)
                self._mark_provider_ok(hop)
                await self._record(
                    org_id=org_id, task=task, provider=hop["provider"], model=hop["model"],
                    prompt_version=prompt.version, attempt=attempt_no,
                    fallback_from=fallback_from, status="success", error=None,
                    tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=latency,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_write_tokens=result.cache_write_tokens,
                )
                return StructuredGeneration(
                    parsed=result.parsed,  # type: ignore[arg-type]
                    provider=hop["provider"],
                    model=hop["model"],
                    prompt_version=prompt.version,
                )

        raise AllProvidersFailedError(task, errors)

    async def generate_with_tools(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        org_id: UUID,
        timeout_override: float | None = None,
        on_delta: DeltaFn | None = None,
        on_thinking: DeltaFn | None = None,
        thinking_level: str | None = None,
        model_override: dict[str, str] | None = None,
        builtin_web_search: dict | None = None,
    ) -> ToolTurn:
        """工具调用一跳(AgentRuntime 用):system 由 agent 卡片提供,路由/降级/trace
        与 generate_structured 同规则;timeout_override(卡片 timeout)优先于路由值。
        on_delta 提供时走 provider 流式接口逐段回调;降级换 provider 前回调 None,
        通知调用方丢弃上一 provider 已流出的半截文本。on_thinking 同规则承接
        思考过程增量(thinking_delta / reasoning summary delta)。
        thinking_level(off/low/medium/high)映射各 provider 思考参数,None=默认。
        model_override 由调用 API 预先校验；提供时只尝试该 provider/model，
        不静默进入任务路由的其他降级位。"""
        async with self._session_factory() as session:
            try:
                route = await resolve_route(session, task, org_id)
            except LookupError as exc:
                raise NoRouteError(str(exc)) from exc

        attempts = self._active_attempts(
            [dict(model_override)]
            if model_override is not None
            else [
                {"provider": route.primary_provider, "model": route.primary_model},
                *route.fallback_chain,
            ]
        )
        errors: list[str] = []
        fallback_from: str | None = None

        await self._check_budget(org_id)
        sem = self._org_semaphore(org_id)
        async with sem if sem is not None else nullcontext():
            for attempt_no, hop in enumerate(attempts, start=1):
                provider = self._providers.get(hop["provider"])
                if provider is None:
                    errors.append("provider_error")
                    continue

                result = None
                for hop_try in range(1 + _SAME_HOP_RETRIES):
                    if fallback_from is not None or hop_try:
                        # 换 provider 或同 hop 加试前,通知调用方丢弃半截流式文本
                        if on_delta is not None:
                            await on_delta(None)
                        if on_thinking is not None:
                            await on_thinking(None)
                    started = time.monotonic()
                    try:
                        result = await provider.generate_with_tools(
                            model=hop["model"],
                            system=system,
                            messages=messages,
                            tools=tools,
                            max_tokens=route.max_tokens,
                            timeout_seconds=timeout_override or route.timeout_seconds,
                            on_delta=on_delta,
                            on_thinking=on_thinking,
                            thinking_level=thinking_level,
                            builtin_web_search=builtin_web_search,
                        )
                        break
                    except ProviderError as exc:
                        safe_error = sanitize_provider_error(exc)
                        latency = int((time.monotonic() - started) * 1000)
                        try:
                            await self._record(
                                org_id=org_id, task=task,
                                provider=hop["provider"], model=hop["model"],
                                prompt_version=None, attempt=attempt_no,
                                fallback_from=fallback_from, status="error",
                                error=safe_error.diagnostic,
                                tokens_in=0, tokens_out=0, latency_ms=latency,
                            )
                        except Exception:
                            logger.error(
                                "provider 失败后的 trace 写入失败: "
                                "task=%s provider=%s model=%s",
                                task,
                                hop["provider"],
                                hop["model"],
                            )
                        errors.append(safe_error.diagnostic)
                        if not safe_error.retryable:
                            raise safe_error from None
                        if (
                            hop_try < _SAME_HOP_RETRIES
                            and safe_error.code in _SAME_HOP_RETRY_CODES
                        ):
                            await asyncio.sleep(_SAME_HOP_RETRY_DELAY_SECONDS)
                            continue
                        self._mark_provider_failed(hop)
                        fallback_from = f"{hop['provider']}:{hop['model']}"
                        break
                if result is None:
                    continue

                latency = int((time.monotonic() - started) * 1000)
                self._mark_provider_ok(hop)
                trace_id = await self._record(
                    org_id=org_id, task=task, provider=hop["provider"], model=hop["model"],
                    prompt_version=None, attempt=attempt_no,
                    fallback_from=fallback_from, status="success", error=None,
                    tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=latency,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_write_tokens=result.cache_write_tokens,
                )
                return ToolTurn(
                    text=result.text,
                    tool_calls=result.tool_calls,
                    stop_reason=result.stop_reason,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    trace_id=trace_id,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_write_tokens=result.cache_write_tokens,
                    thinking=result.thinking,
                    web_searches=result.web_searches,
                    web_search_requests=result.web_search_requests,
                )

        raise AllProvidersFailedError(task, errors)

    async def _check_budget(self, org_id: UUID, *, estimated_tokens: int = 0) -> None:
        """org 当日 token 预算检查(D4):累计 + 本次预计用量超过上限即拒绝。
        预算为 0(默认)不查库零开销;usage_daily 受 RLS,需 org 上下文。"""
        budget = get_settings().llm_org_daily_token_budget
        if budget <= 0:
            return
        # 延迟 import:UsageDaily 随 tenancy 子包落位(蓝图 §5.2),预算默认
        # 关闭时 llm 子包不需要 tenancy 存在。
        from nicekit.models.tenancy import UsageDaily

        async with self._session_factory() as session:
            # 保留手写 set_config(勿删):LLMService 自建 session 不经
            # get_org_session,usage_daily 受 RLS,无 org 上下文会静默查 0 行
            # 导致预算形同虚设。TODO P4:注入 session provider 后收敛到
            # core.db.bind_org_context 统一入口(蓝图 §4 TraceSink/UsageSink 行)。
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(org_id)},
            )
            used = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0
                        )
                    ).where(
                        UsageDaily.org_id == org_id,
                        UsageDaily.usage_date == datetime.now(UTC).date(),
                    )
                )
            ).scalar_one()
        used = int(used)
        if used >= budget or used + estimated_tokens > budget:
            raise LlmBudgetExceededError(
                org_id, used, budget, estimated_tokens=estimated_tokens
            )

    async def _record(
        self,
        *,
        org_id: UUID,
        task: str,
        provider: str,
        model: str,
        prompt_version: int | None,
        attempt: int,
        fallback_from: str | None,
        status: str,
        error: str | None,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        quantity: int = 0,
    ) -> UUID | None:
        async with self._session_factory() as session:
            # 保留手写 set_config(勿删):llm_traces/usage_daily 受 RLS,
            # LLMService 自建 session 必须自带 org 上下文,否则默认 Sql sink
            # 的写入会被 RLS 静默拒绝。TODO P4:改由注入的 session provider
            # 负责绑定 org 上下文后收敛此处(蓝图 §4)。
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(org_id)},
            )
            trace_id = await self._trace_sink.record_trace(
                session,
                {
                    "org_id": org_id, "task": task, "provider": provider, "model": model,
                    "prompt_version": prompt_version, "attempt": attempt,
                    "fallback_from": fallback_from, "status": status, "error": error,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "latency_ms": latency_ms,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_write_tokens": cache_write_tokens,
                },
            )
            if status == "success":
                await self._usage_sink.record_usage(
                    session,
                    org_id=org_id, task=task, provider=provider, model=model,
                    tokens_in=tokens_in, tokens_out=tokens_out, calls=1,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    quantity=quantity,
                )
            await session.commit()
            return trace_id


def env_provider_credentials() -> dict[str, tuple[str, str, str]]:
    """内置双线的 .env 凭证:name → (protocol, api_key, base_url)。

    api_key 经 SecretBox 解密:enc: 密文解出明文,无前缀明文原样返回
    (天然兼容未加密的存量/开发配置)。
    """
    settings = get_settings()
    box = get_secret_box()
    return {
        "openai": (
            "openai",
            box.decrypt(settings.openai_api_key),
            settings.openai_base_url,
        ),
        "anthropic": (
            "anthropic",
            box.decrypt(settings.anthropic_api_key),
            settings.anthropic_base_url,
        ),
    }


def build_protocol_provider(
    protocol: str, api_key: str, base_url: str, *, instance: str = ""
) -> LLMProvider:
    """按接入协议构建 SDK 客户端(同协议可有多个实例,指向不同网关/账号)。

    instance 是 llm_providers.name:兼容性开关按实例生效,不影响同协议的其他实例。
    """
    settings = get_settings()
    if protocol == "anthropic":
        return AnthropicProvider(
            api_key,
            base_url,
            thinking_budget=settings.llm_anthropic_thinking_budget,
            budget_map=settings.llm_anthropic_thinking_budgets,
        )
    return OpenAIProvider(
        api_key,
        base_url,
        effort_map=settings.llm_openai_effort_map,
        send_max_output_tokens=instance not in settings.llm_openai_omit_max_output_tokens,
    )


def get_llm_service() -> LLMService:
    """构建多提供商服务(FastAPI 依赖或 worker 均可用)。

    提供商实例来自 llm_providers 表(runtime_config 进程缓存,任意多个、
    双协议混用,路由降级链按实例名引用);内置 openai/anthropic 两行凭证
    为空时回落 .env,自定义实例凭证为空视为未配置跳过。表未加载(旧库
    未迁移/启动早期)或表里没有某个内置实例(全新部署,管理员尚未配置)时,
    该实例回落 .env 凭证。落库 api_key 经 SecretBox 解密。

    冷却/限流状态注入模块级共享容器:get_llm_service() 每回合新建实例,
    这两份状态必须进程级共享才有意义(TF 原注释的"勿回退"裁决)。
    """
    box = get_secret_box()
    rows = runtime_providers()
    env_creds = env_provider_credentials()
    providers: dict[str, LLMProvider] = {}
    if rows is None:
        for name, (protocol, api_key, base_url) in env_creds.items():
            if api_key:
                providers[name] = build_protocol_provider(
                    protocol, api_key, base_url, instance=name
                )
        return LLMService(
            providers,
            org_semaphores=_shared_org_semaphores,
            provider_cooldowns=_shared_provider_cooldowns,
        )
    for row in rows:
        if not row["enabled"]:
            continue
        env = env_creds.get(row["name"])
        api_key = box.decrypt(row["api_key"]) or (env[1] if env else "")
        base_url = row["base_url"] or (env[2] if env else "")
        if not api_key:
            continue
        providers[row["name"]] = build_protocol_provider(
            row["protocol"], api_key, base_url, instance=row["name"]
        )
    # 表里压根没有的内置实例,用 .env 兜底。全新部署必经此路:迁移把表建出来
    # 是空的、管理员还没配 provider,但 .env 里已经有 key —— 没有这段兜底,
    # 首次对话会以"全部 provider 失败"收场,且 llm_traces 里连一条记录都没有
    # (失败发生在选实例阶段,还没走到调用),排查成本极高。
    # 判据用"表里是否出现过这一行"而不是"是否可用":管理员显式禁用或清空密钥
    # 的实例不该被 .env 复活,那是他的决定。
    configured = {row["name"] for row in rows}
    for name, (protocol, api_key, base_url) in env_creds.items():
        if name in configured or name in providers or not api_key:
            continue
        providers[name] = build_protocol_provider(protocol, api_key, base_url, instance=name)
    return LLMService(
        providers,
        org_semaphores=_shared_org_semaphores,
        provider_cooldowns=_shared_provider_cooldowns,
    )
