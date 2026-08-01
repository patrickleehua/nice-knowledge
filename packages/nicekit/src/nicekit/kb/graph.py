"""Snapshot-scoped typed knowledge graph reads and in-memory insights."""

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

import networkx as nx
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.kb import (
    CanonicalEntity,
    DocumentLifecycleStatus,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    GraphDirection,
    GraphEdgeKind,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    KnowledgeBase,
    RevisionStatus,
    SnapshotFactSupport,
    SourceDocument,
)
from nicekit.models.kb import (
    GraphEdge as GraphEdgeRow,
)

# 类型亲和度(信号 4,权重 1.0)是领域先验:SDK 不内置任何领域词表,默认对所有
# 类型组合取同一常量(即该信号不产生区分度);宿主按自己的类型体系注入权重表
# ——``set_default_type_affinity()`` 全局注册,或 ``load_graph(type_affinity=...)``
# 单次覆盖。键是参与组合的 type 集合(同类型自连用单元素 frozenset)。
DEFAULT_TYPE_AFFINITY = 0.5

_default_type_affinity: dict[frozenset[str], float] = {}


def set_default_type_affinity(
    affinity: Mapping[frozenset[str] | Iterable[str], float] | None,
) -> None:
    """注册宿主的类型亲和度先验(None/空表 = 回到全 DEFAULT_TYPE_AFFINITY)。"""
    global _default_type_affinity
    _default_type_affinity = {
        frozenset(key): float(value) for key, value in (affinity or {}).items()
    }


def get_default_type_affinity() -> dict[frozenset[str], float]:
    return dict(_default_type_affinity)


W_NEIGHBOR, W_TYPE = 1.5, 1.0


@dataclass
class GraphNode:
    id: str  # "{type}:{uuid}"
    type: str
    name: str
    kb_id: str


@dataclass
class GraphEdge:
    src: str
    dst: str
    link_type: str
    weight: float
    origin: str
    status: str
    predicate: str | None = None
    direction: str = "undirected"
    valid_from: date | None = None
    valid_to: date | None = None
    evidence: list[dict] = field(default_factory=list)
    edge_kind: str | None = None


@dataclass
class KbGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    adj: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: 本次加载生效的类型亲和度先验(见 set_default_type_affinity)
    type_affinity: dict[frozenset[str], float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _FactSupportView:
    support: SnapshotFactSupport
    evidence: EvidenceSpan
    revision: DocumentRevision
    document: SourceDocument
    claim: FactClaim


@dataclass(slots=True)
class _VisibleGraphProjection:
    graph: KbGraph
    node_by_entity_id: dict[UUID, GraphNode]
    edge_rows: list[GraphEdgeRow]
    snapshot_id_by_kb_id: dict[UUID, UUID]
    supported_fact_ids_by_snapshot: dict[UUID, set[UUID]]


def node_id(entity_type: str, entity_id: UUID | str) -> str:
    return f"{entity_type}:{entity_id}"


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _support_is_current(view: _FactSupportView) -> bool:
    support = view.support
    evidence = view.evidence
    revision = view.revision
    document = view.document
    claim = view.claim
    return bool(
        support.evidence_span_id == evidence.id
        and support.fact_claim_id == evidence.fact_claim_id == claim.id
        and support.revision_id == evidence.revision_id == revision.id
        and support.doc_id == revision.doc_id == document.id
        and support.org_id == evidence.org_id == revision.org_id == document.org_id == claim.org_id
        and support.kb_id == evidence.kb_id == revision.kb_id == document.kb_id == claim.kb_id
        and _enum_value(document.lifecycle_status) == DocumentLifecycleStatus.ACTIVE.value
        and revision.tombstoned_at is None
        and _enum_value(revision.status)
        in (RevisionStatus.STAGED.value, RevisionStatus.ACTIVE.value)
        and _enum_value(claim.review_status) == FactReviewStatus.CONFIRMED.value
    )


def _evidence_anchor(view: _FactSupportView) -> dict:
    return {
        "fact_claim_id": str(view.claim.id),
        "evidence_span_id": str(view.evidence.id),
        "source_doc_id": str(view.document.id),
        "source_filename": view.document.filename,
        "revision_id": str(view.revision.id),
        "source_sha256": view.revision.sha256,
        "quote_text": view.evidence.quote_text,
        "page": view.evidence.page,
        "start_line": view.evidence.start_line,
        "end_line": view.evidence.end_line,
        "cell_ref": view.evidence.cell_ref,
    }


def _direct_edge_matches_claim(edge: GraphEdgeRow, claim: FactClaim) -> bool:
    if claim.predicate != _enum_value(edge.predicate):
        return False
    if _enum_value(edge.direction) == GraphDirection.UNDIRECTED.value:
        return {
            claim.subject_entity_id,
            claim.object_entity_id,
        } == {edge.src_entity_id, edge.dst_entity_id}
    return (
        claim.subject_entity_id == edge.src_entity_id
        and claim.object_entity_id == edge.dst_entity_id
    )


async def _load_visible_graph_projection(
    session: AsyncSession,
    kb_ids: list[UUID] | None = None,
) -> _VisibleGraphProjection:
    """Resolve the current fail-closed graph view for active KB snapshots."""

    empty = _VisibleGraphProjection(KbGraph(), {}, [], {}, defaultdict(set))
    if kb_ids is not None and not kb_ids:
        return empty

    snapshot_stmt = select(
        KnowledgeBase.org_id,
        KnowledgeBase.id,
        KnowledgeBase.active_snapshot_id,
    ).where(
        KnowledgeBase.active_snapshot_id.is_not(None),
        KnowledgeBase.lifecycle_status == "active",
    )
    if kb_ids is not None:
        snapshot_stmt = snapshot_stmt.where(KnowledgeBase.id.in_(kb_ids))
    active_scope = [
        (org_id, kb_id, snapshot_id)
        for org_id, kb_id, snapshot_id in (await session.execute(snapshot_stmt)).all()
        if snapshot_id is not None
    ]
    if not active_scope:
        return empty
    snapshot_id_by_kb_id = {kb_id: snapshot_id for _org_id, kb_id, snapshot_id in active_scope}

    node_stmt = (
        select(KbSnapshotEntityNode, CanonicalEntity)
        .join(
            CanonicalEntity,
            and_(
                CanonicalEntity.id == KbSnapshotEntityNode.entity_id,
                CanonicalEntity.org_id == KbSnapshotEntityNode.org_id,
                CanonicalEntity.kb_id == KbSnapshotEntityNode.kb_id,
            ),
        )
        .where(
            tuple_(
                KbSnapshotEntityNode.org_id,
                KbSnapshotEntityNode.kb_id,
                KbSnapshotEntityNode.snapshot_id,
            ).in_(active_scope),
            CanonicalEntity.merged_into_entity_id.is_(None),
        )
        .order_by(KbSnapshotEntityNode.snapshot_id, KbSnapshotEntityNode.entity_id)
    )
    node_registry_rows = list((await session.execute(node_stmt)).all())
    if not node_registry_rows:
        return _VisibleGraphProjection(
            KbGraph(),
            {},
            [],
            snapshot_id_by_kb_id,
            defaultdict(set),
        )
    node_row_by_entity_id = {
        node.entity_id: node
        for node, entity in node_registry_rows
        if entity.merged_into_entity_id is None
        and node.org_id == entity.org_id
        and node.kb_id == entity.kb_id
        and snapshot_id_by_kb_id.get(node.kb_id) == node.snapshot_id
    }
    registry_by_entity_id = {
        entity.id: entity
        for node, entity in node_registry_rows
        if node.entity_id in node_row_by_entity_id
    }
    if not node_row_by_entity_id:
        return _VisibleGraphProjection(
            KbGraph(),
            {},
            [],
            snapshot_id_by_kb_id,
            defaultdict(set),
        )

    node_keys = [
        (node.org_id, node.kb_id, node.snapshot_id, node.entity_id)
        for node in node_row_by_entity_id.values()
    ]
    node_support_stmt = (
        select(KbSnapshotEntityNodeSupport)
        .where(
            tuple_(
                KbSnapshotEntityNodeSupport.org_id,
                KbSnapshotEntityNodeSupport.kb_id,
                KbSnapshotEntityNodeSupport.snapshot_id,
                KbSnapshotEntityNodeSupport.entity_id,
            ).in_(node_keys)
        )
        .order_by(
            KbSnapshotEntityNodeSupport.snapshot_id,
            KbSnapshotEntityNodeSupport.entity_id,
            KbSnapshotEntityNodeSupport.id,
        )
    )
    node_support_rows = (await session.execute(node_support_stmt)).scalars().all()

    edge_stmt = (
        select(GraphEdgeRow)
        .where(
            tuple_(
                GraphEdgeRow.org_id,
                GraphEdgeRow.kb_id,
                GraphEdgeRow.snapshot_id,
            ).in_(active_scope)
        )
        .order_by(GraphEdgeRow.snapshot_id, GraphEdgeRow.id)
    )
    edge_rows = list((await session.execute(edge_stmt)).scalars().all())

    node_fact_support_ids = {
        support.fact_support_id
        for support in node_support_rows
        if support.support_type == "fact" and support.fact_support_id is not None
    }
    edge_support_keys = {
        (
            edge.snapshot_id,
            edge.fact_claim_id,
            edge.evidence_span_id,
            edge.source_revision_id,
        )
        for edge in edge_rows
    }
    edge_support_keys.update(
        (
            edge.snapshot_id,
            edge.related_fact_claim_id,
            edge.related_evidence_span_id,
            edge.related_source_revision_id,
        )
        for edge in edge_rows
        if edge.related_fact_claim_id is not None
        and edge.related_evidence_span_id is not None
        and edge.related_source_revision_id is not None
    )
    support_filters = []
    if node_fact_support_ids:
        support_filters.append(SnapshotFactSupport.id.in_(list(node_fact_support_ids)))
    if edge_support_keys:
        support_filters.append(
            tuple_(
                SnapshotFactSupport.snapshot_id,
                SnapshotFactSupport.fact_claim_id,
                SnapshotFactSupport.evidence_span_id,
                SnapshotFactSupport.revision_id,
            ).in_(list(edge_support_keys))
        )

    support_views: list[_FactSupportView] = []
    if support_filters:
        support_stmt = (
            select(
                SnapshotFactSupport,
                EvidenceSpan,
                DocumentRevision,
                SourceDocument,
                FactClaim,
            )
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
                tuple_(
                    SnapshotFactSupport.org_id,
                    SnapshotFactSupport.kb_id,
                    SnapshotFactSupport.snapshot_id,
                ).in_(active_scope),
                or_(*support_filters),
            )
            .order_by(
                SnapshotFactSupport.snapshot_id,
                SnapshotFactSupport.fact_claim_id,
                SnapshotFactSupport.evidence_span_id,
            )
        )
        support_views = [
            _FactSupportView(support, evidence, revision, document, claim)
            for support, evidence, revision, document, claim in (
                await session.execute(support_stmt)
            ).all()
        ]
    current_support_views = [view for view in support_views if _support_is_current(view)]
    support_view_by_id = {view.support.id: view for view in current_support_views}
    support_view_by_edge_key = {
        (
            view.support.snapshot_id,
            view.support.fact_claim_id,
            view.support.evidence_span_id,
            view.support.revision_id,
        ): view
        for view in current_support_views
    }
    supported_fact_ids_by_snapshot: dict[UUID, set[UUID]] = defaultdict(set)
    for view in current_support_views:
        supported_fact_ids_by_snapshot[view.support.snapshot_id].add(view.support.fact_claim_id)

    candidate_edges: dict[UUID, tuple[GraphEdgeRow, list[_FactSupportView]]] = {}
    for edge in edge_rows:
        src_node = node_row_by_entity_id.get(edge.src_entity_id)
        dst_node = node_row_by_entity_id.get(edge.dst_entity_id)
        if (
            snapshot_id_by_kb_id.get(edge.kb_id) != edge.snapshot_id
            or src_node is None
            or dst_node is None
            or (edge.org_id, edge.kb_id, edge.snapshot_id)
            != (src_node.org_id, src_node.kb_id, src_node.snapshot_id)
            or (edge.org_id, edge.kb_id, edge.snapshot_id)
            != (dst_node.org_id, dst_node.kb_id, dst_node.snapshot_id)
        ):
            continue
        primary_view = support_view_by_edge_key.get(
            (
                edge.snapshot_id,
                edge.fact_claim_id,
                edge.evidence_span_id,
                edge.source_revision_id,
            )
        )
        if primary_view is None or (
            primary_view.support.org_id,
            primary_view.support.kb_id,
            primary_view.support.snapshot_id,
        ) != (edge.org_id, edge.kb_id, edge.snapshot_id):
            continue
        edge_kind = _enum_value(edge.edge_kind)
        if edge_kind == GraphEdgeKind.DIRECT.value:
            if not _direct_edge_matches_claim(edge, primary_view.claim):
                continue
            views = [primary_view]
        elif edge_kind == GraphEdgeKind.SHARED_SOURCE.value:
            related_view = support_view_by_edge_key.get(
                (
                    edge.snapshot_id,
                    edge.related_fact_claim_id,
                    edge.related_evidence_span_id,
                    edge.related_source_revision_id,
                )
            )
            if (
                related_view is None
                or (
                    related_view.support.org_id,
                    related_view.support.kb_id,
                    related_view.support.snapshot_id,
                )
                != (edge.org_id, edge.kb_id, edge.snapshot_id)
                or primary_view.claim.subject_entity_id != edge.src_entity_id
                or related_view.claim.subject_entity_id != edge.dst_entity_id
            ):
                continue
            views = [primary_view, related_view]
        else:
            continue
        candidate_edges[edge.id] = (edge, views)

    visible_entity_ids: set[UUID] = set()
    edge_support_ids_by_entity: dict[UUID, set[UUID]] = defaultdict(set)
    for support in node_support_rows:
        node = node_row_by_entity_id.get(support.entity_id)
        entity = registry_by_entity_id.get(support.entity_id)
        if (
            node is None
            or entity is None
            or support.snapshot_id != node.snapshot_id
            or support.org_id != node.org_id
            or support.kb_id != node.kb_id
        ):
            continue
        if (
            support.support_type == "pin"
            and support.fact_support_id is None
            and support.graph_edge_id is None
            and node.pinned_at_build
            and entity.is_pinned
        ):
            visible_entity_ids.add(node.entity_id)
        elif support.support_type == "fact" and support.fact_support_id is not None:
            view = support_view_by_id.get(support.fact_support_id)
            if (
                view is not None
                and (
                    view.support.org_id,
                    view.support.kb_id,
                    view.support.snapshot_id,
                )
                == (node.org_id, node.kb_id, node.snapshot_id)
                and node.entity_id
                in {
                    view.claim.subject_entity_id,
                    view.claim.object_entity_id,
                }
            ):
                visible_entity_ids.add(node.entity_id)
        elif support.support_type == "edge" and support.graph_edge_id is not None:
            edge_pair = candidate_edges.get(support.graph_edge_id)
            if edge_pair is not None and node.entity_id in {
                edge_pair[0].src_entity_id,
                edge_pair[0].dst_entity_id,
            }:
                edge_support_ids_by_entity[node.entity_id].add(support.graph_edge_id)

    changed = True
    while changed:
        changed = False
        for edge_id, (edge, _views) in candidate_edges.items():
            src_supported = (
                edge.src_entity_id in visible_entity_ids
                or edge_id in edge_support_ids_by_entity[edge.src_entity_id]
            )
            dst_supported = (
                edge.dst_entity_id in visible_entity_ids
                or edge_id in edge_support_ids_by_entity[edge.dst_entity_id]
            )
            if not src_supported or not dst_supported:
                continue
            before = len(visible_entity_ids)
            visible_entity_ids.update((edge.src_entity_id, edge.dst_entity_id))
            changed = changed or len(visible_entity_ids) != before

    visible_edge_views = [
        (edge, views)
        for edge, views in candidate_edges.values()
        if edge.src_entity_id in visible_entity_ids and edge.dst_entity_id in visible_entity_ids
    ]
    graph = KbGraph()
    node_by_entity_id: dict[UUID, GraphNode] = {}
    for entity_id in sorted(visible_entity_ids, key=str):
        node = node_row_by_entity_id[entity_id]
        frozen = GraphNode(
            id=node_id(node.entity_type, node.entity_id),
            type=node.entity_type,
            name=node.display_name,
            kb_id=str(node.kb_id),
        )
        graph.nodes[frozen.id] = frozen
        node_by_entity_id[entity_id] = frozen

    for edge, views in visible_edge_views:
        src = node_by_entity_id[edge.src_entity_id]
        dst = node_by_entity_id[edge.dst_entity_id]
        predicate = _enum_value(edge.predicate)
        graph.edges.append(
            GraphEdge(
                src=src.id,
                dst=dst.id,
                link_type=predicate,
                predicate=predicate,
                direction=_enum_value(edge.direction),
                weight=edge.weight,
                origin="system",
                status="confirmed",
                valid_from=edge.valid_from,
                valid_to=edge.valid_to,
                evidence=[_evidence_anchor(view) for view in views],
                edge_kind=_enum_value(edge.edge_kind),
            )
        )

    for edge in graph.edges:
        graph.adj[edge.src].add(edge.dst)
        graph.adj[edge.dst].add(edge.src)
    return _VisibleGraphProjection(
        graph=graph,
        node_by_entity_id=node_by_entity_id,
        edge_rows=[edge for edge, _views in visible_edge_views],
        snapshot_id_by_kb_id=snapshot_id_by_kb_id,
        supported_fact_ids_by_snapshot=supported_fact_ids_by_snapshot,
    )


async def load_graph(
    session: AsyncSession,
    kb_ids: list[UUID] | None = None,
    *,
    type_affinity: Mapping[frozenset[str] | Iterable[str], float] | None = None,
) -> KbGraph:
    """Load the current withdrawal-fenced active graph projection.

    ``type_affinity`` 覆盖本次加载的类型亲和度先验;缺省用
    :func:`set_default_type_affinity` 注册的全局表(再缺省即全 0.5)。
    """

    graph = (await _load_visible_graph_projection(session, kb_ids)).graph
    graph.type_affinity = (
        {frozenset(key): float(value) for key, value in type_affinity.items()}
        if type_affinity is not None
        else get_default_type_affinity()
    )
    return graph


def _adamic_adar(g: KbGraph, a: str, b: str) -> float:
    common = g.adj[a] & g.adj[b]
    score = sum(1.0 / math.log(len(g.adj[n])) for n in common if len(g.adj[n]) > 1)
    return min(score, 1.0)  # 归一,贡献封顶为 1


def relevance(g: KbGraph, a: str, b: str) -> tuple[float, dict[str, float]]:
    """Score two nodes from typed edges plus topology and type affinity."""
    if a not in g.nodes or b not in g.nodes or a == b:
        return 0.0, {}
    connecting_edges = [edge for edge in g.edges if {edge.src, edge.dst} == {a, b}]
    direct_link = max(
        (
            edge.weight
            for edge in connecting_edges
            if (edge.predicate or edge.link_type) != "shared_context"
        ),
        default=0.0,
    )
    shared_context = max(
        (
            edge.weight
            for edge in connecting_edges
            if (edge.predicate or edge.link_type) == "shared_context"
        ),
        default=0.0,
    )
    signals = {
        "direct_link": direct_link,
        "shared_context": shared_context,
        "common_neighbors": W_NEIGHBOR * _adamic_adar(g, a, b),
        "type_affinity": W_TYPE
        * g.type_affinity.get(
            frozenset({g.nodes[a].type, g.nodes[b].type}), DEFAULT_TYPE_AFFINITY
        ),
    }
    return sum(signals.values()), signals


def related_nodes(g: KbGraph, target: str, top_k: int = 10) -> list[dict]:
    scored = []
    for nid in g.nodes:
        if nid == target:
            continue
        score, signals = relevance(g, target, nid)
        if any(
            signals.get(signal, 0.0) > 0
            for signal in ("direct_link", "shared_context", "common_neighbors")
        ):
            scored.append(
                {
                    "node": g.nodes[nid].__dict__,
                    "score": round(score, 3),
                    "signals": {k: round(v, 3) for k, v in signals.items()},
                }
            )
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def graph_insights(g: KbGraph) -> dict:
    """知识库健康度(borrow llm_wiki graph-insights):社区/孤立节点/稀疏社区。"""
    G = nx.Graph()
    G.add_nodes_from(g.nodes)
    valid_edges = [edge for edge in g.edges if edge.src in g.nodes and edge.dst in g.nodes]
    for edge in valid_edges:
        current_weight = G.get_edge_data(edge.src, edge.dst, {}).get("weight", 0.0)
        G.add_edge(edge.src, edge.dst, weight=current_weight + edge.weight)

    isolated = [g.nodes[n].__dict__ for n in g.nodes if not g.adj[n]]
    communities = []
    if G.number_of_edges() > 0:
        # 只在有边的子图上做社区检测:孤立点单独报告,不算单点社区
        connected = G.subgraph([n for n in g.nodes if g.adj[n]])
        for members in nx.community.louvain_communities(connected, weight="weight", seed=42):
            sub = connected.subgraph(members)
            n = len(members)
            density = nx.density(sub) if n > 1 else 0.0
            core = sorted(members, key=lambda m: -G.degree(m))[:3]
            communities.append(
                {
                    "size": n,
                    "density": round(density, 3),
                    "sparse": bool(n >= 3 and density < 0.3),
                    "core_nodes": [g.nodes[c].__dict__ for c in core],
                }
            )
    return {
        "node_count": len(g.nodes),
        "edge_count": len(valid_edges),
        "isolated_count": len(isolated),
        "isolated_nodes": isolated[:50],
        "avg_degree": round(2 * G.number_of_edges() / len(g.nodes), 2) if g.nodes else 0.0,
        "communities": sorted(communities, key=lambda c: -c["size"]),
        "sparse_community_count": sum(1 for c in communities if c["sparse"]),
    }
