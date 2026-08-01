"""实体类型注册表 + 通用实体 API(M3a KB 通用化)。

- /kb/entity-types:类型 CRUD——租户自建类型(如车队价格);内置类型只读
  (不可改不可删,可见于列表供前端 schema 驱动渲染);
- /kb/bases/{kb_id}/entities:通用实体 CRUD——写入必过所属类型 field_schema 强校验;
  快照管理下的库(active_snapshot_id 非空)禁止直接写(与 extraction 确认同款守门),
  实体应经摄入→事实→投影链路进入。
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.api.deps import (
    OrgContext,
    get_org_context,
    get_org_session,
    require_write_role,
)
from nicekit.kb.effective_scope import live_snapshot_projection_filter
from nicekit.kb.entity_types import (
    EntityReviewPolicy,
    EntityTypeInvalid,
    EntityValidationError,
    get_entity_type,
    validate_entity_attributes,
    validate_field_schema,
    validate_filterable_fields,
)
from nicekit.kb.projections import active_projection_filter
from nicekit.models.kb import (
    KbEntity,
    KbEntityType,
    KnowledgeBase,
)

router = APIRouter(prefix="/kb")

Session = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]
# 写角色统一注册在 tenancy/roles.py(A13):内置 platform_admin/org_admin,
# 宿主经 register_write_roles() 追加业务角色,守卫在请求期读注册表。
WriterCtx = Annotated[OrgContext, Depends(require_write_role())]

_REVIEW_POLICIES = tuple(p.value for p in EntityReviewPolicy)


class EntityTypeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type_key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    field_schema: dict
    filterable_fields: list[dict] = Field(default_factory=list)
    card_template: str | None = None
    review_policy: str = EntityReviewPolicy.AI.value


class EntityTypePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    field_schema: dict | None = None
    filterable_fields: list[dict] | None = None
    card_template: str | None = None
    review_policy: str | None = None


class EntityTypeOut(BaseModel):
    id: UUID
    type_key: str
    display_name: str
    description: str | None
    field_schema: dict
    filterable_fields: list
    card_template: str | None
    review_policy: str
    is_builtin: bool
    is_own: bool  # 本 org 自建(可改可删) vs 平台内置(只读)


def _out(row: KbEntityType, org_id: UUID) -> EntityTypeOut:
    return EntityTypeOut(
        id=row.id,
        type_key=row.type_key,
        display_name=row.display_name,
        description=row.description,
        field_schema=row.field_schema,
        filterable_fields=row.filterable_fields,
        card_template=row.card_template,
        review_policy=row.review_policy,
        is_builtin=row.is_builtin,
        is_own=row.org_id == org_id,
    )


def _validate_definition(
    field_schema: dict, filterable_fields: list, review_policy: str
) -> None:
    try:
        validate_field_schema(field_schema)
        validate_filterable_fields(filterable_fields, field_schema)
    except EntityTypeInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if review_policy not in _REVIEW_POLICIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"review_policy 必须是 {'/'.join(_REVIEW_POLICIES)}",
        )


@router.get("/entity-types", response_model=list[EntityTypeOut])
async def list_entity_types(session: Session, ctx: Ctx) -> list[EntityTypeOut]:
    """全部可用类型:本 org 自建 + 平台内置(同 key 时本 org 定义优先展示在前)。"""
    rows = (
        (await session.execute(select(KbEntityType).order_by(KbEntityType.type_key)))
        .scalars()
        .all()
    )
    rows.sort(key=lambda r: (r.type_key, r.org_id != ctx.org_id))
    return [_out(r, ctx.org_id) for r in rows]


@router.post(
    "/entity-types", response_model=EntityTypeOut, status_code=status.HTTP_201_CREATED
)
async def create_entity_type(
    body: EntityTypeIn, session: Session, ctx: WriterCtx
) -> EntityTypeOut:
    _validate_definition(body.field_schema, body.filterable_fields, body.review_policy)
    exists = (
        await session.execute(
            select(KbEntityType.id).where(
                KbEntityType.org_id == ctx.org_id,
                KbEntityType.type_key == body.type_key,
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"类型 {body.type_key} 已存在")
    row = KbEntityType(
        org_id=ctx.org_id,
        type_key=body.type_key,
        display_name=body.display_name,
        description=body.description,
        field_schema=body.field_schema,
        filterable_fields=body.filterable_fields,
        card_template=body.card_template,
        review_policy=body.review_policy,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _out(row, ctx.org_id)


async def _own_type_or_404(
    session: AsyncSession, org_id: UUID, type_id: UUID
) -> KbEntityType:
    row = (
        await session.execute(select(KbEntityType).where(KbEntityType.id == type_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "类型不存在")
    if row.is_builtin or row.org_id != org_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "内置/非本组织类型不可修改")
    return row


@router.patch("/entity-types/{type_id}", response_model=EntityTypeOut)
async def update_entity_type(
    type_id: UUID, body: EntityTypePatch, session: Session, ctx: WriterCtx
) -> EntityTypeOut:
    row = await _own_type_or_404(session, ctx.org_id, type_id)
    field_schema = body.field_schema if body.field_schema is not None else row.field_schema
    filterable = (
        body.filterable_fields
        if body.filterable_fields is not None
        else row.filterable_fields
    )
    review_policy = body.review_policy or row.review_policy
    _validate_definition(field_schema, filterable, review_policy)
    if body.display_name is not None:
        row.display_name = body.display_name
    if body.description is not None:
        row.description = body.description
    if body.card_template is not None:
        row.card_template = body.card_template
    row.field_schema = field_schema
    row.filterable_fields = filterable
    row.review_policy = review_policy
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return _out(row, ctx.org_id)


@router.delete("/entity-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity_type(type_id: UUID, session: Session, ctx: WriterCtx) -> None:
    row = await _own_type_or_404(session, ctx.org_id, type_id)
    used = (
        await session.execute(
            select(KbEntity.id)
            .where(
                KbEntity.org_id == ctx.org_id,
                KbEntity.entity_type_key == row.type_key,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if used is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "该类型下已有实体数据,不可删除"
        )
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# 通用实体 CRUD
# ---------------------------------------------------------------------------


class EntityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type_key: str = Field(min_length=2, max_length=50)
    attributes: dict


class EntityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attributes: dict


class EntityOut(BaseModel):
    id: UUID
    kb_id: UUID
    entity_type_key: str
    name: str
    attributes: dict
    snapshot_id: UUID | None
    created_at: datetime | None


async def _writable_kb_or_409(
    session: AsyncSession, org_id: UUID, kb_id: UUID
) -> KnowledgeBase:
    kb = (
        await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
    ).scalar_one_or_none()
    if kb is None or kb.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    if kb.lifecycle_status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "kb_not_active",
                "message": "知识库当前不可写",
                "lifecycle_status": kb.lifecycle_status,
            },
        )
    if kb.active_snapshot_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该库由快照管理,实体请经摄入→审核→投影链路进入(不可直写)",
        )
    return kb


@router.get("/bases/{kb_id}/entities", response_model=list[EntityOut])
async def list_entities(
    kb_id: UUID,
    session: Session,
    ctx: Ctx,
    entity_type_key: str | None = None,
) -> list[EntityOut]:
    stmt = (
        select(KbEntity)
        .where(
            KbEntity.kb_id == kb_id,
            active_projection_filter(KbEntity),
            live_snapshot_projection_filter(KbEntity, "kb_entity"),
        )
        .order_by(KbEntity.name)
    )
    if entity_type_key:
        stmt = stmt.where(KbEntity.entity_type_key == entity_type_key)
    rows = (await session.execute(stmt)).scalars().all()
    return [EntityOut.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/bases/{kb_id}/entities", response_model=EntityOut, status_code=status.HTTP_201_CREATED
)
async def create_entity(
    kb_id: UUID, body: EntityIn, session: Session, ctx: WriterCtx
) -> EntityOut:
    await _writable_kb_or_409(session, ctx.org_id, kb_id)
    entity_type = await get_entity_type(session, ctx.org_id, body.entity_type_key)
    if entity_type is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"未注册的实体类型:{body.entity_type_key}",
        )
    try:
        name = validate_entity_attributes(entity_type, body.attributes)
    except EntityValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    row = KbEntity(
        org_id=ctx.org_id,
        kb_id=kb_id,
        entity_type_key=entity_type.type_key,
        name=name,
        attributes=body.attributes,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return EntityOut.model_validate(row, from_attributes=True)


@router.patch("/bases/{kb_id}/entities/{entity_id}", response_model=EntityOut)
async def update_entity(
    kb_id: UUID,
    entity_id: UUID,
    body: EntityPatch,
    session: Session,
    ctx: WriterCtx,
) -> EntityOut:
    await _writable_kb_or_409(session, ctx.org_id, kb_id)
    row = (
        await session.execute(
            select(KbEntity).where(KbEntity.id == entity_id, KbEntity.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体不存在")
    entity_type = await get_entity_type(session, ctx.org_id, row.entity_type_key)
    if entity_type is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"实体所属类型 {row.entity_type_key} 已不可用"
        )
    try:
        row.name = validate_entity_attributes(entity_type, body.attributes)
    except EntityValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    row.attributes = body.attributes
    await session.commit()
    await session.refresh(row)
    return EntityOut.model_validate(row, from_attributes=True)


@router.delete("/bases/{kb_id}/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity(
    kb_id: UUID, entity_id: UUID, session: Session, ctx: WriterCtx
) -> None:
    await _writable_kb_or_409(session, ctx.org_id, kb_id)
    row = (
        await session.execute(
            select(KbEntity).where(KbEntity.id == entity_id, KbEntity.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体不存在")
    await session.delete(row)
    await session.commit()
