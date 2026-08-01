"""runtime/app_factory.py:装配语义(§5.7)。

覆盖:中间件顺序(CORS 最外 → 请求日志 → 限流最内,且都不是 BaseHTTPMiddleware)、
``/metrics`` 挂载与限流豁免、background_loops 的启用门控、**停机用 gather 并发回收**
(TF 逐个 await 的 bug:第一个不响应 stop_event 的循环会把停机卡死)。
"""

import asyncio

import httpx
import pytest
from fastapi import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware

from nicekit.core.config import Settings
from nicekit.core.logging import RequestLogMiddleware
from nicekit.core.ratelimit import RateLimitMiddleware
from nicekit.runtime.app_factory import (
    METRICS_PATH,
    BackgroundLoop,
    create_app,
    default_background_loops,
)


def _settings(**overrides) -> Settings:
    values = {
        "task_dispatch_mode": "inline",
        "rate_limit_exempt_paths": ["/api/v1/health"],
        "cors_origins": "http://localhost:3000",
    }
    values.update(overrides)
    return Settings(**values)


def test_middleware_order_matches_tf() -> None:
    """自内向外注册 → user_middleware 里最先的是最外层。"""
    app = create_app(_settings(), background_loops=())
    classes = [m.cls for m in app.user_middleware]
    from fastapi.middleware.cors import CORSMiddleware

    assert classes == [CORSMiddleware, RequestLogMiddleware, RateLimitMiddleware]
    # 纯 ASGI:BaseHTTPMiddleware 会缓冲响应体,把 SSE 变成一次性吐完
    assert not any(issubclass(cls, BaseHTTPMiddleware) for cls in classes)


def test_metrics_route_and_rate_limit_exemption() -> None:
    settings = _settings()
    assert METRICS_PATH not in settings.rate_limit_exempt_paths
    app = create_app(settings, background_loops=())
    assert METRICS_PATH in settings.rate_limit_exempt_paths
    assert any(getattr(route, "path", "") == METRICS_PATH for route in app.routes)


def test_metrics_can_be_disabled() -> None:
    app = create_app(_settings(), background_loops=(), expose_metrics=False)
    assert not any(getattr(route, "path", "") == METRICS_PATH for route in app.routes)


def test_routers_mounted_under_prefix() -> None:
    router = APIRouter()

    @router.get("/ping")
    async def _ping() -> dict:
        return {"pong": True}

    app = create_app(_settings(), routers=[router], background_loops=())
    assert "/api/v1/ping" in app.openapi()["paths"]


def test_background_loop_gating() -> None:
    async def _noop(_factory, *, stop_event) -> None:
        return None

    inline_only = BackgroundLoop("x", _noop, "stale_sweep_interval_seconds")
    always = BackgroundLoop("y", _noop, inline_only=False)
    disabled = BackgroundLoop("z", _noop, "icron_tick_interval_seconds")

    inline = _settings(task_dispatch_mode="inline")
    celery = _settings(task_dispatch_mode="celery")
    off = _settings(icron_tick_interval_seconds=0)

    assert inline_only.enabled(inline) is True
    assert inline_only.enabled(celery) is False  # celery 模式由 beat 驱动,避免双跑
    assert always.enabled(celery) is True
    assert disabled.enabled(off) is False


def test_default_background_loops_cover_sdk_sweepers() -> None:
    names = {loop.name for loop in default_background_loops()}
    assert {
        "runtime_config_refresher",
        "api_heartbeat",
        "stale_task_sweeper",
        "kb_outbox_consumer",
        "icron_ticker",
        "embedding_campaign_finalizer",
    } <= names


async def test_lifespan_runs_and_stops_loops(monkeypatch) -> None:
    started: list[str] = []
    stopped: list[str] = []

    async def _loop(_factory, *, stop_event) -> None:
        started.append("loop")
        await stop_event.wait()
        stopped.append("loop")

    async def _startup_hook(_factory) -> None:
        started.append("hook")

    async def _shutdown_hook(_factory) -> None:
        stopped.append("hook")

    async def _noop_overrides(_factory) -> None:
        return None

    from nicekit.llm import runtime_config

    monkeypatch.setattr(runtime_config, "load_runtime_overrides", _noop_overrides)

    app = create_app(
        _settings(),
        background_loops=[BackgroundLoop("t", _loop, inline_only=False)],
        startup_hooks=(_startup_hook,),
        shutdown_hooks=(_shutdown_hook,),
        session_factory=object(),
        recover_orphans_on_startup=False,
        warm_mcp_on_startup=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
        app.router.lifespan_context(app),
    ):
        await asyncio.sleep(0)  # 让新建的循环任务跑到第一个 await
        assert started == ["hook", "loop"]
        resp = await client.get(METRICS_PATH)
        assert resp.status_code == 200
    # 停机顺序:先并发回收全部循环,再跑 shutdown 钩子
    assert stopped == ["loop", "hook"]


async def test_shutdown_uses_gather_not_sequential_await(monkeypatch) -> None:
    """TF 的 bug:逐个 await 会被第一个不响应 stop_event 的循环卡死。"""

    async def _stubborn(_factory, *, stop_event) -> None:
        # 故意不看 stop_event,只睡够短的一觉:gather 能一起收,逐个 await 也能收,
        # 但下面那个抛异常的循环在逐个 await 写法里会中断回收链。
        await asyncio.sleep(0.05)

    async def _exploding(_factory, *, stop_event) -> None:
        await stop_event.wait()
        raise RuntimeError("退出时炸了")

    tail: list[str] = []

    async def _tail(_factory, *, stop_event) -> None:
        await stop_event.wait()
        tail.append("collected")

    async def _noop_overrides(_factory) -> None:
        return None

    from nicekit.llm import runtime_config

    monkeypatch.setattr(runtime_config, "load_runtime_overrides", _noop_overrides)

    app = create_app(
        _settings(),
        background_loops=[
            BackgroundLoop("stubborn", _stubborn, inline_only=False),
            BackgroundLoop("exploding", _exploding, inline_only=False),
            BackgroundLoop("tail", _tail, inline_only=False),
        ],
        session_factory=object(),
        recover_orphans_on_startup=False,
        warm_mcp_on_startup=False,
    )
    # 停机不该抛,且异常循环之后的循环仍被回收
    async with asyncio.timeout(5):
        async with app.router.lifespan_context(app):
            pass
    assert tail == ["collected"]


def test_tool_registry_must_be_registry_instance() -> None:
    with pytest.raises(TypeError):
        create_app(_settings(), background_loops=(), tool_registry=object())
