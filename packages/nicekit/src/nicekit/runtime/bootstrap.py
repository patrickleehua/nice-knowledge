"""平台装配入口(MIGRATION-PLAN §5.9 A2/A3):把散落的注册与 seed 收敛成一处。

前序波次刻意不做 import 期副作用(避免"碰一下 agent 就把 KB / capabilities
整条依赖链拖进来"),代价是装配方必须显式做这些事。本模块就是那份清单:

- :func:`install_default_ports` —— **注册**(纯内存、幂等、无 IO):
  内置工具自注册(A2)、KB 任务目录条目、IncidentRecorder 默认 SQL 实现、
  KB 通知角色跟随 ``tenancy.roles.write_roles()``;
- :func:`bootstrap_platform` —— **seed**(需要数据库会话,幂等):
  实体类型(至少 ``concept``,否则实体绑定白名单为空)、KB prompt、
  ``kb.answer`` 路由、``agent.default`` 路由、默认 agent 卡;
  并在最后跑一次 prompt 资源变量校验。

两者都可以重复调用。``bootstrap_platform`` 自己开事务边界(commit 一次),
供 demo 的 seed 脚本、测试夹具、宿主 startup hook 共用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.config import Settings, get_settings
from nicekit.models.llm import ModelRoute

logger = logging.getLogger(__name__)

#: 默认 agent 卡与压缩 / 记忆抽取共用的 LLM task
AGENT_DEFAULT_TASK = "agent.default"
#: 默认卡的墙钟预算(agent 主循环是多轮的,给足)
AGENT_DEFAULT_TIMEOUT = 300.0

_ports_installed = False


def install_default_ports(*, force: bool = False) -> None:
    """装配期注册(A2/A3 的"注册"半边)。纯内存操作,不碰数据库。

    1. ``import nicekit.agent.builtin_tools`` —— 17 个通用工具自注册到
       ``agent.tools.default_registry``(builtin_tools 刻意不做 import 期
       自注册以外的副作用,但它本身必须被显式 import 一次);
    2. KB 的 prompt 任务目录条目注册进 ``llm.prompt_catalog``;
    3. ``kb.ports`` 的 IncidentRecorder 绑定 operations 的 SQL 默认实现;
    4. KB 治理通知的收件角色跟随 ``tenancy.roles.write_roles()``——宿主
       ``register_write_roles("editor")`` 之后,editor 一并收到 KB 治理通知。
    """
    global _ports_installed
    if _ports_installed and not force:
        return

    import nicekit.agent.builtin_tools  # noqa: F401 - import 即注册(A2)
    from nicekit.kb import ports as kb_ports
    from nicekit.kb.prompts_seed import register_kb_prompt_catalog
    from nicekit.operations.incidents import SqlIncidentRecorder
    from nicekit.tenancy.roles import write_roles

    register_kb_prompt_catalog()
    kb_ports.set_incident_recorder(SqlIncidentRecorder())
    kb_ports.set_kb_notify_roles(write_roles())
    _ports_installed = True


@dataclass(slots=True)
class BootstrapReport:
    """各 seed 步骤的新建行数(0 = 已是最新,幂等重跑的常态)。"""

    entity_types: int = 0
    kb_prompts: int = 0
    kb_answer_route: int = 0
    agent_default_route: int = 0
    agent_cards: int = 0
    #: 单租户模式下实际使用的分区键(非单租户为 None)
    single_tenant_org: UUID | None = None
    #: 单租户模式下垫出的默认操作者(agent 权限表的 users 外键需要它)
    single_tenant_subject: UUID | None = None
    prompt_resource_issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "single_tenant_org": str(self.single_tenant_org) if self.single_tenant_org else None,
            "single_tenant_subject": (
                str(self.single_tenant_subject) if self.single_tenant_subject else None
            ),
            "entity_types": self.entity_types,
            "kb_prompts": self.kb_prompts,
            "kb_answer_route": self.kb_answer_route,
            "agent_default_route": self.agent_default_route,
            "agent_cards": self.agent_cards,
            "prompt_resource_issues": list(self.prompt_resource_issues),
        }


def build_agent_default_route(settings: Settings) -> ModelRoute | None:
    """按 ``.env`` 的默认 LLM 配置构造 ``agent.default`` 平台路由。

    未配 ``llm_default_model`` 时返回 None —— 没有可用模型时不该凭空写一条
    指向空模型名的路由(路由解析会当它有效,然后在真实调用时才炸)。
    """
    if not settings.llm_default_model:
        return None
    fallback = (
        [{"provider": settings.llm_fallback_provider, "model": settings.llm_fallback_model}]
        if settings.llm_fallback_provider and settings.llm_fallback_model
        else None
    )
    return ModelRoute(
        task=AGENT_DEFAULT_TASK,
        primary_provider=settings.llm_default_provider,
        primary_model=settings.llm_default_model,
        fallback_chain=fallback,
        max_tokens=settings.llm_default_max_tokens,
        timeout_seconds=AGENT_DEFAULT_TIMEOUT,
    )


async def ensure_agent_default_route(session: AsyncSession) -> int:
    """平台级 ``agent.default`` 路由只在缺失时补种(admin 改过的行不动)。"""
    exists = (
        await session.execute(
            select(ModelRoute.id)
            .where(ModelRoute.task == AGENT_DEFAULT_TASK, ModelRoute.org_id.is_(None))  # type: ignore[union-attr]
            .limit(1)
        )
    ).scalar_one_or_none()
    if exists is not None:
        return 0
    route = build_agent_default_route(get_settings())
    if route is None:
        logger.warning(
            "未配置 llm_default_model,跳过 agent.default 路由 seed"
            "(配好 provider 后经 admin /admin/models 补一条即可)"
        )
        return 0
    session.add(route)
    return 1


async def bootstrap_platform(
    session: AsyncSession,
    *,
    org_id: UUID | None = None,
    entity_type_specs: list[dict] | None = None,
    seed_agent_card: bool = True,
    single_tenant: bool = False,
) -> BootstrapReport:
    """幂等 seed 平台基线。调用方只需给一个**普通会话**(非 org 会话)。

    - ``org_id``:seed 归属组织,缺省为 ``settings.platform_org_id``;
    - ``entity_type_specs``:实体类型定义,缺省为 SDK 内置(仅 ``concept`` 兜底);
      宿主传自己的行业类型即可(SDK 不内置任何领域词表);
    - ``seed_agent_card``:是否 seed 中性默认 agent 卡;
    - ``single_tenant``:单租户模式下把 ``SINGLE_TENANT_ORG_ID`` 与默认操作者
      一并垫进 ``organizations`` / ``users``(agent 权限三表对两者都有外键),
      并把 seed 归属改到它 —— 单租户的数据是普通租户数据,不该落在语义为
      "对所有组织可见"的平台 org 下。

    带 RLS 的表(kb_entity_types / agent_cards)由各 ensure_* 自行 SET LOCAL
    org 上下文,所以本函数用普通会话即可;末尾统一 commit 一次。
    """
    install_default_ports()

    from nicekit.agent.prompts import validate_prompt_resources
    from nicekit.agent.seed import ensure_default_agent_card
    from nicekit.kb.answer_seed import ensure_kb_answer_route
    from nicekit.kb.entity_types import ensure_entity_types
    from nicekit.kb.prompts_seed import ensure_kb_prompts

    report = BootstrapReport()
    if single_tenant:
        from nicekit.api.deps import SINGLE_TENANT_ORG_ID, single_tenant_subject_id
        from nicekit.tenancy.orgs import ensure_principal

        target_org = org_id or SINGLE_TENANT_ORG_ID
        # org 与默认操作者都要垫:agent 权限三表对 organizations 与 users 都有
        # 外键,只垫一半的表现是"一切正常,直到第一次改权限偏好才 500"。
        subject = single_tenant_subject_id(target_org)
        await ensure_principal(
            session,
            target_org,
            subject,
            org_name="Single Tenant",
            org_slug="single-tenant",
            user_full_name="Single Tenant Operator",
        )
        report.single_tenant_org = target_org
        report.single_tenant_subject = subject
    else:
        target_org = org_id or get_settings().platform_org_id

    report.entity_types = await ensure_entity_types(
        session, target_org, specs=entity_type_specs
    )
    # ensure_kb_prompts 自带 commit(prompt 表无 RLS,提交后不影响后续 seed)
    report.kb_prompts = await ensure_kb_prompts(session)
    report.kb_answer_route = await ensure_kb_answer_route(session)
    report.agent_default_route = await ensure_agent_default_route(session)
    if seed_agent_card:
        report.agent_cards = await ensure_default_agent_card(session, target_org)
    await session.commit()

    # prompt 资源的变量声明校验:缺变量只告警,不阻断启动(资源是可覆盖的)
    try:
        validate_prompt_resources()
    except Exception as exc:  # noqa: BLE001 - 校验失败不该让平台起不来
        report.prompt_resource_issues.append(str(exc))
        logger.warning("prompt 资源校验未通过:%s", exc)

    logger.info("平台 bootstrap 完成", extra=report.as_dict())
    return report


async def bootstrap_with_factory(
    session_factory: async_sessionmaker[AsyncSession],
    **kwargs,
) -> BootstrapReport:
    """``bootstrap_platform`` 的 session_factory 版本,可直接做 startup hook。"""
    async with session_factory() as session:
        return await bootstrap_platform(session, **kwargs)


__all__ = [
    "AGENT_DEFAULT_TASK",
    "AGENT_DEFAULT_TIMEOUT",
    "BootstrapReport",
    "bootstrap_platform",
    "bootstrap_with_factory",
    "build_agent_default_route",
    "ensure_agent_default_route",
    "install_default_ports",
]
