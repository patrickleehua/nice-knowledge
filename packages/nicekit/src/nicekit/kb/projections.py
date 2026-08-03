"""Snapshot-scoped SQL and Wiki projection materializers."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from sqlalchemy import ColumnElement, and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb.effective_scope import (
    active_knowledge_base_membership_filter,
)
from nicekit.kb.entity_binding import ENTITY_PREDICATE
from nicekit.kb.entity_types import EntityValidationError, validate_entity_attributes
from nicekit.kb.wiki_gen import (
    PAGE_DRAFT_PENDING,
    PAGE_ORIGIN_LLM,
    WIKI_PUBLICATION_STATUS_KEY,
)
from nicekit.models.kb import (
    EvidenceSpan,
    FactClaim,
    GraphPredicate,
    KbChunk,
    KbEntity,
    KbEntityType,
    KbPage,
    KnowledgeBase,
    KnowledgeSnapshot,
    SnapshotFactSupport,
    SnapshotProjectionSupport,
)

if TYPE_CHECKING:
    from nicekit.kb.snapshot import SnapshotBuildContext


#: SQL 投影认领的谓词集合(MIGRATION-PLAN B6):不再有任何硬编码业务谓词,
#: 全部由 KbEntityType 注册表在构建时动态计算(见 load_custom_entity_types)。
SQL_PROJECTION_PREDICATES: frozenset[str] = frozenset()
WIKI_PROJECTION_PREDICATES = frozenset({"wiki_page"})
# 图谱事实(实体提及 + 实体间关系)由 GraphProjectionBuilder 认领:
# 不进 SQL/wiki 投影,但也不算 unsupported
GRAPH_PROJECTION_PREDICATES = frozenset({ENTITY_PREDICATE}) | frozenset(
    predicate.value for predicate in GraphPredicate
)
#: 与实体类型注册表无关的静态谓词;完整的"受支持谓词"要叠加注册类型,
#: 见 :func:`supported_projection_predicates`。
STATIC_PROJECTION_PREDICATES = (
    SQL_PROJECTION_PREDICATES
    | WIKI_PROJECTION_PREDICATES
    | GRAPH_PROJECTION_PREDICATES
)


def supported_projection_predicates(
    entity_types: Mapping[str, KbEntityType],
) -> frozenset[str]:
    """静态谓词 + 已注册实体类型 key;不在其中的 claim 记 unsupported 统计。"""
    return STATIC_PROJECTION_PREDICATES | frozenset(entity_types)


async def load_custom_entity_types(
    session: AsyncSession, org_id: UUID
) -> dict[str, KbEntityType]:
    """通用链路(kb_entities)承载的全部类型:平台内置 + 本 org 自建,
    同 key 时本 org 定义覆盖平台内置。类型被删后的历史 claim 会落 unsupported 统计。"""
    rows = (await session.execute(select(KbEntityType))).scalars().all()
    result: dict[str, KbEntityType] = {}
    for row in sorted(rows, key=lambda r: r.org_id == org_id):  # 本 org 排后 → 覆盖
        if row.org_id == org_id or row.is_builtin:
            result[row.type_key] = row
    return result


class ProjectionPayloadError(ValueError):
    """A supported whole-entity claim cannot be mapped without guessing."""


def active_projection_filter(model: type[Any]) -> ColumnElement[bool]:
    """Match the managed active generation or the pre-3.7 NULL generation."""

    if not hasattr(model, "snapshot_id"):
        raise TypeError("projection model must expose snapshot_id")
    active_snapshot = (
        select(KnowledgeBase.active_snapshot_id)
        .where(
            KnowledgeBase.id == model.kb_id,
            KnowledgeBase.org_id == model.org_id,
        )
        .correlate(model)
        .scalar_subquery()
    )
    managed_active = (
        select(KnowledgeSnapshot.id)
        .join(KnowledgeBase, KnowledgeBase.active_snapshot_id == KnowledgeSnapshot.id)
        .where(
            KnowledgeBase.id == model.kb_id,
            KnowledgeBase.org_id == model.org_id,
            KnowledgeSnapshot.config_manifest.op("?")(
                "required_projection_builders"
            ),
        )
        .correlate(model)
        .exists()
    )
    return and_(
        active_knowledge_base_membership_filter(model),
        or_(
            model.snapshot_id.is_not_distinct_from(active_snapshot),
            and_(model.snapshot_id.is_(None), ~managed_active),
        ),
    )


def dedupe_wiki_claims_by_title(claims: Iterable[FactClaim]) -> list[FactClaim]:
    """wiki 页按 title 唯一:同名保留 created_at 最新的一条(并列时按 id 定序)。

    KbPage 投影与检索卡片投影**必须共用这把去重键**。两边键不一致时,同名 claim
    会各建一张检索卡片,而 KbPage 只留一行,多出来的卡片在检索恢复时反查不到实体
    行,只能被丢弃——白占嵌入成本与候选位,还刷 "实体卡片残留" 告警。
    """
    latest_by_title: dict[str, FactClaim] = {}
    for claim in claims:
        if claim.predicate != "wiki_page":
            continue
        title = _required_text(_effective_payload(claim), "title")
        current = latest_by_title.get(title)
        if current is None or (
            claim.created_at.isoformat() if claim.created_at else "",
            str(claim.id),
        ) > (
            current.created_at.isoformat() if current.created_at else "",
            str(current.id),
        ):
            latest_by_title[title] = claim
    return list(latest_by_title.values())


def projection_row_id(snapshot_id: UUID, projection_type: str, claim_id: UUID) -> UUID:
    """Return an idempotent row id while keeping snapshots physically separate."""

    return uuid5(snapshot_id, f"{projection_type}:{claim_id}")


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionPayloadError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionPayloadError(f"{field} must be a string or null")
    return value.strip() or None


def _optional_number(payload: Mapping[str, Any], field: str) -> int | float | Decimal | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ProjectionPayloadError(f"{field} must be a number or null")
    return value


def _optional_integer(payload: Mapping[str, Any], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionPayloadError(f"{field} must be an integer or null")
    return value


def _optional_date(payload: Mapping[str, Any], field: str) -> date | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ProjectionPayloadError(f"{field} must be an ISO date string or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProjectionPayloadError(f"{field} must be an ISO date string or null") from exc


def _effective_payload(claim: FactClaim) -> dict[str, Any]:
    payload = claim.corrected_payload if claim.corrected_payload is not None else claim.value_json
    if not isinstance(payload, dict):
        raise ProjectionPayloadError(f"claim {claim.id} effective payload must be an object")
    return payload


def _wiki_content_state(
    payload: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None] | None:
    publication_status = payload.get(WIKI_PUBLICATION_STATUS_KEY)
    if publication_status == "rejected":
        return None
    content_markdown = _required_text(payload, "content_markdown")
    if publication_status == "published":
        return content_markdown, None, None
    # 无显式人工决定时:能走到投影的事实已通过事实审核,kb_wiki_auto_publish
    # 开启则直接发布为正文,不再压第二道页面级人工发布(显式 rejected 在上面
    # 永远生效)。关闭开关即回到"确认后仍需逐页点发布"的旧行为。
    if get_settings().kb_wiki_auto_publish:
        return content_markdown, None, None
    return None, content_markdown, PAGE_DRAFT_PENDING


async def _load_claims(
    session: AsyncSession, context: SnapshotBuildContext
) -> list[FactClaim]:
    if not context.fact_claim_ids:
        return []
    claims = list(
        (
            await session.execute(
                select(FactClaim)
                .where(
                    FactClaim.id.in_(context.fact_claim_ids),
                    FactClaim.org_id == context.org_id,
                    FactClaim.kb_id == context.kb_id,
                )
                .order_by(FactClaim.id)
            )
        )
        .scalars()
        .all()
    )
    if len(claims) != len(context.fact_claim_ids):
        raise ProjectionPayloadError("snapshot fact set changed during projection build")
    return claims


async def _evidence_revision_ids(
    session: AsyncSession, claim_ids: Sequence[UUID]
) -> dict[UUID, set[UUID]]:
    if not claim_ids:
        return {}
    rows = (
        await session.execute(
            select(EvidenceSpan.fact_claim_id, EvidenceSpan.revision_id).where(
                EvidenceSpan.fact_claim_id.in_(claim_ids)
            )
        )
    ).all()
    result: dict[UUID, set[UUID]] = defaultdict(set)
    for claim_id, revision_id in rows:
        result[claim_id].add(revision_id)
    return result


def _validate_source_document(
    claim: FactClaim,
    *,
    evidence_revisions: Mapping[UUID, set[UUID]],
    revision_documents: Mapping[UUID, UUID],
) -> UUID:
    if claim.subject_type != "source_document":
        raise ProjectionPayloadError(
            f"claim {claim.id} predicate {claim.predicate!r} requires "
            "subject_type='source_document'"
        )
    matching_documents = {
        revision_documents[revision_id]
        for revision_id in evidence_revisions.get(claim.id, set())
        if revision_id in revision_documents
    }
    if not matching_documents:
        raise ProjectionPayloadError(
            f"claim {claim.id} has no evidence in the candidate revision manifest"
        )
    if claim.subject_id in matching_documents:
        return claim.subject_id
    return min(matching_documents, key=str)


async def _upsert_rows(
    session: AsyncSession, model: type[Any], rows: Sequence[dict[str, Any]]
) -> None:
    if not rows:
        return
    statement = pg_insert(model).values(list(rows))
    immutable = {"id", "created_at"}
    updates = {
        column.name: statement.excluded[column.name]
        for column in model.__table__.columns
        if column.name not in immutable and column.name in rows[0]
    }
    await session.execute(
        statement.on_conflict_do_update(index_elements=[model.id], set_=updates)
    )


async def _replace_projection_supports(
    session: AsyncSession,
    context: SnapshotBuildContext,
    *,
    projection_types: set[str],
    memberships: set[tuple[str, UUID, UUID]],
) -> int:
    """Replace projection provenance with candidate-manifest fact supports."""

    await session.execute(
        delete(SnapshotProjectionSupport).where(
            SnapshotProjectionSupport.snapshot_id == context.snapshot_id,
            SnapshotProjectionSupport.org_id == context.org_id,
            SnapshotProjectionSupport.kb_id == context.kb_id,
            SnapshotProjectionSupport.projection_type.in_(projection_types),
        )
    )
    if not memberships:
        return 0
    claim_ids = {claim_id for _projection_type, _row_id, claim_id in memberships}
    fact_supports = (
        await session.execute(
            select(SnapshotFactSupport.id, SnapshotFactSupport.fact_claim_id)
            .where(
                SnapshotFactSupport.snapshot_id == context.snapshot_id,
                SnapshotFactSupport.org_id == context.org_id,
                SnapshotFactSupport.kb_id == context.kb_id,
                SnapshotFactSupport.fact_claim_id.in_(claim_ids),
            )
            .order_by(
                SnapshotFactSupport.fact_claim_id,
                SnapshotFactSupport.id,
            )
        )
    ).all()
    support_ids_by_claim: dict[UUID, list[UUID]] = defaultdict(list)
    for support_id, claim_id in fact_supports:
        support_ids_by_claim[claim_id].append(support_id)
    unsupported_claim_ids = claim_ids - support_ids_by_claim.keys()
    if unsupported_claim_ids:
        sample = ", ".join(
            str(claim_id)
            for claim_id in sorted(unsupported_claim_ids, key=str)[:20]
        )
        raise ProjectionPayloadError(
            "fact-derived projection has no candidate snapshot support: "
            f"{sample}"
        )

    rows = [
        {
            "id": uuid5(
                context.snapshot_id,
                (
                    f"projection_support:{projection_type}:"
                    f"{projection_row_id}:{support_id}"
                ),
            ),
            "org_id": context.org_id,
            "kb_id": context.kb_id,
            "snapshot_id": context.snapshot_id,
            "projection_type": projection_type,
            "projection_row_id": projection_row_id,
            "fact_support_id": support_id,
        }
        for projection_type, projection_row_id, claim_id in sorted(
            memberships,
            key=lambda item: (item[0], str(item[1]), str(item[2])),
        )
        for support_id in support_ids_by_claim.get(claim_id, ())
    ]
    if not rows:
        return 0
    statement = pg_insert(SnapshotProjectionSupport).values(rows)
    await session.execute(
        statement.on_conflict_do_nothing(
            index_elements=[
                SnapshotProjectionSupport.snapshot_id,
                SnapshotProjectionSupport.projection_type,
                SnapshotProjectionSupport.projection_row_id,
                SnapshotProjectionSupport.fact_support_id,
            ]
        )
    )
    return len(rows)


class SqlProjectionBuilder:
    """把已确认的实体事实物化为 kb_entities 行(领域无关的单一通用路径)。

    MIGRATION-PLAN B6:TF 的 destination/hotel/cost/poi/route_template 五段
    业务分支已删除,任何实体类型都由 KbEntityType 注册表声明,属性整体落
    ``KbEntity.attributes``;物化前复跑一次类型 JSON Schema 强校验——失败即构建
    失败,与"投影不猜测"的既有原则一致(类型定义可能在摄入后被收紧)。
    """

    name = "sql"
    version = "2"

    async def build(
        self, session: AsyncSession, context: SnapshotBuildContext
    ) -> Mapping[str, Any]:
        claims = await _load_claims(session, context)
        entity_types = await load_custom_entity_types(session, context.org_id)
        supported = supported_projection_predicates(entity_types)
        relevant = [claim for claim in claims if claim.predicate in entity_types]
        evidence_revisions = await _evidence_revision_ids(
            session, [claim.id for claim in relevant]
        )
        revision_documents = {
            UUID(item["revision_id"]): UUID(item["doc_id"])
            for item in context.revision_manifest
        }

        projection_memberships: set[tuple[str, UUID, UUID]] = set()
        entity_rows: list[dict[str, Any]] = []
        projected = Counter[str]()
        skipped = Counter[str]()

        for claim in claims:
            if claim.predicate not in entity_types:
                if claim.predicate not in supported:
                    skipped[claim.predicate] += 1
                continue
            source_doc_id = _validate_source_document(
                claim,
                evidence_revisions=evidence_revisions,
                revision_documents=revision_documents,
            )
            payload = _effective_payload(claim)
            type_def = entity_types[claim.predicate]
            try:
                entity_name = validate_entity_attributes(type_def, payload)
            except EntityValidationError as exc:
                raise ProjectionPayloadError(str(exc)) from exc
            row_id = projection_row_id(context.snapshot_id, claim.predicate, claim.id)
            entity_rows.append(
                {
                    "org_id": context.org_id,
                    "kb_id": context.kb_id,
                    "snapshot_id": context.snapshot_id,
                    "id": row_id,
                    "source_doc_id": source_doc_id,
                    "entity_type_key": claim.predicate,
                    "name": entity_name,
                    "attributes": payload,
                }
            )
            projection_memberships.add(("kb_entity", row_id, claim.id))
            projected[claim.predicate] += 1

        await _upsert_rows(session, KbEntity, entity_rows)
        support_count = await _replace_projection_supports(
            session,
            context,
            projection_types={"kb_entity"},
            memberships=projection_memberships,
        )

        return {
            "row_count": len(entity_rows),
            "entity_count": len(entity_rows),
            "entity_type_count": len(projected),
            "support_count": support_count,
            "projected_claims": dict(sorted(projected.items())),
            "unsupported_predicates": dict(sorted(skipped.items())),
        }


class WikiProjectionBuilder:
    name = "wiki"
    version = "2"

    async def build(
        self, session: AsyncSession, context: SnapshotBuildContext
    ) -> Mapping[str, Any]:
        claims = await _load_claims(session, context)
        supported = supported_projection_predicates(
            await load_custom_entity_types(session, context.org_id)
        )
        relevant = dedupe_wiki_claims_by_title(claims)
        evidence_revisions = await _evidence_revision_ids(
            session, [claim.id for claim in relevant]
        )
        revision_documents = {
            UUID(item["revision_id"]): UUID(item["doc_id"])
            for item in context.revision_manifest
        }
        rows: list[dict[str, Any]] = []
        projection_memberships: set[tuple[str, UUID, UUID]] = set()
        skipped = Counter[str]()
        for claim in claims:
            if claim.predicate not in supported:
                skipped[claim.predicate] += 1
        for claim in relevant:
            source_doc_id = _validate_source_document(
                claim,
                evidence_revisions=evidence_revisions,
                revision_documents=revision_documents,
            )
            payload = _effective_payload(claim)
            content_state = _wiki_content_state(payload)
            if content_state is None:
                continue
            meta = payload.get("meta")
            if meta is not None and not isinstance(meta, dict):
                raise ProjectionPayloadError("meta must be an object or null")
            content, draft_content, draft_status = content_state
            row_id = projection_row_id(context.snapshot_id, "wiki_page", claim.id)
            rows.append(
                {
                    "id": row_id,
                    "org_id": context.org_id,
                    "kb_id": context.kb_id,
                    "snapshot_id": context.snapshot_id,
                    "page_type": _required_text(payload, "page_type"),
                    "title": _required_text(payload, "title"),
                    "content": content,
                    "draft_content": draft_content,
                    "draft_status": draft_status,
                    "meta": meta,
                    "origin": PAGE_ORIGIN_LLM,
                    "source_doc_id": source_doc_id,
                    "created_by": None,
                }
            )
            projection_memberships.add(("wiki_page", row_id, claim.id))
        await _upsert_rows(session, KbPage, rows)
        support_count = await _replace_projection_supports(
            session,
            context,
            projection_types={"wiki_page"},
            memberships=projection_memberships,
        )
        return {
            "row_count": len(rows),
            "support_count": support_count,
            "projected_claims": {"wiki_page": len(rows)} if rows else {},
            "unsupported_predicates": dict(sorted(skipped.items())),
        }


# ---------------------------------------------------------------------------
# 实体卡 source_ref 骨架(原 TF kb/entity_cards.py:47-58;B8 整文件删除后保留)
#
# 实体卡是"结构化实体 → 自然语句 chunk"的检索通道:卡片行以
# ``"{entity_type}:{entity_id}"`` 作为 source_ref(delete-then-add 的 upsert 键),
# 检索侧命中卡片行时据此还原为实体命中。卡片正文统一走
# ``entity_types.render_entity_card``(类型 card_template 驱动),不再有逐类型模板。
# 卡片行的物化由 RetrievalProjectionBuilder 负责(retrieval_projection.py)。
# ---------------------------------------------------------------------------

# type token 开放为注册表 key 形态(小写下划线),不枚举任何内置类型;
# UUID 严格匹配保证普通 chunk 的 "{filename}#chunkN" 形态不会误判为卡片。
_CARD_REF_RE = re.compile(
    r"^([a-z][a-z0-9_]{1,49}):([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def card_source_ref(entity_type: str, entity_id: UUID) -> str:
    return f"{entity_type}:{entity_id}"


def parse_card_ref(source_ref: str | None) -> tuple[str, UUID] | None:
    """source_ref 形如 "{entity_type}:{uuid}" 时解析为 (entity_type, id),否则 None。"""
    if not source_ref:
        return None
    match = _CARD_REF_RE.match(source_ref)
    if match is None:
        return None
    return match.group(1), UUID(match.group(2))


async def delete_entity_card(
    session: AsyncSession, entity_type: str, entity_id: UUID, kb_id: UUID
) -> None:
    """删除某实体的卡片 chunk(与 upsert 同一把 source_ref 键)。"""
    await session.execute(
        delete(KbChunk).where(
            KbChunk.kb_id == kb_id,
            KbChunk.source_ref == card_source_ref(entity_type, entity_id),
        )
    )
