"""Optional 1-2 hop recall over the active governed graph projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb.graph import _load_visible_graph_projection
from nicekit.kb.projections import parse_card_ref
from nicekit.models.kb import (
    FactClaim,
    GraphDirection,
    KbChunk,
    KbSnapshotEntityNode,
)


def _fts_config():
    """全文检索配置名参数化(settings.kb_fts_regconfig,baseline 迁移已建)。"""
    return literal_column(f"'{get_settings().kb_fts_regconfig}'::regconfig")


@dataclass(frozen=True, slots=True)
class GraphRecallCandidate:
    chunk: KbChunk
    kind: str
    entity_id: UUID
    score: float
    hops: int
    edge_ids: tuple[UUID, ...]
    predicates: tuple[str, ...]


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


async def graph_recall_candidates(
    session: AsyncSession,
    query: str,
    *,
    kb_ids: list[UUID] | None,
    max_hops: int,
    top_k: int,
    as_of: date,
) -> list[GraphRecallCandidate]:
    """Expand only current visible nodes/edges and restore supported snapshot cards."""

    if max_hops not in (1, 2):
        raise ValueError("graph max_hops must be 1 or 2")
    if top_k <= 0:
        raise ValueError("graph top_k must be positive")
    projection = await _load_visible_graph_projection(session, kb_ids)
    visible_entity_ids = set(projection.node_by_entity_id)
    if not visible_entity_ids:
        return []

    tsquery = func.websearch_to_tsquery(_fts_config(), query)
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
            or card_ref[0] != claim.predicate
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
            )
        )
    return sorted(
        result,
        key=lambda item: (-item.score, item.hops, item.kind, item.entity_id.int),
    )[:top_k]


__all__ = ["GraphRecallCandidate", "graph_recall_candidates"]
