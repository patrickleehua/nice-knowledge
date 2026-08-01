"""抽取实体的归一与绑定 —— 跨文档串联的接点。

图谱节点是 canonical_entities,图边要求事实已绑定 subject_entity_id
(graph_projection._source_entity_id);在此之前实体只能人工新建、人工挑选绑定,
于是抽取出来的事实永远连不成图。本模块把这一步自动化:

- 归一:实体名与别名走 entity_aliases 规范化查找,命中即复用同一 canonical
  entity —— 同名实体在不同文档里指向同一节点,这就是「文档之间串起来」的接点;
  未命中才新建实体并登记别名。
- 绑定:实体事实绑 subject_entity_id;关系事实把两端解析到
  subject_entity_id/object_entity_id。兼容 payload 继续保留 target_entity_id
  作为审计副本,但投影只认正规化外键。
- 时机:只在 AI 审核判定通过时调用(fact_review_ai),绑定失败一律不 confirm,
  改为 escalate 留人工 —— 否则未绑定的关系事实会让整个快照构建失败。
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.entity_resolution import (
    EntityConflictError,
    add_entity_alias,
    create_canonical_entity,
    normalize_alias,
)
from nicekit.kb.entity_types import FALLBACK_TYPE_KEY
from nicekit.models.kb import (
    CanonicalEntity,
    EntityAlias,
    FactClaim,
    GraphPredicate,
    KbEntityType,
)

logger = logging.getLogger(__name__)

# 实体事实的谓词(与关系谓词、SQL/wiki 投影谓词都不重叠)
ENTITY_PREDICATE = "entity_mention"
# 关系谓词:直接边谓词全集,shared_context 由投影自行推导,不作为抽取输出
RELATION_PREDICATES = frozenset(
    predicate.value
    for predicate in GraphPredicate
    if predicate is not GraphPredicate.SHARED_CONTEXT
)

# 抽取白名单的兜底项(MIGRATION-PLAN B17/B18):SDK 只保留领域无关的 concept,
# 其余类型全部由 KbEntityType 注册表(平台内置 + 本 org 自定义)动态供给。
BUILTIN_ENTITY_TYPES: dict[str, str] = {
    FALLBACK_TYPE_KEY: "有知识价值的主题概念",
}


async def allowed_entity_types(session: AsyncSession, org_id: UUID) -> dict[str, str]:
    """兜底白名单 + 注册表可见类型(平台内置 + 本 org 自定义,同 key 时自定义覆盖)。"""
    allowed = dict(BUILTIN_ENTITY_TYPES)
    rows = (await session.execute(select(KbEntityType))).scalars().all()
    for row in sorted(rows, key=lambda r: r.org_id == org_id):
        if row.org_id == org_id or row.is_builtin:
            allowed[row.type_key] = row.description or row.display_name
    return allowed


async def resolve_entity(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    name: str,
    type_key: str,
    aliases: list[str] | tuple[str, ...] = (),
    summary: str | None = None,
) -> CanonicalEntity:
    """按别名归一化查找既有实体,命中即复用,否则新建并登记别名。"""
    normalized = normalize_alias(name)
    existing_id = await session.scalar(
        select(EntityAlias.entity_id).where(
            EntityAlias.kb_id == kb_id,
            EntityAlias.normalized_alias == normalized,
        )
    )
    entity: CanonicalEntity | None = None
    if existing_id is not None:
        entity = await session.get(CanonicalEntity, existing_id)
    if entity is None:
        entity = await create_canonical_entity(
            session,
            org_id=org_id,
            kb_id=kb_id,
            entity_type=type_key,
            canonical_name=name,
            metadata={"summary": summary} if summary else None,
        )
    for alias in aliases:
        if not str(alias).strip():
            continue
        try:
            await add_entity_alias(session, entity=entity, alias=str(alias), source="ai")
        except EntityConflictError:
            # 别名已指向另一个实体:保留既有归一,不因单个别名冲突阻断绑定
            logger.info("entity alias %r already resolves elsewhere (kb=%s), skipped", alias, kb_id)
    return entity


def _payload(claim: FactClaim) -> dict:
    return dict(
        claim.corrected_payload if claim.corrected_payload is not None else claim.value_json
    )


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if isinstance(value, str) else ""


def _aliases(payload: dict) -> list[str]:
    raw = payload.get("aliases")
    return [str(item) for item in raw if str(item).strip()] if isinstance(raw, list) else []


def is_bindable(claim: FactClaim) -> bool:
    """该事实是否属于需要实体绑定的实体/关系事实。"""
    return claim.predicate == ENTITY_PREDICATE or claim.predicate in RELATION_PREDICATES


async def bind_claim_entities(
    session: AsyncSession, claim: FactClaim, *, org_id: UUID
) -> str | None:
    """给实体/关系事实完成归一绑定,成功返回 None,失败返回原因文本。

    调用方(AI 审核)拿到原因即改判 escalate:未绑定的关系事实会让 graph 投影
    直接失败,宁可留人工也不能让整库快照构建不出来。
    """
    if claim.org_id != org_id:
        return "事实与当前租户不一致,无法归一"
    payload = _payload(claim)
    if claim.predicate == ENTITY_PREDICATE:
        name = _text(payload, "name")
        if not name:
            return "实体事实缺少 name,无法归一"
        entity = await resolve_entity(
            session,
            org_id=org_id,
            kb_id=claim.kb_id,
            name=name,
            type_key=_text(payload, "type_key") or FALLBACK_TYPE_KEY,
            aliases=_aliases(payload),
            summary=_text(payload, "summary") or None,
        )
        claim.subject_entity_id = entity.id
        session.add(claim)
        await session.flush()
        return None

    legacy_target = payload.get("target_entity_id")
    legacy_target_id: UUID | None = None
    if legacy_target is not None:
        try:
            legacy_target_id = UUID(str(legacy_target))
        except (TypeError, ValueError, AttributeError):
            return "关系事实包含无效 target_entity_id,无法归一"

    candidate_object_id = claim.object_entity_id or legacy_target_id
    if claim.subject_entity_id is not None and candidate_object_id is not None:
        source = await session.get(CanonicalEntity, claim.subject_entity_id)
        target = await session.get(CanonicalEntity, candidate_object_id)
        if source is None or target is None:
            return "关系实体不存在,无法归一"
        if source.id == target.id:
            return "关系两端归一到同一实体,无法成边"
        if (source.org_id, source.kb_id) != (claim.org_id, claim.kb_id) or (
            target.org_id,
            target.kb_id,
        ) != (claim.org_id, claim.kb_id):
            return "关系实体与事实不属于同一知识库,无法归一"
        if legacy_target_id is not None and legacy_target_id != target.id:
            return "关系事实 target_entity_id 与规范化目标不一致,无法归一"
        claim.object_entity_id = target.id
        claim.corrected_payload = {**payload, "target_entity_id": str(target.id)}
        session.add(claim)
        await session.flush()
        return None

    source_name = _text(payload, "source_name")
    target_name = _text(payload, "target_name")
    if not source_name or not target_name:
        return "关系事实缺少 source_name/target_name,无法归一"
    source = await resolve_entity(
        session,
        org_id=org_id,
        kb_id=claim.kb_id,
        name=source_name,
        type_key=_text(payload, "source_type_key") or FALLBACK_TYPE_KEY,
    )
    target = await resolve_entity(
        session,
        org_id=org_id,
        kb_id=claim.kb_id,
        name=target_name,
        type_key=_text(payload, "target_type_key") or FALLBACK_TYPE_KEY,
    )
    if source.id == target.id:
        # 两端归一到同一实体(同义写法),自环边会让 graph 投影失败
        return "关系两端归一到同一实体,无法成边"
    if (source.org_id, source.kb_id) != (claim.org_id, claim.kb_id) or (
        target.org_id,
        target.kb_id,
    ) != (claim.org_id, claim.kb_id):
        return "关系实体与事实不属于同一知识库,无法归一"
    if claim.subject_entity_id is not None and claim.subject_entity_id != source.id:
        return "关系事实 subject_entity_id 与规范化主体不一致,无法归一"
    if claim.object_entity_id is not None and claim.object_entity_id != target.id:
        return "关系事实 object_entity_id 与规范化目标不一致,无法归一"
    if legacy_target_id is not None and legacy_target_id != target.id:
        return "关系事实 target_entity_id 与规范化目标不一致,无法归一"
    claim.subject_entity_id = source.id
    claim.object_entity_id = target.id
    claim.corrected_payload = {**payload, "target_entity_id": str(target.id)}
    session.add(claim)
    await session.flush()
    return None
