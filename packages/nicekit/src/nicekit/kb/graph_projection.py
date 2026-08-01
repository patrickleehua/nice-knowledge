"""Governed, snapshot-scoped canonical-entity graph materialization."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.projections import (
    ProjectionPayloadError,
    _load_claims,
    _upsert_rows,
)
from nicekit.models.kb import (
    CanonicalEntity,
    EvidenceSpan,
    FactClaim,
    GraphDirection,
    GraphEdge,
    GraphEdgeKind,
    GraphPredicate,
    KbSnapshotEntityNode,
    KbSnapshotEntityNodeSupport,
    SnapshotFactSupport,
)

if TYPE_CHECKING:
    from nicekit.kb.snapshot import SnapshotBuildContext


DIRECT_EDGE_WEIGHT = 1.0
SHARED_SOURCE_WEIGHT = 0.25
SHARED_SOURCE_LINE_WINDOW = 20

DIRECT_PREDICATE_DIRECTIONS: dict[GraphPredicate, GraphDirection] = {
    GraphPredicate.LOCATED_IN: GraphDirection.DIRECTED,
    GraphPredicate.NEAR: GraphDirection.UNDIRECTED,
    GraphPredicate.INCLUDES: GraphDirection.DIRECTED,
    GraphPredicate.PART_OF: GraphDirection.DIRECTED,
    GraphPredicate.SERVES: GraphDirection.DIRECTED,
    GraphPredicate.SUPPORTS: GraphDirection.DIRECTED,
    GraphPredicate.DERIVED_FROM: GraphDirection.DIRECTED,
    GraphPredicate.RELATED: GraphDirection.UNDIRECTED,
}
DIRECT_PREDICATES = frozenset(predicate.value for predicate in DIRECT_PREDICATE_DIRECTIONS)


@dataclass(frozen=True, slots=True)
class _BoundEvidence:
    claim: FactClaim
    evidence: EvidenceSpan
    entity_id: UUID


def graph_edge_id(
    snapshot_id: UUID,
    *,
    edge_kind: GraphEdgeKind,
    predicate: GraphPredicate,
    src_entity_id: UUID,
    dst_entity_id: UUID,
    evidence_span_id: UUID,
    related_evidence_span_id: UUID | None = None,
) -> UUID:
    """Return the stable identity of one evidence-backed snapshot edge."""

    return uuid5(
        snapshot_id,
        ":".join(
            (
                "graph_edge",
                edge_kind.value,
                predicate.value,
                str(src_entity_id),
                str(dst_entity_id),
                str(evidence_span_id),
                str(related_evidence_span_id or ""),
            )
        ),
    )


def evidence_spans_are_neighbors(
    left: EvidenceSpan,
    right: EvidenceSpan,
    *,
    line_window: int = SHARED_SOURCE_LINE_WINDOW,
) -> bool:
    """Require comparable anchors inside one revision, never document-only co-source."""

    if line_window < 0:
        raise ValueError("line_window must not be negative")
    if left.revision_id != right.revision_id:
        return False
    if left.chunk_id is not None and left.chunk_id == right.chunk_id:
        return True
    if left.cell_ref is not None and left.cell_ref == right.cell_ref and left.page == right.page:
        return True
    if None in (left.start_line, left.end_line, right.start_line, right.end_line):
        return False
    if left.page is not None and right.page is not None and left.page != right.page:
        return False
    gap = max(
        0,
        int(left.start_line) - int(right.end_line),
        int(right.start_line) - int(left.end_line),
    )
    return gap <= line_window


def _source_entity_id(claim: FactClaim) -> UUID:
    if claim.subject_entity_id is None:
        raise ProjectionPayloadError(
            f"graph claim {claim.id} must bind its subject to a canonical entity"
        )
    if claim.subject_type == "canonical_entity" and claim.subject_id != claim.subject_entity_id:
        raise ProjectionPayloadError(
            f"graph claim {claim.id} has conflicting canonical subject bindings"
        )
    return claim.subject_entity_id


def _target_entity_id(claim: FactClaim) -> UUID:
    if claim.object_entity_id is None:
        raise ProjectionPayloadError(
            f"graph claim {claim.id} must bind its object to a canonical entity"
        )
    return claim.object_entity_id


def graph_node_id(snapshot_id: UUID, entity_id: UUID) -> UUID:
    return uuid5(snapshot_id, f"graph_node:{entity_id}")


def graph_node_support_id(
    snapshot_id: UUID,
    *,
    entity_id: UUID,
    support_type: str,
    support_id: UUID | None,
) -> UUID:
    return uuid5(
        snapshot_id,
        f"graph_node_support:{entity_id}:{support_type}:{support_id or 'pin'}",
    )


def _ordered_undirected(src_entity_id: UUID, dst_entity_id: UUID) -> tuple[UUID, UUID]:
    return (
        (src_entity_id, dst_entity_id)
        if src_entity_id.int < dst_entity_id.int
        else (dst_entity_id, src_entity_id)
    )


def _validity_intersection(
    left: FactClaim, right: FactClaim
) -> tuple[date | None, date | None] | None:
    starts = [value for value in (left.valid_from, right.valid_from) if value is not None]
    ends = [value for value in (left.valid_to, right.valid_to) if value is not None]
    valid_from = max(starts) if starts else None
    valid_to = min(ends) if ends else None
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        return None
    return valid_from, valid_to


def _neighbor_evidence_pairs(
    bindings: list[_BoundEvidence],
) -> set[tuple[UUID, UUID]]:
    """Generate local candidate pairs without a revision-wide quadratic scan."""

    pairs: set[tuple[UUID, UUID]] = set()

    def add_pair(left: _BoundEvidence, right: _BoundEvidence) -> None:
        if left.evidence.id == right.evidence.id:
            return
        ids = sorted((left.evidence.id, right.evidence.id), key=lambda item: item.int)
        pairs.add((ids[0], ids[1]))

    by_chunk: dict[UUID, list[_BoundEvidence]] = defaultdict(list)
    by_cell: dict[tuple[int | None, str], list[_BoundEvidence]] = defaultdict(list)
    by_page: dict[int | None, list[_BoundEvidence]] = defaultdict(list)
    for binding in bindings:
        evidence = binding.evidence
        if evidence.chunk_id is not None:
            by_chunk[evidence.chunk_id].append(binding)
        if evidence.cell_ref is not None:
            by_cell[(evidence.page, evidence.cell_ref)].append(binding)
        if evidence.start_line is not None and evidence.end_line is not None:
            by_page[evidence.page].append(binding)

    for group in (*by_chunk.values(), *by_cell.values()):
        for left, right in combinations(group, 2):
            add_pair(left, right)

    for group in by_page.values():
        ordered = sorted(
            group,
            key=lambda item: (
                int(item.evidence.start_line or 0),
                int(item.evidence.end_line or 0),
                item.evidence.id.int,
            ),
        )
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if (
                    int(right.evidence.start_line or 0) - int(left.evidence.end_line or 0)
                    > SHARED_SOURCE_LINE_WINDOW
                ):
                    break
                if evidence_spans_are_neighbors(left.evidence, right.evidence):
                    add_pair(left, right)
    return pairs


class GraphProjectionBuilder:
    name = "graph"
    version = "2"

    async def build(self, session: AsyncSession, context: SnapshotBuildContext) -> dict[str, Any]:
        claims = await _load_claims(session, context)
        claim_by_id = {claim.id: claim for claim in claims}
        support_evidence_rows: list[tuple[SnapshotFactSupport, EvidenceSpan]] = []
        if claim_by_id:
            support_evidence_rows = list(
                (
                    await session.execute(
                        select(SnapshotFactSupport, EvidenceSpan)
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
                        .where(
                            SnapshotFactSupport.snapshot_id == context.snapshot_id,
                            SnapshotFactSupport.org_id == context.org_id,
                            SnapshotFactSupport.kb_id == context.kb_id,
                            SnapshotFactSupport.fact_claim_id.in_(claim_by_id),
                        )
                        .order_by(
                            SnapshotFactSupport.fact_claim_id,
                            SnapshotFactSupport.evidence_span_id,
                        )
                    )
                ).all()
            )
        supported_claim_ids = {
            support.fact_claim_id for support, _evidence in support_evidence_rows
        }
        if supported_claim_ids != set(claim_by_id):
            missing = sorted(set(claim_by_id) - supported_claim_ids, key=str)
            raise ProjectionPayloadError(
                "graph projection fact support ledger is incomplete"
                + (f": {missing[0]}" if missing else "")
            )

        evidence_by_claim: dict[UUID, list[EvidenceSpan]] = defaultdict(list)
        fact_node_supports: dict[UUID, set[UUID]] = defaultdict(set)
        for support, evidence in support_evidence_rows:
            evidence_by_claim[evidence.fact_claim_id].append(evidence)
            claim = claim_by_id[support.fact_claim_id]
            if claim.subject_entity_id is not None:
                fact_node_supports[claim.subject_entity_id].add(support.id)
            if claim.object_entity_id is not None:
                fact_node_supports[claim.object_entity_id].add(support.id)

        direct_specs: list[tuple[FactClaim, UUID, UUID, GraphPredicate, GraphDirection]] = []
        for claim in claims:
            if claim.predicate not in DIRECT_PREDICATES:
                continue
            predicate = GraphPredicate(claim.predicate)
            direction = DIRECT_PREDICATE_DIRECTIONS[predicate]
            src_entity_id = _source_entity_id(claim)
            dst_entity_id = _target_entity_id(claim)
            if src_entity_id == dst_entity_id:
                raise ProjectionPayloadError(f"graph claim {claim.id} forms a self-loop")
            if direction == GraphDirection.UNDIRECTED:
                src_entity_id, dst_entity_id = _ordered_undirected(src_entity_id, dst_entity_id)
            direct_specs.append((claim, src_entity_id, dst_entity_id, predicate, direction))

        shared_bindings: list[_BoundEvidence] = []
        for claim in claims:
            if claim.predicate in DIRECT_PREDICATES or claim.subject_entity_id is None:
                continue
            for evidence in evidence_by_claim.get(claim.id, ()):
                shared_bindings.append(_BoundEvidence(claim, evidence, claim.subject_entity_id))

        supported_entity_ids = set(fact_node_supports)
        supported_entities = (
            list(
                (
                    await session.execute(
                        select(CanonicalEntity)
                        .where(
                            CanonicalEntity.id.in_(supported_entity_ids),
                            CanonicalEntity.org_id == context.org_id,
                            CanonicalEntity.kb_id == context.kb_id,
                            CanonicalEntity.merged_into_entity_id.is_(None),
                        )
                        .order_by(CanonicalEntity.id)
                    )
                )
                .scalars()
                .all()
            )
            if supported_entity_ids
            else []
        )
        if {entity.id for entity in supported_entities} != supported_entity_ids:
            raise ProjectionPayloadError(
                "graph projection contains a missing, merged, or cross-KB canonical endpoint"
            )
        pinned_entities = list(
            (
                await session.execute(
                    select(CanonicalEntity)
                    .where(
                        CanonicalEntity.org_id == context.org_id,
                        CanonicalEntity.kb_id == context.kb_id,
                        CanonicalEntity.is_pinned.is_(True),
                        CanonicalEntity.merged_into_entity_id.is_(None),
                    )
                    .order_by(CanonicalEntity.id)
                )
            )
            .scalars()
            .all()
        )
        entity_by_id = {entity.id: entity for entity in [*supported_entities, *pinned_entities]}

        edge_rows: list[dict[str, Any]] = []
        counts = Counter[str]()
        for claim, src_id, dst_id, predicate, direction in direct_specs:
            evidences = evidence_by_claim.get(claim.id, ())
            if not evidences:
                raise ProjectionPayloadError(
                    f"graph claim {claim.id} has no evidence in the snapshot manifest"
                )
            for evidence in evidences:
                edge_rows.append(
                    {
                        "id": graph_edge_id(
                            context.snapshot_id,
                            edge_kind=GraphEdgeKind.DIRECT,
                            predicate=predicate,
                            src_entity_id=src_id,
                            dst_entity_id=dst_id,
                            evidence_span_id=evidence.id,
                        ),
                        "org_id": context.org_id,
                        "kb_id": context.kb_id,
                        "snapshot_id": context.snapshot_id,
                        "src_entity_id": src_id,
                        "dst_entity_id": dst_id,
                        "predicate": predicate.value,
                        "direction": direction.value,
                        "edge_kind": GraphEdgeKind.DIRECT.value,
                        "weight": DIRECT_EDGE_WEIGHT,
                        "valid_from": claim.valid_from,
                        "valid_to": claim.valid_to,
                        "fact_claim_id": claim.id,
                        "evidence_span_id": evidence.id,
                        "related_fact_claim_id": None,
                        "related_evidence_span_id": None,
                        "source_revision_id": evidence.revision_id,
                        "related_source_revision_id": None,
                    }
                )
                counts[GraphEdgeKind.DIRECT.value] += 1

        binding_by_evidence_id = {binding.evidence.id: binding for binding in shared_bindings}
        for left_id, right_id in sorted(
            _neighbor_evidence_pairs(shared_bindings), key=lambda pair: (pair[0].int, pair[1].int)
        ):
            left = binding_by_evidence_id[left_id]
            right = binding_by_evidence_id[right_id]
            if left.claim.id == right.claim.id or left.entity_id == right.entity_id:
                continue
            validity = _validity_intersection(left.claim, right.claim)
            if validity is None:
                continue
            src_id, dst_id = _ordered_undirected(left.entity_id, right.entity_id)
            if src_id != left.entity_id:
                left, right = right, left
            edge_rows.append(
                {
                    "id": graph_edge_id(
                        context.snapshot_id,
                        edge_kind=GraphEdgeKind.SHARED_SOURCE,
                        predicate=GraphPredicate.SHARED_CONTEXT,
                        src_entity_id=src_id,
                        dst_entity_id=dst_id,
                        evidence_span_id=left.evidence.id,
                        related_evidence_span_id=right.evidence.id,
                    ),
                    "org_id": context.org_id,
                    "kb_id": context.kb_id,
                    "snapshot_id": context.snapshot_id,
                    "src_entity_id": src_id,
                    "dst_entity_id": dst_id,
                    "predicate": GraphPredicate.SHARED_CONTEXT.value,
                    "direction": GraphDirection.UNDIRECTED.value,
                    "edge_kind": GraphEdgeKind.SHARED_SOURCE.value,
                    "weight": SHARED_SOURCE_WEIGHT,
                    "valid_from": validity[0],
                    "valid_to": validity[1],
                    "fact_claim_id": left.claim.id,
                    "evidence_span_id": left.evidence.id,
                    "related_fact_claim_id": right.claim.id,
                    "related_evidence_span_id": right.evidence.id,
                    "source_revision_id": left.evidence.revision_id,
                    "related_source_revision_id": right.evidence.revision_id,
                }
            )
            counts[GraphEdgeKind.SHARED_SOURCE.value] += 1

        edge_endpoint_ids = {
            entity_id
            for row in edge_rows
            for entity_id in (row["src_entity_id"], row["dst_entity_id"])
        }
        if not edge_endpoint_ids.issubset(entity_by_id):
            raise ProjectionPayloadError(
                "graph edge endpoint is missing from the candidate entity set"
            )

        node_support_specs: dict[UUID, set[tuple[str, UUID | None]]] = defaultdict(set)
        for entity_id, support_ids in fact_node_supports.items():
            node_support_specs[entity_id].update(("fact", support_id) for support_id in support_ids)
        for row in edge_rows:
            edge_id = row["id"]
            node_support_specs[row["src_entity_id"]].add(("edge", edge_id))
            node_support_specs[row["dst_entity_id"]].add(("edge", edge_id))
        for entity in pinned_entities:
            node_support_specs[entity.id].add(("pin", None))

        node_rows: list[dict[str, Any]] = []
        for entity_id in sorted(node_support_specs, key=str):
            entity = entity_by_id[entity_id]
            supports = node_support_specs[entity_id]
            support_types = {support_type for support_type, _support_id in supports}
            evidence_support_count = sum(
                support_type != "pin" for support_type, _support_id in supports
            )
            if evidence_support_count == 0:
                support_status = "pinned"
                support_source = "pin"
            else:
                support_status = "supported"
                support_source = next(iter(support_types)) if len(support_types) == 1 else "mixed"
            node_rows.append(
                {
                    "id": graph_node_id(context.snapshot_id, entity_id),
                    "org_id": context.org_id,
                    "kb_id": context.kb_id,
                    "snapshot_id": context.snapshot_id,
                    "entity_id": entity_id,
                    "entity_type": entity.entity_type,
                    "display_name": entity.canonical_name,
                    "support_status": support_status,
                    "support_source": support_source,
                    "pinned_at_build": entity.is_pinned,
                    "support_count": evidence_support_count,
                }
            )

        projected_node_ids = {row["entity_id"] for row in node_rows}
        if not edge_endpoint_ids.issubset(projected_node_ids):
            raise ProjectionPayloadError(
                "graph edge endpoint is missing from the snapshot node projection"
            )

        await _upsert_rows(session, KbSnapshotEntityNode, node_rows)
        await _upsert_rows(session, GraphEdge, edge_rows)

        node_support_rows: list[dict[str, Any]] = []
        for entity_id in sorted(node_support_specs, key=str):
            for support_type, support_id in sorted(
                node_support_specs[entity_id],
                key=lambda item: (item[0], str(item[1] or "")),
            ):
                node_support_rows.append(
                    {
                        "id": graph_node_support_id(
                            context.snapshot_id,
                            entity_id=entity_id,
                            support_type=support_type,
                            support_id=support_id,
                        ),
                        "org_id": context.org_id,
                        "kb_id": context.kb_id,
                        "snapshot_id": context.snapshot_id,
                        "entity_id": entity_id,
                        "support_type": support_type,
                        "fact_support_id": (support_id if support_type == "fact" else None),
                        "graph_edge_id": (support_id if support_type == "edge" else None),
                    }
                )
        await _upsert_rows(
            session,
            KbSnapshotEntityNodeSupport,
            node_support_rows,
        )
        return {
            "row_count": len(edge_rows),
            "node_count": len(node_rows),
            "node_support_count": len(node_support_rows),
            "edge_kinds": dict(sorted(counts.items())),
            "direct_weight": DIRECT_EDGE_WEIGHT,
            "shared_source_weight": SHARED_SOURCE_WEIGHT,
            "shared_source_line_window": SHARED_SOURCE_LINE_WINDOW,
        }


__all__ = [
    "DIRECT_EDGE_WEIGHT",
    "DIRECT_PREDICATES",
    "GraphProjectionBuilder",
    "SHARED_SOURCE_LINE_WINDOW",
    "SHARED_SOURCE_WEIGHT",
    "evidence_spans_are_neighbors",
    "graph_edge_id",
    "graph_node_id",
    "graph_node_support_id",
]
