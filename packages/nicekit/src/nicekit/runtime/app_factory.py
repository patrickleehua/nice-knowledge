"""FastAPI 应用装配器(MIGRATION-PLAN §5.7):宿主用 ``create_app()`` 组装 SDK。

搬自 TF backend/app/main.py,改造点:

1. **routers / background_loops / hooks 全部由宿主注入**:TF lifespan 里硬接线的
   9 个业务扫描器改为可注册的 ``BackgroundLoop``;SDK 自带的 KB / icron / 心跳
   循环由 :func:`default_background_loops` 提供(默认启用)。
2. **中间件顺序照 TF**:自内向外注册 → 限流最内、请求日志居中、CORS 最外。
   三者都是纯 ASGI 中间件(**不用 BaseHTTPMiddleware**,它会缓冲响应体,把 SSE
   变成"一次性吐完",是 TF 踩过的坑)。
3. **修 TF 的停机 bug**:``finally`` 里逐个 ``await task`` 会被第一个不响应
   stop_event 的任务卡住(且任一任务抛异常时后续任务永不被回收)。这里统一
   ``asyncio.gather(*tasks, return_exceptions=True)``。
4. ``/metrics`` 挂在应用根,并保证它进入限流豁免路径(core config 的
   ``rate_limit_exempt_paths`` 默认只有 ``/api/v1/health``)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import Settings, get_settings
from nicekit.core.db import get_session_factory
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.core.logging import RequestLogMiddleware, setup_logging
from nicekit.core.metrics import metrics_endpoint
from nicekit.core.ratelimit import RateLimitMiddleware

use_selector_event_loop_on_windows()

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"

#: 常驻后台循环的签名:(session_factory, *, stop_event) -> 协程
LoopFn = Callable[..., Awaitable[None]]
#: 启动/停机钩子:接 session_factory,异常不阻断启动(只记日志)
LifecycleHook = Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BackgroundLoop:
    """一个常驻后台循环及其启用条件。

    ``interval_setting`` 指向 ``Settings`` 上的秒数字段,值 <= 0 即不启动。
    ``inline_only=True``(默认)表示只在 inline 派发模式下由 API 进程兜底 ——
    celery 模式由 beat 驱动,两边同时跑会让同一批任务被抢两次。
    """

    name: str
    fn: LoopFn
    interval_setting: str | None = None
    inline_only: bool = True

    def enabled(self, settings: Settings) -> bool:
        if self.inline_only and settings.task_dispatch_mode != "inline":
            return False
        if self.interval_setting is None:
            return True
        return int(getattr(settings, self.interval_setting, 0) or 0) > 0


async def _run_embedding_campaign_finalizer(
    factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    from nicekit.kb.embedding_reindex import (
        finalize_due_embedding_campaigns,
        reconcile_embedding_campaigns,
    )
    from nicekit.llm.runtime_config import load_runtime_overrides

    settings = get_settings()
    interval = settings.embedding_migration_poll_interval_seconds
    while not stop_event.is_set():
        try:
            await reconcile_embedding_campaigns(
                factory,
                batch_size=settings.embedding_reindex_batch_size,
                lease_seconds=settings.embedding_reindex_lease_seconds,
            )
            finalized = await finalize_due_embedding_campaigns(factory)
            if finalized:
                await load_runtime_overrides(factory)
        except Exception:
            logger.exception("embedding migration 恢复或切换失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass


def default_background_loops() -> tuple[BackgroundLoop, ...]:
    """SDK 自带的常驻循环(顺序即启动顺序)。

    延迟 import:``create_app`` 被 import 时不该把 KB / agent 整条依赖链拉起来。
    """
    from nicekit.agent.icron import run_icron_ticker
    from nicekit.kb.expiry import run_kb_expiry_sweeper
    from nicekit.kb.health_sweep import run_kb_health_sweeper
    from nicekit.kb.orphan_audit import run_kb_orphan_sweeper
    from nicekit.kb.outbox import run_outbox_consumer
    from nicekit.kb.purge_due import run_kb_purge_due_sweeper
    from nicekit.kb.review_sweep import run_fact_review_sweeper
    from nicekit.llm.runtime_config import run_runtime_config_refresher
    from nicekit.operations.runtime import run_api_heartbeat
    from nicekit.runtime.reliability import run_sweeper

    return (
        # 运行时配置刷新与 API 心跳:两种派发模式下都要跑(不是周期任务)
        BackgroundLoop(
            "runtime_config_refresher", run_runtime_config_refresher, inline_only=False
        ),
        BackgroundLoop("api_heartbeat", run_api_heartbeat, inline_only=False),
        BackgroundLoop("stale_task_sweeper", run_sweeper, "stale_sweep_interval_seconds"),
        BackgroundLoop(
            "kb_expiry_sweeper", run_kb_expiry_sweeper, "kb_expiry_sweep_interval_seconds"
        ),
        BackgroundLoop(
            "kb_fact_review_sweeper",
            run_fact_review_sweeper,
            "kb_ai_review_sweep_interval_seconds",
        ),
        BackgroundLoop(
            "kb_health_sweeper", run_kb_health_sweeper, "kb_health_sweep_interval_seconds"
        ),
        BackgroundLoop(
            "kb_orphan_sweeper", run_kb_orphan_sweeper, "kb_orphan_audit_interval_seconds"
        ),
        BackgroundLoop(
            "kb_purge_due_sweeper", run_kb_purge_due_sweeper, "kb_purge_due_interval_seconds"
        ),
        BackgroundLoop(
            "kb_outbox_consumer", run_outbox_consumer, "kb_outbox_poll_interval_seconds"
        ),
        BackgroundLoop("icron_ticker", run_icron_ticker, "icron_tick_interval_seconds"),
        BackgroundLoop(
            "embedding_campaign_finalizer",
            _run_embedding_campaign_finalizer,
            "embedding_migration_poll_interval_seconds",
        ),
    )


def _exempt_metrics_path(settings: Settings) -> None:
    """把 ``/metrics`` 并入限流豁免路径(抓取不该被 429 打断)。"""
    if METRICS_PATH not in settings.rate_limit_exempt_paths:
        settings.rate_limit_exempt_paths = [*settings.rate_limit_exempt_paths, METRICS_PATH]


def _check_auth_wiring(routers: Sequence[APIRouter]) -> None:
    """启动自检:系统里"当前是谁"必须只有一个来源。

    宿主经 ``deps.set_principal_resolver()`` 接管身份后,若 SDK 的 auth/members
    还挂着,库里就有两套主体:一套是 SDK 自己签发 JWT、自己建的 users/memberships,
    另一套是宿主 resolver 现算出来的 org_id/user_id。两边可以指向不同的人甚至不同
    租户,而请求走哪套取决于客户端带了什么头 —— 越权排查时"这行是谁写的"根本推不
    出来。这种接线错误在运行期只表现为偶发串号,所以必须在装配期就炸掉。

    反向组合(内置 JWT 却没挂 auth)只是登录端点缺失,可能是宿主自己发 SDK 格式
    token 的有意安排,记 warning 不阻断。routers 为空时不提醒:那时根本没有 API
    面,谈不上"少了登录端点"。

    延迟 import:``nicekit.api.v1.router`` 会拉起全部 v1 router 的依赖链,只在真
    需要自检时付这个代价(与本模块其余延迟 import 的理由一致)。
    """
    from nicekit.api import deps
    from nicekit.api.v1.router import mounted_auth_router_names

    mounted = mounted_auth_router_names(routers)
    # 记录本次装配挂了哪些身份路由:装配之后才 set_principal_resolver 的顺序
    # 同样是错的接线,但那时 create_app 早跑完了,只有反向拦截才能盖住
    # (见 deps.set_principal_resolver)。
    deps.record_mounted_auth_routers(mounted)
    if not deps.uses_builtin_auth():
        if mounted:
            raise RuntimeError(
                f"身份接线自相矛盾:已用 set_principal_resolver() 接管身份,"
                f"却仍挂载了 SDK 的身份路由 {list(mounted)}。"
                "两套身份来源并存会让'当前是谁'不可推理,请从 routers 里排除 "
                "auth/members:default_routers(exclude=('auth', 'members'));"
                "确有需要可传 create_app(check_auth_wiring=False) 关闭本自检。"
            )
        return
    if routers and "auth" not in mounted:
        logger.warning(
            "仍在使用 SDK 自带的 JWT 认证,但 routers 里没有 auth router:"
            "登录端点缺失,客户端拿不到 token。若由宿主自行签发 SDK 格式的 token,"
            "这是预期行为,可传 check_auth_wiring=False 静音。"
        )


def create_app(
    settings: Settings | None = None,
    *,
    routers: Iterable[APIRouter] = (),
    router_prefix: str = "/api/v1",
    background_loops: Sequence[BackgroundLoop] | None = None,
    startup_hooks: Sequence[LifecycleHook] = (),
    shutdown_hooks: Sequence[LifecycleHook] = (),
    tool_registry: object | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    title: str | None = None,
    version: str | None = None,
    expose_metrics: bool = True,
    recover_orphans_on_startup: bool = True,
    warm_mcp_on_startup: bool = True,
    check_auth_wiring: bool = True,
) -> FastAPI:
    """组装一个可运行的 FastAPI 应用。

    - ``routers``:要挂载的 v1 routers(``nicekit.api.v1.default_routers()``
      给出 SDK 全量);``router_prefix`` 统一前缀。
    - ``background_loops``:``None`` = 用 :func:`default_background_loops`;
      传空序列 = 完全不起后台循环(测试常用)。
    - ``startup_hooks`` / ``shutdown_hooks``:接 ``session_factory`` 的协程,
      单个失败只记日志、不阻断进程(启动期任何一步都不该让服务起不来)。
    - ``tool_registry``:宿主自定义工具注册表;传入即设为 agent 的默认注册表。
    - ``check_auth_wiring``:启动期校验身份来源唯一(见 :func:`_check_auth_wiring`)。
    """
    resolved = settings or get_settings()
    # routers 可能是生成器,自检与挂载都要遍历,先定格;自检放在任何副作用
    # (tool_registry 合并)之前,接线错就别把全局注册表改脏了。
    mounted_routers = tuple(routers)
    if check_auth_wiring:
        _check_auth_wiring(mounted_routers)
    if tool_registry is not None:
        # 宿主自建 registry 并入 SDK 默认表(agent service 缺省从它取工具目录);
        # 重名会报错 —— 静默覆盖会让"哪份实现在跑"变成运行期谜题。
        from nicekit.agent.tools import ToolRegistry, default_registry

        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("tool_registry 必须是 nicekit.agent.tools.ToolRegistry 实例")
        if tool_registry is not default_registry:
            default_registry.merge(tool_registry)

    loops = tuple(default_background_loops() if background_loops is None else background_loops)
    factory_override = session_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动:结构化日志 → 运行时配置 → 孤儿恢复 → MCP 预热 → 常驻循环;
        关闭:置位 stop_event 后**并发** await 全部循环(TF 的逐个 await 会卡死)。"""
        from nicekit.llm.runtime_config import load_runtime_overrides

        setup_logging()
        factory = factory_override or get_session_factory()
        logger.info(
            "服务启动",
            extra={
                "dispatch_mode": resolved.task_dispatch_mode,
                "log_format": resolved.log_format,
            },
        )
        # 供应商、系统模型路由与外部服务配置先于业务加载
        await load_runtime_overrides(factory)
        if recover_orphans_on_startup:
            try:
                from nicekit.runtime.reliability import recover_orphan_tasks

                await recover_orphan_tasks(factory)
            except Exception:
                logger.exception("启动恢复孤儿任务失败(不阻断启动)")
        if warm_mcp_on_startup:
            try:
                from nicekit.agent.mcp_manager import warm_enabled_mcp_servers

                await warm_enabled_mcp_servers(factory, resolved.platform_org_id)
            except Exception:
                logger.exception("启动加载 MCP 服务失败(不阻断启动)")
        for hook in startup_hooks:
            try:
                await hook(factory)
            except Exception:
                logger.exception("启动钩子执行失败(不阻断启动):%s", getattr(hook, "__name__", hook))

        stop_event = asyncio.Event()
        tasks: list[asyncio.Task] = []
        for loop in loops:
            if not loop.enabled(resolved):
                continue
            tasks.append(
                asyncio.create_task(loop.fn(factory, stop_event=stop_event), name=loop.name)
            )
        logger.info("后台循环已启动", extra={"loops": [task.get_name() for task in tasks]})
        try:
            yield
        finally:
            stop_event.set()
            if tasks:
                # 并发回收:逐个 await 会被第一个慢循环卡住,异常也会漏掉后续任务
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for task, result in zip(tasks, results, strict=True):
                    if isinstance(result, BaseException):
                        logger.warning(
                            "后台循环退出异常 loop=%s error=%s", task.get_name(), result
                        )
            for hook in shutdown_hooks:
                try:
                    await hook(factory)
                except Exception:
                    logger.exception("停机钩子执行失败:%s", getattr(hook, "__name__", hook))

    app = FastAPI(
        title=title or resolved.app_name,
        version=version or resolved.service_version,
        lifespan=lifespan,
    )
    # 中间件自内向外注册:最后注册的最外层。CORS 最外(429 等短路响应也带 CORS 头);
    # 请求日志居中——包住限流,故限流 429 也拿到 request_id 头并被访问日志记录;
    # 限流最内,紧贴业务处理。三者都是纯 ASGI 中间件,不缓冲响应体(SSE 必需)。
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in resolved.cors_origins.split(",") if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Content-Disposition", "X-Preview-Filename"],
    )
    for router in mounted_routers:
        app.include_router(router, prefix=router_prefix)
    if expose_metrics:
        # Prometheus 文本导出:根路径无鉴权只读(不含租户业务明文),
        # 限流中间件对 /metrics 豁免,抓取不受 429 影响
        _exempt_metrics_path(resolved)
        app.add_api_route(METRICS_PATH, metrics_endpoint, methods=["GET"], include_in_schema=False)
    return app


__all__ = [
    "METRICS_PATH",
    "BackgroundLoop",
    "LifecycleHook",
    "LoopFn",
    "create_app",
    "default_background_loops",
]
