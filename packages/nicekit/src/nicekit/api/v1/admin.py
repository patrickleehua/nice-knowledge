"""平台管理 API —— Agent 域(仅 platform_admin;MCP 另开 org_admin 可用的子路由)。

搬自 TF backend/app/api/v1/admin.py 中与 Agent 子系统相关的部分:
Prompt Registry / Prompt 目录登记 / Prompt 资源(只读)/ 组装预览 /
Agent 卡与版本 / 工具目录 / Agent 运行日志(运行时上下文快照)/ 技能包 / MCP servers。

**本文件当前不含**(按迁移波次划分,避免与并行线撞车):
- 模型路由 / 提供商实例 / 外部服务配置:属 LLM 层管理面。TF 的模型路由 PATCH
  路径带一个"换嵌入主模型即启动重嵌 campaign"的副作用,直接耦合
  `kb.embedding_reindex` 与 `services/dispatch`(任务分发注册表),两者都不在
  本波交付范围。P4 装配期补齐时应把该副作用做成可注册的路由变更钩子,而不是
  让 SDK 的 admin 直接 import KB。
- 租户管理(orgs/users)、用量看板、计费、LLM traces:P4。
- KB 相关 admin 端点:P3c。

访问控制在路由层(require_role):organizations/users/prompts/model_routes 均为
无 RLS 的平台配置表。
"""

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, case, cast, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent import prompts as promptres
from nicekit.agent.mcp_manager import mcp_client_manager
from nicekit.agent.runtime_snapshot import CONTEXT_SNAPSHOT_EVENT_TYPE
from nicekit.agent.seed import DEFAULT_AGENT_TASK
from nicekit.agent.service import agent_card_cache_key, resolve_card
from nicekit.agent.skills import (
    SkillError,
    delete_skill,
    list_skill_files,
    list_skills,
    read_skill_file,
    write_skill,
)
from nicekit.agent.tools import default_registry
from nicekit.api.deps import OrgContext, get_org_context, get_org_session, require_role
from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.core.db import get_session
from nicekit.llm import prompt_catalog
from nicekit.llm.registry import prompt_cache_key
from nicekit.models.chat import AgentCard, AgentCardVersion, ChatEvent, ChatSession
from nicekit.models.llm import Prompt, PromptCatalogEntry
from nicekit.models.mcp import McpServer
from nicekit.models.tenancy import Role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_role(Role.PLATFORM_ADMIN))])
mcp_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.ORG_ADMIN))],
)

Session = Annotated[AsyncSession, Depends(get_session)]
OrgSession = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]


# ---------- Prompt Registry ----------


class PromptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)


class PromptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


class PromptOut(BaseModel):
    """列表项 = DB 行原字段 + 目录展示元信息(旧字段零破坏,只加不改)。

    元信息合并顺序:源码 BUILTIN_PROMPT_CATALOG > DB 在线登记
    (prompt_catalog_entries)> 回落机器名。builtin 语义保持"仅源码目录",
    在线登记过的自定义任务用 registered 单独标识。
    """

    id: UUID
    task: str
    version: int
    content: str
    is_active: bool
    created_by: UUID | None
    created_at: datetime | None
    # 目录附加:未登记目录的任务 name_zh 回落为 task 本身、description 为空
    name_zh: str
    description: str
    category: str
    builtin: bool
    # True = 该 task 在 prompt_catalog_entries 有在线登记(与 builtin 正交)
    registered: bool
    # content 中扫出的 {{xxx}} 占位符,仅展示用:DB prompt 运行时零变量替换,
    # 占位符会原样发给模型,变量拼接由调用方代码完成(与资源化 prompt 不同)
    variables: list[str]


# 与 nicekit/agent/prompts/loader.py 的 _VAR_RE 同款:{{var}},var 限标识符
_PROMPT_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _prompt_out(prompt: Prompt, db_entry: PromptCatalogEntry | None) -> PromptOut:
    entry = prompt_catalog.lookup(prompt.task)
    if entry is not None:
        name_zh, description = entry["name_zh"], entry["description"]
    elif db_entry is not None:
        name_zh, description = db_entry.name_zh, db_entry.description
    else:
        name_zh, description = prompt.task, ""
    return PromptOut(
        id=prompt.id,
        task=prompt.task,
        version=prompt.version,
        content=prompt.content,
        is_active=prompt.is_active,
        created_by=prompt.created_by,
        created_at=prompt.created_at,
        name_zh=name_zh,
        description=description,
        category=prompt_catalog.category_of(prompt.task),
        builtin=entry is not None,
        registered=db_entry is not None,
        variables=sorted(set(_PROMPT_VAR_RE.findall(prompt.content))),
    )


@router.get("/prompts", response_model=list[PromptOut])
async def list_prompts(
    session: Session, category: str | None = None, q: str | None = None
) -> list[PromptOut]:
    """全量列表(量小不分页);category 精确匹配、q 对 task/中文名做子串匹配。

    过滤放在元信息合并之后做:category 需要 category_of 推导、q 需要合并后的
    name_zh,在 SQL 里都算不出来。
    """
    rows = (
        (await session.execute(select(Prompt).order_by(Prompt.task, Prompt.version.desc())))
        .scalars()
        .all()
    )
    entries = (await session.execute(select(PromptCatalogEntry))).scalars().all()
    by_task = {item.task: item for item in entries}
    items = [_prompt_out(row, by_task.get(row.task)) for row in rows]
    if category:
        items = [item for item in items if item.category == category]
    if q and q.strip():
        needle = q.strip().lower()
        items = [
            item
            for item in items
            if needle in item.task.lower() or needle in item.name_zh.lower()
        ]
    return items


@router.post("/prompts", response_model=Prompt, status_code=status.HTTP_201_CREATED)
async def create_prompt_version(body: PromptCreate, session: Session) -> Prompt:
    """同 task 版本号自增;内容一经登记不可改,改动 = 发新版本。"""
    max_version = (
        await session.execute(
            select(func.coalesce(func.max(Prompt.version), 0)).where(Prompt.task == body.task)
        )
    ).scalar_one()
    prompt = Prompt(task=body.task, version=max_version + 1, content=body.content)
    session.add(prompt)
    await session.commit()
    await session.refresh(prompt)
    await cache.delete(prompt_cache_key(body.task))
    return prompt


@router.patch("/prompts/{prompt_id}", response_model=Prompt)
async def update_prompt(prompt_id: UUID, body: PromptUpdate, session: Session) -> Prompt:
    prompt = (
        await session.execute(select(Prompt).where(Prompt.id == prompt_id))
    ).scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt 不存在")
    prompt.is_active = body.is_active
    session.add(prompt)
    await session.commit()
    await session.refresh(prompt)
    await cache.delete(prompt_cache_key(prompt.task))
    return prompt


# ---------- 自定义任务目录登记(prompt_catalog_entries) ----------
# 内置任务的元信息随源码 BUILTIN_PROMPT_CATALOG 治理,这里只收自定义任务;
# 目录不影响运行时取 prompt 内容,故不碰 prompt_cache_key 缓存。


class PromptCatalogUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_zh: str = Field(min_length=1, max_length=100)
    description: str


def _reject_builtin_catalog_task(task: str) -> None:
    if prompt_catalog.lookup(task) is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "内置任务的目录信息随源码 BUILTIN_PROMPT_CATALOG 治理,不支持在线修改",
        )


@router.put("/prompt-catalog/{task}", response_model=PromptCatalogEntry)
async def upsert_prompt_catalog_entry(
    task: str, body: PromptCatalogUpsert, session: Session, ctx: Ctx
) -> PromptCatalogEntry:
    """upsert 自定义任务的中文名/作用描述(目录可原地改写,不做版本化)。"""
    if len(task) > 100:
        # 与 prompts.task / prompt_catalog_entries.task 的 String(100) 对齐,
        # 提前拒掉,别让超长 task 变成 DB 报错 500
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "task 长度不能超过 100")
    _reject_builtin_catalog_task(task)
    entry = (
        await session.execute(
            select(PromptCatalogEntry).where(PromptCatalogEntry.task == task)
        )
    ).scalar_one_or_none()
    if entry is None:
        entry = PromptCatalogEntry(
            task=task,
            name_zh=body.name_zh,
            description=body.description,
            created_by=ctx.user_id,
        )
    else:
        entry.name_zh = body.name_zh
        entry.description = body.description
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


@router.delete("/prompt-catalog/{task}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_catalog_entry(task: str, session: Session) -> None:
    """清除自定义任务的目录登记;列表侧该 task 回落机器名展示。"""
    _reject_builtin_catalog_task(task)
    entry = (
        await session.execute(
            select(PromptCatalogEntry).where(PromptCatalogEntry.task == task)
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该任务没有目录登记")
    await session.delete(entry)
    await session.commit()


# ---------- Prompt 资源(源码内置 + 宿主注册目录,只读) ----------
# 区别于上面的数据库 Prompt Registry:system prompt 分层段(global_*/agent_role_*)
# 随源码发布,不走在线编辑,这里只提供排查用的只读预览(改内容需改
# nicekit/agent/prompts/resources/*.md 或宿主注册目录下的同名文件并重启)


class PromptResourceOut(BaseModel):
    id: str
    title: str
    category: str
    description: str
    variables: list[str]
    # source 指向消费方源码,content_chars 让列表页不带全文也能看出各段体量
    # ——全文只在详情端点返回,列表保持轻
    source: str
    content_chars: int
    # 资源来自哪个 root:宿主覆盖 SDK 内置时,一眼能看出生效的是哪一份
    origin: str


class PromptResourceDetail(PromptResourceOut):
    content: str


class PromptResourceBlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    values: dict[str, str] | None = None


class PromptResourcePreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[PromptResourceBlockIn] = Field(min_length=1)


class PromptResourcePreviewOut(BaseModel):
    text: str


@router.get("/prompt-resources", response_model=list[PromptResourceOut])
async def list_prompt_resources() -> list[PromptResourceOut]:
    return [
        PromptResourceOut(
            id=res.id,
            title=res.title,
            category=res.category,
            description=res.description,
            variables=list(res.variables),
            source=res.source,
            content_chars=len(res.content),
            origin=res.origin,
        )
        for res in promptres.list_prompts()
    ]


@router.get("/prompt-resources/{resource_id}", response_model=PromptResourceDetail)
async def get_prompt_resource(resource_id: str) -> PromptResourceDetail:
    try:
        res = promptres.load_prompt(resource_id)
    except promptres.PromptResourceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt 资源不存在") from exc
    return PromptResourceDetail(
        id=res.id,
        title=res.title,
        category=res.category,
        description=res.description,
        variables=list(res.variables),
        source=res.source,
        content_chars=len(res.content),
        origin=res.origin,
        content=res.content,
    )


@router.post("/prompt-resources/preview", response_model=PromptResourcePreviewOut)
async def preview_prompt_resources(
    body: PromptResourcePreviewIn,
) -> PromptResourcePreviewOut:
    """按 blocks 顺序渲染组合,预览模型实际收到的 system prompt 文本。"""
    try:
        composed = promptres.compose(
            [promptres.PromptBlock(id=block.id, values=block.values) for block in body.blocks]
        )
    except promptres.PromptResourceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return PromptResourcePreviewOut(text=composed)


class PromptAssemblyBlockOut(BaseModel):
    id: str
    chars: int


class RuntimeOnlyBlockOut(BaseModel):
    id: str
    title: str
    description: str


class PromptAssemblyOut(BaseModel):
    card_id: str
    card_name: str
    system_prompt_chars: int
    blocks: list[PromptAssemblyBlockOut]
    text: str
    runtime_only_blocks: list[RuntimeOnlyBlockOut]


# 预览走的是与运行时同一条组装函数,但运行时还有若干"每轮派生"的动态段
# (取决于会话绑定的作用域/记忆/工具历史等现场状态),预览端点拿不到也不该
# 模拟。这里固定枚举它们的说明,让前端明示"预览之外还有什么",避免把
# 预览误当成模型收到的完整 prompt。
# 宿主注册的 ContextProvider 会在运行时追加更多块,块 id 即 provider 给出的
# block_id;它们不在这份固定清单里,排障时看运行时快照的 system_prompt_blocks。
_RUNTIME_ONLY_BLOCKS = (
    RuntimeOnlyBlockOut(
        id="context_providers",
        title="宿主上下文段",
        description=(
            "宿主注册的 ContextProvider 每轮从当前作用域派生注入的上下文段;"
            "仅运行时出现,预览不含。"
        ),
    ),
    RuntimeOnlyBlockOut(
        id="memory_recall",
        title="相关记忆",
        description="按本轮话题从长期记忆召回的相关记忆段;仅运行时出现,预览不含。",
    ),
    RuntimeOnlyBlockOut(
        id="tool_trace",
        title="本会话已查过",
        description="本会话已完成的工具调用痕迹,跨压缩边界聚合;仅运行时出现,预览不含。",
    ),
    RuntimeOnlyBlockOut(
        id="capability_context",
        title="运行时能力说明",
        description="运行时能力降级说明,仅当能力被裁剪时追加在末尾;仅运行时出现,预览不含。",
    ),
    RuntimeOnlyBlockOut(
        id="goal_context",
        title="会话目标",
        description="会话目标以 user 角色消息注入,不属于 system prompt;仅运行时出现,预览不含。",
    ),
)


@router.get("/prompt-assembly", response_model=PromptAssemblyOut)
async def preview_prompt_assembly(
    card_id: UUID, session: OrgSession, ctx: Ctx
) -> PromptAssemblyOut:
    """按 agent 卡动态组装 system prompt 预览:真实产物而非拼字符串的近似。

    卡经 resolve_card 解析 active 版本(与运行时同一条链路,含缓存),再用
    build_system_prompt_blocks 组装——预览与模型实际收到的静态部分逐字符一致。
    """
    try:
        card = await resolve_card(session, ctx.org_id, card_id)
    except LookupError as exc:
        # 卡不存在/已停用/没有 active 版本统一 404:对预览方而言都是"组不出来"
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    text, blocks = promptres.build_system_prompt_blocks(card.system_prompt, None)
    return PromptAssemblyOut(
        card_id=str(card.card_id),
        card_name=card.name,
        system_prompt_chars=len(text),
        blocks=[PromptAssemblyBlockOut(id=b["id"], chars=b["chars"]) for b in blocks],
        text=text,
        runtime_only_blocks=list(_RUNTIME_ONLY_BLOCKS),
    )


# ---------- Agent 卡 ----------


class AgentCardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, max_length=100)
    org_id: UUID | None = None
    # 可被主 agent 委派的专员卡;默认关,开放委派必须显式勾选
    delegatable: bool = False


class AgentCardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, max_length=100)
    sort_order: int | None = None
    is_active: bool | None = None
    delegatable: bool | None = None


class AgentVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(min_length=1)
    model_task: str = Field(min_length=1, max_length=100)
    max_turns: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    tools: list[str] = Field(default_factory=list)
    mcp_bindings: list[dict] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


def _version_summary(version: AgentCardVersion | None) -> dict | None:
    if version is None:
        return None
    return {
        "id": str(version.id),
        "card_id": str(version.card_id),
        "version": version.version,
        "system_prompt": version.system_prompt,
        "model_task": version.model_task,
        "max_turns": version.max_turns,
        "timeout_seconds": version.timeout_seconds,
        "tools": version.tools,
        "mcp_bindings": version.mcp_bindings,
        "skills": version.skills,
        "status": version.status,
        "is_active": version.is_active,
        "created_at": version.created_at,
    }


def _card_payload(card: AgentCard, version: AgentCardVersion | None = None) -> dict:
    return {
        "id": str(card.id),
        "org_id": str(card.org_id) if card.org_id else None,
        "is_platform": card.org_id is None or card.org_id == get_settings().platform_org_id,
        "name": card.name,
        "description": card.description,
        "icon": card.icon,
        "sort_order": card.sort_order,
        "is_active": card.is_active,
        "delegatable": card.delegatable,
        "created_at": card.created_at,
        "active_version": _version_summary(version),
    }


@router.get("/agent-cards")
async def list_agent_cards_admin(session: OrgSession, org_id: UUID | None = None) -> list[dict]:
    stmt = (
        select(AgentCard, AgentCardVersion)
        .outerjoin(
            AgentCardVersion,
            (AgentCardVersion.card_id == AgentCard.id) & AgentCardVersion.is_active,
        )
        .order_by(AgentCard.sort_order, AgentCard.created_at)
    )
    if org_id is not None:
        stmt = stmt.where(AgentCard.org_id == org_id)
    rows = (await session.execute(stmt)).all()
    return [_card_payload(card, version) for card, version in rows]


@router.post("/agent-cards", status_code=status.HTTP_201_CREATED)
async def create_agent_card(body: AgentCardCreate, session: OrgSession) -> dict:
    card = AgentCard(
        **body.model_dump(),
        # deprecated compatibility columns; no runtime reads are made from them.
        system_prompt="",
        model_task=DEFAULT_AGENT_TASK,
        max_turns=8,
        timeout_seconds=300,
        tools=[],
    )
    session.add(card)
    await session.commit()
    await session.refresh(card)
    return _card_payload(card)


async def _agent_card_or_404(session: AsyncSession, card_id: UUID) -> AgentCard:
    card = (
        await session.execute(select(AgentCard).where(AgentCard.id == card_id))
    ).scalar_one_or_none()
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent 卡不存在")
    return card


@router.patch("/agent-cards/{card_id}")
async def update_agent_card(card_id: UUID, body: AgentCardUpdate, session: OrgSession) -> dict:
    card = await _agent_card_or_404(session, card_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(card, key, value)
    session.add(card)
    await session.commit()
    await session.refresh(card)
    await cache.delete(agent_card_cache_key(card.id))
    return _card_payload(card)


@router.get("/agent-cards/{card_id}/versions")
async def list_agent_card_versions(card_id: UUID, session: OrgSession) -> list[AgentCardVersion]:
    await _agent_card_or_404(session, card_id)
    return (
        (
            await session.execute(
                select(AgentCardVersion)
                .where(AgentCardVersion.card_id == card_id)
                .order_by(AgentCardVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/agent-cards/{card_id}/versions",
    response_model=AgentCardVersion,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_card_version(
    card_id: UUID, body: AgentVersionCreate, session: OrgSession, ctx: Ctx
) -> AgentCardVersion:
    await _agent_card_or_404(session, card_id)
    unknown = sorted(set(body.tools) - set(default_registry.names()))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"未注册工具:{', '.join(unknown)}",
        )
    next_version = (
        await session.execute(
            select(func.coalesce(func.max(AgentCardVersion.version), 0)).where(
                AgentCardVersion.card_id == card_id
            )
        )
    ).scalar_one() + 1
    version = AgentCardVersion(
        card_id=card_id,
        version=next_version,
        **body.model_dump(),
        is_active=False,
        status="draft",
        created_by=ctx.user_id,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


@router.post("/agent-cards/{card_id}/versions/{version_id}/activate")
async def activate_agent_card_version(card_id: UUID, version_id: UUID, session: OrgSession) -> dict:
    await _agent_card_or_404(session, card_id)
    versions = (
        (
            await session.execute(
                select(AgentCardVersion)
                .where(AgentCardVersion.card_id == card_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    target = next((version for version in versions if version.id == version_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent 版本不存在")
    for version in versions:
        if version.is_active:
            version.is_active = False
            version.status = "archived"
        if version.id == version_id:
            version.is_active = True
            version.status = "active"
        session.add(version)
    await session.commit()
    await session.refresh(target)
    await cache.delete(agent_card_cache_key(card_id))
    return _version_summary(target) or {}


@router.delete("/agent-cards/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_card(card_id: UUID, session: OrgSession) -> None:
    """硬删除 agent 卡(版本随外键级联)。有会话引用时 409,请改为停用。"""
    card = await _agent_card_or_404(session, card_id)
    in_use = (
        await session.execute(
            select(func.count())
            .select_from(ChatSession)
            .where(ChatSession.agent_card_id == card_id)
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent_in_use")
    try:
        await session.delete(card)
        await session.commit()
    except IntegrityError as exc:
        # RLS 下看不到其它 org 的会话引用,外键兜底
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "agent_in_use") from exc
    await cache.delete(agent_card_cache_key(card_id))


@router.get("/agent-tools")
async def list_agent_tools() -> list[dict]:
    return [
        {
            "name": tool.name,
            "label": tool.label or tool.name,
            "category": tool.category,
            "description": tool.description,
            "side_effect": getattr(tool, "side_effect", "unknown"),
            "confirm": tool.confirm,
        }
        for tool in sorted(default_registry, key=lambda item: (item.category, item.name))
    ]


# ---------- Agent 运行日志(运行时上下文快照,audit-only) ----------
# 快照由 agent/runtime_snapshot.py 在每个 run 首轮 emit 并随 chat_events
# 落库;此处只读展示供排障,绝不把载荷回灌到任何后续上下文(不是记忆源)。
# 平台管理员跨租户读取依赖 chat_events 的 platform_read RLS 策略(与 llm_traces 同构)。


class AgentRunSummary(BaseModel):
    session_id: UUID
    run_id: UUID
    started_at: datetime | None
    last_event_at: datetime | None
    event_count: int
    has_snapshot: bool


AgentRunStatus = Literal["success", "failed", "waiting", "running"]


class AgentRunItem(AgentRunSummary):
    status: AgentRunStatus


class AgentRunStats(BaseModel):
    total: int
    success: int
    failed: int
    waiting: int
    running: int
    snapshots: int
    average_events: float


class AgentRunTrendPoint(BaseModel):
    day: date
    success: int
    failed: int
    waiting: int
    running: int


class AgentRunPage(BaseModel):
    items: list[AgentRunItem]
    total: int
    page: int
    page_size: int
    stats: AgentRunStats
    trend: list[AgentRunTrendPoint]


def _agent_run_aggregate():
    event_type = ChatEvent.payload["type"].astext
    done_reason = ChatEvent.payload["reason"].astext
    return (
        select(
            ChatEvent.session_id.label("session_id"),
            ChatEvent.run_id.label("run_id"),
            func.min(ChatEvent.created_at).label("started_at"),
            func.max(ChatEvent.created_at).label("last_event_at"),
            func.count().label("event_count"),
            func.bool_or(event_type == CONTEXT_SNAPSHOT_EVENT_TYPE).label(
                "has_snapshot"
            ),
            func.bool_or(
                (event_type == "error")
                | (
                    (event_type == "turn.done")
                    & done_reason.in_(("error", "cancelled", "max_turns"))
                )
            ).label("has_failed"),
            func.bool_or(
                (event_type == "turn.done")
                & done_reason.in_(("confirm", "ask_user"))
            ).label("has_waiting"),
            func.bool_or(
                (event_type == "turn.done") & (done_reason == "end_turn")
            ).label("has_succeeded"),
        )
        .group_by(ChatEvent.session_id, ChatEvent.run_id)
        .subquery()
    )


def _agent_run_status(aggregate):
    return case(
        (aggregate.c.has_failed.is_(True), "failed"),
        (aggregate.c.has_waiting.is_(True), "waiting"),
        (aggregate.c.has_succeeded.is_(True), "success"),
        else_="running",
    )


@router.get("/agent-runs", response_model=AgentRunPage)
async def list_agent_runs(
    org_session: OrgSession,
    page: int = 1,
    page_size: int = 20,
    days: int = 30,
    run_status: AgentRunStatus | None = None,
    has_snapshot: bool | None = None,
    query: str = "",
) -> AgentRunPage:
    """分页查询 agent runs,并返回同一筛选范围内的状态概览与按日趋势。"""
    page = max(page, 1)
    page_size = max(10, min(page_size, 100))
    if days not in (7, 30, 90):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "days 只支持 7、30、90")
    aggregate = _agent_run_aggregate()
    status_expr = _agent_run_status(aggregate)

    base_filters = [
        aggregate.c.last_event_at >= datetime.now(UTC) - timedelta(days=days)
    ]
    if has_snapshot is not None:
        base_filters.append(aggregate.c.has_snapshot.is_(has_snapshot))
    query = query.strip()
    if query:
        pattern = f"{query[:36]}%"
        base_filters.append(
            cast(aggregate.c.run_id, String).ilike(pattern)
            | cast(aggregate.c.session_id, String).ilike(pattern)
        )

    list_filters = [*base_filters]
    if run_status is not None:
        list_filters.append(status_expr == run_status)

    total = int(
        (
            await org_session.execute(
                select(func.count()).select_from(aggregate).where(*list_filters)
            )
        ).scalar_one()
    )
    rows = (
        await org_session.execute(
            select(
                aggregate.c.session_id,
                aggregate.c.run_id,
                aggregate.c.started_at,
                aggregate.c.last_event_at,
                aggregate.c.event_count,
                aggregate.c.has_snapshot,
                status_expr.label("status"),
            )
            .where(*list_filters)
            .order_by(aggregate.c.last_event_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    stats_row = (
        await org_session.execute(
            select(
                func.count(),
                func.count().filter(status_expr == "success"),
                func.count().filter(status_expr == "failed"),
                func.count().filter(status_expr == "waiting"),
                func.count().filter(status_expr == "running"),
                func.count().filter(aggregate.c.has_snapshot.is_(True)),
                func.coalesce(func.avg(aggregate.c.event_count), 0),
            )
            .select_from(aggregate)
            .where(*base_filters)
        )
    ).one()
    stats = AgentRunStats(
        total=stats_row[0],
        success=stats_row[1],
        failed=stats_row[2],
        waiting=stats_row[3],
        running=stats_row[4],
        snapshots=stats_row[5],
        average_events=float(stats_row[6]),
    )

    trend_day = func.date(aggregate.c.last_event_at).label("day")
    trend_rows = (
        await org_session.execute(
            select(
                trend_day,
                func.count().filter(status_expr == "success"),
                func.count().filter(status_expr == "failed"),
                func.count().filter(status_expr == "waiting"),
                func.count().filter(status_expr == "running"),
            )
            .where(*base_filters)
            .group_by(trend_day)
            .order_by(trend_day)
        )
    ).all()

    return AgentRunPage(
        items=[
            AgentRunItem(
                session_id=row[0],
                run_id=row[1],
                started_at=row[2],
                last_event_at=row[3],
                event_count=row[4],
                has_snapshot=bool(row[5]),
                status=row[6],
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        stats=stats,
        trend=[
            AgentRunTrendPoint(
                day=row[0],
                success=row[1],
                failed=row[2],
                waiting=row[3],
                running=row[4],
            )
            for row in trend_rows
        ],
    )


@router.get("/agent-runs/recent", response_model=list[AgentRunSummary])
async def recent_agent_runs(
    org_session: OrgSession,
    limit: int = 20,
) -> list[AgentRunSummary]:
    """最近的 agent run 列表(按 chat_events 聚合),供运行日志页挑选 run。"""
    limit = max(1, min(limit, 100))
    rows = (
        await org_session.execute(
            select(
                ChatEvent.session_id,
                ChatEvent.run_id,
                func.min(ChatEvent.created_at),
                func.max(ChatEvent.created_at),
                func.count(),
                func.bool_or(
                    ChatEvent.payload["type"].astext == CONTEXT_SNAPSHOT_EVENT_TYPE
                ),
            )
            .group_by(ChatEvent.session_id, ChatEvent.run_id)
            .order_by(func.max(ChatEvent.created_at).desc())
            .limit(limit)
        )
    ).all()
    return [
        AgentRunSummary(
            session_id=row[0],
            run_id=row[1],
            started_at=row[2],
            last_event_at=row[3],
            event_count=row[4],
            has_snapshot=bool(row[5]),
        )
        for row in rows
    ]


@router.get("/agent-runs/{run_id}/context-snapshot")
async def agent_run_context_snapshot(
    run_id: UUID,
    org_session: OrgSession,
) -> dict:
    """查询某 run 的运行时上下文快照;无快照(旧 run / 续跑轮)如实 404。"""
    row = (
        await org_session.execute(
            select(ChatEvent)
            .where(
                ChatEvent.run_id == run_id,
                ChatEvent.payload["type"].astext == CONTEXT_SNAPSHOT_EVENT_TYPE,
            )
            # 理论上每 run 只有一条;取 seq 最大的兜底,口径确定
            .order_by(ChatEvent.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该 run 没有上下文快照")
    return row.payload


# ---------- 技能包 ----------


@router.get("/skills")
async def list_agent_skills() -> list[dict]:
    return [
        {
            "slug": skill.slug,
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "category": skill.category,
            "files": list_skill_files(skill.slug),
        }
        for skill in list_skills()
    ]


class SkillWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)


@router.get("/skills/{slug}")
async def get_agent_skill(slug: str) -> dict:
    try:
        content = read_skill_file(slug, "SKILL.md")
        files = list_skill_files(slug)
    except SkillError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"slug": slug, "content": content, "files": files}


@router.put("/skills/{slug}")
async def put_agent_skill(slug: str, body: SkillWrite) -> dict:
    """新建或更新技能包的 SKILL.md(目录不存在则创建)。"""
    try:
        write_skill(slug, body.content)
    except SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"slug": slug}


@router.delete("/skills/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_skill(slug: str, session: OrgSession) -> None:
    """删除技能包目录。被 active agent 版本绑定时 409。

    RLS 下只能看到平台与本 org 的卡;其它 org 私有卡若绑定了该 skill,
    运行期 skills_list/skill_view 会优雅降级为"未绑定/不存在",不会报错。
    """
    bound_cards = (
        (
            await session.execute(
                select(AgentCard.name)
                .join(AgentCardVersion, AgentCardVersion.card_id == AgentCard.id)
                .where(
                    AgentCardVersion.status == "active",
                    AgentCardVersion.skills.contains([slug]),
                )
            )
        )
        .scalars()
        .all()
    )
    if bound_cards:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": "skill_in_use", "cards": list(bound_cards)},
        )
    try:
        delete_skill(slug)
    except SkillError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------- MCP servers ----------


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: UUID | None = None
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    transport: str = Field(pattern=r"^(streamable_http|sse|stdio)$")
    # http/sse 必填;stdio 留空
    endpoint_url: str = Field(default="", max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    # stdio 传输:command 必填,args 逐项,env 附加到子进程环境
    command: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    connect_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    call_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    enabled_tools: list[str] | None = None
    enabled: bool = True


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$"
    )
    transport: str | None = Field(default=None, pattern=r"^(streamable_http|sse|stdio)$")
    endpoint_url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] | None = None
    command: str | None = Field(default=None, max_length=500)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    connect_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    call_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    enabled_tools: list[str] | None = None
    enabled: bool | None = None


def _validate_mcp_shape(transport: str, endpoint_url: str, command: str | None) -> None:
    """按传输协议校验必填项:http/sse 要 endpoint_url,stdio 要 command。"""
    if transport == "stdio":
        if not (command or "").strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "stdio 传输必须配置 command")
    elif not endpoint_url.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "该传输协议必须配置 endpoint_url"
        )


def _require_platform_for_stdio(ctx: OrgContext, transport: str) -> None:
    """stdio 在宿主机拉起任意子进程,只允许平台管理员配置。"""
    if transport == "stdio" and ctx.role != Role.PLATFORM_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "stdio 传输仅平台管理员可配置")


class McpToolResponse(BaseModel):
    name: str
    description: str
    schema_: dict = Field(alias="schema", serialization_alias="schema")


class McpServerStatusResponse(BaseModel):
    server_id: UUID
    status: str
    latency_ms: int | None
    checked_at: datetime | None
    error: str | None
    tools: list[McpToolResponse]


def _serialize_mcp_status(result: dict) -> dict:
    return {
        **result,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "schema": tool.schema,
            }
            for tool in result["tools"]
        ],
    }


async def _mcp_or_404(session: AsyncSession, server_id: UUID, ctx: OrgContext) -> McpServer:
    server = (
        await session.execute(select(McpServer).where(McpServer.id == server_id))
    ).scalar_one_or_none()
    if server is None or (ctx.role != Role.PLATFORM_ADMIN and server.org_id != ctx.org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server 不存在")
    return server


@mcp_router.get("/mcp-servers", response_model=list[McpServer])
async def list_mcp_servers(session: OrgSession, ctx: Ctx) -> list[McpServer]:
    stmt = select(McpServer).order_by(McpServer.name)
    if ctx.role != Role.PLATFORM_ADMIN:
        stmt = stmt.where(McpServer.org_id == ctx.org_id)
    return (await session.execute(stmt)).scalars().all()


@mcp_router.get("/mcp-servers/status", response_model=list[McpServerStatusResponse])
async def list_mcp_server_statuses(
    session: OrgSession, ctx: Ctx, refresh: bool = True
) -> list[dict]:
    stmt = select(McpServer).order_by(McpServer.name)
    if ctx.role != Role.PLATFORM_ADMIN:
        stmt = stmt.where(McpServer.org_id == ctx.org_id)
    servers = (await session.execute(stmt)).scalars().all()
    results = await asyncio.gather(
        *(mcp_client_manager.status(server, refresh=refresh) for server in servers)
    )
    return [_serialize_mcp_status(result) for result in results]


@mcp_router.get("/mcp-servers/{server_id}/status", response_model=McpServerStatusResponse)
async def get_mcp_server_status(server_id: UUID, session: OrgSession, ctx: Ctx) -> dict:
    server = await _mcp_or_404(session, server_id, ctx)
    result = await mcp_client_manager.status(server, refresh=True)
    return _serialize_mcp_status(result)


@mcp_router.post("/mcp-servers", response_model=McpServer, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(body: McpServerCreate, session: OrgSession, ctx: Ctx) -> McpServer:
    _require_platform_for_stdio(ctx, body.transport)
    _validate_mcp_shape(body.transport, body.endpoint_url, body.command)
    data = body.model_dump()
    if ctx.role != Role.PLATFORM_ADMIN:
        data["org_id"] = ctx.org_id
    server = McpServer(**data)
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return server


@mcp_router.patch("/mcp-servers/{server_id}", response_model=McpServer)
async def update_mcp_server(
    server_id: UUID, body: McpServerUpdate, session: OrgSession, ctx: Ctx
) -> McpServer:
    server = await _mcp_or_404(session, server_id, ctx)
    patch = body.model_dump(exclude_unset=True)
    transport = patch.get("transport", server.transport)
    _require_platform_for_stdio(ctx, transport)
    _validate_mcp_shape(
        transport,
        patch.get("endpoint_url", server.endpoint_url) or "",
        patch.get("command", server.command),
    )
    for key, value in patch.items():
        setattr(server, key, value)
    session.add(server)
    await session.commit()
    await session.refresh(server)
    await mcp_client_manager.invalidate(server.id)
    return server


@mcp_router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(server_id: UUID, session: OrgSession, ctx: Ctx) -> None:
    server = await _mcp_or_404(session, server_id, ctx)
    await session.delete(server)
    await session.commit()
    await mcp_client_manager.invalidate(server_id)


@mcp_router.post("/mcp-servers/{server_id}/test")
async def test_mcp_server(server_id: UUID, session: OrgSession, ctx: Ctx) -> dict:
    server = await _mcp_or_404(session, server_id, ctx)
    try:
        tools = await mcp_client_manager.list_tools(server, force_refresh=True)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {
        "ok": True,
        "tools": [
            {"name": tool.name, "description": tool.description, "schema": tool.schema}
            for tool in tools
        ],
    }
