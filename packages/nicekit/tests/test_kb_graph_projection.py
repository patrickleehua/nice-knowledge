from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nicekit.kb.graph_projection import (
    DIRECT_EDGE_WEIGHT,
    SHARED_SOURCE_WEIGHT,
    GraphProjectionBuilder,
    _target_entity_id,
    evidence_spans_are_neighbors,
    graph_edge_id,
    graph_node_id,
    graph_node_support_id,
)
from nicekit.kb.projection_gc import _PROJECTION_MODELS
from nicekit.models.kb import (
    EvidenceSpan,
    FactClaim,
    GraphEdge,
    GraphEdgeKind,
    GraphPredicate,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    SnapshotFactSupport,
)


def _result(*, rows=None, scalars=None):
    result = MagicMock()
    result.all.return_value = rows or []
    result.scalars.return_value.all.return_value = scalars or []
    return result


def _evidence(*, revision_id=None, chunk_id=None, start=1, end=1, page=1):
    return EvidenceSpan(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        fact_claim_id=uuid4(),
        revision_id=revision_id or uuid4(),
        chunk_id=chunk_id,
        page=page,
        start_line=start,
        end_line=end,
        quote_text="evidence",
    )


def test_graph_weights_keep_direct_above_shared_source() -> None:
    assert DIRECT_EDGE_WEIGHT > SHARED_SOURCE_WEIGHT > 0


def test_graph_edge_ids_are_deterministic_and_snapshot_scoped() -> None:
    snapshot, other_snapshot = uuid4(), uuid4()
    src, dst, evidence = uuid4(), uuid4(), uuid4()
    kwargs = dict(
        edge_kind=GraphEdgeKind.DIRECT,
        predicate=GraphPredicate.NEAR,
        src_entity_id=src,
        dst_entity_id=dst,
        evidence_span_id=evidence,
    )
    assert graph_edge_id(snapshot, **kwargs) == graph_edge_id(snapshot, **kwargs)
    assert graph_edge_id(snapshot, **kwargs) != graph_edge_id(other_snapshot, **kwargs)


def test_graph_node_and_support_ids_are_deterministic_and_snapshot_scoped() -> None:
    snapshot, other_snapshot = uuid4(), uuid4()
    entity_id, fact_support_id = uuid4(), uuid4()

    assert graph_node_id(snapshot, entity_id) == graph_node_id(snapshot, entity_id)
    assert graph_node_id(snapshot, entity_id) != graph_node_id(other_snapshot, entity_id)
    assert graph_node_support_id(
        snapshot,
        entity_id=entity_id,
        support_type="fact",
        support_id=fact_support_id,
    ) == graph_node_support_id(
        snapshot,
        entity_id=entity_id,
        support_type="fact",
        support_id=fact_support_id,
    )
    assert graph_node_support_id(
        snapshot,
        entity_id=entity_id,
        support_type="fact",
        support_id=fact_support_id,
    ) != graph_node_support_id(
        snapshot,
        entity_id=entity_id,
        support_type="pin",
        support_id=None,
    )


def test_relation_object_foreign_key_is_authoritative_over_legacy_payload() -> None:
    object_entity_id = uuid4()
    claim = FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="canonical_entity",
        subject_id=uuid4(),
        subject_entity_id=uuid4(),
        object_entity_id=object_entity_id,
        predicate=GraphPredicate.NEAR,
        value_json={"target_entity_id": str(uuid4())},
        raw_payload={"target_entity_id": str(uuid4())},
        corrected_payload={"target_entity_id": str(uuid4())},
    )

    assert _target_entity_id(claim) == object_entity_id


async def test_graph_builder_materializes_nodes_before_edges_and_supports() -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    src_id, dst_id = uuid4(), uuid4()
    claim_id, evidence_id, revision_id = uuid4(), uuid4(), uuid4()
    fact_support_id = uuid4()
    claim = FactClaim(
        id=claim_id,
        org_id=org_id,
        kb_id=kb_id,
        subject_type="canonical_entity",
        subject_id=src_id,
        subject_entity_id=src_id,
        object_entity_id=dst_id,
        predicate=GraphPredicate.NEAR,
        value_json={"target_entity_id": str(uuid4())},
        raw_payload={"target_entity_id": str(uuid4())},
    )
    evidence = EvidenceSpan(
        id=evidence_id,
        org_id=org_id,
        kb_id=kb_id,
        fact_claim_id=claim_id,
        revision_id=revision_id,
        page=1,
        start_line=1,
        end_line=2,
        quote_text="A 靠近 B",
    )
    fact_support = SnapshotFactSupport(
        id=fact_support_id,
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        fact_claim_id=claim_id,
        evidence_span_id=evidence_id,
        revision_id=revision_id,
        doc_id=uuid4(),
    )
    src = SimpleNamespace(
        id=src_id,
        entity_type="poi",
        canonical_name="A",
        is_pinned=False,
    )
    dst = SimpleNamespace(
        id=dst_id,
        entity_type="poi",
        canonical_name="B",
        is_pinned=False,
    )
    context = SimpleNamespace(
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        fact_claim_ids=[claim_id],
    )
    session = AsyncMock()
    session.execute.side_effect = [
        _result(scalars=[claim]),
        _result(rows=[(fact_support, evidence)]),
        _result(scalars=[src, dst]),
        _result(scalars=[]),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]

    stats = await GraphProjectionBuilder().build(session, context)

    assert stats["row_count"] == 1
    assert stats["node_count"] == 2
    assert stats["node_support_count"] == 4
    write_statements = [str(call.args[0]) for call in session.execute.await_args_list[-3:]]
    assert "kb_snapshot_entity_nodes" in write_statements[0]
    assert "kb_graph_edges" in write_statements[1]
    assert "kb_snapshot_entity_node_supports" in write_statements[2]


def test_shared_source_requires_revision_local_anchor_neighborhood() -> None:
    revision, chunk = uuid4(), uuid4()
    same_chunk_left = _evidence(revision_id=revision, chunk_id=chunk, start=1, end=2)
    same_chunk_right = _evidence(revision_id=revision, chunk_id=chunk, start=100, end=101)
    nearby = _evidence(revision_id=revision, start=20, end=22)
    nearby_right = _evidence(revision_id=revision, start=40, end=42)
    far = _evidence(revision_id=revision, start=80, end=81)
    other_revision = _evidence(start=20, end=22)

    assert evidence_spans_are_neighbors(same_chunk_left, same_chunk_right)
    assert evidence_spans_are_neighbors(nearby, nearby_right)
    assert not evidence_spans_are_neighbors(nearby, far)
    assert not evidence_spans_are_neighbors(nearby, other_revision)


def test_graph_model_has_typed_composite_endpoint_and_evidence_fks() -> None:
    constraint_names = {constraint.name for constraint in GraphEdge.__table__.constraints}
    assert "ck_kb_graph_edge_predicate" in constraint_names
    assert "ck_kb_graph_edge_direction" in constraint_names
    assert "ck_kb_graph_edge_valid_range" in constraint_names
    assert "fk_kb_graph_edges_src_entity" in constraint_names
    assert "fk_kb_graph_edges_dst_entity" in constraint_names
    assert "fk_kb_graph_edges_src_node" in constraint_names
    assert "fk_kb_graph_edges_dst_node" in constraint_names
    assert "fk_kb_graph_edges_evidence_span" in constraint_names
    assert "fk_kb_graph_edges_related_source_revision" in constraint_names


def test_projection_gc_deletes_graph_dependencies_in_fk_safe_order() -> None:
    order = {model: index for index, model in enumerate(_PROJECTION_MODELS)}

    assert order[KbSnapshotEntityNodeSupport] < order[GraphEdge]
    assert order[GraphEdge] < order[KbSnapshotEntityNode]
    assert order[KbSnapshotEntityNode] < order[SnapshotFactSupport]


def test_line_window_rejects_negative_configuration() -> None:
    evidence = _evidence()
    with pytest.raises(ValueError, match="line_window"):
        evidence_spans_are_neighbors(evidence, evidence, line_window=-1)
