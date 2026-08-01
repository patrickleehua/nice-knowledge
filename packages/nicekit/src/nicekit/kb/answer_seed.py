"""kb.answer 独立模型路由 seed(幂等)。

知识问答此前蹭 agent.workbench 的路由,导致问答被动继承 agent 的 300s 超时
与后续调参。拆成独立任务后,平台可单独为问答挑模型/降级链而不影响 agent。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.core.config import Settings, get_settings
from nicekit.kb.answer import KB_ANSWER_TASK
from nicekit.models.llm import ModelRoute

# 问答是单跳生成,不需要 agent 工作台多轮循环的 300s 预算
KB_ANSWER_TIMEOUT = 120.0


def build_kb_answer_route(settings: Settings) -> ModelRoute | None:
    """按平台默认 LLM 配置构造路由;未配置默认模型时返回 None(与 agent seed 同规则)。"""

    if not settings.llm_default_model:
        return None
    fallback = (
        [{"provider": settings.llm_fallback_provider, "model": settings.llm_fallback_model}]
        if settings.llm_fallback_provider and settings.llm_fallback_model
        else None
    )
    return ModelRoute(
        task=KB_ANSWER_TASK,
        primary_provider=settings.llm_default_provider,
        primary_model=settings.llm_default_model,
        fallback_chain=fallback,
        max_tokens=settings.llm_default_max_tokens,
        timeout_seconds=KB_ANSWER_TIMEOUT,
    )


async def ensure_kb_answer_route(session: AsyncSession) -> int:
    """平台级默认路由只在缺失时补种;业务方后续经 admin 调整过的行不动。"""

    route_exists = (
        await session.execute(
            select(ModelRoute.id)
            .where(ModelRoute.task == KB_ANSWER_TASK, ModelRoute.org_id.is_(None))  # type: ignore[union-attr]
            .limit(1)
        )
    ).scalar_one_or_none()
    if route_exists is not None:
        return 0
    route = build_kb_answer_route(get_settings())
    if route is None:
        return 0
    session.add(route)
    return 1
