"""统一检索:structured/sparse/dense/graph 四路混合召回与重排。

RLS 保证只能读到本 org、显式共享与平台层;相关性决定主序,layer 仅同分破局。
空结果如实返回,不编造;过期条目如实标注 stale(来源文档 expires_at 已过),
不过滤——陈旧资料仍有价值,但 LLM 与前端必须看到标记。

结构化通道是**领域无关**的(MIGRATION-PLAN B9-B14):不认识任何行业字段,
只认 ``KbEntityType`` 注册表——按 ``type_key`` 分组召回 ``KbEntity``,
过滤条件由调用方以 :class:`StructuredFilter` 声明并按类型的
``filterable_fields`` 校验(复用 ``entity_lookup.build_entity_filters``),
词面打分作用在 ``name`` 提升列与类型声明的 text 型属性上。
"""

import hashlib
import json
import logging
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from itertools import zip_longest
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Select,
    and_,
    bindparam,
    case,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.domain.kb_media import (
    ImageSourceCitation,
    KnowledgeMediaReference,
    NormalizedBBox,
)
from nicekit.kb.effective_scope import (
    active_knowledge_base_filter,
    effective_chunk_filter,
    live_document_revision_filter,
    live_snapshot_projection_filter,
)
from nicekit.kb.embedding import (
    EmbeddingFingerprint,
    EmbeddingService,
    EmbeddingUnavailableError,
    normalize_embedding_config,
)
from nicekit.kb.entity_lookup import EntityLookupError, build_entity_filters
from nicekit.kb.entity_types import FILTERABLE_TYPES, get_entity_type
from nicekit.kb.graph_search import graph_recall_candidates
from nicekit.kb.metrics import (
    KB_SEARCH_DENSE_DEGRADED,
    KB_SEARCH_EMPTY,
    KB_SEARCH_REFUSALS,
    KB_SEARCH_RERANK_DEGRADED,
)
from nicekit.kb.projections import (
    active_projection_filter,
    load_custom_entity_types,
    parse_card_ref,
)
from nicekit.kb.rerank import (
    RERANK_TOP_N,
    Reranker,
    RerankError,
    get_rerank_service,
)
from nicekit.kb.retrieval_projection import IMAGE_SNAPSHOT_META_KEY
from nicekit.llm.capability_routes import (
    EMBEDDING_ROUTE_TASK,
    capability_route_endpoints,
    provider_model_endpoint,
)
from nicekit.llm.runtime_config import runtime_overrides
from nicekit.llm.service import LlmBudgetExceededError
from nicekit.models.kb import (
    EMBEDDING_DIM,
    DocType,
    DocumentLifecycleStatus,
    DocumentRevision,
    EmbeddingMigrationCampaign,
    EmbeddingReindexJob,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    KbChunk,
    KbChunkEmbedding,
    KbEntity,
    KbEntityType,
    KbImageAsset,
    KbPage,
    KnowledgeBase,
    KnowledgeSnapshot,
    RevisionStatus,
    SnapshotFactSupport,
    SourceDocument,
)

logger = logging.getLogger(__name__)

_RRF_K = 60  # structured、sparse、dense、graph 四路统一融合常数。
_SPARSE_FALLBACK_MIN_TERMS = 2
_SPARSE_FALLBACK_MAX_TERMS = 8
# 锚点=长名词(通常是地名/专名),SQL 与 Python 双侧都必须命中;非锚点词组按 quorum 松绑,
# 避免自然问句里任一弱词缺失就整句零召回(R4 验收 bad case 的根因)。
_SPARSE_ANCHOR_MIN_LENGTH = 3
_SPARSE_FALLBACK_QUORUM = 0.6
_SPARSE_FALLBACK_FETCH_MULTIPLIER = 3
_MUST_INCLUDE_MAX_TERMS = 5
# 稀疏回退的停用词与同义扩展是**语料相关**的,SDK 只给空默认,宿主按自己的
# 语料注册(MIGRATION-PLAN B15/B16);注册值进 search_execution_manifest 存证。
_SPARSE_FALLBACK_NOISE: frozenset[str] = frozenset()
_SPARSE_FALLBACK_SYNONYMS: dict[str, tuple[str, ...]] = {}


def set_sparse_fallback_noise(terms: Iterable[str] | None) -> None:
    """注册稀疏回退的噪声词(不参与词组构造);None/空 = 不过滤。"""
    global _SPARSE_FALLBACK_NOISE
    _SPARSE_FALLBACK_NOISE = frozenset(str(term) for term in (terms or ()))


def set_sparse_fallback_synonyms(
    synonyms: Mapping[str, Iterable[str]] | None,
) -> None:
    """注册稀疏回退的同义扩展表(term -> 备选词元);None/空 = 不扩展。"""
    global _SPARSE_FALLBACK_SYNONYMS
    _SPARSE_FALLBACK_SYNONYMS = {
        str(term): tuple(str(item) for item in expansions)
        for term, expansions in (synonyms or {}).items()
    }


def _fts_config_name() -> str:
    """全文检索配置名参数化(baseline 迁移已按同名创建 zhparser 配置)。"""
    return get_settings().kb_fts_regconfig


def _fts_config():
    return literal_column(f"'{_fts_config_name()}'::regconfig")


_SIMPLE_SEARCH_CONFIG = literal_column("'simple'::regconfig")
_HNSW_ITERATIVE_SCAN = "relaxed_order"
_MAX_TOP_K = 100
_DEFAULT_RERANKER = object()
_DEFAULT_DUAL_READ = object()
#: wiki 页面在检索侧的 kind(唯一的非实体结构化 kind)
PAGE_KIND = "page"
_CITATION_REFS_KEY = "_citation_refs"
_FULLWIDTH_ASCII = "".join(chr(code) for code in range(0xFF01, 0xFF5F))
_ASCII_PRINTABLE = "".join(chr(code) for code in range(0x21, 0x7F))
_IMAGE_ALT_TEXT_MAX_CHARS = 2_000
_IMAGE_CITATION_QUOTE_MAX_CHARS = 4_000


@dataclass(frozen=True)
class SearchHit:
    kind: str  # 已注册实体类型 key / page / chunk
    layer: str  # tenant / platform / shared
    kb_id: str
    source: str
    confidence: float
    data: dict
    citation: dict | None = None
    media_refs: tuple[KnowledgeMediaReference, ...] = ()


#: 声明式过滤算子:eq(等值)/min(下界)/max(上界),与
#: ``entity_lookup.build_entity_filters`` 的 ``field`` / ``field__min`` / ``field__max``
#: 键形态一一对应(number/date 才支持范围)。
STRUCTURED_FILTER_OPS = ("eq", "min", "max")


class StructuredFilterError(ValueError):
    """过滤条件未在实体类型的 filterable_fields 中声明,或算子不合法。"""


@dataclass(frozen=True, slots=True)
class StructuredFilter:
    """一条领域无关的结构化过滤:``KbEntity.attributes[field] <op> value``。

    ``field`` 必须在目标 ``KbEntityType.filterable_fields`` 中声明,值类型由
    类型声明(text/number/date)决定;未声明的字段一律拒绝,防任意 JSONB 探测。
    """

    field: str
    op: str = "eq"
    value: Any = None

    def __post_init__(self) -> None:
        if self.op not in STRUCTURED_FILTER_OPS:
            raise StructuredFilterError(
                f"不支持的过滤算子 {self.op!r},可选 {'/'.join(STRUCTURED_FILTER_OPS)}"
            )
        if not str(self.field).strip():
            raise StructuredFilterError("过滤条件缺少 field")

    @property
    def lookup_key(self) -> str:
        return self.field if self.op == "eq" else f"{self.field}__{self.op}"


@dataclass(frozen=True)
class StructuredSearchQuery:
    """结构化通道的调用契约(替代 TF 的 StructuredSearchFilters/QuoteFilters)。

    - ``type_keys``:限定参与召回的实体类型;为空表示本 org 可见的全部注册类型。
      显式给出时,每个类型都必须能声明全部 ``filters`` 字段,否则抛
      :class:`StructuredFilterError`(调用方拼错字段要立刻可见)。
      不指定类型时,声明不了某字段的类型会被静默跳过。
    - ``filters``:声明式字段条件,进 SQL 硬过滤。
    - ``match_terms``:附加词面,与 query 一起参与 name/属性打分(不做硬过滤)。
    - ``must_include``:必含词,既进结构化打分加成,也驱动独立的 must_include 通道。
    """

    type_keys: tuple[str, ...] = ()
    filters: tuple[StructuredFilter, ...] = ()
    match_terms: tuple[str, ...] = ()
    must_include: tuple[str, ...] = ()

    def filters_for(self, entity_type: KbEntityType) -> list[Any] | None:
        """按类型声明校验并编译为 SQLAlchemy 条件;类型声明不了则返回 None。"""
        if not self.filters:
            return []
        lookup = {item.lookup_key: item.value for item in self.filters}
        try:
            return build_entity_filters(entity_type, lookup)
        except EntityLookupError as exc:
            if self.type_keys:
                raise StructuredFilterError(str(exc)) from exc
            return None


@dataclass(frozen=True)
class _SparseFallbackGroup:
    alternatives: tuple[tuple[str, ...], ...]
    phrase: bool = False
    required: bool = False


@dataclass(frozen=True)
class _SparseLexeme:
    alias: str
    value: str


CandidateKey = tuple[str, UUID]


@dataclass
class _Candidate:
    key: CandidateKey
    hit: SearchHit
    native_scores: dict[str, float]


@dataclass(frozen=True)
class DualReadTarget:
    config: dict
    kb_ids: list[UUID]
    manifest: dict


def _layer(org_id: UUID, row_org: UUID) -> str:
    if row_org == org_id:
        return "tenant"
    if row_org == get_settings().platform_org_id:
        return "platform"
    return "shared"


_LAYER_ORDER = {"tenant": 0, "shared": 1, "platform": 2}


def _tenant_first(hits: list[SearchHit]) -> list[SearchHit]:
    return sorted(
        hits,
        key=lambda hit: (
            -hit.confidence,
            _LAYER_ORDER.get(hit.layer, 9),
            hit.kind,
            str(hit.data.get("id", "")),
        ),
    )


async def _expired_doc_ids(session: AsyncSession, doc_ids: set) -> set:
    """给定 source_doc_id 集合,返回其中 expires_at 已过期的文档 id 集合。"""
    ids = {d for d in doc_ids if d is not None}
    if not ids:
        return set()
    rows = (
        (
            await session.execute(
                select(SourceDocument.id).where(
                    SourceDocument.id.in_(ids),
                    SourceDocument.expires_at < datetime.now(UTC),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def _effective_chunk_filter():
    return effective_chunk_filter()


def _stale_flag(is_stale: bool) -> dict:
    return {"stale": True} if is_stale else {}


# ---- 实体卡片还原:卡片 chunk 命中 → 实体 SearchHit -------------------------
#
# 卡片行的 source_ref 形如 "{kind}:{uuid}"(projections.card_source_ref)。
# kind 是 wiki 页面的 PAGE_KIND,或任意已注册的 KbEntityType.type_key;
# 实体数据一律从 KbEntity.attributes 直出(B14:TF 的五分支 _entity_full_data 已删)。

_CARD_SOURCE_PATHS = {PAGE_KIND: "kb_pages"}
_ENTITY_SOURCE_PATH = "kb_entities"


def _page_hit_data(row: KbPage) -> dict:
    return {
        "id": str(row.id),
        "title": row.title,
        "page_type": row.page_type,
        "content": row.content,
    }


def _entity_hit_data(row: KbEntity) -> dict:
    """通用实体的外发数据:提升列 + attributes 全量展平(名字同值)。"""
    return {
        "id": str(row.id),
        "entity_type_key": row.entity_type_key,
        "name": row.name,
        **(row.attributes or {}),
    }


def dedupe_card_hits(existing: list[SearchHit], card_hits: list[SearchHit]) -> list[SearchHit]:
    """同一实体已被 structured 通道命中(或卡片间重复)时去重。"""
    seen = {(h.kind, h.data.get("id")) for h in existing}
    fresh: list[SearchHit] = []
    for hit in card_hits:
        key = (hit.kind, hit.data.get("id"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(hit)
    return fresh


async def _restore_card_hits(
    session: AsyncSession,
    org_id: UUID,
    card_refs: list[tuple[KbChunk, float, str, UUID]],
    max_score: float,
) -> list[SearchHit]:
    """卡片 chunk → 实体 SearchHit(confidence 沿用 RRF 归一分)。
    实体已删而卡片残留时跳过并 warning,不炸检索。"""
    rows_by_kind: dict[str, dict[UUID, Any]] = {}
    for kind in {k for _, _, k, _ in card_refs}:
        ids = [eid for _, _, k, eid in card_refs if k == kind]
        if kind == PAGE_KIND:
            statement = select(KbPage).where(
                KbPage.id.in_(ids),
                active_projection_filter(KbPage),
                live_snapshot_projection_filter(KbPage, "wiki_page"),
            )
        else:  # 任意注册类型:通用实体表,按 type_key 双重校验
            statement = select(KbEntity).where(
                KbEntity.id.in_(ids),
                KbEntity.entity_type_key == kind,
                active_projection_filter(KbEntity),
                live_snapshot_projection_filter(KbEntity, "kb_entity"),
            )
        rows = (await session.execute(statement)).scalars().all()
        rows_by_kind[kind] = {row.id: row for row in rows}
    # 实体行无行级时效,陈旧度看来源文档 expires_at(与 structured 通道同口径)
    expired_docs = await _expired_doc_ids(
        session,
        {
            getattr(row, "source_doc_id", None)
            for rows in rows_by_kind.values()
            for row in rows.values()
        },
    )
    hits: list[SearchHit] = []
    for chunk, score, kind, entity_id in card_refs:
        row = rows_by_kind.get(kind, {}).get(entity_id)
        if row is None or row.snapshot_id != chunk.snapshot_id:
            logger.warning("实体卡片残留:%s 对应实体不存在,跳过", chunk.source_ref)
            continue
        if kind == PAGE_KIND:
            full_data = _page_hit_data(row)
            source_path = _CARD_SOURCE_PATHS[PAGE_KIND]
            stale = False
        else:
            full_data = _entity_hit_data(row)
            source_path = _ENTITY_SOURCE_PATH
            stale = row.source_doc_id in expired_docs
        hits.append(
            SearchHit(
                kind=kind,
                layer=_layer(org_id, row.org_id),
                kb_id=str(row.kb_id),
                source=f"{source_path}/{row.id}",
                confidence=round(score / max_score, 4),
                data={
                    **full_data,
                    # 无 snapshot_id 时 _attach_citations 无法给实体附 fact_evidence
                    "snapshot_id": (
                        str(row.snapshot_id) if getattr(row, "snapshot_id", None) else None
                    ),
                    "via": "semantic_card",
                    **_stale_flag(stale),
                },
            )
        )
    return hits


def filter_vector_hits(rows: list[tuple[KbChunk, float]], max_distance: float) -> list[KbChunk]:
    """向量命中按 cosine 距离阈值过滤(KB-5B):bge-m3 余弦距离口径,
    语义相关通常 <0.55,乱码/无关查询普遍 >0.68(见 settings.kb_vector_max_distance)。
    relaxed iterative scan 允许轻微失序，因此在 Python 严格按距离重排后再交给 RRF。"""
    ordered = sorted(
        ((chunk, float(dist)) for chunk, dist in rows if dist is not None),
        key=lambda item: (item[1], str(item[0].id)),
    )
    return [chunk for chunk, dist in ordered if dist <= max_distance]


def merge_dense_candidates(
    versioned: list[tuple[KbChunk, float]],
    legacy: list[tuple[KbChunk, float]],
    *,
    max_distance: float,
    top_k: int,
) -> list[KbChunk]:
    """Merge same-model stores; versioned rows override legacy duplicates."""
    _validate_top_k(top_k)
    best: dict[UUID, tuple[KbChunk, float]] = {}
    for chunk, raw_distance in versioned:
        if raw_distance is None:
            continue
        distance = float(raw_distance)
        if distance > max_distance:
            continue
        previous = best.get(chunk.id)
        if previous is None or distance < previous[1]:
            best[chunk.id] = (chunk, distance)
    for chunk, raw_distance in legacy:
        if raw_distance is None or chunk.id in best:
            continue
        distance = float(raw_distance)
        if distance > max_distance:
            continue
        previous = best.get(chunk.id)
        if previous is None or distance < previous[1]:
            best[chunk.id] = (chunk, distance)
    return filter_vector_hits(list(best.values()), max_distance)[:top_k]


def merge_dense_candidates_scored(
    versioned: list[tuple[KbChunk, float]],
    legacy: list[tuple[KbChunk, float]],
    *,
    max_distance: float,
    top_k: int,
) -> list[tuple[KbChunk, float]]:
    """Merge one model's stores and retain cosine distance for native scoring."""
    _validate_top_k(top_k)
    best: dict[UUID, tuple[KbChunk, float]] = {}
    for chunk, raw_distance in versioned:
        if raw_distance is None:
            continue
        distance = float(raw_distance)
        if distance > max_distance:
            continue
        previous = best.get(chunk.id)
        if previous is None or distance < previous[1]:
            best[chunk.id] = (chunk, distance)
    for chunk, raw_distance in legacy:
        if raw_distance is None or chunk.id in best:
            continue
        distance = float(raw_distance)
        if distance > max_distance:
            continue
        previous = best.get(chunk.id)
        if previous is None or distance < previous[1]:
            best[chunk.id] = (chunk, distance)
    return sorted(best.values(), key=lambda item: (item[1], str(item[0].id)))[:top_k]


def merge_dense_rankings(
    rankings: list[list[tuple[KbChunk, float]]],
) -> list[tuple[KbChunk, float]]:
    """Collapse a dual-read window into one dense ranking and therefore one RRF vote."""
    merged: dict[UUID, tuple[int, float, KbChunk]] = {}
    for ranking in rankings:
        for rank, (chunk, distance) in enumerate(ranking, start=1):
            current = merged.get(chunk.id)
            candidate = (rank, float(distance), chunk)
            if current is None or candidate[:2] < current[:2]:
                merged[chunk.id] = candidate
    ordered = sorted(merged.values(), key=lambda item: (item[0], item[1], str(item[2].id)))
    return [(chunk, distance) for _rank, distance, chunk in ordered]


def _validate_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= _MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {_MAX_TOP_K}")


def _vector_candidate_limit(top_k: int) -> int:
    _validate_top_k(top_k)
    return max(top_k * 3, 50)


def search_execution_manifest(*, top_k: int) -> dict[str, Any]:
    """Describe the constants and settings used by this exact search execution."""
    _validate_top_k(top_k)
    return {
        "top_k": top_k,
        "rrf_k": _RRF_K,
        "max_top_k": _MAX_TOP_K,
        "candidate_limit": min(_vector_candidate_limit(top_k), _MAX_TOP_K),
        "vector_max_distance": get_settings().kb_vector_max_distance,
        "graph_channel": {
            "enabled": get_settings().kb_graph_search_enabled,
            "max_hops": get_settings().kb_graph_max_hops,
            "enable_gate_min_gain_points": 5.0,
        },
        "fts_config": _fts_config_name(),
        "sparse_query_formulation": {
            "primary": "websearch_to_tsquery",
            "fallback_token_source": "ts_debug_lexemes",
            "fallback_exact_config": "simple",
            "fallback_when": "primary_count_less_than_top_k",
            "selection_order": "length_desc_then_lexical",
            "drop_single_character": True,
            "drop_unmapped_tokens": True,
            "noise": sorted(_SPARSE_FALLBACK_NOISE),
            "min_terms": _SPARSE_FALLBACK_MIN_TERMS,
            "max_terms": _SPARSE_FALLBACK_MAX_TERMS,
            "synonyms": {
                term: list(expansions)
                for term, expansions in sorted(_SPARSE_FALLBACK_SYNONYMS.items())
            },
            "noun_compound": "exact_lexeme_or_parser_components",
            "fallback_boolean": "required_anchors_and_then_optional_groups_or",
            "anchor_min_length": _SPARSE_ANCHOR_MIN_LENGTH,
            "anchor_alias": "n",
            "quorum": _SPARSE_FALLBACK_QUORUM,
            "quorum_verification": "normalized_substring_on_chunk_text",
            "fetch_multiplier": _SPARSE_FALLBACK_FETCH_MULTIPLIER,
            "merge": "primary_first_dedupe_chunk_id_then_top_k",
        },
        "must_include_channel": {
            "query": "plainto_tsquery",
            "max_terms": _MUST_INCLUDE_MAX_TERMS,
            "merge": "round_robin_per_term",
        },
        "dense_requires_lexical_corroboration": True,
        "hnsw_iterative_scan": _HNSW_ITERATIVE_SCAN,
        "rerank_top_n": RERANK_TOP_N,
    }


def structured_search_manifest() -> dict[str, Any]:
    """结构化通道的执行契约存证(领域无关:不含任何行业字段)。"""
    return {
        "schema_version": "generic-entity-filters-v1",
        "entity_source": "kb_entities",
        "type_source": "kb_entity_types",
        "grouping": "by_type_key",
        "filter_ops": list(STRUCTURED_FILTER_OPS),
        "filter_validation": "kb_entity_types.filterable_fields",
        "filter_field_types": list(FILTERABLE_TYPES),
        "score_fields": {
            "name": _ENTITY_NAME_WEIGHT,
            "declared_text_attributes": _ENTITY_ATTRIBUTE_WEIGHT,
        },
        "must_include_bonus": _MUST_INCLUDE_BONUS,
        "page_channel": {"kind": PAGE_KIND, "fields": ["title", "content"]},
    }


def reciprocal_rank_fusion[RankKey](
    rankings: list[list[RankKey]],
    *,
    k: int = _RRF_K,
    ranking_scores: list[list[float]] | None = None,
) -> dict[RankKey, float]:
    """Fuse independent rankings; channel-native scores never cross model boundaries."""
    if ranking_scores is not None and (
        len(ranking_scores) != len(rankings)
        or any(
            len(scores) != len(ranking)
            for ranking, scores in zip(rankings, ranking_scores, strict=True)
        )
    ):
        raise ValueError("ranking scores must align with rankings")
    scores: dict[RankKey, float] = {}
    for channel_index, ranking in enumerate(rankings):
        channel_scores = ranking_scores[channel_index] if ranking_scores is not None else None
        effective_rank = 0
        previous_score: float | None = None
        for position, item_id in enumerate(ranking, start=1):
            native_score = channel_scores[position - 1] if channel_scores is not None else None
            if position == 1 or channel_scores is None or native_score != previous_score:
                effective_rank = position
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + effective_rank)
            previous_score = native_score
    return scores


async def _versioned_vector_candidates(
    session: AsyncSession,
    *,
    qvec: list[float],
    fingerprint: EmbeddingFingerprint,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> list[tuple[KbChunk, float]]:
    candidate_limit = _vector_candidate_limit(top_k)
    distance = KbChunkEmbedding.embedding.cosine_distance(qvec)  # type: ignore[union-attr]
    embedded_text = case(
        (
            and_(
                KbChunk.heading_path.is_not(None),
                func.length(KbChunk.heading_path) > 0,
            ),
            KbChunk.heading_path + literal("\n") + KbChunk.content,
        ),
        else_=KbChunk.content,
    )
    current_content_hash = func.encode(
        func.digest(func.convert_to(embedded_text, "UTF8"), "sha256"), "hex"
    )
    stmt = (
        select(KbChunk, distance.label("distance"))
        .join(KbChunkEmbedding, KbChunkEmbedding.chunk_id == KbChunk.id)
        .where(
            KbChunkEmbedding.provider == fingerprint.provider,
            KbChunkEmbedding.model == fingerprint.model,
            KbChunkEmbedding.dim == fingerprint.dim,
            KbChunkEmbedding.content_hash == current_content_hash,
            KbChunk.quarantined.is_(False),
            _effective_chunk_filter(),
        )
        .order_by(distance)
    )
    if kb_ids is not None:
        stmt = stmt.where(KbChunk.kb_id.in_(kb_ids))
    rows = (await session.execute(stmt.limit(candidate_limit))).all()
    return [(chunk, float(distance_value)) for chunk, distance_value in rows]


async def _enable_relaxed_vector_scan(session: AsyncSession) -> None:
    """Enable pgvector iterative scan for all dense reads in this transaction."""
    await session.execute(text(f"SET LOCAL hnsw.iterative_scan = '{_HNSW_ITERATIVE_SCAN}'"))


def _has_search_terms(query: str) -> bool:
    return any(char.isalnum() for char in query)


def _sparse_group_required(term: str, aliases: set[str]) -> bool:
    """Anchor rule: long nouns (usually proper names) must match; the rest stay optional."""
    return (
        len(term) >= _SPARSE_ANCHOR_MIN_LENGTH
        and "n" in aliases
        and term not in _SPARSE_FALLBACK_SYNONYMS
    )


def _controlled_sparse_groups(
    lexemes: list[_SparseLexeme],
) -> tuple[_SparseFallbackGroup, ...]:
    """Keep noun compounds exact while bounding their parser-derived alternatives."""
    aliases_by_value: dict[str, set[str]] = {}
    for lexeme in lexemes:
        value = lexeme.value.strip()
        if len(value) <= 1 or value in _SPARSE_FALLBACK_NOISE:
            continue
        aliases_by_value.setdefault(value, set()).add(lexeme.alias)

    retained = set(
        sorted(aliases_by_value, key=lambda term: (-len(term), term))[:_SPARSE_FALLBACK_MAX_TERMS]
    )
    if len(retained) < _SPARSE_FALLBACK_MIN_TERMS:
        return ()

    groups: list[_SparseFallbackGroup] = []
    for compound in sorted(retained, key=lambda term: (-len(term), term)):
        if compound not in retained or "n" not in aliases_by_value[compound]:
            continue
        components = tuple(
            sorted(
                (term for term in retained if term != compound and term in compound),
                key=lambda term: (-len(term), term),
            )
        )
        if not components:
            continue
        # 单一子词(如 卢浮宫→卢浮)作 OR 备选:chunk 侧分词可能只落子词词元。
        component_alternative = components if len(components) >= 2 else (components[0],)
        groups.append(
            _SparseFallbackGroup(
                alternatives=((compound,), component_alternative),
                required=_sparse_group_required(compound, aliases_by_value[compound]),
            )
        )
        retained.difference_update({compound, *components})

    for term in sorted(retained, key=lambda value: (-len(value), value)):
        alternatives = ((term,),) + tuple(
            (expansion,) for expansion in _SPARSE_FALLBACK_SYNONYMS.get(term, ())
        )
        groups.append(
            _SparseFallbackGroup(
                alternatives=alternatives,
                required=_sparse_group_required(term, aliases_by_value[term]),
            )
        )
    return tuple(groups) if len(groups) >= _SPARSE_FALLBACK_MIN_TERMS else ()


def _sparse_lexemes_statement(query: str) -> Select:
    bound_query = bindparam("sparse_lexeme_query", value=query)
    parsed = func.ts_debug(_fts_config(), bound_query).table_valued(
        "alias",
        "description",
        "token",
        "dictionaries",
        "dictionary",
        "lexemes",
    )
    return select(parsed.c.alias, parsed.c.lexemes).where(func.cardinality(parsed.c.lexemes) > 0)


def _ranked_sparse_statement(
    tsquery,
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> Select:
    rank = func.ts_rank_cd(KbChunk.tsv, tsquery).label("sparse_rank")
    stmt = (
        select(KbChunk, rank)
        .where(
            KbChunk.tsv.bool_op("@@")(tsquery),
            KbChunk.quarantined.is_(False),
            _effective_chunk_filter(),
        )
        .order_by(rank.desc(), KbChunk.id)
    )
    if kb_ids is not None:
        stmt = stmt.where(KbChunk.kb_id.in_(kb_ids))
    return stmt.limit(top_k)


def _sparse_chunk_statement(
    query: str,
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> Select:
    _validate_top_k(top_k)
    bound_query = bindparam("sparse_query", value=query)
    tsquery = func.websearch_to_tsquery(_fts_config(), bound_query)
    return _ranked_sparse_statement(
        tsquery,
        top_k=top_k,
        kb_ids=kb_ids,
    )


def _fallback_sparse_chunk_statement(
    groups: tuple[_SparseFallbackGroup, ...],
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> Select:
    _validate_top_k(top_k)
    if len(groups) < _SPARSE_FALLBACK_MIN_TERMS:
        raise ValueError("sparse fallback requires at least two term groups")

    group_queries = []
    for group_index, group in enumerate(groups):
        if not group.alternatives or any(not alternative for alternative in group.alternatives):
            raise ValueError("sparse fallback groups must not be empty")
        if group.phrase and (len(group.alternatives) != 1 or len(group.alternatives[0]) < 2):
            raise ValueError("sparse fallback phrase groups require one multi-term branch")

        alternative_queries = []
        for alternative_index, alternative in enumerate(group.alternatives):
            if group.phrase:
                alternative_query = func.phraseto_tsquery(
                    _SIMPLE_SEARCH_CONFIG,
                    bindparam(
                        f"sparse_fallback_{group_index}_{alternative_index}",
                        value=" ".join(alternative),
                    ),
                )
            else:
                term_queries = [
                    func.plainto_tsquery(
                        _SIMPLE_SEARCH_CONFIG,
                        bindparam(
                            f"sparse_fallback_{group_index}_{alternative_index}_{term_index}",
                            value=term,
                        ),
                    )
                    for term_index, term in enumerate(alternative)
                ]
                alternative_query = term_queries[0]
                for term_query in term_queries[1:]:
                    alternative_query = alternative_query.op("&&")(term_query)
            alternative_queries.append(alternative_query)

        group_query = alternative_queries[0]
        for alternative_query in alternative_queries[1:]:
            group_query = group_query.op("||")(alternative_query)
        group_queries.append(group_query)

    def _joined(queries: list[Any], operator: str) -> Any:
        joined = queries[0]
        for query in queries[1:]:
            joined = joined.op(operator)(query)
        return joined

    required_queries = [
        query for query, group in zip(group_queries, groups, strict=True) if group.required
    ]
    optional_queries = [
        query for query, group in zip(group_queries, groups, strict=True) if not group.required
    ]
    # 锚点组全 AND;非锚点组 SQL 侧仅要求命中其一,精确 quorum 由
    # _fallback_quorum_rows 在 Python 侧按原文复核,避免整句 AND 零召回。
    if required_queries and optional_queries:
        tsquery = _joined(required_queries, "&&").op("&&")(_joined(optional_queries, "||"))
    elif required_queries:
        tsquery = _joined(required_queries, "&&")
    else:
        tsquery = _joined(optional_queries, "||")
    return _ranked_sparse_statement(tsquery, top_k=top_k, kb_ids=kb_ids)


def _fallback_group_matches(group: _SparseFallbackGroup, normalized_text: str) -> bool:
    if group.phrase:
        # 与 SQL 侧 phraseto_tsquery 对齐:短语组必须相邻出现,
        # 否则表格跨行的"巴黎…五星"会被子串共现误判。
        return any(
            _normalize_constraint("".join(alternative)) in normalized_text
            for alternative in group.alternatives
        )
    return any(
        all(_normalize_constraint(term) in normalized_text for term in alternative)
        for alternative in group.alternatives
    )


def _fallback_quorum_rows(
    rows: list[tuple[KbChunk, float]],
    groups: tuple[_SparseFallbackGroup, ...],
    *,
    top_k: int,
) -> list[tuple[KbChunk, float]]:
    """Re-verify anchors and the optional-group quorum on raw chunk text.

    Substring checks are parser-agnostic: they hold even when the chunk's
    tsvector lexemes differ from the query-side segmentation.
    """
    _validate_top_k(top_k)
    required = [group for group in groups if group.required]
    optional = [group for group in groups if not group.required]
    need = math.ceil(len(optional) * _SPARSE_FALLBACK_QUORUM) if optional else 0
    kept: list[tuple[KbChunk, float]] = []
    for chunk, rank in rows:
        normalized_text = _normalize_constraint(f"{chunk.heading_path or ''}\n{chunk.content}")
        if not all(_fallback_group_matches(group, normalized_text) for group in required):
            continue
        matched = sum(_fallback_group_matches(group, normalized_text) for group in optional)
        if matched < need:
            continue
        kept.append((chunk, rank))
        if len(kept) == top_k:
            break
    return kept


def _merge_sparse_rows(
    primary: list[tuple[KbChunk, float]],
    fallback: list[tuple[KbChunk, float]],
    *,
    top_k: int,
) -> list[tuple[KbChunk, float]]:
    """Keep precise primary rank first, then fill unique slots from fallback."""
    _validate_top_k(top_k)
    merged: list[tuple[KbChunk, float]] = []
    seen: set[UUID] = set()
    for rows in (primary, fallback):
        for chunk, rank in rows:
            if chunk.id in seen:
                continue
            seen.add(chunk.id)
            merged.append((chunk, float(rank)))
            if len(merged) == top_k:
                return merged
    return merged


async def _sparse_chunk_hits(
    session: AsyncSession,
    query: str,
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> list[tuple[KbChunk, float]]:
    if not _has_search_terms(query):
        return []
    primary = [
        (chunk, float(rank))
        for chunk, rank in (
            await session.execute(_sparse_chunk_statement(query, top_k=top_k, kb_ids=kb_ids))
        ).all()
    ]
    if len(primary) == top_k:
        return primary

    parsed_rows = (await session.execute(_sparse_lexemes_statement(query))).all()
    lexemes = [
        _SparseLexeme(alias=str(alias), value=str(value))
        for alias, values in parsed_rows
        for value in (values or ())
    ]
    groups = _controlled_sparse_groups(lexemes)
    if not groups:
        return primary
    fetch_limit = min(top_k * _SPARSE_FALLBACK_FETCH_MULTIPLIER, _MAX_TOP_K)
    candidate_rows = [
        (chunk, float(rank))
        for chunk, rank in (
            await session.execute(
                _fallback_sparse_chunk_statement(
                    groups,
                    top_k=fetch_limit,
                    kb_ids=kb_ids,
                )
            )
        ).all()
    ]
    fallback = _fallback_quorum_rows(candidate_rows, groups, top_k=top_k)
    return _merge_sparse_rows(primary, fallback, top_k=top_k)


def _round_robin_merge(
    per_term: list[list[tuple[KbChunk, float]]], *, top_k: int
) -> list[tuple[KbChunk, float]]:
    """Interleave per-term rankings so every term keeps its best hits at the head."""
    _validate_top_k(top_k)
    merged: list[tuple[KbChunk, float]] = []
    seen: set[UUID] = set()
    for tier in zip_longest(*per_term):
        for row in tier:
            if row is None or row[0].id in seen:
                continue
            seen.add(row[0].id)
            merged.append(row)
            if len(merged) == top_k:
                return merged
    return merged


async def _must_include_chunk_hits(
    session: AsyncSession,
    terms: tuple[str, ...],
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
) -> list[tuple[KbChunk, float]]:
    """Guarantee sparse coverage for demand-mandated terms with one query per term.

    Round-robin merge keeps every mandated term's best hits at the head, so a
    broad query cannot crowd a must-include entity out of the candidate pool.
    """
    _validate_top_k(top_k)
    cleaned = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    per_term: list[list[tuple[KbChunk, float]]] = []
    for index, term in enumerate(cleaned[:_MUST_INCLUDE_MAX_TERMS]):
        tsquery = func.plainto_tsquery(
            _fts_config(), bindparam(f"must_include_{index}", value=term)
        )
        rows = (
            await session.execute(_ranked_sparse_statement(tsquery, top_k=top_k, kb_ids=kb_ids))
        ).all()
        per_term.append([(chunk, float(rank)) for chunk, rank in rows])
    return _round_robin_merge(per_term, top_k=top_k)


async def _ready_target_config(
    session: AsyncSession,
    *,
    kb_ids: list[UUID] | None,
) -> tuple[dict, list[UUID]] | None:
    campaign = (
        await session.execute(
            select(EmbeddingMigrationCampaign)
            .where(EmbeddingMigrationCampaign.status.in_(("running", "dual_read")))
            .order_by(EmbeddingMigrationCampaign.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if campaign is None:
        return None
    stmt = select(EmbeddingReindexJob.kb_id).where(
        EmbeddingReindexJob.campaign_id == campaign.id,
        EmbeddingReindexJob.status == "ready",
    )
    if kb_ids is not None:
        stmt = stmt.where(EmbeddingReindexJob.kb_id.in_(kb_ids))
    ready_kb_ids = list((await session.execute(stmt)).scalars().all())
    if not ready_kb_ids:
        return None
    return normalize_embedding_config(campaign.target_config), ready_kb_ids


async def resolve_dual_read_target(
    session: AsyncSession, *, kb_ids: list[UUID], lock: bool = True
) -> DualReadTarget | None:
    """Capture a dual-read target from the transaction's MVCC snapshot."""
    del lock  # Compatibility only; kb.retrieve uses REPEATABLE READ, never row locks.
    campaign_stmt = (
        select(EmbeddingMigrationCampaign)
        .where(EmbeddingMigrationCampaign.status.in_(("running", "dual_read")))
        .order_by(EmbeddingMigrationCampaign.created_at.desc())
        .limit(1)
    )
    campaign = (await session.execute(campaign_stmt)).scalar_one_or_none()
    if campaign is None:
        return None
    jobs_stmt = (
        select(EmbeddingReindexJob)
        .where(
            EmbeddingReindexJob.campaign_id == campaign.id,
            EmbeddingReindexJob.status == "ready",
            EmbeddingReindexJob.kb_id.in_(kb_ids),
        )
        .order_by(EmbeddingReindexJob.kb_id)
    )
    jobs = list((await session.execute(jobs_stmt)).scalars())
    if not jobs:
        return None
    config = normalize_embedding_config(campaign.target_config)
    api_key, base_url = _configured_embedding_transport(config)
    config = {
        **config,
        "_resolved_api_key": api_key,
        "_resolved_base_url": base_url,
    }
    manifest = {
        "campaign_id": str(campaign.id),
        "target_fingerprint": EmbeddingFingerprint.from_config(config).as_dict(),
        "ready_kb_ids": [str(job.kb_id) for job in jobs],
        "endpoint_sha256": hashlib.sha256(base_url.rstrip("/").encode()).hexdigest(),
    }
    return DualReadTarget(config=config, kb_ids=[job.kb_id for job in jobs], manifest=manifest)


def _configured_embedding_transport(config: dict) -> tuple[str | None, str]:
    settings = get_settings()
    overrides = runtime_overrides("embedding")
    provider = (
        config.get("provider")
        or overrides.get("provider")
        or getattr(settings, "embedding_provider", "")
    )
    model = (
        config.get("model")
        or overrides.get("model")
        or getattr(settings, "embedding_model", "")
    )
    endpoint = (
        provider_model_endpoint(
            str(provider),
            str(model),
            capability="embedding",
        )
        if provider and model
        else None
    )
    api_key = endpoint.api_key if endpoint is not None else None
    base_url = endpoint.base_url if endpoint is not None else ""
    return (
        str(api_key).strip() if api_key else None,
        str(base_url).strip(),
    )


def _configured_embedder(config: dict) -> EmbeddingService:
    if "_resolved_base_url" in config:
        api_key = config.get("_resolved_api_key")
        base_url = str(config["_resolved_base_url"])
    else:
        api_key, base_url = _configured_embedding_transport(config)
    if not api_key:
        raise ValueError("dual-read embedding credentials are unavailable")
    return EmbeddingService(
        provider=config["provider"],
        model=config["model"],
        dim=config["dim"],
        api_key=api_key,
        base_url=base_url,
    )


def _field_match_score(query: str, *weighted_fields: tuple[Any, float]):
    """Return a bounded SQL relevance score without comparing unrelated fields."""
    scores = []
    for column, weight in weighted_fields:
        reverse = literal(query).ilike(func.concat("%", column, "%")) & (func.length(column) >= 2)
        scores.append(
            case(
                (func.lower(column) == query.casefold(), weight),
                (column.ilike(f"%{query}%"), weight * 0.85),
                (reverse, weight * 0.7),
                else_=0.0,
            )
        )
    return func.greatest(*scores)


def _multi_term_score(terms: tuple[str, ...], *weighted_fields: tuple[Any, float]):
    cleaned = tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))
    if not cleaned:
        return literal(0.0)
    return func.greatest(*(_field_match_score(term, *weighted_fields) for term in cleaned))


def _normalize_constraint(value: str | None) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value or "").casefold()
        if not char.isspace()
    )


def _normalize_entity_name(value: str | None) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value or "").casefold()
        if char.isalnum()
    )


def _normalized_sql_text(column):
    compatibility_ascii = func.translate(
        func.replace(column, "\u3000", " "),
        _FULLWIDTH_ASCII,
        _ASCII_PRINTABLE,
    )
    return func.lower(
        func.regexp_replace(func.btrim(compatibility_ascii), r"[[:space:]]+", "", "g")
    )


# ---- 结构化通道(领域无关) ---------------------------------------------------

#: 词面打分权重:实体名 vs 类型声明的 text 型属性
_ENTITY_NAME_WEIGHT = 1.0
_ENTITY_ATTRIBUTE_WEIGHT = 0.65
#: must_include 词命中实体名时的分数加成(与 TF 的 must_include_hit 同语义)
_MUST_INCLUDE_BONUS = 0.2
#: 参与词面打分的类型声明字段类型(number/date 只做过滤,不做模糊词面匹配)
_SCORABLE_FILTER_TYPES = frozenset({"text"})


def _declared_text_fields(entity_type: KbEntityType) -> tuple[str, ...]:
    """类型声明中可参与词面打分的属性(filterable_fields 里的 text 字段)。"""
    fields: list[str] = []
    for item in entity_type.filterable_fields or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("field") or "").strip()
        if not name or name == "name":
            continue
        if str(item.get("type") or "text") in _SCORABLE_FILTER_TYPES:
            fields.append(name)
    return tuple(dict.fromkeys(fields))


def _structured_search_terms(
    query: str, structured: StructuredSearchQuery | None
) -> tuple[str, ...]:
    extra = (
        (*structured.match_terms, *structured.must_include)
        if structured is not None
        else ()
    )
    return tuple(
        dict.fromkeys(term.strip() for term in (*extra, query) if term.strip())
    )


async def _resolve_structured_types(
    session: AsyncSession,
    org_id: UUID,
    structured: StructuredSearchQuery | None,
) -> list[KbEntityType]:
    """解析参与结构化召回的类型定义(显式 type_keys 优先,否则全部注册类型)。"""
    if structured is not None and structured.type_keys:
        resolved: list[KbEntityType] = []
        for type_key in dict.fromkeys(structured.type_keys):
            entity_type = await get_entity_type(session, org_id, type_key)
            if entity_type is None:
                raise StructuredFilterError(f"未注册的实体类型:{type_key}")
            resolved.append(entity_type)
        return resolved
    registry = await load_custom_entity_types(session, org_id)
    return [registry[key] for key in sorted(registry)]


async def _structured_candidates(
    session: AsyncSession,
    org_id: UUID,
    query: str,
    *,
    top_k: int,
    kb_ids: list[UUID] | None,
    filters: StructuredSearchQuery | None,
) -> list[_Candidate]:
    """把全部实体投影 + wiki 页面排成同一条结构化召回通道(单一泛化路径)。

    实体侧按 ``type_key`` 分组,每组一条语句:
      * 硬过滤 = 该类型声明字段编译出的 JSONB 条件(``build_entity_filters``);
      * 打分 = 检索词面对 ``KbEntity.name``(权重 1.0)与类型声明的 text 型属性
        (权重 0.65)的最优匹配,must_include 命中实体名再加 0.2。
    """
    candidate_limit = top_k
    must_include_terms = tuple(
        term for term in (filters.must_include if filters is not None else ()) if term.strip()
    )
    search_terms = _structured_search_terms(query, filters)
    candidates: list[_Candidate] = []

    entity_types = await _resolve_structured_types(session, org_id, filters)
    entity_rows: list[tuple[KbEntity, float]] = []
    for entity_type in entity_types:
        criteria = filters.filters_for(entity_type) if filters is not None else []
        if criteria is None:  # 该类型声明不了这些过滤字段 → 不参与本次召回
            continue
        weighted: list[tuple[Any, float]] = [(KbEntity.name, _ENTITY_NAME_WEIGHT)]
        weighted += [
            (KbEntity.attributes[field_name].astext, _ENTITY_ATTRIBUTE_WEIGHT)
            for field_name in _declared_text_fields(entity_type)
        ]
        score = _multi_term_score(search_terms, *weighted)
        if must_include_terms:
            score = score + case(
                (
                    _multi_term_score(must_include_terms, (KbEntity.name, 1.0)) > 0,
                    _MUST_INCLUDE_BONUS,
                ),
                else_=0.0,
            )
        statement = select(KbEntity, score.label("native_score")).where(
            score > 0,
            KbEntity.entity_type_key == entity_type.type_key,
            active_projection_filter(KbEntity),
            live_snapshot_projection_filter(KbEntity, "kb_entity"),
            *criteria,
        )
        if kb_ids is not None:
            statement = statement.where(KbEntity.kb_id.in_(kb_ids))
        entity_rows += (
            await session.execute(
                statement.order_by(score.desc(), KbEntity.id).limit(candidate_limit)
            )
        ).all()

    expired_docs = await _expired_doc_ids(
        session, {row.source_doc_id for row, _score in entity_rows}
    )
    for row, score in entity_rows:
        candidates.append(
            _Candidate(
                key=(row.entity_type_key, row.id),
                hit=SearchHit(
                    kind=row.entity_type_key,
                    layer=_layer(org_id, row.org_id),
                    kb_id=str(row.kb_id),
                    source=f"{_ENTITY_SOURCE_PATH}/{row.id}",
                    confidence=0.0,
                    data={
                        **_entity_hit_data(row),
                        "snapshot_id": str(row.snapshot_id) if row.snapshot_id else None,
                        "must_include_hit": any(
                            term.casefold() in (row.name or "").casefold()
                            or (row.name or "").casefold() in term.casefold()
                            for term in must_include_terms
                        ),
                        **_stale_flag(row.source_doc_id in expired_docs),
                    },
                ),
                native_scores={"structured": float(score)},
            )
        )

    page_score = _multi_term_score(search_terms, (KbPage.title, 1.0), (KbPage.content, 0.5))
    page_statement = select(KbPage, page_score.label("native_score")).where(
        page_score > 0,
        active_projection_filter(KbPage),
        live_snapshot_projection_filter(KbPage, "wiki_page"),
    )
    if kb_ids is not None:
        page_statement = page_statement.where(KbPage.kb_id.in_(kb_ids))
    page_rows = (
        await session.execute(
            page_statement.order_by(page_score.desc(), KbPage.id).limit(candidate_limit)
        )
    ).all()
    for row, score in page_rows:
        candidates.append(
            _Candidate(
                key=(PAGE_KIND, row.id),
                hit=SearchHit(
                    kind=PAGE_KIND,
                    layer=_layer(org_id, row.org_id),
                    kb_id=str(row.kb_id),
                    source=f"{_CARD_SOURCE_PATHS[PAGE_KIND]}/{row.id}",
                    confidence=0.0,
                    data={
                        "id": str(row.id),
                        "snapshot_id": str(row.snapshot_id) if row.snapshot_id else None,
                        "page_type": row.page_type,
                        "title": row.title,
                        "content": (row.content or "")[:500],
                    },
                ),
                native_scores={"structured": float(score)},
            )
        )

    return sorted(
        candidates,
        key=lambda item: (
            -item.native_scores["structured"],
            item.key[0],
            str(item.key[1]),
        ),
    )[:candidate_limit]


def _citation_payload(
    claim_id: UUID,
    evidence: EvidenceSpan,
    revision: DocumentRevision,
) -> dict:
    return {
        "kind": "fact_evidence",
        "fact_claim_id": str(claim_id),
        "evidence_span_id": str(evidence.id),
        "revision_id": str(evidence.revision_id),
        "chunk_id": str(evidence.chunk_id) if evidence.chunk_id else None,
        "source_doc_id": str(revision.doc_id),
        "source_sha256": revision.sha256,
        "page": evidence.page,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "cell_ref": evidence.cell_ref,
        "quote_text": evidence.quote_text,
    }


def _source_chunk_matches(
    hit: SearchHit,
    chunk: KbChunk,
    revision: DocumentRevision,
    source_document: SourceDocument,
) -> bool:
    return hit.kind == "chunk" and not (
        chunk.quarantined
        or getattr(chunk, "content_kind", "text") != "text"
        or getattr(chunk, "image_asset_id", None) is not None
        or chunk.snapshot_id is None
        or chunk.revision_id is None
        or chunk.source_doc_id is None
        or str(source_document.doc_type) != DocType.GENERAL.value
        or str(source_document.lifecycle_status)
        != DocumentLifecycleStatus.ACTIVE.value
        or str(revision.status) == RevisionStatus.TOMBSTONED.value
        or revision.tombstoned_at is not None
        or str(chunk.id) != str(hit.data.get("id"))
        or str(chunk.kb_id) != hit.kb_id
        or str(chunk.snapshot_id) != str(hit.data.get("snapshot_id"))
        or str(chunk.revision_id) != str(hit.data.get("revision_id"))
        or str(chunk.source_doc_id) != str(hit.data.get("source_doc_id"))
        or revision.id != chunk.revision_id
        or revision.org_id != chunk.org_id
        or revision.kb_id != chunk.kb_id
        or revision.doc_id != chunk.source_doc_id
        or source_document.id != revision.doc_id
        or source_document.org_id != revision.org_id
        or source_document.kb_id != revision.kb_id
        or not revision.sha256
        or not chunk.content.strip()
    )


def _source_span_payload(
    hit: SearchHit,
    chunk: KbChunk,
    revision: DocumentRevision,
    source_document: SourceDocument,
) -> dict | None:
    """Build provenance only from a versioned snapshot chunk and its revision."""
    if not _source_chunk_matches(hit, chunk, revision, source_document):
        return None
    page = chunk.page
    start_line = chunk.start_line
    end_line = chunk.end_line
    has_page_anchor = isinstance(page, int) and not isinstance(page, bool) and page >= 1
    has_line_anchor = (
        isinstance(start_line, int)
        and not isinstance(start_line, bool)
        and isinstance(end_line, int)
        and not isinstance(end_line, bool)
        and start_line >= 1
        and end_line >= start_line
    )
    if not (has_page_anchor or has_line_anchor):
        return None
    return {
        "kind": "source_span",
        "revision_id": str(revision.id),
        "chunk_id": str(chunk.id),
        "source_doc_id": str(revision.doc_id),
        "source_sha256": revision.sha256,
        "page": page if has_page_anchor else None,
        "start_line": start_line if has_line_anchor else None,
        "end_line": end_line if has_line_anchor else None,
        "cell_ref": None,
        "quote_text": chunk.content,
    }


def _image_chunk_matches(
    hit: SearchHit,
    chunk: KbChunk,
    revision: DocumentRevision,
    source_document: SourceDocument,
    asset: KbImageAsset,
    knowledge_base: KnowledgeBase,
) -> bool:
    return hit.kind == "chunk" and not (
        chunk.quarantined
        or chunk.content_kind != "image"
        or chunk.image_asset_id is None
        or chunk.snapshot_id is None
        or chunk.revision_id is None
        or chunk.source_doc_id is None
        or str(source_document.lifecycle_status)
        != DocumentLifecycleStatus.ACTIVE.value
        or str(revision.status) == RevisionStatus.TOMBSTONED.value
        or revision.tombstoned_at is not None
        or str(chunk.id) != str(hit.data.get("id"))
        or str(chunk.kb_id) != hit.kb_id
        or str(chunk.snapshot_id) != str(hit.data.get("snapshot_id"))
        or str(chunk.revision_id) != str(hit.data.get("revision_id"))
        or str(chunk.source_doc_id) != str(hit.data.get("source_doc_id"))
        or str(chunk.image_asset_id) != str(hit.data.get("image_asset_id"))
        or hit.data.get("content_kind") != "image"
        or revision.id != chunk.revision_id
        or revision.org_id != chunk.org_id
        or revision.kb_id != chunk.kb_id
        or revision.doc_id != chunk.source_doc_id
        or source_document.id != revision.doc_id
        or source_document.org_id != revision.org_id
        or source_document.kb_id != revision.kb_id
        or asset.id != chunk.image_asset_id
        or asset.revision_id != revision.id
        or asset.doc_id != source_document.id
        or asset.org_id != chunk.org_id
        or asset.kb_id != chunk.kb_id
        or asset.original_object_key is None
        or asset.thumbnail_object_key is None
        or asset.content_type not in {"image/png", "image/jpeg", "image/webp"}
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or not asset.image_sha256
        or not revision.sha256
        or not chunk.content.strip()
        or knowledge_base.id != chunk.kb_id
        or knowledge_base.org_id != chunk.org_id
        or knowledge_base.active_snapshot_id != chunk.snapshot_id
        or knowledge_base.lifecycle_status != "active"
        or _frozen_image_meta(chunk) is None
    )


def _frozen_image_meta(chunk: KbChunk) -> dict[str, str | None] | None:
    meta = chunk.meta
    if not isinstance(meta, dict):
        return None
    image_meta = meta.get(IMAGE_SNAPSHOT_META_KEY)
    if not isinstance(image_meta, dict) or set(image_meta) != {
        "alt_text",
        "caption",
        "ocr_text",
        "enrichment_fingerprint",
    }:
        return None
    alt_text = image_meta["alt_text"]
    caption = image_meta["caption"]
    ocr_text = image_meta["ocr_text"]
    fingerprint = image_meta["enrichment_fingerprint"]
    if (
        not isinstance(alt_text, str)
        or not alt_text.strip()
        or len(alt_text) > _IMAGE_ALT_TEXT_MAX_CHARS
        or (caption is not None and (not isinstance(caption, str) or len(caption) > 2_000))
        or (ocr_text is not None and (not isinstance(ocr_text, str) or len(ocr_text) > 4_000))
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
    ):
        return None
    expected_alt = (caption or ocr_text or "")[:_IMAGE_ALT_TEXT_MAX_CHARS]
    if alt_text != expected_alt:
        return None
    return {
        "alt_text": alt_text,
        "caption": caption,
        "ocr_text": ocr_text,
        "enrichment_fingerprint": fingerprint,
    }


def _image_source_media(
    chunk: KbChunk,
    revision: DocumentRevision,
    asset: KbImageAsset,
) -> tuple[dict, KnowledgeMediaReference]:
    bbox = NormalizedBBox.model_validate(asset.bbox) if asset.bbox is not None else None
    citation = ImageSourceCitation(
        asset_id=asset.id,
        revision_id=revision.id,
        source_doc_id=revision.doc_id,
        source_sha256=revision.sha256,
        image_sha256=asset.image_sha256,
        page=asset.page,
        slide=asset.slide,
        bbox=bbox,
        quote_text=chunk.content[:_IMAGE_CITATION_QUOTE_MAX_CHARS],
    )
    frozen_meta = _frozen_image_meta(chunk)
    if frozen_meta is None:
        raise ValueError("image retrieval chunk has no approved alt text")
    alt_text = str(frozen_meta["alt_text"])
    base_url = f"/api/v1/kb/image-assets/{asset.id}"
    snapshot_query = f"?snapshot_id={chunk.snapshot_id}"
    media = KnowledgeMediaReference(
        asset_id=asset.id,
        snapshot_id=chunk.snapshot_id,
        alt_text=alt_text,
        content_type=asset.content_type,
        width=asset.width,
        height=asset.height,
        page=asset.page,
        slide=asset.slide,
        bbox=bbox,
        citation=citation,
        thumbnail_url=f"{base_url}/thumbnail{snapshot_query}",
        content_url=f"{base_url}/content{snapshot_query}",
    )
    return citation.model_dump(mode="json"), media


async def _attach_citations(session: AsyncSession, hits: list[SearchHit]) -> list[SearchHit]:
    """Attach only exact, confirmed claim-to-evidence relationships."""
    chunk_hits: dict[tuple[str, str, str], SearchHit] = {}
    chunk_ids: set[UUID] = set()
    image_chunk_ids: set[UUID] = set()
    for hit in hits:
        if (
            hit.kind != "chunk"
            or not hit.data.get("id")
            or not hit.data.get("snapshot_id")
            or not hit.data.get("revision_id")
            or not hit.data.get("source_doc_id")
        ):
            continue
        try:
            chunk_id = UUID(str(hit.data["id"]))
        except (TypeError, ValueError):
            continue
        key = (hit.kind, str(chunk_id), hit.kb_id)
        chunk_hits[key] = hit
        chunk_ids.add(chunk_id)
        if hit.data.get("content_kind") == "image":
            image_chunk_ids.add(chunk_id)

    verified_chunks: dict[
        tuple[str, str, str], tuple[KbChunk, DocumentRevision, SourceDocument]
    ] = {}
    verified_images: dict[
        tuple[str, str, str],
        tuple[KbChunk, DocumentRevision, SourceDocument, KbImageAsset, KnowledgeBase],
    ] = {}
    text_chunk_ids = chunk_ids - image_chunk_ids
    if text_chunk_ids:
        chunk_rows = (
            await session.execute(
                select(KbChunk, DocumentRevision, SourceDocument)
                .join(
                    DocumentRevision,
                    and_(
                        DocumentRevision.id == KbChunk.revision_id,
                        DocumentRevision.org_id == KbChunk.org_id,
                        DocumentRevision.kb_id == KbChunk.kb_id,
                    ),
                )
                .join(
                    SourceDocument,
                    and_(
                        SourceDocument.id == KbChunk.source_doc_id,
                        SourceDocument.id == DocumentRevision.doc_id,
                        SourceDocument.org_id == KbChunk.org_id,
                        SourceDocument.kb_id == KbChunk.kb_id,
                    ),
                )
                .join(
                    KnowledgeBase,
                    and_(
                        KnowledgeBase.id == KbChunk.kb_id,
                        KnowledgeBase.org_id == KbChunk.org_id,
                    ),
                )
                .where(
                    KbChunk.id.in_(text_chunk_ids),
                    KbChunk.snapshot_id.is_not(None),
                    KbChunk.quarantined.is_(False),
                    KbChunk.content_kind == "text",
                    live_document_revision_filter(),
                    SourceDocument.doc_type == DocType.GENERAL,
                    KnowledgeBase.active_snapshot_id == KbChunk.snapshot_id,
                    active_knowledge_base_filter(),
                )
                .order_by(KbChunk.id)
            )
        ).all()
        for chunk, revision, source_document in chunk_rows:
            key = ("chunk", str(chunk.id), str(chunk.kb_id))
            hit = chunk_hits.get(key)
            if hit is not None and _source_chunk_matches(hit, chunk, revision, source_document):
                verified_chunks[key] = (chunk, revision, source_document)
    if image_chunk_ids:
        image_rows = (
            await session.execute(
                select(
                    KbChunk,
                    DocumentRevision,
                    SourceDocument,
                    KbImageAsset,
                    KnowledgeBase,
                )
                .join(
                    DocumentRevision,
                    and_(
                        DocumentRevision.id == KbChunk.revision_id,
                        DocumentRevision.org_id == KbChunk.org_id,
                        DocumentRevision.kb_id == KbChunk.kb_id,
                    ),
                )
                .join(
                    SourceDocument,
                    and_(
                        SourceDocument.id == KbChunk.source_doc_id,
                        SourceDocument.id == DocumentRevision.doc_id,
                        SourceDocument.org_id == KbChunk.org_id,
                        SourceDocument.kb_id == KbChunk.kb_id,
                    ),
                )
                .join(
                    KbImageAsset,
                    and_(
                        KbImageAsset.id == KbChunk.image_asset_id,
                        KbImageAsset.revision_id == KbChunk.revision_id,
                        KbImageAsset.doc_id == KbChunk.source_doc_id,
                        KbImageAsset.org_id == KbChunk.org_id,
                        KbImageAsset.kb_id == KbChunk.kb_id,
                    ),
                )
                .join(
                    KnowledgeBase,
                    and_(
                        KnowledgeBase.id == KbChunk.kb_id,
                        KnowledgeBase.org_id == KbChunk.org_id,
                    ),
                )
                .where(
                    KbChunk.id.in_(image_chunk_ids),
                    KbChunk.snapshot_id.is_not(None),
                    KbChunk.quarantined.is_(False),
                    KbChunk.content_kind == "image",
                    live_document_revision_filter(),
                    KnowledgeBase.active_snapshot_id == KbChunk.snapshot_id,
                    active_knowledge_base_filter(),
                )
                .order_by(KbChunk.id)
            )
        ).all()
        for chunk, revision, source_document, asset, knowledge_base in image_rows:
            key = ("chunk", str(chunk.id), str(chunk.kb_id))
            hit = chunk_hits.get(key)
            if hit is not None and _image_chunk_matches(
                hit,
                chunk,
                revision,
                source_document,
                asset,
                knowledge_base,
            ):
                verified_images[key] = (
                    chunk,
                    revision,
                    source_document,
                    asset,
                    knowledge_base,
                )

    entity_hits: dict[tuple[str, str, str], SearchHit] = {}
    entity_snapshot_ids: set[UUID] = set()
    for hit in hits:
        # 实体命中 = 非 chunk 且带 id+snapshot(含 M3a 自定义类型;page 等无卡片的
        # kind 查不到对应 source_ref 卡片,自然不会误挂引用)
        if (
            hit.kind == "chunk"
            or not hit.data.get("id")
            or not hit.data.get("snapshot_id")
        ):
            continue
        try:
            snapshot_id = UUID(str(hit.data["snapshot_id"]))
        except (TypeError, ValueError):
            continue
        entity_hits[(hit.kind, str(hit.data["id"]), hit.kb_id)] = hit
        entity_snapshot_ids.add(snapshot_id)

    snapshot_scopes: dict[UUID, tuple[UUID, UUID, set[UUID]]] = {}
    if entity_snapshot_ids:
        snapshots = list(
            (
                await session.execute(
                    select(KnowledgeSnapshot)
                    .join(
                        KnowledgeBase,
                        and_(
                            KnowledgeBase.id == KnowledgeSnapshot.kb_id,
                            KnowledgeBase.org_id == KnowledgeSnapshot.org_id,
                            KnowledgeBase.active_snapshot_id == KnowledgeSnapshot.id,
                        ),
                    )
                    .where(
                        KnowledgeSnapshot.id.in_(entity_snapshot_ids),
                        KnowledgeSnapshot.status == "active",
                        active_knowledge_base_filter(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for snapshot in snapshots:
            if not isinstance(snapshot.revision_manifest, list):
                continue
            revision_ids: set[UUID] = set()
            valid_manifest = True
            for item in snapshot.revision_manifest:
                if not isinstance(item, dict) or not item.get("revision_id"):
                    valid_manifest = False
                    break
                try:
                    revision_ids.add(UUID(str(item["revision_id"])))
                except (TypeError, ValueError):
                    valid_manifest = False
                    break
            if valid_manifest:
                snapshot_scopes[snapshot.id] = (
                    snapshot.org_id,
                    snapshot.kb_id,
                    revision_ids,
                )
    entity_refs = {key: f"{key[0]}:{key[1]}" for key in entity_hits}
    entity_claims: dict[tuple[str, str, str], UUID] = {}
    if entity_refs:
        cards = list(
            (
                await session.execute(
                    select(KbChunk)
                    .where(KbChunk.source_ref.in_(set(entity_refs.values())))
                    .order_by(KbChunk.id)
                )
            )
            .scalars()
            .all()
        )
        ref_to_key = {value: key for key, value in entity_refs.items()}
        for card in cards:
            key = ref_to_key.get(card.source_ref or "")
            hit = entity_hits.get(key) if key is not None else None
            raw_claim_id = (card.meta or {}).get("fact_claim_id")
            try:
                snapshot_id = UUID(str(hit.data["snapshot_id"])) if hit else None
            except (KeyError, TypeError, ValueError):
                snapshot_id = None
            snapshot_scope = (
                snapshot_scopes.get(snapshot_id) if snapshot_id is not None else None
            )
            if (
                key is None
                or hit is None
                or snapshot_scope is None
                or card.snapshot_id is None
                or card.quarantined
                or key[2] != str(card.kb_id)
                or snapshot_scope[0] != card.org_id
                or snapshot_scope[1] != card.kb_id
                or str(card.snapshot_id) != str(hit.data.get("snapshot_id"))
                or not raw_claim_id
            ):
                continue
            try:
                entity_claims.setdefault(key, UUID(str(raw_claim_id)))
            except (TypeError, ValueError):
                logger.warning("检索卡片包含无效 fact_claim_id: %s", raw_claim_id)

    chunk_refs: dict[tuple[str, str, str], set[tuple[UUID, UUID]]] = {}
    for key, hit in chunk_hits.items():
        if key not in verified_chunks:
            continue
        refs: set[tuple[UUID, UUID]] = set()
        raw_refs = hit.data.get(_CITATION_REFS_KEY)
        if isinstance(raw_refs, list):
            for raw in raw_refs:
                if not isinstance(raw, dict) or set(raw) != {
                    "fact_claim_id",
                    "evidence_span_id",
                }:
                    continue
                try:
                    refs.add(
                        (
                            UUID(str(raw["fact_claim_id"])),
                            UUID(str(raw["evidence_span_id"])),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        if refs:
            chunk_refs[key] = refs
    criteria = []
    if entity_claims:
        criteria.append(FactClaim.id.in_(set(entity_claims.values())))
    requested_pairs = {pair for refs in chunk_refs.values() for pair in refs}
    if requested_pairs:
        criteria.append(
            and_(
                FactClaim.id.in_({pair[0] for pair in requested_pairs}),
                EvidenceSpan.id.in_({pair[1] for pair in requested_pairs}),
            )
        )
    rows = []
    if criteria:
        rows = (
            await session.execute(
                select(FactClaim, EvidenceSpan)
                .join(
                    EvidenceSpan,
                    and_(
                        EvidenceSpan.fact_claim_id == FactClaim.id,
                        EvidenceSpan.org_id == FactClaim.org_id,
                        EvidenceSpan.kb_id == FactClaim.kb_id,
                    ),
                )
                .where(FactClaim.review_status == "confirmed", or_(*criteria))
                .order_by(FactClaim.id, EvidenceSpan.id)
            )
        ).all()

    revision_ids = {evidence.revision_id for _claim, evidence in rows}
    revisions: dict[UUID, tuple[DocumentRevision, SourceDocument]] = {}
    if revision_ids:
        revisions = {
            revision.id: (revision, source_document)
            for revision, source_document in (
                await session.execute(
                    select(DocumentRevision, SourceDocument)
                    .join(
                        SourceDocument,
                        and_(
                            SourceDocument.id == DocumentRevision.doc_id,
                            SourceDocument.org_id == DocumentRevision.org_id,
                            SourceDocument.kb_id == DocumentRevision.kb_id,
                        ),
                    )
                    .join(
                        KnowledgeBase,
                        and_(
                            KnowledgeBase.id == DocumentRevision.kb_id,
                            KnowledgeBase.org_id == DocumentRevision.org_id,
                        ),
                    )
                    .where(
                        DocumentRevision.id.in_(revision_ids),
                        live_document_revision_filter(),
                        active_knowledge_base_filter(),
                    )
                )
            ).all()
        }
    support_snapshot_ids = set(entity_snapshot_ids)
    support_snapshot_ids.update(
        chunk.snapshot_id
        for chunk, _revision, _document in verified_chunks.values()
        if chunk.snapshot_id is not None
    )
    support_snapshot_ids.update(
        chunk.snapshot_id
        for chunk, _revision, _document, _asset, _kb in verified_images.values()
        if chunk.snapshot_id is not None
    )
    active_supports: set[tuple[UUID, UUID, UUID]] = set()
    if rows and support_snapshot_ids:
        active_supports = {
            (snapshot_id, fact_claim_id, evidence_span_id)
            for snapshot_id, fact_claim_id, evidence_span_id in (
                await session.execute(
                    select(
                        SnapshotFactSupport.snapshot_id,
                        SnapshotFactSupport.fact_claim_id,
                        SnapshotFactSupport.evidence_span_id,
                    )
                    .join(
                        KnowledgeBase,
                        and_(
                            KnowledgeBase.id == SnapshotFactSupport.kb_id,
                            KnowledgeBase.org_id == SnapshotFactSupport.org_id,
                            KnowledgeBase.active_snapshot_id
                            == SnapshotFactSupport.snapshot_id,
                        ),
                    )
                    .join(
                        DocumentRevision,
                        and_(
                            DocumentRevision.id
                            == SnapshotFactSupport.revision_id,
                            DocumentRevision.org_id
                            == SnapshotFactSupport.org_id,
                            DocumentRevision.kb_id == SnapshotFactSupport.kb_id,
                        ),
                    )
                    .join(
                        SourceDocument,
                        and_(
                            SourceDocument.id == SnapshotFactSupport.doc_id,
                            SourceDocument.id == DocumentRevision.doc_id,
                            SourceDocument.org_id == SnapshotFactSupport.org_id,
                            SourceDocument.kb_id == SnapshotFactSupport.kb_id,
                        ),
                    )
                    .where(
                        SnapshotFactSupport.snapshot_id.in_(
                            support_snapshot_ids
                        ),
                        SnapshotFactSupport.fact_claim_id.in_(
                            {claim.id for claim, _evidence in rows}
                        ),
                        SnapshotFactSupport.evidence_span_id.in_(
                            {evidence.id for _claim, evidence in rows}
                        ),
                        live_document_revision_filter(),
                        active_knowledge_base_filter(),
                    )
                )
            ).all()
        }
    citations: dict[tuple[str, str, str], dict] = {}
    claim_keys: dict[UUID, list[tuple[str, str, str]]] = {}
    entity_snapshots: dict[tuple[str, str, str], UUID] = {}
    for key, claim_id in entity_claims.items():
        claim_keys.setdefault(claim_id, []).append(key)
        try:
            entity_snapshots[key] = UUID(str(entity_hits[key].data["snapshot_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    for claim, evidence in rows:
        revision_source = revisions.get(evidence.revision_id)
        revision = revision_source[0] if revision_source is not None else None
        if (
            revision is None
            or revision.org_id != claim.org_id
            or revision.kb_id != claim.kb_id
            or not revision.sha256
        ):
            continue
        citation = _citation_payload(claim.id, evidence, revision)
        for key in claim_keys.get(claim.id, []):
            snapshot_id = entity_snapshots.get(key)
            snapshot_scope = (
                snapshot_scopes.get(snapshot_id) if snapshot_id is not None else None
            )
            # 实体 kind 就是 claim 谓词(= 已注册的实体类型 key),必须严格相等
            kind_matches = key[0] == claim.predicate
            if (
                kind_matches
                and key[2] == str(claim.kb_id)
                and snapshot_scope is not None
                and snapshot_scope[0] == claim.org_id
                and snapshot_scope[1] == claim.kb_id
                and evidence.revision_id in snapshot_scope[2]
                and (snapshot_id, claim.id, evidence.id) in active_supports
            ):
                citations.setdefault(key, citation)
        pair = (claim.id, evidence.id)
        for chunk_key, refs in chunk_refs.items():
            chunk_source = verified_chunks.get(chunk_key)
            if (
                pair in refs
                and chunk_source is not None
                and chunk_key[2] == str(claim.kb_id)
                and evidence.revision_id == chunk_source[1].id
                and chunk_source[0].snapshot_id is not None
                and (
                    chunk_source[0].snapshot_id,
                    claim.id,
                    evidence.id,
                )
                in active_supports
            ):
                citations.setdefault(chunk_key, citation)
    if verified_images:
        image_assets = {
            source[3].id: (key, source) for key, source in verified_images.items()
        }
        image_evidence_rows = (
            await session.execute(
                select(FactClaim, EvidenceSpan)
                .join(
                    EvidenceSpan,
                    and_(
                        EvidenceSpan.fact_claim_id == FactClaim.id,
                        EvidenceSpan.org_id == FactClaim.org_id,
                        EvidenceSpan.kb_id == FactClaim.kb_id,
                    ),
                )
                .where(
                    FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                    EvidenceSpan.image_asset_id.in_(set(image_assets)),
                )
                .order_by(FactClaim.id, EvidenceSpan.id)
            )
        ).all()
        for claim, evidence in image_evidence_rows:
            image_source = image_assets.get(evidence.image_asset_id)
            if image_source is None:
                continue
            key, (_chunk, revision, _document, asset, _knowledge_base) = image_source
            if (
                claim.org_id == asset.org_id
                and claim.kb_id == asset.kb_id
                and evidence.org_id == asset.org_id
                and evidence.kb_id == asset.kb_id
                and evidence.revision_id == revision.id
                and _chunk.snapshot_id is not None
                and (_chunk.snapshot_id, claim.id, evidence.id)
                in active_supports
            ):
                citations.setdefault(
                    key,
                    _citation_payload(claim.id, evidence, revision),
                )
    attached = []
    for hit in hits:
        key = (hit.kind, str(hit.data.get("id")), hit.kb_id)
        image_source = verified_images.get(key)
        citation = citations.get(key)
        media_refs: tuple[KnowledgeMediaReference, ...] = ()
        if image_source is not None:
            image_citation, media = _image_source_media(
                image_source[0],
                image_source[1],
                image_source[3],
            )
            citation = citation or image_citation
            media_refs = (media,)
        if citation is None and hit.kind == "chunk":
            chunk_source = verified_chunks.get(key)
            if chunk_source is not None:
                citation = _source_span_payload(hit, *chunk_source)
        if hit.kind == "chunk" and key not in verified_chunks and image_source is None:
            continue
        if (
            hit.kind != "chunk"
            and hit.data.get("snapshot_id") is not None
            and citation is None
        ):
            continue
        attached.append(
            replace(
                hit,
                citation=citation,
                media_refs=media_refs,
                data={key: value for key, value in hit.data.items() if key != _CITATION_REFS_KEY},
            )
        )
    return attached


#: rerank 文本的通用字段白名单(领域无关;实体的业务属性另走
#: _custom_entity_attributes 按 KbEntity.attributes 追加)
_RERANK_FIELDS = (
    "name",
    "title",
    "page_type",
    "heading_path",
    "content",
    "summary",
    "description",
    "category",
    "notes",
)
# 自定义实体单属性进 rerank 文本的截断上限:防单个巨型属性挤掉其余属性,
# 总长仍受 _rerank_document 的 max_chars 约束。
_RERANK_ATTRIBUTE_MAX_CHARS = 400
# 自定义实体 data 中的系统键:非业务词面,不进 rerank 文本。
_RERANK_ATTRIBUTE_SKIP_KEYS = frozenset(
    {"id", "entity_type_key", "snapshot_id", "via", "stale", _CITATION_REFS_KEY}
)


def _custom_entity_attributes(hit: SearchHit) -> dict:
    """实体命中的业务属性;chunk/page 等非实体 kind 一律返回空。

    实体 hit 的 attributes 展平进 data 顶层并带 entity_type_key 标记;
    兼容嵌套 attributes dict 形态。
    """
    nested = hit.data.get("attributes")
    if isinstance(nested, dict):
        return nested
    if hit.data.get("entity_type_key"):
        return {
            key: value
            for key, value in hit.data.items()
            if key not in _RERANK_ATTRIBUTE_SKIP_KEYS
        }
    return {}


def _rerank_attribute_text(value: object) -> str:
    """属性值 → rerank 文本片段:跳过空值,嵌套结构 JSON 序列化,单字段截断。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text_value = value.strip()
    elif isinstance(value, bool):
        text_value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text_value = str(value)
    else:
        if not value:  # 空 list/dict
            return ""
        try:
            text_value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return ""
    return text_value[:_RERANK_ATTRIBUTE_MAX_CHARS]


def _rerank_document(hit: SearchHit, *, max_chars: int = 4000) -> str:
    """Serialize stable relevance fields without forwarding arbitrary nested data.

    内置 kind 仅输出白名单字段;自定义实体(M3a)在白名单之后追加其业务属性,
    使 cross-encoder 能看到自定义属性词面(白名单字段优先保留在前,总长受限)。
    """
    parts = [hit.kind]
    emitted: set[str] = set()
    for field in _RERANK_FIELDS:
        value = hit.data.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(f"{field}: {value.strip()}")
            emitted.add(field)
    for key, value in _custom_entity_attributes(hit).items():
        if key in emitted:
            continue
        text_value = _rerank_attribute_text(value)
        if text_value:
            parts.append(f"{key}: {text_value}")
    return "\n".join(parts)[:max_chars]


async def search_kb(
    session: AsyncSession,
    org_id: UUID,
    query: str,
    *,
    top_k: int = 10,
    kb_ids: list[UUID] | None = None,
    embedder: EmbeddingService | None = None,
    reranker: Reranker | None | object = _DEFAULT_RERANKER,
    dual_read_target: DualReadTarget | None | object = _DEFAULT_DUAL_READ,
    structured_filters: StructuredSearchQuery | None = None,
    graph_enabled: bool | None = None,
    graph_max_hops: int | None = None,
) -> list[SearchHit]:
    """Run all enabled recall channels through one namespaced RRF."""
    _validate_top_k(top_k)
    if len(query) > 1000:
        raise ValueError("query must not exceed 1000 characters")
    query = query.strip()
    if not _has_search_terms(query) or kb_ids == []:
        return []

    channel_limit = min(_vector_candidate_limit(top_k), _MAX_TOP_K)
    registry: dict[CandidateKey, _Candidate] = {}
    structured = await _structured_candidates(
        session,
        org_id,
        query,
        top_k=channel_limit,
        kb_ids=kb_ids,
        filters=structured_filters,
    )
    for candidate in structured:
        registry[candidate.key] = candidate
    structured_ranking = [candidate.key for candidate in structured]
    settings = get_settings()
    use_graph = settings.kb_graph_search_enabled if graph_enabled is None else graph_enabled
    max_graph_hops = settings.kb_graph_max_hops if graph_max_hops is None else graph_max_hops
    graph_ranking: list[CandidateKey] = []
    if use_graph:
        as_of = date.today()
        graph_rows = await graph_recall_candidates(
            session,
            query,
            kb_ids=kb_ids,
            max_hops=max_graph_hops,
            top_k=channel_limit,
            as_of=as_of,
        )
        if graph_rows:
            graph_by_key = {
                (row.kind, row.entity_id): row for row in graph_rows
            }
            restored_graph_hits = await _restore_card_hits(
                session,
                org_id,
                [
                    (row.chunk, row.score, row.kind, row.entity_id)
                    for row in graph_rows
                ],
                max(row.score for row in graph_rows),
            )
            for hit in restored_graph_hits:
                key = (hit.kind, UUID(str(hit.data["id"])))
                graph_row = graph_by_key.get(key)
                if graph_row is None:
                    continue
                graph_ranking.append(key)
                existing = registry.get(key)
                if existing is None:
                    registry[key] = _Candidate(
                        key=key,
                        hit=replace(
                            hit,
                            data={
                                **hit.data,
                                "via": "graph",
                                "graph_hops": graph_row.hops,
                                "graph_predicates": list(graph_row.predicates),
                                "graph_edge_ids": [
                                    str(edge_id) for edge_id in graph_row.edge_ids
                                ],
                            },
                        ),
                        native_scores={"graph": graph_row.score},
                    )
                else:
                    existing.native_scores["graph"] = max(
                        existing.native_scores.get("graph", 0.0), graph_row.score
                    )
            graph_ranking = list(dict.fromkeys(graph_ranking))
    sparse_rows = await _sparse_chunk_hits(session, query, top_k=channel_limit, kb_ids=kb_ids)
    must_terms = structured_filters.must_include if structured_filters is not None else ()
    must_rows = (
        await _must_include_chunk_hits(session, must_terms, top_k=channel_limit, kb_ids=kb_ids)
        if must_terms
        else []
    )

    dense_model_rankings: list[list[tuple[KbChunk, float]]] = []
    if embedder is not None:
        try:
            [qvec] = await embedder.embed([query], org_id=org_id, task="kb.search.embedding")
            label = embedder.label
            fingerprint = getattr(embedder, "fingerprint", None)
            if fingerprint is None:
                provider, separator, model = label.partition(":")
                if not separator or not provider or not model:
                    raise ValueError("embedder label must be provider:model")
                fingerprint = EmbeddingFingerprint(provider, model, len(qvec))
            await _enable_relaxed_vector_scan(session)
            active_versioned = await _versioned_vector_candidates(
                session,
                qvec=qvec,
                fingerprint=fingerprint,
                top_k=channel_limit,
                kb_ids=kb_ids,
            )
            distance = KbChunk.embedding.cosine_distance(qvec)  # type: ignore[union-attr]
            legacy_statement = (
                select(KbChunk, distance.label("distance"))
                .where(KbChunk.embedding.is_not(None))  # type: ignore[union-attr]
                .where(KbChunk.embedding_model == label)
                .where(KbChunk.quarantined.is_(False))
                .where(_effective_chunk_filter())
                .order_by(distance)
            )
            if kb_ids is not None:
                legacy_statement = legacy_statement.where(KbChunk.kb_id.in_(kb_ids))
            legacy_rows = (
                await session.execute(
                    legacy_statement.limit(_vector_candidate_limit(channel_limit))
                )
            ).all()
            dense_model_rankings.append(
                merge_dense_candidates_scored(
                    active_versioned,
                    [(row[0], float(row[1])) for row in legacy_rows],
                    max_distance=get_settings().kb_vector_max_distance,
                    top_k=channel_limit,
                )
            )

            if dual_read_target is _DEFAULT_DUAL_READ:
                legacy_target = await _ready_target_config(session, kb_ids=kb_ids)
                target = (
                    DualReadTarget(config=legacy_target[0], kb_ids=legacy_target[1], manifest={})
                    if legacy_target is not None
                    else None
                )
            else:
                target = dual_read_target
            if target is not None:
                target_config, ready_kb_ids = target.config, target.kb_ids
                target_fingerprint = EmbeddingFingerprint.from_config(target_config)
                if target_fingerprint != fingerprint:
                    target_embedder: EmbeddingService | None = None
                    try:
                        target_embedder = _configured_embedder(target_config)
                        [target_qvec] = await target_embedder.embed(
                            [query],
                            org_id=org_id,
                            task="kb.search.embedding.dual_read",
                        )
                        target_rows = await _versioned_vector_candidates(
                            session,
                            qvec=target_qvec,
                            fingerprint=target_fingerprint,
                            top_k=channel_limit,
                            kb_ids=ready_kb_ids,
                        )
                        dense_model_rankings.append(
                            merge_dense_candidates_scored(
                                target_rows,
                                [],
                                max_distance=get_settings().kb_vector_max_distance,
                                top_k=channel_limit,
                            )
                        )
                    except (
                        EmbeddingUnavailableError,
                        LlmBudgetExceededError,
                        ValueError,
                    ) as exc:
                        logger.warning("目标 embedding 双读降级: %s", exc)
                    finally:
                        if target_embedder is not None:
                            await target_embedder.close()
        except (EmbeddingUnavailableError, LlmBudgetExceededError) as exc:
            KB_SEARCH_DENSE_DEGRADED.inc()
            logger.warning("KB dense 检索降级: %s", exc)

    dense_rows = merge_dense_rankings(dense_model_rankings)[:channel_limit]
    channel_chunks = (
        [chunk for chunk, _score in sparse_rows]
        + [chunk for chunk, _score in must_rows]
        + [chunk for chunk, _distance in dense_rows]
    )
    card_chunks: dict[CandidateKey, tuple[KbChunk, str, UUID]] = {}
    plain_chunks: dict[UUID, KbChunk] = {}
    for chunk in channel_chunks:
        card_ref = parse_card_ref(chunk.source_ref)
        if card_ref is None:
            plain_chunks[chunk.id] = chunk
        else:
            card_chunks.setdefault((card_ref[0], card_ref[1]), (chunk, *card_ref))

    expired_chunks = await _expired_doc_ids(
        session, {chunk.source_doc_id for chunk in plain_chunks.values()}
    )
    for chunk in plain_chunks.values():
        key = ("chunk", chunk.id)
        registry.setdefault(
            key,
            _Candidate(
                key=key,
                hit=SearchHit(
                    kind="chunk",
                    layer=_layer(org_id, chunk.org_id),
                    kb_id=str(chunk.kb_id),
                    source=chunk.source_ref or f"kb_chunks/{chunk.id}",
                    confidence=0.0,
                    data={
                        "id": str(chunk.id),
                        "content": chunk.content,
                        "source_doc_id": (
                            str(chunk.source_doc_id) if chunk.source_doc_id else None
                        ),
                        "content_kind": chunk.content_kind,
                        "image_asset_id": (
                            str(chunk.image_asset_id) if chunk.image_asset_id else None
                        ),
                        "revision_id": (str(chunk.revision_id) if chunk.revision_id else None),
                        "snapshot_id": (str(chunk.snapshot_id) if chunk.snapshot_id else None),
                        "heading_path": chunk.heading_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "page": chunk.page,
                        "cell_ref": (
                            (chunk.meta or {}).get("cell_ref")
                            if isinstance((chunk.meta or {}).get("cell_ref"), str)
                            else None
                        ),
                        _CITATION_REFS_KEY: ((chunk.meta or {}).get("citation_refs")),
                        "sibling_count": 0,
                        **_stale_flag(chunk.source_doc_id in expired_chunks),
                    },
                ),
                native_scores={},
            ),
        )

    if card_chunks:
        restored = await _restore_card_hits(
            session,
            org_id,
            [(chunk, 1.0, kind, entity_id) for chunk, kind, entity_id in card_chunks.values()],
            1.0,
        )
        for hit in restored:
            key = (hit.kind, UUID(hit.data["id"]))
            registry.setdefault(key, _Candidate(key=key, hit=hit, native_scores={}))

    def channel_key(chunk: KbChunk) -> CandidateKey:
        card_ref = parse_card_ref(chunk.source_ref)
        return card_ref if card_ref is not None else ("chunk", chunk.id)

    def unique_ranking(keys: list[CandidateKey]) -> list[CandidateKey]:
        return list(dict.fromkeys(key for key in keys if key in registry))

    sparse_ranking = unique_ranking([channel_key(chunk) for chunk, _ in sparse_rows])
    must_ranking = unique_ranking([channel_key(chunk) for chunk, _ in must_rows])
    dense_ranking = unique_ranking([channel_key(chunk) for chunk, _ in dense_rows])
    for chunk, sparse_score in sparse_rows:
        key = channel_key(chunk)
        if key in registry:
            registry[key].native_scores["sparse"] = max(
                registry[key].native_scores.get("sparse", 0.0),
                float(sparse_score),
            )
    for chunk, must_score in must_rows:
        key = channel_key(chunk)
        if key in registry:
            registry[key].native_scores["must_include"] = max(
                registry[key].native_scores.get("must_include", 0.0),
                float(must_score),
            )
    for chunk, distance in dense_rows:
        key = channel_key(chunk)
        if key in registry:
            registry[key].native_scores["dense"] = max(
                registry[key].native_scores.get("dense", float("-inf")),
                1.0 - float(distance),
            )

    if not structured_ranking and not sparse_ranking and not must_ranking and not graph_ranking:
        # 仅 dense 命中且无任何词面/结构化佐证:语义近邻不构成证据,如实拒答
        # (负例拒答门禁;r4-acceptance 禁止只调距离阈值放行)
        if dense_ranking:
            KB_SEARCH_REFUSALS.inc()
        else:
            KB_SEARCH_EMPTY.inc()  # 各通道均无候选:真·空结果
        return []

    ranked_channels = [
        (ranking, [registry[key].native_scores[channel] for key in ranking])
        for channel, ranking in (
            ("structured", structured_ranking),
            ("sparse", sparse_ranking),
            ("must_include", must_ranking),
            ("dense", dense_ranking),
            ("graph", graph_ranking),
        )
        if ranking
    ]
    rrf_scores = reciprocal_rank_fusion(
        [ranking for ranking, _scores in ranked_channels],
        ranking_scores=[scores for _ranking, scores in ranked_channels],
    )
    if not rrf_scores:
        KB_SEARCH_EMPTY.inc()
        return []
    rrf_order = sorted(
        rrf_scores,
        key=lambda key: (
            -rrf_scores[key],
            _LAYER_ORDER.get(registry[key].hit.layer, 9),
            key[0],
            str(key[1]),
        ),
    )

    configured_reranker: Reranker | None
    if reranker is _DEFAULT_RERANKER:
        try:
            configured_reranker = get_rerank_service()
        except RerankError as exc:
            KB_SEARCH_RERANK_DEGRADED.labels(reason="config").inc()
            logger.warning("KB rerank 配置降级为 RRF: %s", exc)
            configured_reranker = None
    else:
        configured_reranker = reranker

    rerank_scores: dict[CandidateKey, float] = {}
    if configured_reranker is not None:
        rerank_keys = rrf_order[:RERANK_TOP_N]
        documents = [_rerank_document(registry[key].hit) for key in rerank_keys]
        try:
            scores = await configured_reranker.rerank(
                org_id=org_id, query=query, documents=documents
            )
            if len(scores) != len(rerank_keys):
                raise ValueError("reranker returned an incomplete score vector")
            rerank_scores = dict(zip(rerank_keys, map(float, scores), strict=True))
        except (RerankError, ValueError) as exc:
            KB_SEARCH_RERANK_DEGRADED.labels(reason="call").inc()
            logger.warning("KB rerank 降级为 RRF: %s", exc)

    final_keys = sorted(
        rrf_order,
        key=lambda key: (
            0 if key in rerank_scores else 1,
            -rerank_scores.get(key, rrf_scores[key]),
            _LAYER_ORDER.get(registry[key].hit.layer, 9),
            key[0],
            str(key[1]),
        ),
    )
    folded_keys: list[CandidateKey] = []
    source_representatives: dict[str, CandidateKey] = {}
    sibling_counts: dict[CandidateKey, int] = {}
    for key in final_keys:
        hit = registry[key].hit
        source_doc_id = hit.data.get("source_doc_id") if hit.kind == "chunk" else None
        if isinstance(source_doc_id, str) and source_doc_id:
            representative = source_representatives.get(source_doc_id)
            if representative is not None:
                sibling_counts[representative] = sibling_counts.get(representative, 0) + 1
                continue
            source_representatives[source_doc_id] = key
        folded_keys.append(key)
    folded_keys = folded_keys[:top_k]
    hits = [
        SearchHit(
            kind=registry[key].hit.kind,
            layer=registry[key].hit.layer,
            kb_id=registry[key].hit.kb_id,
            source=registry[key].hit.source,
            confidence=rerank_scores.get(key, rrf_scores[key]),
            data={
                **registry[key].hit.data,
                "sibling_count": sibling_counts.get(
                    key, registry[key].hit.data.get("sibling_count", 0)
                ),
                "scores": {
                    "native": dict(sorted(registry[key].native_scores.items())),
                    "rrf": rrf_scores[key],
                    "rerank": rerank_scores.get(key),
                },
            },
        )
        for key in folded_keys
    ]
    return await _attach_citations(session, hits)


def default_embedder() -> EmbeddingService | None:
    s = get_settings()
    o = runtime_overrides("embedding")
    provider = str(o.get("provider") or s.embedding_provider)
    model = str(o.get("model") or s.embedding_model)
    endpoint = provider_model_endpoint(
        provider,
        model,
        capability="embedding",
    )
    if endpoint is not None:
        return EmbeddingService()
    return None


def default_embedding_execution() -> tuple[EmbeddingService | None, dict, dict]:
    """Resolve the exact default embedder once and return secret-free metadata."""
    settings = get_settings()
    overrides = runtime_overrides("embedding")
    provider = str(overrides.get("provider") or settings.embedding_provider)
    model = str(overrides.get("model") or settings.embedding_model)
    route_endpoints = capability_route_endpoints(
        EMBEDDING_ROUTE_TASK,
        "embedding",
        active_primary=(provider, model),
    )
    endpoint = next(
        (
            candidate
            for candidate in route_endpoints
            if (candidate.provider, candidate.model) == (provider, model)
        ),
        None,
    ) or provider_model_endpoint(
        provider,
        model,
        capability="embedding",
    )
    fingerprint = EmbeddingFingerprint.from_config(
        {"provider": provider, "model": model, "dim": EMBEDDING_DIM}
    ).as_dict()
    if endpoint is None:
        return None, fingerprint, {"enabled": False}
    base_url = endpoint.base_url
    manifest = {
        "enabled": True,
        "endpoint_sha256": hashlib.sha256(
            str(base_url).strip().rstrip("/").encode("utf-8")
        ).hexdigest(),
    }
    service_kwargs: dict[str, Any] = {
        "api_key": endpoint.api_key,
        "base_url": str(base_url),
        "provider": provider,
        "model": model,
        "dim": EMBEDDING_DIM,
    }
    fallback_endpoints = [
        candidate
        for candidate in route_endpoints
        if (candidate.provider, candidate.model) != (provider, model)
    ]
    if fallback_endpoints:
        service_kwargs["fallback_endpoints"] = fallback_endpoints
    return EmbeddingService(**service_kwargs), fingerprint, manifest
