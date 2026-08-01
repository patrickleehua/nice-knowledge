from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nicekit.kb import graph_search as graph_search_module
from nicekit.kb.graph import (
    GraphEdge,
    GraphNode,
    KbGraph,
    _VisibleGraphProjection,
    graph_insights,
    load_graph,
    related_nodes,
    relevance,
)
from nicekit.kb.graph_search import graph_recall_candidates
from nicekit.models.kb import (
    CanonicalEntity,
    DocType,
    DocumentLifecycleStatus,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    GraphDirection,
    GraphEdgeKind,
    GraphPredicate,
    KbChunk,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    RevisionStatus,
    SnapshotFactSupport,
    SourceDocument,
)
from nicekit.models.kb import GraphEdge as GraphEdgeRow


def _result(*, rows=None, scalars=None):
    result = MagicMock()
    result.all.return_value = rows or []
    result.scalars.return_value.all.return_value = scalars or []
    return result


async def test_graph_without_active_snapshot_is_empty() -> None:
    session = AsyncMock()
    session.execute.return_value = _result(rows=[])

    graph = await load_graph(session, [uuid4()])

    assert graph.nodes == {}
    assert graph.edges == []
    session.execute.assert_awaited_once()


async def test_active_snapshot_excludes_registry_only_ghost_entities() -> None:
    kb_id, snapshot_id = uuid4(), uuid4()
    session = AsyncMock()
    session.execute.side_effect = [
        _result(rows=[(uuid4(), kb_id, snapshot_id)]),
        _result(rows=[]),
    ]

    graph = await load_graph(session, [kb_id])

    assert graph.nodes == {}
    assert graph.edges == []
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert "kb_snapshot_entity_nodes" in statements[1]


async def test_active_snapshot_keeps_current_pinned_only_frozen_node() -> None:
    org_id, kb_id, snapshot_id, entity_id = uuid4(), uuid4(), uuid4(), uuid4()
    entity = CanonicalEntity(
        id=entity_id,
        org_id=org_id,
        kb_id=kb_id,
        entity_type="hotel",
        canonical_name="注册表中的新名称",
        is_pinned=True,
    )
    node = KbSnapshotEntityNode(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        entity_id=entity_id,
        entity_type="hotel",
        display_name="快照冻结名称",
        support_status="pinned",
        support_source="pin",
        pinned_at_build=True,
        support_count=0,
    )
    pin_support = KbSnapshotEntityNodeSupport(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        entity_id=entity_id,
        support_type="pin",
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _result(rows=[(org_id, kb_id, snapshot_id)]),
        _result(rows=[(node, entity)]),
        _result(scalars=[pin_support]),
        _result(scalars=[]),
    ]

    graph = await load_graph(session, [kb_id])

    assert graph.nodes[f"hotel:{entity_id}"].name == "快照冻结名称"
    assert graph.edges == []
    assert graph_insights(graph)["isolated_count"] == 1


async def test_graph_reads_typed_active_snapshot_with_evidence_anchors() -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    src_id, dst_id = uuid4(), uuid4()
    claim_id, evidence_id = uuid4(), uuid4()
    doc_id, revision_id = uuid4(), uuid4()
    src = CanonicalEntity(
        id=src_id,
        org_id=org_id,
        kb_id=kb_id,
        entity_type="poi",
        canonical_name="注册表卢浮宫",
    )
    dst = CanonicalEntity(
        id=dst_id,
        org_id=org_id,
        kb_id=kb_id,
        entity_type="destination",
        canonical_name="巴黎",
    )
    src_node = KbSnapshotEntityNode(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        entity_id=src_id,
        entity_type="poi",
        display_name="快照卢浮宫",
        support_status="supported",
        support_source="mixed",
        support_count=2,
    )
    dst_node = KbSnapshotEntityNode(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        entity_id=dst_id,
        entity_type="destination",
        display_name="快照巴黎",
        support_status="supported",
        support_source="mixed",
        support_count=2,
    )
    fact_support_id = uuid4()
    edge = GraphEdgeRow(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        src_entity_id=src_id,
        dst_entity_id=dst_id,
        predicate=GraphPredicate.LOCATED_IN,
        direction=GraphDirection.DIRECTED,
        edge_kind=GraphEdgeKind.DIRECT,
        weight=1.0,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        fact_claim_id=claim_id,
        evidence_span_id=evidence_id,
        source_revision_id=revision_id,
    )
    claim = FactClaim(
        id=claim_id,
        org_id=org_id,
        kb_id=kb_id,
        subject_entity_id=src_id,
        object_entity_id=dst_id,
        subject_type="canonical_entity",
        subject_id=src_id,
        predicate=GraphPredicate.LOCATED_IN.value,
        value_json={},
        raw_payload={},
        review_status=FactReviewStatus.CONFIRMED,
    )
    evidence = EvidenceSpan(
        id=evidence_id,
        org_id=org_id,
        kb_id=kb_id,
        fact_claim_id=claim_id,
        revision_id=revision_id,
        page=3,
        start_line=10,
        end_line=12,
        quote_text="卢浮宫位于法国巴黎。",
    )
    revision = DocumentRevision(
        id=revision_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_no=1,
        sha256="a" * 64,
        original_object_key="kb/source.pdf",
        status=RevisionStatus.ACTIVE,
    )
    document = SourceDocument(
        id=doc_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="巴黎指南.pdf",
        object_key="kb/source.pdf",
        sha256="a" * 64,
        doc_type=DocType.GENERAL,
        lifecycle_status=DocumentLifecycleStatus.ACTIVE,
    )
    fact_support = SnapshotFactSupport(
        id=fact_support_id,
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        fact_claim_id=claim_id,
        evidence_span_id=evidence_id,
        revision_id=revision_id,
        doc_id=doc_id,
    )
    node_supports = [
        KbSnapshotEntityNodeSupport(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity_id,
            support_type="fact",
            fact_support_id=fact_support_id,
        )
        for entity_id in (src_id, dst_id)
    ]
    node_supports.extend(
        KbSnapshotEntityNodeSupport(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity_id,
            support_type="edge",
            graph_edge_id=edge.id,
        )
        for entity_id in (src_id, dst_id)
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _result(rows=[(org_id, kb_id, snapshot_id)]),
        _result(rows=[(src_node, src), (dst_node, dst)]),
        _result(scalars=node_supports),
        _result(scalars=[edge]),
        _result(rows=[(fact_support, evidence, revision, document, claim)]),
    ]

    graph = await load_graph(session, [kb_id])

    assert set(graph.nodes) == {f"poi:{src_id}", f"destination:{dst_id}"}
    assert graph.nodes[f"poi:{src_id}"].name == "快照卢浮宫"
    assert len(graph.edges) == 1
    typed = graph.edges[0]
    assert typed.predicate == "located_in"
    assert typed.direction == "directed"
    assert typed.valid_from == date(2026, 1, 1)
    assert typed.status == "confirmed"
    assert typed.evidence == [
        {
            "fact_claim_id": str(claim_id),
            "evidence_span_id": str(evidence_id),
            "source_doc_id": str(doc_id),
            "source_filename": "巴黎指南.pdf",
            "revision_id": str(revision_id),
            "source_sha256": "a" * 64,
            "quote_text": "卢浮宫位于法国巴黎。",
            "page": 3,
            "start_line": 10,
            "end_line": 12,
            "cell_ref": None,
        }
    ]
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("kb_graph_edges" in statement for statement in statements)
    assert all("kb_links" not in statement for statement in statements)


@pytest.mark.parametrize("fence", ["document", "revision", "missing_support"])
async def test_current_fence_hides_nodes_and_edge_before_snapshot_rebuild(
    fence: str,
) -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    src_id, dst_id, claim_id = uuid4(), uuid4(), uuid4()
    evidence_id, revision_id, doc_id, fact_support_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    entities = [
        CanonicalEntity(
            id=entity_id,
            org_id=org_id,
            kb_id=kb_id,
            entity_type=entity_type,
            canonical_name=name,
        )
        for entity_id, entity_type, name in (
            (src_id, "poi", "卢浮宫"),
            (dst_id, "destination", "巴黎"),
        )
    ]
    nodes = [
        KbSnapshotEntityNode(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity.id,
            entity_type=entity.entity_type,
            display_name=entity.canonical_name,
            support_status="supported",
            support_source="fact",
            support_count=1,
        )
        for entity in entities
    ]
    claim = FactClaim(
        id=claim_id,
        org_id=org_id,
        kb_id=kb_id,
        subject_entity_id=src_id,
        object_entity_id=dst_id,
        subject_type="canonical_entity",
        subject_id=src_id,
        predicate=GraphPredicate.LOCATED_IN.value,
        value_json={},
        raw_payload={},
        review_status=FactReviewStatus.CONFIRMED,
    )
    edge = GraphEdgeRow(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        src_entity_id=src_id,
        dst_entity_id=dst_id,
        predicate=GraphPredicate.LOCATED_IN,
        direction=GraphDirection.DIRECTED,
        edge_kind=GraphEdgeKind.DIRECT,
        weight=1.0,
        fact_claim_id=claim_id,
        evidence_span_id=evidence_id,
        source_revision_id=revision_id,
    )
    fact_support = SnapshotFactSupport(
        id=fact_support_id,
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        fact_claim_id=claim_id,
        evidence_span_id=evidence_id,
        revision_id=revision_id,
        doc_id=doc_id,
    )
    evidence = EvidenceSpan(
        id=evidence_id,
        org_id=org_id,
        kb_id=kb_id,
        fact_claim_id=claim_id,
        revision_id=revision_id,
        page=1,
        quote_text="卢浮宫位于巴黎。",
    )
    revision = DocumentRevision(
        id=revision_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_no=1,
        sha256="b" * 64,
        original_object_key="kb/source.pdf",
        status=(RevisionStatus.TOMBSTONED if fence == "revision" else RevisionStatus.ACTIVE),
    )
    withdrawn = SourceDocument(
        id=doc_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="已撤回.pdf",
        object_key="kb/source.pdf",
        sha256="b" * 64,
        doc_type=DocType.GENERAL,
        lifecycle_status=(
            DocumentLifecycleStatus.WITHDRAWAL_PENDING
            if fence == "document"
            else DocumentLifecycleStatus.ACTIVE
        ),
    )
    node_supports = [
        KbSnapshotEntityNodeSupport(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity.id,
            support_type="fact",
            fact_support_id=fact_support_id,
        )
        for entity in entities
    ]
    session = AsyncMock()
    session.execute.side_effect = [
        _result(rows=[(org_id, kb_id, snapshot_id)]),
        _result(rows=list(zip(nodes, entities, strict=True))),
        _result(scalars=node_supports),
        _result(scalars=[edge]),
        _result(
            rows=(
                []
                if fence == "missing_support"
                else [(fact_support, evidence, revision, withdrawn, claim)]
            )
        ),
    ]

    graph = await load_graph(session, [kb_id])

    assert graph.nodes == {}
    assert graph.edges == []


async def test_shared_source_edge_requires_both_current_evidence_supports() -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    src_id, dst_id = sorted((uuid4(), uuid4()), key=lambda value: value.int)
    claim_ids = [uuid4(), uuid4()]
    evidence_ids = [uuid4(), uuid4()]
    revision_ids = [uuid4(), uuid4()]
    doc_ids = [uuid4(), uuid4()]
    support_ids = [uuid4(), uuid4()]
    entities = [
        CanonicalEntity(
            id=entity_id,
            org_id=org_id,
            kb_id=kb_id,
            entity_type="poi",
            canonical_name=name,
        )
        for entity_id, name in ((src_id, "A"), (dst_id, "B"))
    ]
    nodes = [
        KbSnapshotEntityNode(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity.id,
            entity_type="poi",
            display_name=entity.canonical_name,
            support_status="supported",
            support_source="edge",
            support_count=1,
        )
        for entity in entities
    ]
    claims = [
        FactClaim(
            id=claim_id,
            org_id=org_id,
            kb_id=kb_id,
            subject_entity_id=entity_id,
            subject_type="canonical_entity",
            subject_id=entity_id,
            predicate="description",
            value_json={},
            raw_payload={},
            review_status=FactReviewStatus.CONFIRMED,
        )
        for claim_id, entity_id in zip(claim_ids, (src_id, dst_id), strict=True)
    ]
    edge = GraphEdgeRow(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        src_entity_id=src_id,
        dst_entity_id=dst_id,
        predicate=GraphPredicate.SHARED_CONTEXT,
        direction=GraphDirection.UNDIRECTED,
        edge_kind=GraphEdgeKind.SHARED_SOURCE,
        weight=0.25,
        fact_claim_id=claim_ids[0],
        evidence_span_id=evidence_ids[0],
        source_revision_id=revision_ids[0],
        related_fact_claim_id=claim_ids[1],
        related_evidence_span_id=evidence_ids[1],
        related_source_revision_id=revision_ids[1],
    )
    supports = [
        SnapshotFactSupport(
            id=support_id,
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            fact_claim_id=claim_id,
            evidence_span_id=evidence_id,
            revision_id=revision_id,
            doc_id=doc_id,
        )
        for support_id, claim_id, evidence_id, revision_id, doc_id in zip(
            support_ids,
            claim_ids,
            evidence_ids,
            revision_ids,
            doc_ids,
            strict=True,
        )
    ]
    evidences = [
        EvidenceSpan(
            id=evidence_id,
            org_id=org_id,
            kb_id=kb_id,
            fact_claim_id=claim_id,
            revision_id=revision_id,
            page=1,
            quote_text=f"证据 {index}",
        )
        for index, (claim_id, evidence_id, revision_id) in enumerate(
            zip(claim_ids, evidence_ids, revision_ids, strict=True),
            start=1,
        )
    ]
    revisions = [
        DocumentRevision(
            id=revision_id,
            org_id=org_id,
            kb_id=kb_id,
            doc_id=doc_id,
            revision_no=1,
            sha256=str(index) * 64,
            original_object_key=f"kb/{index}.pdf",
            status=RevisionStatus.ACTIVE,
        )
        for index, (revision_id, doc_id) in enumerate(
            zip(revision_ids, doc_ids, strict=True),
            start=1,
        )
    ]
    documents = [
        SourceDocument(
            id=doc_id,
            org_id=org_id,
            kb_id=kb_id,
            filename=f"{index}.pdf",
            object_key=f"kb/{index}.pdf",
            sha256=str(index) * 64,
            doc_type=DocType.GENERAL,
            lifecycle_status=(
                DocumentLifecycleStatus.ACTIVE
                if index == 1
                else DocumentLifecycleStatus.WITHDRAWAL_PENDING
            ),
        )
        for index, doc_id in enumerate(doc_ids, start=1)
    ]
    node_supports = [
        KbSnapshotEntityNodeSupport(
            id=uuid4(),
            org_id=org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            entity_id=entity.id,
            support_type="edge",
            graph_edge_id=edge.id,
        )
        for entity in entities
    ]
    session = AsyncMock()
    session.execute.side_effect = [
        _result(rows=[(org_id, kb_id, snapshot_id)]),
        _result(rows=list(zip(nodes, entities, strict=True))),
        _result(scalars=node_supports),
        _result(scalars=[edge]),
        _result(
            rows=[
                (support, evidence, revision, document, claim)
                for support, evidence, revision, document, claim in zip(
                    supports,
                    evidences,
                    revisions,
                    documents,
                    claims,
                    strict=True,
                )
            ]
        ),
    ]

    graph = await load_graph(session, [kb_id])

    assert graph.nodes == {}
    assert graph.edges == []


async def test_graph_search_seeds_and_traverses_only_visible_projection(
    monkeypatch,
) -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    seed_id, target_id, claim_id, card_entity_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    seed_node = GraphNode(f"poi:{seed_id}", "poi", "巴黎", str(kb_id))
    target_node = GraphNode(f"hotel:{target_id}", "hotel", "酒店", str(kb_id))
    edge = GraphEdgeRow(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        src_entity_id=seed_id,
        dst_entity_id=target_id,
        predicate=GraphPredicate.RELATED,
        direction=GraphDirection.DIRECTED,
        edge_kind=GraphEdgeKind.DIRECT,
        weight=0.8,
        fact_claim_id=uuid4(),
        evidence_span_id=uuid4(),
        source_revision_id=uuid4(),
    )
    projection = _VisibleGraphProjection(
        graph=KbGraph(
            nodes={seed_node.id: seed_node, target_node.id: target_node},
        ),
        node_by_entity_id={seed_id: seed_node, target_id: target_node},
        edge_rows=[edge],
        snapshot_id_by_kb_id={kb_id: snapshot_id},
        supported_fact_ids_by_snapshot={snapshot_id: {claim_id}},
    )

    async def visible_projection(*_args, **_kwargs):
        return projection

    monkeypatch.setattr(
        graph_search_module,
        "_load_visible_graph_projection",
        visible_projection,
    )
    claim = FactClaim(
        id=claim_id,
        org_id=org_id,
        kb_id=kb_id,
        subject_entity_id=target_id,
        subject_type="canonical_entity",
        subject_id=card_entity_id,
        predicate="hotel",
        value_json={},
        raw_payload={},
        review_status=FactReviewStatus.CONFIRMED,
    )
    card = KbChunk(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        content="参考酒店",
        source_ref=f"hotel:{card_entity_id}",
        meta={"fact_claim_id": str(claim_id)},
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _result(scalars=[seed_id]),
        _result(scalars=[claim]),
        _result(scalars=[card]),
    ]

    candidates = await graph_recall_candidates(
        session,
        "巴黎",
        kb_ids=[kb_id],
        max_hops=1,
        top_k=5,
        as_of=date(2026, 7, 1),
    )

    assert len(candidates) == 1
    assert candidates[0].entity_id == card_entity_id
    assert candidates[0].edge_ids == (edge.id,)
    seed_statement = str(session.execute.await_args_list[0].args[0])
    assert "kb_snapshot_entity_nodes" in seed_statement
    assert "canonical_entities" not in seed_statement


def test_related_scoring_uses_typed_shared_context_not_legacy_sources() -> None:
    graph = KbGraph()
    graph.nodes["poi:a"] = GraphNode("poi:a", "poi", "A", "kb")
    graph.nodes["poi:b"] = GraphNode("poi:b", "poi", "B", "kb")
    graph.edges.append(
        GraphEdge(
            "poi:a",
            "poi:b",
            "shared_context",
            0.25,
            "system",
            "confirmed",
            predicate="shared_context",
        )
    )
    graph.adj["poi:a"].add("poi:b")
    graph.adj["poi:b"].add("poi:a")
    _, signals = relevance(graph, "poi:a", "poi:b")

    assert signals["shared_context"] == 0.25
    assert "shared_source" not in signals
    assert related_nodes(graph, "poi:a", top_k=1)[0]["node"]["id"] == "poi:b"
