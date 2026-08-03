"""demo 宿主的 ASGI 入口:``create_app()`` 组装 nicekit + 示例扩展点。

一个真实宿主要做的事在这里全了(共约 20 行有效代码):
1. 注册自己的扩展点(``install_demo_extensions``);
2. 选择要挂的 routers(这里用 SDK 全量 ``default_routers()``);
3. ``create_app(...)`` —— 中间件顺序、lifespan、后台循环、``/metrics`` 由 SDK 负责。

启动一律走 ``run.py``(Windows 上直接跑 uvicorn 会让 DB 后台任务静默失败,
见 MIGRATION-PLAN §8 风险 5)。
"""

from __future__ import annotations

from fastapi import FastAPI
from nicekit.api.v1.router import default_routers
from nicekit.core.config import get_settings
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from demo_backend.extensions import install_demo_extensions


async def _log_ready(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """启动钩子示例:确认扩展点已就位(真实宿主可换成预热缓存 / 校验配置)。"""
    import logging

    from nicekit.agent.service import registered_context_providers
    from nicekit.agent.tools import default_registry

    logging.getLogger(__name__).info(
        "demo 宿主就绪",
        extra={
            "tools": len(default_registry),
            "context_providers": [p.name for p in registered_context_providers()],
        },
    )


def build_app() -> FastAPI:
    settings = get_settings()
    # 注册(纯内存、幂等):SDK 内置工具 + KB 目录 + IncidentRecorder + 写角色通知
    install_default_ports()
    # 宿主扩展点:自定义工具表 + ContextProvider + ResourceResolver
    host_tools = install_demo_extensions()
    return create_app(
        settings,
        routers=default_routers(),
        tool_registry=host_tools,
        startup_hooks=(_log_ready,),
        title="Nice Knowledge",
    )


app = build_app()
