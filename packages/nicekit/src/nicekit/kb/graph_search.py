"""Optional 1-2 hop recall over the active governed graph projection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb.graph import (
    _FactSupportView,
    _load_visible_graph_projection,
    _support_is_current,
    _VisibleGraphProjection,
)
from nicekit.kb.projections import parse_card_ref
from nicekit.kb.retrieval_projection import card_kind_for_predicate
from nicekit.models.kb import (
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    GraphDirection,
    KbChunk,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    SnapshotFactSupport,
    SourceDocument,
)

#: 恢复模式:``card`` 是"检索卡片 → 结构化实体行"的旧通道(search.py 走
#: ``_restore_card_hits``);``chunk`` 是新增的"图谱实体 → 原始证据切片"通道,
#: 命中的是普通 chunk 行,消费侧必须按普通 chunk 还原(见模块尾部契约说明)。
CARD_RESTORE = "card"
CHUNK_RESTORE = "chunk"
#: chunk 模式候选统一用的 kind;与 search.py 里普通 chunk 候选键 ``("chunk", chunk.id)``
#: 同形,融合时天然与 sparse/dense 命中的同一 chunk 合并。
CHUNK_CANDIDATE_KIND = "chunk"


def _fts_config():
    """全文检索配置名参数化(settings.kb_fts_regconfig,baseline 迁移已建)。"""
    return literal_column(f"'{get_settings().kb_fts_regconfig}'::regconfig")


def _seed_tsquery(query: str):
    """种子发现用**析取**匹配实体名,而不是 websearch 默认的合取。

    自然问句里实体名只占一个词:"modelMemory 是干嘛的"会被编译成
    ``'modelmemory' & '是' <-> '干'``,要求实体名同时含疑问词,必然零种子——
    图谱一路因此对所有问句形态失效。实体名是专名,析取不会被功能词带偏
    (没有实体叫"是"),命中后还有边扩展、时效剪枝与证据校验层层收紧。

    做法是把编译结果里的 ``&`` 换成 ``|`` 再解析回 tsquery:此时词元已被
    词典归一并带引号,``to_tsquery`` 不会二次分词。空查询原样得到空 tsquery。
    """
    compiled = cast(func.websearch_to_tsquery(_fts_config(), query), Text)
    return func.to_tsquery(_fts_config(), func.replace(compiled, "&", "|"))


@dataclass(frozen=True, slots=True)
class GraphRecallCandidate:
    chunk: KbChunk
    kind: str
    entity_id: UUID
    score: float
    hops: int
    edge_ids: tuple[UUID, ...]
    predicates: tuple[str, ...]
    #: ``CARD_RESTORE``(默认,保持旧消费契约)或 ``CHUNK_RESTORE``。
    restore_mode: str = CARD_RESTORE
    #: chunk 模式下命中的图谱锚点实体(卡片模式与 ``entity_id`` 同义,故可为 None)。
    anchor_entity_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Path:
    score: float
    hops: int
    snapshot_id: UUID
    edge_ids: tuple[UUID, ...]
    predicates: tuple[str, ...]


def _better(candidate: _Path, previous: _Path | None) -> bool:
    if previous is None:
        return True
    return (-candidate.score, candidate.hops, candidate.edge_ids) < (
        -previous.score,
        previous.hops,
        previous.edge_ids,
    )


def _sort_key(item: GraphRecallCandidate) -> tuple[float, int, int, str, int]:
    """同分同跳时卡片优先(信息密度更高),再按 kind / id 定序保证结果稳定。"""
    return (
        -item.score,
        item.hops,
        0 if item.restore_mode == CARD_RESTORE else 1,
        item.kind,
        item.entity_id.int,
    )


async def _current_fact_support_views(
    session: AsyncSession,
    support_ids: set[UUID],
    active_snapshot_keys: list[tuple[UUID, UUID]],
) -> dict[UUID, _FactSupportView]:
    """按 graph.py 同一口径复核 fact support 的现时有效性。

    join 条件与 :func:`nicekit.kb.graph._load_visible_graph_projection` 逐条对齐
    (org/kb 全程同行校验),再套用 :func:`nicekit.kb.graph._support_is_current`
    的文档生命周期 / revision 状态 / 事实审核状态三重闸门——恢复路径换了,
    可见性判定的强度不变。
    """

    if not support_ids or not active_snapshot_keys:
        return {}
    statement = (
        select(SnapshotFactSupport, EvidenceSpan, DocumentRevision, SourceDocument, FactClaim)
        .join(
            EvidenceSpan,
            and_(
                EvidenceSpan.id == SnapshotFactSupport.evidence_span_id,
                EvidenceSpan.fact_claim_id == SnapshotFactSupport.fact_claim_id,
                EvidenceSpan.revision_id == SnapshotFactSupport.revision_id,
                EvidenceSpan.org_id == SnapshotFactSupport.org_id,
                EvidenceSpan.kb_id == SnapshotFactSupport.kb_id,
            ),
        )
        .join(
            DocumentRevision,
            and_(
                DocumentRevision.id == SnapshotFactSupport.revision_id,
                DocumentRevision.doc_id == SnapshotFactSupport.doc_id,
                DocumentRevision.org_id == SnapshotFactSupport.org_id,
                DocumentRevision.kb_id == SnapshotFactSupport.kb_id,
            ),
        )
        .join(
            SourceDocument,
            and_(
                SourceDocument.id == SnapshotFactSupport.doc_id,
                SourceDocument.org_id == SnapshotFactSupport.org_id,
                SourceDocument.kb_id == SnapshotFactSupport.kb_id,
            ),
        )
        .join(
            FactClaim,
            and_(
                FactClaim.id == SnapshotFactSupport.fact_claim_id,
                FactClaim.org_id == SnapshotFactSupport.org_id,
                FactClaim.kb_id == SnapshotFactSupport.kb_id,
            ),
        )
        .where(
            SnapshotFactSupport.id.in_(list(support_ids)),
            tuple_(
                SnapshotFactSupport.kb_id,
                SnapshotFactSupport.snapshot_id,
            ).in_(active_snapshot_keys),
        )
        .order_by(
            SnapshotFactSupport.snapshot_id,
            SnapshotFactSupport.fact_claim_id,
            SnapshotFactSupport.evidence_span_id,
        )
    )
    views = [
        _FactSupportView(support, evidence, revision, document, claim)
        for support, evidence, revision, document, claim in (
            await session.execute(statement)
        ).all()
    ]
    return {view.support.id: view for view in views if _support_is_current(view)}


def _chunk_covers_evidence(chunk: KbChunk, evidence: EvidenceSpan) -> bool:
    """判断某 chunk 是否承载了这条证据的原文(不做任何模糊猜测)。

    优先用显式外键 ``chunk_id``;没有时退回同一 revision 内的锚点重叠——行区间
    相交,或(无行锚时)页码相同。任何一侧锚点缺失即判否,宁可少召回也不猜。
    """

    if evidence.chunk_id is not None:
        return chunk.id == evidence.chunk_id
    if evidence.image_asset_id is not None:
        return chunk.image_asset_id == evidence.image_asset_id
    if None not in (evidence.start_line, evidence.end_line, chunk.start_line, chunk.end_line):
        if int(chunk.start_line) > int(evidence.end_line) or int(chunk.end_line) < int(
            evidence.start_line
        ):
            return False
        return evidence.page is None or chunk.page is None or chunk.page == evidence.page
    if evidence.page is not None and chunk.page is not None:
        return chunk.page == evidence.page
    return False


async def _evidence_chunk_candidates(
    session: AsyncSession,
    *,
    discovered: dict[UUID, _Path],
    active_snapshot_keys: list[tuple[UUID, UUID]],
) -> list[GraphRecallCandidate]:
    """图谱实体 → 节点 fact 支撑 → 证据跨度 → 原始 chunk。

    普通文档摄入只产出 ``entity_mention`` 与关系谓词事实,这两类**刻意**不建
    检索卡片(见 ingestion.py 的投影分工),因此"卡片是唯一恢复路径"会让图谱
    这一路恒空。这里补上第二条恢复路径:直接落回证据所在的原文切片,既不需要
    为每条提及重复建卡,又让图谱扩展出的邻居实体带回真正可读的上下文。
    """

    if not discovered or not active_snapshot_keys:
        return []
    support_rows = (
        await session.execute(
            select(
                KbSnapshotEntityNodeSupport.entity_id,
                KbSnapshotEntityNodeSupport.snapshot_id,
                KbSnapshotEntityNodeSupport.fact_support_id,
            )
            .join(
                KbSnapshotEntityNode,
                and_(
                    KbSnapshotEntityNode.org_id == KbSnapshotEntityNodeSupport.org_id,
                    KbSnapshotEntityNode.kb_id == KbSnapshotEntityNodeSupport.kb_id,
                    KbSnapshotEntityNode.snapshot_id
                    == KbSnapshotEntityNodeSupport.snapshot_id,
                    KbSnapshotEntityNode.entity_id == KbSnapshotEntityNodeSupport.entity_id,
                ),
            )
            .where(
                KbSnapshotEntityNodeSupport.entity_id.in_(list(discovered)),
                KbSnapshotEntityNodeSupport.support_type == "fact",
                KbSnapshotEntityNodeSupport.fact_support_id.is_not(None),
                tuple_(
                    KbSnapshotEntityNodeSupport.kb_id,
                    KbSnapshotEntityNodeSupport.snapshot_id,
                ).in_(active_snapshot_keys),
            )
            .order_by(
                KbSnapshotEntityNodeSupport.snapshot_id,
                KbSnapshotEntityNodeSupport.entity_id,
                KbSnapshotEntityNodeSupport.fact_support_id,
            )
        )
    ).all()
    entity_ids_by_support: dict[UUID, set[UUID]] = defaultdict(set)
    for entity_id, snapshot_id, fact_support_id in support_rows:
        path = discovered.get(entity_id)
        # 节点支撑必须落在这条图路径所属的同一快照里,跨快照拼接一律丢弃
        if path is None or fact_support_id is None or path.snapshot_id != snapshot_id:
            continue
        entity_ids_by_support[fact_support_id].add(entity_id)
    if not entity_ids_by_support:
        return []

    views = await _current_fact_support_views(
        session, set(entity_ids_by_support), active_snapshot_keys
    )
    if not views:
        return []
    # 证据 → 可归因的锚点实体(必须是该事实的主语或宾语,与 graph.py 的节点支撑同规则)
    anchors_by_evidence: dict[UUID, tuple[_FactSupportView, set[UUID]]] = {}
    for support_id, entity_ids in entity_ids_by_support.items():
        view = views.get(support_id)
        if view is None:
            continue
        claim_endpoints = {view.claim.subject_entity_id, view.claim.object_entity_id}
        anchors = {
            entity_id
            for entity_id in entity_ids
            if entity_id in claim_endpoints
            and discovered[entity_id].snapshot_id == view.support.snapshot_id
        }
        if not anchors:
            continue
        current = anchors_by_evidence.setdefault(view.evidence.id, (view, set()))
        current[1].update(anchors)
    if not anchors_by_evidence:
        return []

    revision_ids = {view.evidence.revision_id for view, _anchors in anchors_by_evidence.values()}
    chunks = list(
        (
            await session.execute(
                select(KbChunk).where(
                    KbChunk.revision_id.in_(list(revision_ids)),
                    tuple_(KbChunk.kb_id, KbChunk.snapshot_id).in_(active_snapshot_keys),
                    KbChunk.quarantined.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    chunks_by_revision: dict[UUID, list[KbChunk]] = defaultdict(list)
    for chunk in chunks:
        # 卡片行走 card 通道恢复,不重复进 chunk 通道
        if chunk.revision_id is None or parse_card_ref(chunk.source_ref) is not None:
            continue
        chunks_by_revision[chunk.revision_id].append(chunk)

    best: dict[UUID, tuple[KbChunk, _Path, UUID]] = {}
    for view, anchors in anchors_by_evidence.values():
        support = view.support
        for chunk in chunks_by_revision.get(view.evidence.revision_id, ()):
            if (
                chunk.org_id != support.org_id
                or chunk.kb_id != support.kb_id
                or chunk.snapshot_id != support.snapshot_id
                or not _chunk_covers_evidence(chunk, view.evidence)
            ):
                continue
            for anchor_id in sorted(anchors, key=lambda item: item.int):
                path = discovered[anchor_id]
                if path.snapshot_id != chunk.snapshot_id:
                    continue
                previous = best.get(chunk.id)
                if previous is None or _better(path, previous[1]):
                    best[chunk.id] = (chunk, path, anchor_id)
    return [
        GraphRecallCandidate(
            chunk=chunk,
            kind=CHUNK_CANDIDATE_KIND,
            entity_id=chunk.id,
            score=path.score,
            hops=path.hops,
            edge_ids=path.edge_ids,
            predicates=path.predicates,
            restore_mode=CHUNK_RESTORE,
            anchor_entity_id=anchor_id,
        )
        for chunk, path, anchor_id in best.values()
    ]


async def _entity_card_candidates(
    session: AsyncSession,
    *,
    discovered: dict[UUID, _Path],
    projection: _VisibleGraphProjection,
) -> list[GraphRecallCandidate]:
    """图谱实体 → 已确认事实 → 检索卡片(wiki 页 / 注册结构化实体类型)。"""

    supported_fact_ids = {
        fact_id
        for snapshot_id in {path.snapshot_id for path in discovered.values()}
        for fact_id in projection.supported_fact_ids_by_snapshot.get(snapshot_id, ())
    }
    if not supported_fact_ids:
        return []
    claims = list(
        (
            await session.execute(
                select(FactClaim).where(
                    FactClaim.subject_entity_id.in_(list(discovered)),
                    FactClaim.id.in_(list(supported_fact_ids)),
                    FactClaim.review_status == "confirmed",
                )
            )
        )
        .scalars()
        .all()
    )
    claims_by_id = {claim.id: claim for claim in claims}
    if not claims_by_id:
        return []
    snapshot_ids = {path.snapshot_id for path in discovered.values()}
    cards = list(
        (
            await session.execute(
                select(KbChunk).where(
                    KbChunk.snapshot_id.in_(snapshot_ids),
                    KbChunk.quarantined.is_(False),
                    KbChunk.meta["fact_claim_id"].astext.in_(
                        [str(claim_id) for claim_id in claims_by_id]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    result: list[GraphRecallCandidate] = []
    for card in cards:
        raw_claim_id: Any = (card.meta or {}).get("fact_claim_id")
        try:
            claim = claims_by_id[UUID(str(raw_claim_id))]
        except (KeyError, TypeError, ValueError):
            continue
        path = discovered.get(claim.subject_entity_id) if claim.subject_entity_id else None
        card_ref = parse_card_ref(card.source_ref)
        if (
            path is None
            or card.snapshot_id != path.snapshot_id
            or claim.id not in projection.supported_fact_ids_by_snapshot.get(path.snapshot_id, ())
            or card_ref is None
            # 卡片 kind 与谓词并不总相等:wiki_page 建卡时改名成 "page"(对齐
            # KbPage / search.PAGE_KIND),自定义实体类型才是 kind == predicate。
            # 旧实现拿 kind 直接比谓词,于是 wiki 卡片 100% 被静默丢弃;这里改用
            # 与建卡侧同一把 card_kind_for_predicate,两侧命名规则只有一份定义。
            or card_ref[0] != card_kind_for_predicate(claim.predicate)
        ):
            continue
        result.append(
            GraphRecallCandidate(
                chunk=card,
                kind=card_ref[0],
                entity_id=card_ref[1],
                score=path.score,
                hops=path.hops,
                edge_ids=path.edge_ids,
                predicates=path.predicates,
                restore_mode=CARD_RESTORE,
                anchor_entity_id=claim.subject_entity_id,
            )
        )
    return result


async def graph_recall_candidates(
    session: AsyncSession,
    query: str,
    *,
    kb_ids: list[UUID] | None,
    max_hops: int,
    top_k: int,
    as_of: date,
) -> list[GraphRecallCandidate]:
    """Expand only current visible nodes/edges and restore supported snapshot rows.

    恢复路径有两条,同一批扩展结果共用:卡片路径(结构化实体 / wiki 页)与证据
    切片路径(普通文档的实体提及与关系事实)。两条都以"当前 active 快照 + 现时
    有效支撑"为前提,谁都不能绕过 :func:`_load_visible_graph_projection` 的围栏。
    """

    if max_hops not in (1, 2):
        raise ValueError("graph max_hops must be 1 or 2")
    if top_k <= 0:
        raise ValueError("graph top_k must be positive")
    projection = await _load_visible_graph_projection(session, kb_ids)
    visible_entity_ids = set(projection.node_by_entity_id)
    if not visible_entity_ids:
        return []

    tsquery = _seed_tsquery(query)
    frozen_name_tsv = func.to_tsvector(
        _fts_config(),
        KbSnapshotEntityNode.display_name,
    )
    rank = func.ts_rank_cd(frozen_name_tsv, tsquery)
    active_snapshot_keys = list(projection.snapshot_id_by_kb_id.items())
    seed_statement = (
        select(KbSnapshotEntityNode.entity_id)
        .where(
            KbSnapshotEntityNode.entity_id.in_(list(visible_entity_ids)),
            tuple_(
                KbSnapshotEntityNode.kb_id,
                KbSnapshotEntityNode.snapshot_id,
            ).in_(active_snapshot_keys),
            frozen_name_tsv.op("@@")(tsquery),
        )
        .order_by(rank.desc(), KbSnapshotEntityNode.entity_id)
        .limit(top_k)
    )
    seed_ids = set((await session.execute(seed_statement)).scalars().all())
    if not seed_ids:
        return []

    frontier: dict[UUID, _Path | None] = {seed_id: None for seed_id in seed_ids}
    discovered: dict[UUID, _Path] = {}
    for hop in range(1, max_hops + 1):
        frontier_ids = set(frontier)
        next_frontier: dict[UUID, _Path] = {}
        for edge in projection.edge_rows:
            if (edge.valid_from is not None and edge.valid_from > as_of) or (
                edge.valid_to is not None and edge.valid_to < as_of
            ):
                continue
            if edge.src_entity_id in frontier_ids:
                source_id, target_id = edge.src_entity_id, edge.dst_entity_id
            elif (
                str(getattr(edge.direction, "value", edge.direction))
                == GraphDirection.UNDIRECTED.value
                and edge.dst_entity_id in frontier_ids
            ):
                source_id, target_id = edge.dst_entity_id, edge.src_entity_id
            else:
                continue
            source_path = frontier[source_id]
            if source_path is not None and source_path.snapshot_id != edge.snapshot_id:
                continue
            score = float(edge.weight) * (source_path.score if source_path else 1.0)
            path = _Path(
                score=score,
                hops=hop,
                snapshot_id=edge.snapshot_id,
                edge_ids=(*(source_path.edge_ids if source_path else ()), edge.id),
                predicates=(
                    *(source_path.predicates if source_path else ()),
                    str(edge.predicate),
                ),
            )
            if target_id in seed_ids or not _better(path, discovered.get(target_id)):
                continue
            discovered[target_id] = path
            next_frontier[target_id] = path
        frontier = next_frontier
        if not frontier:
            break
    if not discovered:
        return []

    cards = await _entity_card_candidates(
        session, discovered=discovered, projection=projection
    )
    # 卡片优先、逐实体回落:有卡片的实体已经有更凝练的表示,不再重复带出它的
    # 原文切片(避免同一实体在结果里出现两遍);只有拿不到卡片的实体才走证据
    # 切片路径——普通文档的实体提及与关系事实按设计就不建卡,全部落在这一档。
    carded_entity_ids = {card.anchor_entity_id for card in cards}
    uncarded = {
        entity_id: path
        for entity_id, path in discovered.items()
        if entity_id not in carded_entity_ids
    }
    result = [
        *cards,
        *await _evidence_chunk_candidates(
            session,
            discovered=uncarded,
            active_snapshot_keys=active_snapshot_keys,
        ),
    ]
    return sorted(result, key=_sort_key)[:top_k]


__all__ = [
    "CARD_RESTORE",
    "CHUNK_CANDIDATE_KIND",
    "CHUNK_RESTORE",
    "GraphRecallCandidate",
    "graph_recall_candidates",
]
