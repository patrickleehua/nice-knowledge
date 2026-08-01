"""通用实体结构化查询:按类型 filterable_fields 声明动态构建 JSONB 条件。

结构化取数的正交入口——不走语义检索,直接对 kb_entities 做声明字段的
精确/范围过滤;RLS(org/共享/快照可见性)自动生效,因此天然只读
当前 active 快照的物化行(或非快照库的直写行)。

安全边界:filter 键必须在类型 filterable_fields 中声明,未声明的键一律拒绝
(防任意 JSONB 探测);text 走精确等值,number/date 支持等值与 min/max 范围。

已知限制:JSONB attributes->>field 的 astext(+number cast)表达式条件不走
索引(GIN 只服务包含查询),当前实体量级(单类型千行内)顺扫可接受;
量级上来后再按热点字段建表达式索引,现阶段不建。
"""

from uuid import UUID

from sqlalchemy import Numeric, cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.kb.effective_scope import live_snapshot_projection_filter
from nicekit.kb.entity_types import get_entity_type
from nicekit.kb.projections import active_projection_filter
from nicekit.models.kb import KbEntity, KbEntityType

_RANGE_SUFFIXES = ("__min", "__max")


class EntityLookupError(ValueError):
    """filter 键未声明或值类型不合法。"""


def _declared(entity_type: KbEntityType) -> dict[str, str]:
    return {
        str(item["field"]): str(item.get("type") or "text")
        for item in entity_type.filterable_fields or []
        if isinstance(item, dict) and item.get("field")
    }


def build_entity_filters(entity_type: KbEntityType, filters: dict) -> list:
    """filters → SQLAlchemy 条件列表。
    键形态:声明字段名(等值),number/date 字段另支持 `<field>__min` / `<field>__max`。"""
    declared = _declared(entity_type)
    criteria = []
    for key, value in filters.items():
        if value is None:
            continue
        field, op = key, "eq"
        for suffix in _RANGE_SUFFIXES:
            if key.endswith(suffix):
                field, op = key[: -len(suffix)], suffix[2:]
                break
        ftype = declared.get(field)
        if ftype is None:
            raise EntityLookupError(
                f"字段 {field} 未在类型 {entity_type.type_key} 的可过滤声明中"
            )
        column = KbEntity.attributes[field].astext
        if ftype == "number":
            column = cast(column, Numeric)
            value = float(value)
        elif op != "eq" and ftype == "text":
            raise EntityLookupError(f"text 字段 {field} 不支持范围过滤")
        else:
            value = str(value)
        if op == "eq":
            criteria.append(column == value)
        elif op == "min":
            criteria.append(column >= value)
        else:
            criteria.append(column <= value)
    return criteria


async def lookup_entities(
    session: AsyncSession,
    org_id: UUID,
    type_key: str,
    filters: dict | None = None,
    *,
    limit: int = 50,
) -> list[KbEntity]:
    """按类型 + 声明字段过滤查询通用实体(RLS 定界,active 快照行可见)。
    类型未注册抛 EntityLookupError。"""
    entity_type = await get_entity_type(session, org_id, type_key)
    if entity_type is None:
        raise EntityLookupError(f"未注册的实体类型:{type_key}")
    stmt = (
        select(KbEntity)
        .where(
            KbEntity.entity_type_key == entity_type.type_key,
            active_projection_filter(KbEntity),
            live_snapshot_projection_filter(KbEntity, "kb_entity"),
        )
        .order_by(KbEntity.name)
        .limit(limit)
    )
    for criterion in build_entity_filters(entity_type, filters or {}):
        stmt = stmt.where(criterion)
    return list((await session.execute(stmt)).scalars().all())
