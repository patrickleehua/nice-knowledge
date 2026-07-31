"""运行时服务配置缓存:service_configs 的进程级只读副本。

get_llm_service() / default_embedder() 等同步工厂在每次请求里构建客户端,
无法 await 查库,故这里维护一份进程内缓存:
- 启动时 load_runtime_overrides() 全量加载;
- lifespan 周期刷新(默认 60s,多进程/worker 各自刷新);
- admin 保存 service_configs 后在 API 进程内立即刷新。

读取方拿到的是"已滤掉空值键"的覆盖字典,.env 兜底逻辑由读取方 merge。

SDK 化改造(蓝图 §5.3):TF 的模块级全局(_overrides/_providers/_model_routes)
收进 RuntimeConfig 类;模块级保留一个默认实例 + 原函数式入口的薄包装,
搬运代码与宿主默认路径零改动,测试/多实例宿主可自建 RuntimeConfig 隔离状态。
"""

import logging
import time

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from nicekit.models.llm import ModelRoute
from nicekit.models.llm_provider import LlmProvider
from nicekit.models.service_config import ServiceConfig

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 60


def _runtime_service_payload(name: str, payload: dict | None) -> dict:
    values = {
        key: value
        for key, value in (payload or {}).items()
        if value not in ("", None)
    }
    # 保留 TF 对 embedding/rerank 的特判:这两个槽位的 ServiceConfig 行属于
    # 能力配置归一(embedding 只认 provider/model/dim 三键,rerank 的模型
    # 选择已全部走 model_routes 故覆盖恒空)——读的是平台 service_configs
    # 表,不是业务泄漏,SDK 保留。
    if name == "embedding":
        return {
            key: values[key]
            for key in ("provider", "model", "dim")
            if key in values
        }
    if name == "rerank":
        return {}
    return values


class RuntimeConfig:
    """service_configs + llm_providers + 平台 model_routes 的进程内缓存。"""

    def __init__(self) -> None:
        self._overrides: dict[str, dict] = {}
        # None = 未加载(回落 .env 内置双线)
        self._providers: list[dict] | None = None
        self._model_routes: dict[str, dict] | None = None
        self._loaded_at: float = 0.0

    def overrides(self, name: str) -> dict:
        """同步读取某服务的覆盖配置(未加载/无配置时为空 dict = 全走 .env)。"""
        return self._overrides.get(name, {})

    def providers(self) -> list[dict] | None:
        """同步读取模型提供商实例列表(含 disabled,读取方自行过滤);
        None = 表尚未加载/迁移,调用方回落 .env 内置双线。"""
        return self._providers

    def model_route(self, task: str) -> dict | None:
        """Return the active platform route cached for synchronous model adapters."""
        if self._model_routes is None:
            return None
        route = self._model_routes.get(task)
        return dict(route) if route is not None else None

    def age(self) -> float | None:
        return (time.monotonic() - self._loaded_at) if self._loaded_at else None

    async def load(self, session_factory: async_sessionmaker) -> None:
        """全量加载 service_configs + llm_providers 到进程缓存;
        失败保留旧值(不阻断调用方)。"""
        try:
            async with session_factory() as session:
                rows = (await session.execute(select(ServiceConfig))).scalars().all()
                provider_rows = (
                    (await session.execute(select(LlmProvider))).scalars().all()
                )
                route_rows = (
                    (
                        await session.execute(
                            select(ModelRoute)
                            .where(ModelRoute.org_id.is_(None), ModelRoute.is_active)  # type: ignore[union-attr]
                            .order_by(ModelRoute.created_at.desc(), ModelRoute.id)
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:
            logger.exception("加载运行时服务配置失败,沿用进程内旧配置")
            return
        self._overrides = {
            row.name: payload
            for row in rows
            if (payload := _runtime_service_payload(row.name, row.payload))
        }
        self._providers = [
            {
                "name": row.name,
                "protocol": row.protocol,
                "api_key": row.api_key or "",
                "base_url": row.base_url or "",
                "models": list(row.models or []),
                "model_metadata": dict(row.model_metadata or {}),
                "enabled": row.enabled,
            }
            for row in provider_rows
        ]
        routes: dict[str, dict] = {}
        for row in route_rows:
            routes.setdefault(
                row.task,
                {
                    "primary_provider": row.primary_provider,
                    "primary_model": row.primary_model,
                    "fallback_chain": list(row.fallback_chain or []),
                    "timeout_seconds": row.timeout_seconds,
                },
            )
        self._model_routes = routes
        self._loaded_at = time.monotonic()

    async def run_refresher(
        self, session_factory: async_sessionmaker, *, stop_event
    ) -> None:
        """常驻刷新循环(lifespan 托管):stop_event 置位即退出。"""
        import asyncio

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS
                )
            except TimeoutError:
                await self.load(session_factory)


# 模块级默认实例:函数式入口(下)与 get_llm_service() 默认路径共用。
default_runtime_config = RuntimeConfig()


def runtime_overrides(name: str) -> dict:
    """薄包装:默认实例的 overrides()(TF 兼容入口)。"""
    return default_runtime_config.overrides(name)


def runtime_providers() -> list[dict] | None:
    """薄包装:默认实例的 providers()(TF 兼容入口)。"""
    return default_runtime_config.providers()


def runtime_model_route(task: str) -> dict | None:
    """薄包装:默认实例的 model_route()(TF 兼容入口)。"""
    return default_runtime_config.model_route(task)


def runtime_overrides_age() -> float | None:
    """薄包装:默认实例的 age()(TF 兼容入口)。"""
    return default_runtime_config.age()


async def load_runtime_overrides(session_factory: async_sessionmaker) -> None:
    """薄包装:默认实例的 load()(TF 兼容入口)。"""
    await default_runtime_config.load(session_factory)


async def run_runtime_config_refresher(
    session_factory: async_sessionmaker, *, stop_event
) -> None:
    """薄包装:默认实例的 run_refresher()(TF 兼容入口)。"""
    await default_runtime_config.run_refresher(session_factory, stop_event=stop_event)
