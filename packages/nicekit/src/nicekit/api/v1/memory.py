"""长期记忆管理 API:组织设置里的"记忆"页据此列表/编辑/失效。

为什么要有人工入口:记忆由 LLM 从对话沉淀,再严的管道也拦不住全部误判。
没有人工纠正与失效的通道,一条错误偏好会被反复召回、反复影响后续判断,
而且没人知道它从哪来。所以这里的三个能力缺一不可:
- **列表 + 来源追溯**:每条都能看到 source(会话 id / 写入工具)与 source_message_id;
- **编辑**:改标题/正文/类型/置信,而不是只能删了重记;
- **失效**:软删(status=rejected)。删掉痕迹会让下一轮抽取把同一条脏记忆
  原样再写一遍——rejected 记录本身就是防污染管道的输入(见 memory.ingest_candidate)。

角色门槛:读全员开放(一线要能核对 agent 凭什么这么说),写限 org_admin/
platform_admin(记忆是跨会话复用的组织资产,不该谁都能改)。

搬自 TF backend/app/api/v1/memory.py。SDK 化改造:
- `scope` 词表泛化:内置只有 `org`,其余由宿主 `register_memory_scopes()` 注册,
  查询参数按注册表校验而不是硬编码 Literal;
- `scope_ref_id → 展示名`的解析(TF 查 Project.title / Customer.name)改为
  宿主注册的 `ScopeLabelResolver`,SDK 默认不解析(只回 ref 原值)。
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.api.deps import OrgContext, get_org_context, get_org_session, require_role
from nicekit.models.memory import (
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    known_memory_scopes,
)
from nicekit.models.tenancy import Role

router = APIRouter(prefix="/memory")

_ADMIN_ROLES = (Role.ORG_ADMIN, Role.PLATFORM_ADMIN)

MemoryTypeLiteral = Literal[
    "preference", "preference_candidate", "constraint", "decision", "fact", "risk"
]
MemoryStatusLiteral = Literal["active", "superseded", "rejected"]


# ---------------------------------------------------------------------------
# 扩展点:scope_ref_id → 展示名
# ---------------------------------------------------------------------------

# 宿主实现:给定一批 (scope, scope_ref_id),返回 {scope_ref_id: 展示名}。
# 解析不出来的留空即可(引用对象可能已被删),列表页回落显示 ref 原值。
ScopeLabelResolver = Callable[
    [AsyncSession, UUID, Sequence[tuple[str, str]]], Awaitable[dict[str, str]]
]


async def _null_scope_label_resolver(
    session: AsyncSession, org_id: UUID, refs: Sequence[tuple[str, str]]
) -> dict[str, str]:
    return {}


_scope_label_resolver: ScopeLabelResolver = _null_scope_label_resolver


def set_scope_label_resolver(resolver: ScopeLabelResolver | None) -> None:
    """注入宿主的 scope_ref_id 展示名解析(传 None 恢复"不解析")。"""
    global _scope_label_resolver
    _scope_label_resolver = resolver or _null_scope_label_resolver


class MemoryOut(BaseModel):
    id: UUID
    scope: str
    scope_ref_id: str | None
    # 宿主 ScopeLabelResolver 给出的展示名:列表页只有一个 UUID 没法用,
    # 但这是展示用的附加值,解析不出就留空(引用对象可能已被删)
    scope_ref_label: str | None = None
    memory_type: str
    title: str
    content: str
    source: str
    source_message_id: UUID | None
    confidence: float
    status: str
    hit_count: int
    sightings: int
    last_hit_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    total: int


class MemoryPatch(BaseModel):
    """全字段可选,只更新显式传入的字段(不传 ≠ 置空)。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    memory_type: MemoryTypeLiteral | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: MemoryStatusLiteral | None = None


class MemoryRejectBody(BaseModel):
    """失效原因必填:三个月后回看时,"为什么当初废了这条"必须有据可查。"""

    reason: str = Field(min_length=1, max_length=200)


def _to_out(item: MemoryItem, label: str | None = None) -> MemoryOut:
    return MemoryOut(
        id=item.id,
        scope=item.scope,
        scope_ref_id=item.scope_ref_id,
        scope_ref_label=label,
        memory_type=item.memory_type,
        title=item.title,
        content=item.content,
        source=item.source,
        source_message_id=item.source_message_id,
        confidence=float(item.confidence or 0.0),
        status=item.status,
        hit_count=item.hit_count,
        sightings=item.sightings,
        last_hit_at=item.last_hit_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _scope_labels(
    session: AsyncSession, org_id: UUID, items: Sequence[MemoryItem]
) -> dict[str, str]:
    """scope_ref_id → 展示名。宿主未注册解析器时返回空表(前端回落显示 ref)。

    解析失败不让整页 500:展示名是锦上添花,记忆本体才是这个页面的主体。
    """
    refs = [
        (item.scope, item.scope_ref_id)
        for item in items
        if item.scope_ref_id and item.scope != MemoryScope.ORG.value
    ]
    if not refs:
        return {}
    try:
        return await _scope_label_resolver(session, org_id, refs)
    except Exception:  # noqa: BLE001 - 展示名解析失败不影响列表本身
        return {}


def _validate_scope(scope: str | None) -> str | None:
    """按注册表校验 scope(内置 org + 宿主 register_memory_scopes 注册的)。"""
    if scope is None:
        return None
    if scope not in known_memory_scopes():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "scope 取值非法")
    return scope


@router.get("", response_model=MemoryListOut)
async def list_memories(
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    scope: str | None = None,
    memory_type: MemoryTypeLiteral | None = None,
    memory_status: MemoryStatusLiteral | None = None,
    scope_ref_id: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> MemoryListOut:
    """记忆列表。默认不过滤 status——人工审阅正需要看到 rejected 那些。"""
    conditions = [MemoryItem.org_id == ctx.org_id]
    scope_value = _validate_scope(scope)
    if scope_value is not None:
        conditions.append(MemoryItem.scope == scope_value)
    if memory_type is not None:
        conditions.append(MemoryItem.memory_type == memory_type)
    if memory_status is not None:
        conditions.append(MemoryItem.status == memory_status)
    if scope_ref_id:
        conditions.append(MemoryItem.scope_ref_id == scope_ref_id)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(
            MemoryItem.title.ilike(pattern) | MemoryItem.content.ilike(pattern)
        )

    total = (
        await session.execute(
            select(func.count()).select_from(MemoryItem).where(*conditions)
        )
    ).scalar_one()
    rows = list(
        (
            await session.execute(
                select(MemoryItem)
                .where(*conditions)
                .order_by(MemoryItem.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    labels = await _scope_labels(session, ctx.org_id, rows)
    return MemoryListOut(
        items=[_to_out(row, labels.get(row.scope_ref_id or "")) for row in rows],
        total=int(total or 0),
    )


@router.get("/scopes")
async def list_memory_scopes() -> dict:
    """筛选下拉的词表(内置 + 宿主注册),避免两端各写一份枚举。"""
    return {
        "scopes": sorted(known_memory_scopes()),
        "types": list(MEMORY_TYPES),
        "statuses": list(MEMORY_STATUSES),
    }


@router.get("/{memory_id}", response_model=MemoryOut)
async def get_memory(
    memory_id: UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> MemoryOut:
    item = await _load(session, ctx.org_id, memory_id)
    labels = await _scope_labels(session, ctx.org_id, [item])
    return _to_out(item, labels.get(item.scope_ref_id or ""))


@router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: UUID,
    body: MemoryPatch,
    ctx: Annotated[OrgContext, Depends(require_role(*_ADMIN_ROLES))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> MemoryOut:
    """人工订正一条记忆。

    source 不允许改:人工订正的是内容,不是它的出处。把来源改成"人工"会让
    "这条当初是怎么来的"永久丢失,而那正是排查记忆污染时最需要的线索。
    """
    item = await _load(session, ctx.org_id, memory_id)
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的字段")
    for key, value in payload.items():
        setattr(item, key, value)
    item.updated_at = datetime.now(UTC)
    session.add(item)
    await session.commit()
    labels = await _scope_labels(session, ctx.org_id, [item])
    return _to_out(item, labels.get(item.scope_ref_id or ""))


@router.post("/{memory_id}/reject", response_model=MemoryOut)
async def reject_memory(
    memory_id: UUID,
    body: MemoryRejectBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_ADMIN_ROLES))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> MemoryOut:
    """删除 = 置 rejected(软删)。

    走 POST /reject 而不是 DELETE:失效原因是必填的,而 DELETE 带请求体在各类
    HTTP 客户端里的支持参差不齐(前端 api.delete 就不接受 body)。物理删也不可
    取——那会让下一轮抽取把同一条脏记忆原样再写一遍,rejected 记录本身就是
    防污染管道的输入。失效原因追加进正文,人工回看时能直接看到判断依据。
    """
    item = await _load(session, ctx.org_id, memory_id)
    if item.status == MemoryStatus.REJECTED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "该记忆已被标记为失效")
    item.status = MemoryStatus.REJECTED.value
    item.content = f"{item.content}\n[已失效] {body.reason}"
    item.updated_at = datetime.now(UTC)
    session.add(item)
    await session.commit()
    return _to_out(item)


async def _load(session: AsyncSession, org_id: UUID, memory_id: UUID) -> MemoryItem:
    """org_id 显式入 where:除 RLS 之外再加一道应用层围栏,跨组织读一律 404。"""
    item = (
        await session.execute(
            select(MemoryItem).where(
                MemoryItem.id == memory_id, MemoryItem.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    return item


# 供前端筛选下拉直接消费,避免两端各写一份枚举文案
MEMORY_TYPES = tuple(item.value for item in MemoryType)
MEMORY_STATUSES = tuple(item.value for item in MemoryStatus)
