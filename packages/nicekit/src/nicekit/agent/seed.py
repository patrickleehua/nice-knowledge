"""SDK 默认 agent 卡 seed(幂等,带版本升级)。

搬自 TF backend/app/services/agent/seed.py,但**只保留机制,不保留任何业务内容**
(MIGRATION-PLAN §5.4):TF 的 workbench SOP prompt(:81-147)、research /
quote 专员卡与它们的 prompt、63 个工具白名单一律不搬——那是行业 SOP,属于
宿主的 seed。SDK 这里只给一张中性默认卡:名字取 `service.default_card_name()`
(默认 `assistant`),启用全部内置通用工具,角色段直接用 `agent_role_generic`
资源正文。宿主要的专员卡/业务卡在 apps/demo 或自己的装配层里 seed。

版本机制(同 TF):seed 版本号高于库内最高版本才发新版并激活;业务方经 admin
Agent 管理升过更高版本则不动,seed 永远不会把人工改动踩掉。
"""

import logging
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.builtin_tools import BUILTIN_TOOL_NAMES
from nicekit.agent.prompts import render
from nicekit.agent.service import default_card_name
from nicekit.core.config import get_settings
from nicekit.models.chat import AgentCard, AgentCardVersion

logger = logging.getLogger(__name__)

# 默认卡的 LLM task 名。与 compression/memory 的默认 task 同名,宿主只要配一条
# `agent.default` 路由,agent 主链路 + 摘要 + 记忆抽取就全都能跑起来。
DEFAULT_AGENT_TASK = "agent.default"
DEFAULT_CARD_SEED_VERSION = 1
DEFAULT_CARD_MAX_TURNS = 12
DEFAULT_CARD_TIMEOUT_SECONDS = 300
DEFAULT_CARD_DESCRIPTION = "通用助手:在已开放的工具范围内查事实、读写数据并如实汇报"


def default_card_role_prompt() -> str:
    """默认卡的 system_prompt:`agent_role_generic` 资源正文(去掉标题行)。

    卡上的 system_prompt 只承担"角色职责"这一段,标题由组装时的
    `agent_role_custom` 包装给出(见 prompts/assembly.py),所以这里把资源
    正文的【…】标题行剥掉,免得组装后出现两个同名标题。
    """
    body = render("agent_role_generic").strip()
    lines = body.splitlines()
    if lines and lines[0].strip().startswith("【"):
        body = "\n".join(lines[1:]).strip()
    return body


def default_card_tools() -> list[str]:
    """默认卡的工具白名单:SDK 内置通用工具全集。

    刻意读 `BUILTIN_TOOL_NAMES` 而不是 `default_registry.names()`:后者会把宿主
    注册的业务工具一并卷进来,让"默认卡"在不同宿主里含义不同。宿主要给默认卡
    加自己的工具,应当在 admin 里发新版本(那是一个需要人明确表态的决定)。
    """
    return list(BUILTIN_TOOL_NAMES)


def _seed_version(card_id: UUID, *, system_prompt: str, tools: list[str]) -> AgentCardVersion:
    return AgentCardVersion(
        card_id=card_id,
        version=DEFAULT_CARD_SEED_VERSION,
        system_prompt=system_prompt,
        model_task=DEFAULT_AGENT_TASK,
        max_turns=DEFAULT_CARD_MAX_TURNS,
        timeout_seconds=DEFAULT_CARD_TIMEOUT_SECONDS,
        tools=tools,
        is_active=True,
        status="active",
    )


async def ensure_default_agent_card(
    session: AsyncSession, org_id: UUID | None = None
) -> int:
    """seed 一张中性默认 agent 卡(幂等)。返回新建/升级数(0 表示已是最新)。

    org_id=None 时 seed 到平台组织(settings.platform_org_id),它对所有组织可见
    并作为兜底(`service.resolve_card` 先找本 org 同名卡,再回落平台卡)。
    传具体 org_id 则为该组织 seed 一张覆盖卡。

    调用方负责事务边界:本函数只 add/flush,不 commit——装配期通常要和其他
    seed(实体类型、prompt 目录、模型路由)一起落盘。
    """
    target_org = org_id or get_settings().platform_org_id
    name = default_card_name()
    system_prompt = default_card_role_prompt()
    tools = default_card_tools()

    # agent_cards 开了 FORCE RLS:seed 会话需要目标 org 上下文(事务级,commit 前有效)
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"),
        {"org": str(target_org)},
    )
    card = (
        await session.execute(
            select(AgentCard)
            .where(AgentCard.org_id == target_org, AgentCard.name == name)
            .limit(1)
        )
    ).scalar_one_or_none()
    if card is None:
        card = AgentCard(
            org_id=target_org,
            name=name,
            description=DEFAULT_CARD_DESCRIPTION,
            system_prompt=system_prompt,
            model_task=DEFAULT_AGENT_TASK,
            max_turns=DEFAULT_CARD_MAX_TURNS,
            timeout_seconds=float(DEFAULT_CARD_TIMEOUT_SECONDS),
            tools=tools,
        )
        session.add(card)
        await session.flush()
        session.add(_seed_version(card.id, system_prompt=system_prompt, tools=tools))
        logger.info("seed 默认 agent 卡 org=%s name=%s", target_org, name)
        return 1

    max_version = (
        await session.execute(
            select(func.max(AgentCardVersion.version)).where(
                AgentCardVersion.card_id == card.id
            )
        )
    ).scalar_one() or 0
    if max_version >= DEFAULT_CARD_SEED_VERSION:
        return 0

    # 发版:停用旧激活版本 → 插入 seed 新版并激活,卡本体字段同步
    actives = (
        (
            await session.execute(
                select(AgentCardVersion).where(
                    AgentCardVersion.card_id == card.id,
                    AgentCardVersion.is_active,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in actives:
        row.is_active = False
        session.add(row)
    await session.flush()
    session.add(_seed_version(card.id, system_prompt=system_prompt, tools=tools))
    card.system_prompt = system_prompt
    card.model_task = DEFAULT_AGENT_TASK
    card.max_turns = DEFAULT_CARD_MAX_TURNS
    card.timeout_seconds = float(DEFAULT_CARD_TIMEOUT_SECONDS)
    card.tools = tools
    session.add(card)
    logger.info("升级默认 agent 卡 org=%s name=%s v%s", target_org, name, DEFAULT_CARD_SEED_VERSION)
    return 1


__all__ = [
    "DEFAULT_AGENT_TASK",
    "DEFAULT_CARD_DESCRIPTION",
    "DEFAULT_CARD_MAX_TURNS",
    "DEFAULT_CARD_SEED_VERSION",
    "DEFAULT_CARD_TIMEOUT_SECONDS",
    "default_card_role_prompt",
    "default_card_tools",
    "ensure_default_agent_card",
]
