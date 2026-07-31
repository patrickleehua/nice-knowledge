"""Prompt Registry 与模型路由解析。

redis 缓存(排期项 4/4):prompt/路由是每次 LLM 调用都要查、变更极低频的
热数据(admin 操作才变)。TTL 短(默认 60s)+ admin 写路径主动 delete 键
(共享 redis,API 与 worker 同步失效);redis 不可达自动回源查库。
"""

from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.llm.capability_routes import LLM_ROUTE_TASK
from nicekit.models.llm import ModelRoute, Prompt


class PromptNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class ActivePrompt:
    """get_active_prompt 的返回契约(缓存命中时不构造 ORM 对象)。"""

    version: int
    content: str


@dataclass(frozen=True)
class ResolvedRoute:
    primary_provider: str
    primary_model: str
    fallback_chain: list[dict]  # [{"provider": ..., "model": ...}]
    max_tokens: int
    timeout_seconds: float


def prompt_cache_key(task: str) -> str:
    return f"llm:prompt:{task}"


def route_cache_key(task: str, org_id: UUID | None) -> str:
    return f"llm:route:{task}:{org_id or 'platform'}"


async def get_active_prompt(session: AsyncSession, task: str) -> ActivePrompt:
    """取 task 的最高 active 版本(短 TTL 缓存)。"""
    ttl = get_settings().llm_registry_cache_ttl_seconds
    key = prompt_cache_key(task)
    if ttl > 0:
        cached = await cache.get_json(key)
        if cached is not None:
            return ActivePrompt(**cached)
    prompt = (
        await session.execute(
            select(Prompt)
            .where(Prompt.task == task, Prompt.is_active)
            .order_by(Prompt.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(f"task {task!r} 没有 active prompt,先在 Registry 登记")
    result = ActivePrompt(version=prompt.version, content=prompt.content)
    if ttl > 0:
        await cache.set_json(key, asdict(result), ttl)
    return result


async def resolve_route(session: AsyncSession, task: str, org_id: UUID) -> ResolvedRoute:
    """解析顺序:org 级路由 → 平台级路由(org_id IS NULL)→ settings 全局默认。
    缓存按 (task, org_id) 存最终解析结果(含平台兜底/默认值展开)。"""
    ttl = get_settings().llm_registry_cache_ttl_seconds
    key = route_cache_key(task, org_id)
    if ttl > 0:
        cached = await cache.get_json(key)
        if cached is not None:
            return ResolvedRoute(**cached)
    route_obj = await _resolve_route_from_db(session, task, org_id)
    if ttl > 0:
        await cache.set_json(key, asdict(route_obj), ttl)
    return route_obj


async def _resolve_route_from_db(
    session: AsyncSession, task: str, org_id: UUID
) -> ResolvedRoute:
    routes = (
        await session.execute(
            select(ModelRoute).where(
                ModelRoute.task == task,
                ModelRoute.is_active,
                (ModelRoute.org_id == org_id) | (ModelRoute.org_id.is_(None)),  # type: ignore[union-attr]
            )
        )
    ).scalars().all()

    route = next((r for r in routes if r.org_id == org_id), None) or next(
        (r for r in routes if r.org_id is None), None
    )
    if route is None and task != LLM_ROUTE_TASK:
        # 平台级 llm.default 是"对话模型"板块:任务没有专属路由时的统一兜底,
        # 让模型选择留在数据库而不是各部署的 .env。
        route = (
            await session.execute(
                select(ModelRoute).where(
                    ModelRoute.task == LLM_ROUTE_TASK,
                    ModelRoute.is_active,
                    ModelRoute.org_id.is_(None),  # type: ignore[union-attr]
                )
            )
        ).scalars().first()
    if route is not None:
        return ResolvedRoute(
            primary_provider=route.primary_provider,
            primary_model=route.primary_model,
            fallback_chain=list(route.fallback_chain or []),
            max_tokens=route.max_tokens,
            timeout_seconds=route.timeout_seconds,
        )

    settings = get_settings()
    if not settings.llm_default_model:
        raise LookupError(
            f"task {task!r} 无 model_routes、无 {LLM_ROUTE_TASK} 兜底路由,"
            "且未配置 LLM_DEFAULT_MODEL"
        )
    fallback = (
        [{"provider": settings.llm_fallback_provider, "model": settings.llm_fallback_model}]
        if settings.llm_fallback_provider and settings.llm_fallback_model
        else []
    )
    return ResolvedRoute(
        primary_provider=settings.llm_default_provider,
        primary_model=settings.llm_default_model,
        fallback_chain=fallback,
        max_tokens=settings.llm_default_max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
    )
