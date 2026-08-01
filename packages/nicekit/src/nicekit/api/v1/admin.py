"""平台管理 API(仅 platform_admin;MCP 另开 org_admin 可用的子路由)。

搬自 TF backend/app/api/v1/admin.py。覆盖面:
运维诊断与手动探测 / 租户管理 / 模型路由 / 提供商实例(双协议多实例)/
Prompt Registry 与目录登记 / Prompt 资源(只读)与组装预览 / Agent 卡与版本 /
工具目录 / Agent 运行日志 / 技能包 / MCP servers / 外部服务配置 / 用量 /
模型计费 / LLM 请求日志。

SDK 化改造:
- ``SERVICE_CONFIG_NAMES`` 删掉 TF 的 ``"ota"``(旅游比价属业务);
- 落库前统一经 ``core.secretbox`` 加密:提供商 ``api_key``、服务配置里的
  密钥形字段、MCP 的 ``headers``/``env``;读出面一律掩码;
- 工具目录从模块级 TOOLS 改查注入的 ``ToolRegistry``;
- 系统能力槽位从 ``llm.capability_routes`` 的**注册表**派生,宿主追加的槽位
  自动出现在模型路由目录里。

两处 KB 触点(MIGRATION-PLAN §5.9 A12):
①``_prepare_embedding_route_campaign()`` —— ``kb.embedding`` 路由换主模型时
自动建重嵌 campaign 并经 ``runtime.dispatch`` 派发;②``_resolved_embedding_config()``
—— service-configs 的 embedding 分支。

访问控制在路由层(require_role):organizations/users/prompts/model_routes 均为
无 RLS 的平台配置表。
"""

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from anthropic import AsyncAnthropic
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
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
from nicekit.capabilities.imagegen import service as imagegen_service
from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.core.db import get_session, get_session_factory
from nicekit.core.secretbox import get_secret_box
from nicekit.core.security import hash_password
from nicekit.domain.model_catalog import (
    ManualModelCapabilities,
    ModelCapability,
    ModelCatalogCapabilityFilter,
    ProviderModelCatalogEntry,
    ProviderModelMetadata,
)
from nicekit.kb.embedding import (
    EmbeddingFingerprint,
    EmbeddingUnavailableError,
    normalize_embedding_config,
)
from nicekit.kb.embedding_reindex import (
    EmbeddingCampaignConflictError,
    EmbeddingDimensionMismatchError,
    create_embedding_campaign,
)
from nicekit.llm import prompt_catalog
from nicekit.llm.capability_routes import EMBEDDING_ROUTE_TASK, capability_slots
from nicekit.llm.connectivity import check_instance, check_model
from nicekit.llm.model_catalog import (
    build_model_catalog,
    catalog_entry,
    merge_imported_metadata,
    metadata_from_manual,
    metadata_from_provider_model,
    metadata_from_registry,
    normalize_model_ids,
    provider_metadata_for_read,
    provider_model_catalog_entry,
    reconcile_provider_metadata,
)
from nicekit.llm.registry import prompt_cache_key, route_cache_key
from nicekit.llm.runtime_config import load_runtime_overrides
from nicekit.llm.service import LlmBudgetExceededError, env_provider_credentials
from nicekit.models.chat import AgentCard, AgentCardVersion, ChatEvent, ChatSession
from nicekit.models.kb import EMBEDDING_DIM
from nicekit.models.llm import (
    LlmTrace,
    ModelPrice,
    ModelRoute,
    Prompt,
    PromptCatalogEntry,
)
from nicekit.models.llm_provider import LlmProvider
from nicekit.models.mcp import McpServer
from nicekit.models.service_config import ServiceConfig
from nicekit.models.tenancy import Membership, Organization, Role, UsageDaily, User
from nicekit.runtime.dispatch import dispatch_embedding_campaign

logger = logging.getLogger(__name__)

#: 密钥读出面统一掩码(落库是 SecretBox 密文,回传密文同样无意义)
SERVICE_SECRET_MASK = "********"

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


def _encrypt_mcp_secrets(values: dict[str, str] | None) -> dict[str, str]:
    """MCP ``headers``/``env`` 落库前逐值加密(§5.1;mcp_manager 连接前解密)。"""
    box = get_secret_box()
    return {str(k): box.encrypt(str(v)) for k, v in (values or {}).items()}


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
    data["headers"] = _encrypt_mcp_secrets(data.get("headers"))
    data["env"] = _encrypt_mcp_secrets(data.get("env"))
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
    for field in ("headers", "env"):
        if field in patch:
            patch[field] = _encrypt_mcp_secrets(patch[field])
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


# ---------- 运维诊断 ----------


@router.get("/operations/diagnostics")
async def get_operations_diagnostics() -> dict:
    """Deep operator view; provider inference remains scheduled, never per request."""
    from nicekit.operations.diagnostics import collect_operator_diagnostics

    return await collect_operator_diagnostics(get_session_factory())


@router.post("/operations/probe")
async def run_operations_probe() -> dict:
    """Run provider and MCP probes now and return the refreshed diagnostics.

    The scheduled cycle stays the source of truth for ``ProviderProbeStatus``;
    this endpoint runs the same code path so an operator does not have to wait
    out the interval after fixing a credential. MCP connections are refreshed
    after the provider cycle. This makes real upstream calls, hence POST rather
    than a cache-warming GET.
    """
    from nicekit.operations.diagnostics import collect_operator_diagnostics
    from nicekit.operations.probes import run_scheduled_provider_probes

    factory = get_session_factory()
    try:
        await run_scheduled_provider_probes(factory)
    except Exception as exc:  # noqa: BLE001 - never surface upstream internals
        # A probe failure is data, not an API error: the per-capability status
        # is already persisted, so still return the refreshed snapshot.
        logger.warning("手动能力检测未完整执行:%s", type(exc).__name__)
    return await collect_operator_diagnostics(factory, refresh_mcp=True)


# ---------- 租户管理 ----------


class OrgCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    # 可选:同时开通首个 org_admin(已注册用户只加成员关系,忽略密码/姓名)
    admin_email: EmailStr | None = None
    admin_password: str | None = Field(default=None, min_length=8)
    admin_full_name: str | None = None


class OrgUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    member_count: int
    created_at: datetime | None


async def _org_out(session: AsyncSession, org: Organization) -> OrgOut:
    count = (
        await session.execute(
            select(func.count()).select_from(Membership).where(Membership.org_id == org.id)
        )
    ).scalar_one()
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        is_active=org.is_active,
        member_count=count,
        created_at=org.created_at,
    )


@router.get("/orgs", response_model=list[OrgOut])
async def list_orgs(session: Session) -> list[OrgOut]:
    orgs = (
        (await session.execute(select(Organization).order_by(Organization.created_at)))
        .scalars()
        .all()
    )
    counts = dict(
        (
            await session.execute(
                select(Membership.org_id, func.count()).group_by(Membership.org_id)
            )
        ).all()
    )
    return [
        OrgOut(
            id=o.id,
            name=o.name,
            slug=o.slug,
            is_active=o.is_active,
            member_count=counts.get(o.id, 0),
            created_at=o.created_at,
        )
        for o in orgs
    ]


@router.post("/orgs", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(body: OrgCreate, session: Session) -> OrgOut:
    exists = (
        await session.execute(select(Organization).where(Organization.slug == body.slug))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"slug {body.slug!r} 已被占用")

    org = Organization(name=body.name, slug=body.slug)
    session.add(org)
    await session.flush()

    if body.admin_email is not None:
        user = (
            await session.execute(select(User).where(User.email == body.admin_email))
        ).scalar_one_or_none()
        if user is None:
            if not body.admin_password:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "新用户必须提供 admin_password(至少 8 位)",
                )
            user = User(
                email=body.admin_email,
                password_hash=hash_password(body.admin_password),
                full_name=body.admin_full_name or body.admin_email.split("@")[0],
            )
            session.add(user)
            await session.flush()
        session.add(Membership(org_id=org.id, user_id=user.id, role=Role.ORG_ADMIN))

    await session.commit()
    await session.refresh(org)
    return await _org_out(session, org)


@router.patch("/orgs/{org_id}", response_model=OrgOut)
async def update_org(org_id: UUID, body: OrgUpdate, session: Session) -> OrgOut:
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    if body.name is not None:
        org.name = body.name
    if body.is_active is not None:
        org.is_active = body.is_active
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return await _org_out(session, org)


# ---------- 模型路由 ----------


class RouteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: UUID | None = None  # None = 平台默认
    task: str = Field(min_length=1, max_length=100)
    primary_provider: str = Field(min_length=1, max_length=50)
    primary_model: str = Field(min_length=1, max_length=300)
    fallback_chain: list[dict] | None = None
    max_tokens: int = Field(default=8192, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    is_active: bool = True

    @field_validator("task")
    @classmethod
    def normalize_task(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task 不能为空")
        return value


class RouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_provider: str | None = Field(default=None, min_length=1, max_length=50)
    primary_model: str | None = Field(default=None, min_length=1, max_length=300)
    fallback_chain: list[dict] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    is_active: bool | None = None


class RouteBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[str] = Field(min_length=1, max_length=100)
    org_id: UUID | None = None
    primary_provider: str = Field(min_length=1, max_length=50)
    primary_model: str = Field(min_length=1, max_length=300)
    fallback_chain: list[dict] | None = None
    max_tokens: int = Field(default=8192, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0)
    is_active: bool = True

    @field_validator("tasks")
    @classmethod
    def normalize_tasks(cls, values: list[str]) -> list[str]:
        tasks = [value.strip() for value in values]
        if any(not task for task in tasks):
            raise ValueError("tasks 不能包含空任务")
        if len(set(tasks)) != len(tasks):
            raise ValueError("tasks 不能重复")
        return tasks


class RouteTaskCatalogOut(BaseModel):
    task: str
    label: str
    description: str
    category: str
    is_system: bool


def _validate_chain(chain: list[dict] | None) -> None:
    for hop in chain or []:
        if set(hop.keys()) != {"provider", "model"} or not all(
            isinstance(v, str) and v for v in hop.values()
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                'fallback_chain 每项必须是 {"provider": str, "model": str}',
            )


# 系统能力槽位的展示文案。槽位本身来自 llm.capability_routes 注册表(宿主可追加),
# 这里只为四个内置槽位提供中文名/说明;未登记文案的槽位回退用 task 名。
_SYSTEM_ROUTE_LABELS: dict[str, str] = {
    "kb.embedding": "嵌入",
    "kb.search.rerank": "重排",
    "kb.image.caption": "视觉",
    "llm.default": "对话",
}

_SYSTEM_ROUTE_DESCRIPTIONS: dict[str, str] = {
    "kb.embedding": "知识库向量化;换主模型会先启动重嵌",
    "kb.search.rerank": "检索结果重排;按顺序即时降级",
    "kb.image.caption": "图片描述与 Agent 图像核验",
    "llm.default": "普通生成任务没有专属路由时的统一基线",
}


def _system_route_label(task: str) -> str:
    label = _SYSTEM_ROUTE_LABELS.get(task)
    return f"{label}模型" if label else task


_ROUTE_ONLY_TASK_CATALOG: dict[str, tuple[str, str, str]] = {
    DEFAULT_AGENT_TASK: (
        "Agent 工作台",
        "默认 Agent 卡与已发布专员共用的长任务模型路由",
        "agent",
    ),
    "kb.answer": (
        "知识库问答",
        "知识库问答生成链路;可独立控制回答模型、时延和降级顺序",
        "kb",
    ),
}


async def _model_route_task_catalog(session: AsyncSession) -> list[RouteTaskCatalogOut]:
    """汇总受治理的可路由任务;越靠后的来源展示优先级越高。"""
    prompt_tasks = (await session.execute(select(Prompt.task).distinct())).scalars().all()
    custom_entries = (await session.execute(select(PromptCatalogEntry))).scalars().all()
    cards = (
        (
            await session.execute(
                select(AgentCard)
                .where(AgentCard.is_active.is_(True))
                .order_by(AgentCard.name, AgentCard.id)
            )
        )
        .scalars()
        .all()
    )
    active_versions = (
        (
            await session.execute(
                select(AgentCardVersion).where(AgentCardVersion.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    version_by_card = {version.card_id: version for version in active_versions}
    route_tasks = (await session.execute(select(ModelRoute.task).distinct())).scalars().all()

    items: dict[str, RouteTaskCatalogOut] = {}

    def put(
        task: str,
        *,
        label: str,
        description: str,
        category: str,
        is_system: bool = False,
    ) -> None:
        items[task] = RouteTaskCatalogOut(
            task=task,
            label=label,
            description=description,
            category=category,
            is_system=is_system,
        )

    # 既有路由和未登记 Prompt 仅提供标识回退,避免历史任务从目录消失。
    for task in sorted(set(route_tasks) | set(prompt_tasks)):
        put(task, label=task, description="", category=prompt_catalog.category_of(task))

    # Agent 当前生效版本提供面向运营者的名称和说明。
    seen_agent_tasks: set[str] = set()
    for card in cards:
        version = version_by_card.get(card.id)
        task = version.model_task if version is not None else card.model_task
        if task in seen_agent_tasks:
            continue
        seen_agent_tasks.add(task)
        put(
            task,
            label=card.name,
            description=card.description or "Agent 运行时使用的模型任务",
            category="agent",
        )

    # 在线登记和源码内置目录的描述比实体名称更精确。
    for entry in custom_entries:
        put(
            entry.task,
            label=entry.name_zh,
            description=entry.description,
            category=prompt_catalog.category_of(entry.task),
        )
    for task, entry in prompt_catalog.BUILTIN_PROMPT_CATALOG.items():
        put(
            task,
            label=entry["name_zh"],
            description=entry["description"],
            category=entry["category"],
        )
    for task, (label, description, category) in _ROUTE_ONLY_TASK_CATALOG.items():
        put(task, label=label, description=description, category=category)

    # 系统能力槽位拥有最高展示优先级,并由前端单独管理。
    for task in capability_slots():
        put(
            task,
            label=_system_route_label(task),
            description=_SYSTEM_ROUTE_DESCRIPTIONS.get(task, ""),
            category="system",
            is_system=True,
        )

    return sorted(
        items.values(),
        key=lambda item: (
            not item.is_system,
            item.category,
            item.label.casefold(),
            item.task,
        ),
    )


async def _ensure_route_scope_available(
    session: AsyncSession, *, tasks: list[str], org_id: UUID | None
) -> None:
    statement = select(ModelRoute).where(ModelRoute.task.in_(tasks))
    statement = (
        statement.where(ModelRoute.org_id.is_(None))
        if org_id is None
        else statement.where(ModelRoute.org_id == org_id)
    )
    existing = (await session.execute(statement)).scalars().all()
    if existing:
        conflicts = "、".join(sorted({route.task for route in existing}))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"所选作用域已配置任务路由:{conflicts}",
        )


async def _ensure_route_org_exists(session: AsyncSession, org_id: UUID | None) -> None:
    if org_id is None:
        return
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org_id 对应组织不存在")


async def _validate_route_targets(
    session: AsyncSession,
    *,
    task: str,
    org_id: UUID | None,
    primary_provider: str,
    primary_model: str,
    fallback_chain: list[dict] | None,
) -> None:
    hops = [
        {"provider": primary_provider, "model": primary_model},
        *(fallback_chain or []),
    ]
    seen: set[tuple[str, str]] = set()
    for hop in hops:
        key = (hop["provider"].strip(), hop["model"].strip())
        if key in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"路由链节点不可重复:{key[0]}/{key[1]}",
            )
        seen.add(key)

    slot = capability_slots().get(task)
    if slot is None:
        return
    label = _system_route_label(task)
    if org_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{label}是平台级系统路由,不支持组织级覆盖",
        )
    providers = {
        row.name: row for row in (await session.execute(select(LlmProvider))).scalars().all()
    }
    for hop in hops:
        provider = providers.get(hop["provider"])
        if provider is None or not provider.enabled:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"系统路由提供商不可用:{hop['provider']}",
            )
        if slot.openai_only and provider.protocol != "openai":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{label}路由仅支持 OpenAI 兼容协议:{provider.name}",
            )
        metadata = provider_metadata_for_read(provider).get(hop["model"])
        if metadata is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"模型不在提供商目录中:{provider.name}/{hop['model']}",
            )
        missing = [
            capability
            for capability in slot.capabilities
            if capability not in metadata.capabilities
        ]
        if missing:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"模型未标注 {'+'.join(missing)} 能力:{provider.name}/{hop['model']}",
            )


async def _prepare_embedding_route_campaign(
    session: AsyncSession,
    *,
    task: str,
    provider: str,
    model: str,
):
    """A12 触点①:``kb.embedding`` 主模型变更 → 建重嵌 campaign(指纹相同则跳过)。

    campaign 在路由落库**之前**创建:建不出来(维度不匹配 / 探测失败 / 预算超限)
    就整个请求失败,绝不出现"路由已切、向量还是旧模型"的半途状态。
    """
    if task != EMBEDDING_ROUTE_TASK:
        return None
    row = (
        await session.execute(select(ServiceConfig).where(ServiceConfig.name == "embedding"))
    ).scalar_one_or_none()
    source = _resolved_embedding_config(row.payload if row is not None else None)
    target = {"provider": provider, "model": model, "dim": source["dim"]}
    if EmbeddingFingerprint.from_config(source) == EmbeddingFingerprint.from_config(target):
        return None
    try:
        return await create_embedding_campaign(
            get_session_factory(),
            target,
            dual_read_seconds=get_settings().embedding_dual_read_seconds,
        )
    except EmbeddingDimensionMismatchError as exc:
        detail = exc.as_dict()
        detail["runbook"] = EMBEDDING_OFFLINE_RUNBOOK
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from exc
    except EmbeddingUnavailableError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "embedding_probe_failed", "message": str(exc)},
        ) from exc
    except LlmBudgetExceededError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"code": "embedding_budget_exceeded", "message": str(exc)},
        ) from exc
    except EmbeddingCampaignConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# ---------- 模型提供商实例(多实例双协议混用) ----------


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    protocol: str = Field(pattern=r"^(openai|anthropic)$")
    api_key: str = Field(default="", max_length=500)
    base_url: str = Field(default="", max_length=2000)
    models: list[str] = Field(default_factory=list)
    model_metadata: dict[str, ManualModelCapabilities] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("models")
    @classmethod
    def _normalize_models(cls, values: list[str]) -> list[str]:
        return normalize_model_ids(values)

    @model_validator(mode="after")
    def _metadata_models_exist(self) -> "ProviderCreate":
        unknown = set(self.model_metadata) - set(self.models)
        if unknown:
            raise ValueError("model_metadata 只能引用 models 中的模型")
        return self


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str | None = Field(default=None, pattern=r"^(openai|anthropic)$")
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=2000)
    models: list[str] | None = None
    model_metadata: dict[str, ManualModelCapabilities] | None = None
    enabled: bool | None = None

    @field_validator("models")
    @classmethod
    def _normalize_models(cls, values: list[str] | None) -> list[str] | None:
        return normalize_model_ids(values) if values is not None else None


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    protocol: str
    api_key: str  # 掩码值,永不回传明文/密文
    has_api_key: bool
    base_url: str
    models: list[str]
    model_metadata: dict[str, ProviderModelMetadata]
    enabled: bool
    is_builtin: bool
    created_at: datetime | None
    updated_at: datetime | None


class ProviderModelCapabilityUpdate(ManualModelCapabilities):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=300)
    mode: Literal["manual", "auto"] = "manual"

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        return normalize_model_ids([value])[0]

    @model_validator(mode="after")
    def _auto_has_no_manual_values(self) -> "ProviderModelCapabilityUpdate":
        if self.mode == "auto" and (self.capabilities or self.input_modalities is not None):
            raise ValueError("auto 模式不可同时提交手工能力")
        return self


class ProviderModelImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(min_length=1, max_length=2000)

    @field_validator("models")
    @classmethod
    def _normalize_models(cls, values: list[str]) -> list[str]:
        return normalize_model_ids(values)


class ProviderModelConnectivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=300)

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        return normalize_model_ids([value])[0]


class ProviderConnectivityRead(BaseModel):
    """Connectivity outcome. Never carries raw upstream bodies or credentials."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    scope: Literal["instance", "model"]
    ok: bool
    latency_ms: float | None
    error_code: str | None
    model_count: int | None = None
    probed_capability: ModelCapability | None = None


def _provider_read(row: LlmProvider) -> ProviderRead:
    """读出面永远掩码 api_key:落库值是 SecretBox 密文,回传密文同样无意义。"""
    return ProviderRead(
        id=row.id,
        name=row.name,
        protocol=row.protocol,
        api_key=SERVICE_SECRET_MASK if row.api_key else "",
        has_api_key=bool(row.api_key),
        base_url=row.base_url,
        models=normalize_model_ids(row.models or []),
        model_metadata=provider_metadata_for_read(row),
        enabled=row.enabled,
        is_builtin=row.is_builtin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _encrypt_api_key(value: str) -> str:
    """落库前加密(空串直通;掩码值由调用方先行剔除)。"""
    return get_secret_box().encrypt(value) if value else ""


async def _provider_or_404(session: AsyncSession, provider_id: UUID) -> LlmProvider:
    row = (
        await session.execute(select(LlmProvider).where(LlmProvider.id == provider_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提供商不存在")
    return row


async def _fetch_provider_models(row: LlmProvider) -> list[object]:
    env = env_provider_credentials().get(row.name)
    api_key = get_secret_box().decrypt(row.api_key) if row.api_key else (env[1] if env else "")
    base_url = row.base_url or (env[2] if env else "")
    if not api_key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "该实例未配置 API 密钥")
    client: object | None = None
    try:
        if row.protocol == "anthropic":
            client = AsyncAnthropic(api_key=api_key, base_url=base_url or None)
        else:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        page = await client.models.list(timeout=15)  # type: ignore[union-attr]
        return list(page.data)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"该网关不支持模型导入或调用失败:{exc}",
        ) from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


@router.get("/providers", response_model=list[ProviderRead])
async def list_providers(session: Session) -> list[ProviderRead]:
    rows = (
        (
            await session.execute(
                # name 兜底排序:内置两行 created_at 相同,顺序必须稳定
                # (前端"默认选中第一项"依赖它)
                select(LlmProvider).order_by(
                    LlmProvider.is_builtin.desc(),
                    LlmProvider.created_at,
                    LlmProvider.name,
                )
            )
        )
        .scalars()
        .all()
    )
    return [_provider_read(row) for row in rows]


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(body: ProviderCreate, session: Session) -> ProviderRead:
    values = body.model_dump(exclude={"model_metadata"})
    values["api_key"] = _encrypt_api_key(values.get("api_key") or "")
    row = LlmProvider(**values)
    row.model_metadata = reconcile_provider_metadata(row.models, {})
    for model_id, manual in body.model_metadata.items():
        row.model_metadata[model_id] = metadata_from_manual(manual).model_dump(mode="json")
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "实例名已存在") from exc
    await session.refresh(row)
    await load_runtime_overrides(get_session_factory())
    return _provider_read(row)


@router.patch("/providers/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: UUID, body: ProviderUpdate, session: Session
) -> ProviderRead:
    row = await _provider_or_404(session, provider_id)
    patch = body.model_dump(exclude_unset=True)
    manual_metadata = patch.pop("model_metadata", None)
    if row.is_builtin and patch.get("protocol") not in (None, row.protocol):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "内置实例协议不可改")
    if "api_key" in patch:
        # 前端回传掩码 = "不改密钥"(读出面拿不到明文,只能这么表达)
        if patch["api_key"] == SERVICE_SECRET_MASK:
            patch.pop("api_key")
        else:
            patch["api_key"] = _encrypt_api_key(patch["api_key"] or "")
    final_models = patch.get("models", row.models or [])
    unknown_metadata = set(manual_metadata or {}) - set(final_models)
    if unknown_metadata:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "model_metadata 只能引用 models 中的模型",
        )
    for key, value in patch.items():
        setattr(row, key, value)
    row.models = normalize_model_ids(final_models)
    row.model_metadata = reconcile_provider_metadata(row.models, row.model_metadata)
    for model_id, manual in (manual_metadata or {}).items():
        current = row.model_metadata.get(model_id, {})
        provider_metadata = (
            current.get("provider_metadata", {}) if isinstance(current, dict) else {}
        )
        row.model_metadata[model_id] = metadata_from_manual(
            ManualModelCapabilities.model_validate(manual),
            provider_metadata=provider_metadata,
        ).model_dump(mode="json")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await load_runtime_overrides(get_session_factory())
    return _provider_read(row)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: UUID, session: Session) -> None:
    row = await _provider_or_404(session, provider_id)
    if row.is_builtin:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "内置实例不可删除,可停用")
    routes = (await session.execute(select(ModelRoute))).scalars().all()
    referenced = any(
        route.primary_provider == row.name
        or any(hop.get("provider") == row.name for hop in route.fallback_chain or [])
        for route in routes
    )
    if referenced:
        raise HTTPException(status.HTTP_409_CONFLICT, "有模型路由引用该实例,先调整路由再删除")
    await session.delete(row)
    await session.commit()
    await load_runtime_overrides(get_session_factory())


@router.get(
    "/providers/{provider_id}/discover-models",
    response_model=list[ProviderModelCatalogEntry],
)
async def discover_provider_models(
    provider_id: UUID,
    session: Session,
) -> list[ProviderModelCatalogEntry]:
    """Read the remote catalog without changing the provider inventory."""
    row = await _provider_or_404(session, provider_id)
    discovered: dict[str, ProviderModelMetadata] = {}
    try:
        for provider_model in await _fetch_provider_models(row):
            model_id, metadata = metadata_from_provider_model(provider_model)
            discovered[model_id] = metadata
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"网关模型目录格式无效:{exc}",
        ) from exc
    return [
        catalog_entry(row.name, model_id, metadata)
        for model_id, metadata in sorted(discovered.items(), key=lambda item: item[0].casefold())
    ]


@router.post("/providers/{provider_id}/import-models", response_model=ProviderRead)
async def import_provider_models(
    provider_id: UUID,
    body: ProviderModelImport,
    session: Session,
) -> ProviderRead:
    """Re-read the remote catalog and persist only the explicitly selected IDs."""
    row = await _provider_or_404(session, provider_id)
    discovered = await _fetch_provider_models(row)
    selected_ids = set(body.models)
    selected: list[object] = []
    discovered_ids: set[str] = set()
    try:
        for provider_model in discovered:
            model_id, _ = metadata_from_provider_model(provider_model)
            discovered_ids.add(model_id)
            if model_id in selected_ids:
                selected.append(provider_model)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"网关模型目录格式无效:{exc}",
        ) from exc
    missing = selected_ids - discovered_ids
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"所选模型已不在网关目录中:{', '.join(sorted(missing))}",
        )
    row.models, row.model_metadata = merge_imported_metadata(
        existing_models=row.models or [],
        existing_metadata=row.model_metadata,
        imported_models=selected,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await load_runtime_overrides(get_session_factory())
    return _provider_read(row)


@router.post(
    "/providers/{provider_id}/test-connection",
    response_model=ProviderConnectivityRead,
)
async def test_provider_connection(
    provider_id: UUID,
    session: Session,
) -> ProviderConnectivityRead:
    """Prove the credentials and network path by listing the remote catalog."""
    row = await _provider_or_404(session, provider_id)
    result = await check_instance(row, env=env_provider_credentials().get(row.name))
    return ProviderConnectivityRead(
        provider=row.name,
        scope="instance",
        ok=result.ok,
        latency_ms=result.latency_ms,
        error_code=result.error_code,
        model_count=result.model_count,
    )


@router.post(
    "/providers/{provider_id}/test-model",
    response_model=ProviderConnectivityRead,
)
async def test_provider_model(
    provider_id: UUID,
    body: ProviderModelConnectivityRequest,
    session: Session,
) -> ProviderConnectivityRead:
    """Send one minimal real request, shaped by the model's declared capability.

    A gateway routinely lists models it cannot serve, so a successful instance
    check says nothing about any single model ID.
    """
    row = await _provider_or_404(session, provider_id)
    result = await check_model(row, body.model, env=env_provider_credentials().get(row.name))
    return ProviderConnectivityRead(
        provider=row.name,
        model=body.model,
        scope="model",
        ok=result.ok,
        latency_ms=result.latency_ms,
        error_code=result.error_code,
        probed_capability=result.probed_capability,
    )


@router.put(
    "/providers/{provider_id}/model-capabilities",
    response_model=ProviderModelCatalogEntry,
)
async def update_provider_model_capabilities(
    provider_id: UUID,
    body: ProviderModelCapabilityUpdate,
    session: Session,
) -> ProviderModelCatalogEntry:
    row = await _provider_or_404(session, provider_id)
    if body.model not in set(row.models or []):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在")
    row.model_metadata = reconcile_provider_metadata(row.models, row.model_metadata)
    current = row.model_metadata.get(body.model, {})
    provider_metadata = current.get("provider_metadata", {}) if isinstance(current, dict) else {}
    if body.mode == "auto":
        resolved = metadata_from_registry(body.model)
        resolved.provider_metadata = dict(provider_metadata)
    else:
        resolved = metadata_from_manual(
            ManualModelCapabilities(
                capabilities=body.capabilities,
                input_modalities=body.input_modalities,
            ),
            provider_metadata=provider_metadata,
        )
    row.model_metadata[body.model] = resolved.model_dump(mode="json")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await load_runtime_overrides(get_session_factory())
    return provider_model_catalog_entry(row, body.model)


@router.get("/model-catalog", response_model=list[ProviderModelCatalogEntry])
async def get_provider_model_catalog(
    session: Session,
    capability: ModelCatalogCapabilityFilter | None = None,
) -> list[ProviderModelCatalogEntry]:
    providers = (await session.execute(select(LlmProvider))).scalars().all()
    return build_model_catalog(providers, capability=capability)


@router.get("/models", response_model=list[ModelRoute])
async def list_routes(session: Session) -> list[ModelRoute]:
    return (
        (
            await session.execute(
                select(ModelRoute).order_by(ModelRoute.task, ModelRoute.org_id.nulls_first())
            )
        )
        .scalars()
        .all()
    )


@router.get("/model-route-tasks", response_model=list[RouteTaskCatalogOut])
async def list_model_route_tasks(session: Session) -> list[RouteTaskCatalogOut]:
    return await _model_route_task_catalog(session)


async def _invalidate_route_cache(route: ModelRoute) -> None:
    """路由变更主动失效解析缓存(共享 redis,API/worker 同步生效):
    org 级删精确键;平台级(org_id=None)影响所有 org,按 task 前缀删。"""
    if route.org_id is not None:
        await cache.delete(route_cache_key(route.task, route.org_id))
    else:
        await cache.delete_by_prefix(f"llm:route:{route.task}:")


@router.post("/models", response_model=ModelRoute, status_code=status.HTTP_201_CREATED)
async def create_route(
    body: RouteCreate,
    session: Session,
    background: BackgroundTasks,
) -> ModelRoute:
    _validate_chain(body.fallback_chain)
    await _ensure_route_org_exists(session, body.org_id)
    await _ensure_route_scope_available(session, tasks=[body.task], org_id=body.org_id)
    await _validate_route_targets(
        session,
        task=body.task,
        org_id=body.org_id,
        primary_provider=body.primary_provider,
        primary_model=body.primary_model,
        fallback_chain=body.fallback_chain,
    )
    campaign = (
        await _prepare_embedding_route_campaign(
            session,
            task=body.task,
            provider=body.primary_provider,
            model=body.primary_model,
        )
        if body.is_active
        else None
    )
    route = ModelRoute(**body.model_dump())
    session.add(route)
    await session.commit()
    await session.refresh(route)
    await _invalidate_route_cache(route)
    await load_runtime_overrides(get_session_factory())
    if campaign is not None:
        await dispatch_embedding_campaign(campaign.id, background)
    return route


@router.post(
    "/models/batch",
    response_model=list[ModelRoute],
    status_code=status.HTTP_201_CREATED,
)
async def create_routes_batch(body: RouteBatchCreate, session: Session) -> list[ModelRoute]:
    """为多个未配置业务任务原子创建同一份专属路由。"""
    _validate_chain(body.fallback_chain)
    await _ensure_route_org_exists(session, body.org_id)

    catalog = await _model_route_task_catalog(session)
    custom_tasks = {item.task for item in catalog if not item.is_system}
    unknown = sorted(set(body.tasks) - custom_tasks)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"任务不在可配置目录中:{'、'.join(unknown)}",
        )
    await _ensure_route_scope_available(session, tasks=body.tasks, org_id=body.org_id)
    for task in body.tasks:
        await _validate_route_targets(
            session,
            task=task,
            org_id=body.org_id,
            primary_provider=body.primary_provider,
            primary_model=body.primary_model,
            fallback_chain=body.fallback_chain,
        )

    common = body.model_dump(exclude={"tasks"})
    routes = [ModelRoute(task=task, **common) for task in body.tasks]
    session.add_all(routes)
    await session.commit()
    for route in routes:
        await session.refresh(route)
        await _invalidate_route_cache(route)
    await load_runtime_overrides(get_session_factory())
    return routes


@router.patch("/models/{route_id}", response_model=ModelRoute)
async def update_route(
    route_id: UUID,
    body: RouteUpdate,
    session: Session,
    background: BackgroundTasks,
) -> ModelRoute:
    route = (
        await session.execute(select(ModelRoute).where(ModelRoute.id == route_id))
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "路由不存在")
    data = body.model_dump(exclude_unset=True)
    if "fallback_chain" in data:
        _validate_chain(data["fallback_chain"])
    target_provider = data.get("primary_provider", route.primary_provider)
    target_model = data.get("primary_model", route.primary_model)
    target_active = data.get("is_active", route.is_active)
    route_fields_changed = bool(
        {"primary_provider", "primary_model", "fallback_chain"}.intersection(data)
    )
    if target_active or route_fields_changed:
        await _validate_route_targets(
            session,
            task=route.task,
            org_id=route.org_id,
            primary_provider=target_provider,
            primary_model=target_model,
            fallback_chain=data.get("fallback_chain", route.fallback_chain),
        )
    activates_route = bool(target_active and not route.is_active)
    changes_active_primary = bool(
        target_active and {"primary_provider", "primary_model"}.intersection(data)
    )
    campaign = (
        await _prepare_embedding_route_campaign(
            session,
            task=route.task,
            provider=target_provider,
            model=target_model,
        )
        if activates_route or changes_active_primary
        else None
    )
    for key, value in data.items():
        setattr(route, key, value)
    session.add(route)
    await session.commit()
    await session.refresh(route)
    await _invalidate_route_cache(route)
    await load_runtime_overrides(get_session_factory())
    if campaign is not None:
        await dispatch_embedding_campaign(campaign.id, background)
    return route


@router.delete("/models/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(route_id: UUID, session: Session) -> None:
    route = (
        await session.execute(select(ModelRoute).where(ModelRoute.id == route_id))
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "路由不存在")
    await session.delete(route)
    await session.commit()
    await _invalidate_route_cache(route)
    await load_runtime_overrides(get_session_factory())


# ---------- 外部服务配置(联网搜索 / 图片生成 / 天气) ----------
# 模型与凭证统一经 /admin/providers 和 /admin/models 管理;这里只放非模型类
# 外部服务的凭证与开关,.env 仅作迁移期兜底。TF 的 "ota" 属旅游业务,不在 SDK。

SERVICE_CONFIG_NAMES = ("websearch", "imagegen", "weather")
EMBEDDING_OFFLINE_RUNBOOK = "docs/operations/embedding-dimension-migration.md"


class ServiceConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict


def _resolved_embedding_config(payload: dict | None) -> dict:
    """A12 触点②:service-configs 的 embedding 分支(缺省回落 .env / 内置维度)。"""
    settings = get_settings()
    values = payload or {}
    raw_dim = values.get("dim")
    try:
        dim = int(raw_dim) if raw_dim not in (None, "") else EMBEDDING_DIM
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_embedding_dimension", "message": "dim 必须为正整数"},
        ) from exc
    try:
        return normalize_embedding_config(
            {
                "provider": values.get("provider") or settings.embedding_provider,
                "model": values.get("model") or settings.embedding_model,
                "dim": dim,
            }
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_embedding_config", "message": str(exc)},
        ) from exc


def _is_service_secret(key: str) -> bool:
    """Any credential-shaped key is masked on read.

    Matched on the ``_key`` / ``_secret`` / ``_token`` suffix rather than an
    explicit allowlist: a new service must not leak its credential just because
    nobody remembered to register the field name.
    """
    return key in {"api_key", "key", "secret", "token"} or key.endswith(
        ("_api_key", "_key", "_secret", "_token", "_password")
    )


def _mask_service_secrets(payload: dict) -> dict:
    return {
        key: SERVICE_SECRET_MASK if _is_service_secret(key) and value else value
        for key, value in payload.items()
    }


def _merge_masked_service_secrets(payload: dict, current: dict | None) -> dict:
    """掩码原样回传 = 保留旧值(读出面拿不到明文,前端只能这么表达"不改")。"""
    merged = dict(payload)
    for key, value in payload.items():
        if _is_service_secret(key) and value == SERVICE_SECRET_MASK:
            merged[key] = (current or {}).get(key, "")
    return merged


def _encrypt_service_secrets(payload: dict) -> dict:
    """落库前把密钥形字段经 SecretBox 加密(非密钥字段原样)。"""
    box = get_secret_box()
    return {
        key: (
            box.encrypt(value)
            if _is_service_secret(key) and isinstance(value, str) and value
            else value
        )
        for key, value in payload.items()
    }


@router.get("/service-configs")
async def list_service_configs(session: Session) -> dict:
    """全部外部服务配置(平台管理员;空 payload = 全走 .env)。"""
    rows = (
        (
            await session.execute(
                select(ServiceConfig).where(ServiceConfig.name.in_(SERVICE_CONFIG_NAMES))
            )
        )
        .scalars()
        .all()
    )
    by_name = {row.name: _mask_service_secrets(row.payload) for row in rows}
    return {name: by_name.get(name, {}) for name in SERVICE_CONFIG_NAMES}


@router.get("/service-configs/imagegen/readiness")
async def imagegen_readiness(session: Session) -> dict:
    """Resolved, non-secret readiness metadata for deployment verification."""
    row = (
        await session.execute(select(ServiceConfig).where(ServiceConfig.name == "imagegen"))
    ).scalar_one_or_none()
    return imagegen_service.image_service_readiness(
        row.payload if row is not None else None
    ).as_dict()


@router.put("/service-configs/{name}")
async def put_service_config(name: str, body: ServiceConfigIn, session: Session) -> dict:
    """整体覆盖某服务配置;键与 .env 字段对应,空串/缺失键回落 .env。"""
    if name not in SERVICE_CONFIG_NAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "未知服务")
    row = (
        await session.execute(select(ServiceConfig).where(ServiceConfig.name == name))
    ).scalar_one_or_none()
    merged_payload = _merge_masked_service_secrets(
        body.payload, row.payload if row is not None else None
    )
    if name == "imagegen":
        try:
            payload = imagegen_service.normalize_imagegen_payload(merged_payload)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "invalid_imagegen_config", "message": str(exc)},
            ) from exc
    else:
        payload = merged_payload
    payload = _encrypt_service_secrets(payload)

    if row is None:
        row = ServiceConfig(name=name, payload=payload)
    else:
        row.payload = payload
    session.add(row)
    await session.commit()
    # API 进程内立即生效;其余进程(worker)靠周期刷新兜底
    await load_runtime_overrides(get_session_factory())
    return {"name": name, "payload": _mask_service_secrets(row.payload)}


# ---------- 用量看板 ----------


class UsageRow(BaseModel):
    org_id: UUID
    org_name: str
    task: str
    tokens_in: int
    tokens_out: int
    calls: int
    quantity: int  # 通用计量数:存储类 = 字节,LLM 行恒 0


@router.get("/usage", response_model=list[UsageRow])
async def usage_overview(
    org_session: Annotated[AsyncSession, Depends(get_org_session)],
    days: int = 30,
) -> list[UsageRow]:
    """按 org×task 聚合近 N 天用量。平台管理员的 JWT org = 平台 org,
    usage_daily 的 platform_read 策略据此放行全租户 SELECT(写入面不受影响)。"""
    since = date.today() - timedelta(days=max(1, min(days, 365)))
    rows = (
        await org_session.execute(
            select(
                UsageDaily.org_id,
                func.coalesce(Organization.name, "(已删除)").label("org_name"),
                UsageDaily.task,
                func.sum(UsageDaily.tokens_in),
                func.sum(UsageDaily.tokens_out),
                func.sum(UsageDaily.calls),
                func.sum(UsageDaily.quantity),
            )
            .join(Organization, Organization.id == UsageDaily.org_id, isouter=True)
            .where(UsageDaily.usage_date >= since)
            .group_by(UsageDaily.org_id, Organization.name, UsageDaily.task)
            .order_by(func.sum(UsageDaily.tokens_in).desc())
        )
    ).all()
    return [
        UsageRow(
            org_id=r[0],
            org_name=r[1],
            task=r[2],
            tokens_in=r[3],
            tokens_out=r[4],
            calls=r[5],
            quantity=r[6],
        )
        for r in rows
    ]


# ---------- 模型计费(billing) ----------


class PriceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    # 单位:USD / 1M tokens
    input_price: Decimal = Field(ge=0)
    output_price: Decimal = Field(ge=0)
    cache_read_price: Decimal = Field(default=Decimal(0), ge=0)
    cache_write_price: Decimal = Field(default=Decimal(0), ge=0)


class PriceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    input_price: Decimal | None = Field(default=None, ge=0)
    output_price: Decimal | None = Field(default=None, ge=0)
    cache_read_price: Decimal | None = Field(default=None, ge=0)
    cache_write_price: Decimal | None = Field(default=None, ge=0)


@router.get("/model-prices", response_model=list[ModelPrice])
async def list_model_prices(session: Session) -> list[ModelPrice]:
    return (
        (
            await session.execute(
                select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model)
            )
        )
        .scalars()
        .all()
    )


@router.post("/model-prices", response_model=ModelPrice, status_code=status.HTTP_201_CREATED)
async def create_model_price(body: PriceCreate, session: Session) -> ModelPrice:
    exists = (
        await session.execute(
            select(ModelPrice).where(
                ModelPrice.provider == body.provider, ModelPrice.model == body.model
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该 provider+model 已有价格,请编辑现有条目"
        )
    price = ModelPrice(**body.model_dump())
    session.add(price)
    await session.commit()
    await session.refresh(price)
    return price


@router.patch("/model-prices/{price_id}", response_model=ModelPrice)
async def update_model_price(price_id: UUID, body: PriceUpdate, session: Session) -> ModelPrice:
    price = (
        await session.execute(select(ModelPrice).where(ModelPrice.id == price_id))
    ).scalar_one_or_none()
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "价格条目不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(price, key, value)
    await session.commit()
    await session.refresh(price)
    return price


@router.delete("/model-prices/{price_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_price(price_id: UUID, session: Session) -> None:
    price = (
        await session.execute(select(ModelPrice).where(ModelPrice.id == price_id))
    ).scalar_one_or_none()
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "价格条目不存在")
    await session.delete(price)
    await session.commit()


_PER_MILLION = Decimal(1_000_000)


def _cost_of(
    price: ModelPrice | None,
    tokens_in: int,
    tokens_out: int,
    cache_read: int,
    cache_write: int,
) -> float | None:
    """计费公式(与记账口径配套,tokens_in = 非缓存新输入);未定价返回 None。"""
    if price is None:
        return None
    cost = (
        tokens_in * price.input_price
        + tokens_out * price.output_price
        + cache_read * price.cache_read_price
        + cache_write * price.cache_write_price
    ) / _PER_MILLION
    return float(cost)


class BillingDaily(BaseModel):
    usage_date: date
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    calls: int
    cost: float  # 未定价组合按 0 计入,unpriced 里如实标注


class BillingModelRow(BaseModel):
    provider: str
    model: str
    calls: int
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: float | None  # None = 未定价
    avg_cost: float | None  # cost / calls


class BillingOrgRow(BaseModel):
    org_id: UUID
    org_name: str
    calls: int
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: float


class BillingSummary(BaseModel):
    calls: int
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost: float
    # 输入侧缓存命中率 = cache_read / (cache_read + tokens_in);无输入时 None
    cache_hit_rate: float | None
    unpriced: list[str]  # 有用量但未定价的 "provider:model"


class BillingOut(BaseModel):
    summary: BillingSummary
    daily: list[BillingDaily]
    by_model: list[BillingModelRow]
    by_org: list[BillingOrgRow]


@router.get("/billing", response_model=BillingOut)
async def billing_overview(
    org_session: Annotated[AsyncSession, Depends(get_org_session)],
    days: int = 30,
) -> BillingOut:
    """LLM 用量计费汇总:usage_daily(平台读策略)× model_prices。

    只统计 LLM 行(tokens/缓存计量 > 0);internal 行 token 恒 0,天然不参与。
    未定价组合成本按 0 计入合计,并在 summary.unpriced 里如实列出。
    """
    since = date.today() - timedelta(days=max(1, min(days, 365)))
    rows = (
        await org_session.execute(
            select(
                UsageDaily.usage_date,
                UsageDaily.org_id,
                func.coalesce(Organization.name, "(已删除)").label("org_name"),
                UsageDaily.provider,
                UsageDaily.model,
                func.sum(UsageDaily.tokens_in),
                func.sum(UsageDaily.tokens_out),
                func.sum(UsageDaily.cache_read_tokens),
                func.sum(UsageDaily.cache_write_tokens),
                func.sum(UsageDaily.calls),
            )
            .join(Organization, Organization.id == UsageDaily.org_id, isouter=True)
            .where(UsageDaily.usage_date >= since)
            .group_by(
                UsageDaily.usage_date,
                UsageDaily.org_id,
                Organization.name,
                UsageDaily.provider,
                UsageDaily.model,
            )
        )
    ).all()

    prices = {
        (p.provider, p.model): p
        for p in (await org_session.execute(select(ModelPrice))).scalars().all()
    }

    daily: dict[date, dict] = {}
    by_model: dict[tuple[str, str], dict] = {}
    by_org: dict[UUID, dict] = {}
    total = {"calls": 0, "tin": 0, "tout": 0, "cr": 0, "cw": 0, "cost": 0.0}
    unpriced: set[str] = set()

    for usage_date, org_id, org_name, provider, model, tin, tout, cr, cw, calls in rows:
        if tin + tout + cr + cw == 0:
            continue  # 非 LLM 计量行(存储/上传)不参与 token 计费
        cost = _cost_of(prices.get((provider, model)), tin, tout, cr, cw)
        if cost is None:
            unpriced.add(f"{provider}:{model}")
        billed = cost or 0.0

        d = daily.setdefault(
            usage_date, {"tin": 0, "tout": 0, "cr": 0, "cw": 0, "calls": 0, "cost": 0.0}
        )
        m = by_model.setdefault(
            (provider, model),
            {"tin": 0, "tout": 0, "cr": 0, "cw": 0, "calls": 0, "cost": 0.0},
        )
        o = by_org.setdefault(
            org_id,
            {"name": org_name, "tin": 0, "tout": 0, "cr": 0, "cw": 0, "calls": 0, "cost": 0.0},
        )
        for bucket in (d, m, o, total):
            bucket["tin"] += tin
            bucket["tout"] += tout
            bucket["cr"] += cr
            bucket["cw"] += cw
            bucket["calls"] += calls
            bucket["cost"] += billed

    input_side = total["tin"] + total["cr"]
    return BillingOut(
        summary=BillingSummary(
            calls=total["calls"],
            tokens_in=total["tin"],
            tokens_out=total["tout"],
            cache_read_tokens=total["cr"],
            cache_write_tokens=total["cw"],
            total_tokens=total["tin"] + total["tout"] + total["cr"] + total["cw"],
            cost=round(total["cost"], 4),
            cache_hit_rate=(total["cr"] / input_side) if input_side else None,
            unpriced=sorted(unpriced),
        ),
        daily=[
            BillingDaily(
                usage_date=day,
                tokens_in=v["tin"],
                tokens_out=v["tout"],
                cache_read_tokens=v["cr"],
                cache_write_tokens=v["cw"],
                calls=v["calls"],
                cost=round(v["cost"], 4),
            )
            for day, v in sorted(daily.items())
        ],
        by_model=[
            BillingModelRow(
                provider=provider,
                model=model,
                calls=v["calls"],
                tokens_in=v["tin"],
                tokens_out=v["tout"],
                cache_read_tokens=v["cr"],
                cache_write_tokens=v["cw"],
                cost=round(v["cost"], 4) if f"{provider}:{model}" not in unpriced else None,
                avg_cost=(
                    round(v["cost"] / v["calls"], 6)
                    if v["calls"] and f"{provider}:{model}" not in unpriced
                    else None
                ),
            )
            for (provider, model), v in sorted(
                by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True
            )
        ],
        by_org=[
            BillingOrgRow(
                org_id=org_id,
                org_name=v["name"],
                calls=v["calls"],
                tokens_in=v["tin"],
                tokens_out=v["tout"],
                cache_read_tokens=v["cr"],
                cache_write_tokens=v["cw"],
                cost=round(v["cost"], 4),
            )
            for org_id, v in sorted(by_org.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        ],
    )


# ---------- LLM 请求日志 ----------


class TraceRow(BaseModel):
    id: UUID
    created_at: datetime | None
    org_name: str
    task: str
    provider: str
    model: str
    status: str
    error: str | None
    attempt: int
    fallback_from: str | None
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost: float | None  # 按当前价格现算;未定价 None


class TracePage(BaseModel):
    items: list[TraceRow]
    total: int
    page: int
    page_size: int


@router.get("/llm-traces", response_model=TracePage)
async def llm_trace_log(
    org_session: Annotated[AsyncSession, Depends(get_org_session)],
    days: int = 7,
    page: int = 1,
    page_size: int = 50,
    status_filter: str | None = None,
) -> TracePage:
    """LLM 请求日志(平台侧跨租户,llm_traces platform_read 策略):
    倒序分页;status_filter ∈ success/error;成本按当前价格现算(历史价不回溯)。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 90)))

    conditions = [LlmTrace.created_at >= since]
    if status_filter in ("success", "error"):
        conditions.append(LlmTrace.status == status_filter)

    total = (
        await org_session.execute(
            select(func.count()).select_from(LlmTrace).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await org_session.execute(
            select(LlmTrace, func.coalesce(Organization.name, "(已删除)"))
            .join(Organization, Organization.id == LlmTrace.org_id, isouter=True)
            .where(*conditions)
            .order_by(LlmTrace.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    prices = {
        (p.provider, p.model): p
        for p in (await org_session.execute(select(ModelPrice))).scalars().all()
    }
    return TracePage(
        items=[
            TraceRow(
                id=t.id,
                created_at=t.created_at,
                org_name=org_name,
                task=t.task,
                provider=t.provider,
                model=t.model,
                status=t.status,
                error=t.error,
                attempt=t.attempt,
                fallback_from=t.fallback_from,
                tokens_in=t.tokens_in,
                tokens_out=t.tokens_out,
                cache_read_tokens=t.cache_read_tokens,
                cache_write_tokens=t.cache_write_tokens,
                latency_ms=t.latency_ms,
                cost=_cost_of(
                    prices.get((t.provider, t.model)),
                    t.tokens_in,
                    t.tokens_out,
                    t.cache_read_tokens,
                    t.cache_write_tokens,
                ),
            )
            for t, org_name in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
