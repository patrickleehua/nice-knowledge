"""Unified KB retrieval ranking and input guard unit tests."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from nicekit.kb import search as search_module
from nicekit.kb.projections import card_source_ref
from nicekit.kb.rerank import RerankError
from nicekit.kb.search import (
    SearchHit,
    StructuredSearchQuery,
    _Candidate,
    _citation_payload,
    _controlled_sparse_groups,
    _fallback_quorum_rows,
    _fallback_sparse_chunk_statement,
    _has_search_terms,
    _merge_sparse_rows,
    _rerank_document,
    _round_robin_merge,
    _source_span_payload,
    _sparse_chunk_statement,
    _SparseFallbackGroup,
    _SparseLexeme,
    _structured_search_terms,
    _vector_candidate_limit,
    filter_vector_hits,
    merge_dense_candidates,
    merge_dense_candidates_scored,
    merge_dense_rankings,
    reciprocal_rank_fusion,
    search_execution_manifest,
    search_kb,
)
from nicekit.llm.capability_routes import ModelEndpoint
from nicekit.models.kb import (
    DocType,
    DocumentRevision,
    EvidenceSpan,
    KbChunk,
    KbEntity,
    SourceDocument,
)


def _revision(*, org_id: UUID, kb_id: UUID, doc_id: UUID) -> DocumentRevision:
    return DocumentRevision(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_no=1,
        sha256="a" * 64,
        original_object_key="tests/source.md",
    )


def test_search_execution_manifest_uses_live_search_constants() -> None:
    manifest = search_execution_manifest(top_k=8)
    assert manifest == {
        "top_k": 8,
        "rrf_k": 60,
        "max_top_k": 100,
        "candidate_limit": 50,
        "vector_max_distance": search_module.get_settings().kb_vector_max_distance,
        "graph_channel": {
            "enabled": search_module.get_settings().kb_graph_search_enabled,
            "max_hops": search_module.get_settings().kb_graph_max_hops,
            "enable_gate_min_gain_points": 5.0,
        },
        "fts_config": search_module.get_settings().kb_fts_regconfig,
        "sparse_query_formulation": {
            "primary": "websearch_to_tsquery",
            "fallback_token_source": "ts_debug_lexemes",
            "fallback_exact_config": "simple",
            "fallback_when": "primary_count_less_than_top_k",
            "selection_order": "length_desc_then_lexical",
            "drop_single_character": True,
            "drop_unmapped_tokens": True,
            "noise": sorted(search_module.DEFAULT_SPARSE_FALLBACK_NOISE),
            "noise_source": "sdk_default_stopwords",
            "min_terms": 1,
            "max_terms": 8,
            "synonyms": {},
            "noun_compound": "exact_lexeme_or_parser_components",
            "fallback_boolean": "required_anchors_and_then_optional_groups_or",
            "anchor_min_length": 3,
            "anchor_aliases": ["n"],
            "ascii_anchor_aliases": ["e"],
            "ascii_anchor_min_length": 3,
            "ascii_anchor_rule": "identifier_shape_camel_or_acronym_or_separator_or_alnum",
            "quorum": 0.6,
            "quorum_with_anchor": "at_most_optional_count_minus_one",
            "quorum_verification": "normalized_substring_on_chunk_text",
            "fetch_multiplier": 3,
            "merge": "primary_first_dedupe_chunk_id_then_top_k",
        },
        "must_include_channel": {
            "query": "plainto_tsquery",
            "max_terms": 5,
            "merge": "round_robin_per_term",
        },
        "dense_requires_lexical_corroboration": True,
        "hnsw_iterative_scan": "relaxed_order",
        "rerank_top_n": 50,
    }


def test_structured_search_terms_preserve_original_query() -> None:
    structured = StructuredSearchQuery(
        match_terms=("华东仓",),
        must_include=("A 型主机",),
    )
    assert _structured_search_terms("华东仓 A 型主机 单价", structured) == (
        "华东仓",
        "A 型主机",
        "华东仓 A 型主机 单价",
    )


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> list[object]:
        return self.rows


class _RowsSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _RowsResult(self.rows)


def test_fact_evidence_citation_carries_verified_revision_source() -> None:
    org_id, kb_id, doc_id, claim_id = uuid4(), uuid4(), uuid4(), uuid4()
    revision = _revision(org_id=org_id, kb_id=kb_id, doc_id=doc_id)
    evidence = EvidenceSpan(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        fact_claim_id=claim_id,
        revision_id=revision.id,
        chunk_id=uuid4(),
        page=3,
        start_line=10,
        end_line=12,
        cell_ref="B7",
        quote_text="已确认事实证据",
    )

    assert _citation_payload(claim_id, evidence, revision) == {
        "kind": "fact_evidence",
        "fact_claim_id": str(claim_id),
        "evidence_span_id": str(evidence.id),
        "revision_id": str(revision.id),
        "chunk_id": str(evidence.chunk_id),
        "source_doc_id": str(doc_id),
        "source_sha256": "a" * 64,
        "page": 3,
        "start_line": 10,
        "end_line": 12,
        "cell_ref": "B7",
        "quote_text": "已确认事实证据",
    }


def test_source_span_requires_versioned_snapshot_chunk_and_matching_revision() -> None:
    org_id, kb_id, doc_id, chunk_id, snapshot_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    revision = _revision(org_id=org_id, kb_id=kb_id, doc_id=doc_id)
    source_document = SourceDocument(
        id=doc_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="source.md",
        object_key="tests/source.md",
        sha256="a" * 64,
        doc_type=DocType.GENERAL,
    )
    chunk = KbChunk(
        id=chunk_id,
        org_id=org_id,
        kb_id=kb_id,
        revision_id=revision.id,
        snapshot_id=snapshot_id,
        source_doc_id=doc_id,
        content="不可变原文",
        page=2,
        start_line=5,
        end_line=7,
    )
    hit = SearchHit(
        kind="chunk",
        layer="tenant",
        kb_id=str(kb_id),
        source="tests/source.md#0",
        confidence=1.0,
        data={
            "id": str(chunk_id),
            "source_doc_id": str(doc_id),
            "revision_id": str(revision.id),
            "snapshot_id": str(snapshot_id),
            "content": "不可变原文",
            "page": 2,
            "start_line": 5,
            "end_line": 7,
            "cell_ref": None,
        },
    )

    assert _source_span_payload(hit, chunk, revision, source_document) == {
        "kind": "source_span",
        "revision_id": str(revision.id),
        "chunk_id": str(chunk_id),
        "source_doc_id": str(doc_id),
        "source_sha256": "a" * 64,
        "page": 2,
        "start_line": 5,
        "end_line": 7,
        "cell_ref": None,
        "quote_text": "不可变原文",
    }

    legacy = replace(hit, data={**hit.data, "snapshot_id": None})
    assert _source_span_payload(legacy, chunk, revision, source_document) is None
    wrong_source = replace(hit, data={**hit.data, "source_doc_id": str(uuid4())})
    assert _source_span_payload(wrong_source, chunk, revision, source_document) is None
    assert (
        _source_span_payload(
            hit,
            chunk.model_copy(update={"quarantined": True}),
            revision,
            source_document,
        )
        is None
    )
    assert (
        _source_span_payload(
            hit,
            chunk,
            revision,
            source_document.model_copy(update={"doc_type": "product"}),
        )
        is None
    )
    assert (
        _source_span_payload(
            hit,
            chunk,
            revision,
            source_document.model_copy(
                update={"lifecycle_status": "withdrawal_pending"}
            ),
        )
        is None
    )
    assert (
        _source_span_payload(
            hit,
            chunk,
            revision.model_copy(update={"status": "tombstoned"}),
            source_document,
        )
        is None
    )
    unanchored = chunk.model_copy(update={"page": None, "start_line": None, "end_line": None})
    assert _source_span_payload(hit, unanchored, revision, source_document) is None
    half_anchored = chunk.model_copy(update={"page": None, "start_line": 5, "end_line": None})
    assert _source_span_payload(hit, half_anchored, revision, source_document) is None


def test_default_embedding_execution_resolves_runtime_once(monkeypatch) -> None:
    settings = SimpleNamespace(
        embedding_provider="env-provider",
        embedding_model="env-model",
    )
    runtime_calls = []
    captured = {}

    class FakeEmbeddingService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(search_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        search_module,
        "runtime_overrides",
        lambda name: (
            runtime_calls.append(name)
            or {
                "api_key": "runtime-key",
                "base_url": "https://runtime.example/v1",
                "provider": "runtime-provider",
                "model": "runtime-model",
            }
        ),
    )
    endpoint = ModelEndpoint(
        provider="runtime-provider",
        model="runtime-model",
        api_key="provider-key",
        base_url="https://provider.example/v1",
    )
    monkeypatch.setattr(
        search_module,
        "capability_route_endpoints",
        lambda *_args, **_kwargs: [endpoint],
    )
    monkeypatch.setattr(search_module, "EmbeddingService", FakeEmbeddingService)

    _service, fingerprint, manifest = search_module.default_embedding_execution()

    assert runtime_calls == ["embedding"]
    assert captured == {
        "api_key": "provider-key",
        "base_url": "https://provider.example/v1",
        "provider": "runtime-provider",
        "model": "runtime-model",
        "dim": 1024,
    }
    assert fingerprint == {
        "provider": "runtime-provider",
        "model": "runtime-model",
        "dim": 1024,
    }
    assert "key" not in manifest


def test_dual_read_transport_uses_provider_inventory(monkeypatch) -> None:
    endpoint = ModelEndpoint(
        provider="catalog",
        model="embedding-model",
        api_key="provider-key",
        base_url="https://provider.example/v1",
    )
    monkeypatch.setattr(
        search_module,
        "provider_model_endpoint",
        lambda *_args, **_kwargs: endpoint,
    )
    assert search_module._configured_embedding_transport(
        {"provider": "catalog", "model": "embedding-model"}
    ) == (
        "provider-key",
        "https://provider.example/v1",
    )


def test_dual_read_transport_has_no_legacy_credential_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        search_module,
        "get_settings",
        lambda: SimpleNamespace(
            embedding_provider="catalog",
            embedding_model="embedding-model",
        ),
    )
    monkeypatch.setattr(
        search_module,
        "runtime_overrides",
        lambda _name: {
            "api_key": "legacy-key",
            "base_url": "https://legacy.example/v1",
        },
    )
    monkeypatch.setattr(
        search_module,
        "provider_model_endpoint",
        lambda *_args, **_kwargs: None,
    )
    assert search_module._configured_embedding_transport({}) == (
        None,
        "",
    )


def _chunk(source_doc_id=None, content: str = "c") -> KbChunk:
    return KbChunk(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        source_doc_id=source_doc_id,
        content=content,
    )


def _search_candidate(
    *,
    candidate_id: UUID | None = None,
    kind: str = "page",
    layer: str = "tenant",
    source_doc_id: UUID | None = None,
    structured_score: float = 1.0,
) -> _Candidate:
    candidate_id = candidate_id or uuid4()
    data = {"id": str(candidate_id), "name": f"candidate-{candidate_id}"}
    if kind == "chunk":
        data = {
            "id": str(candidate_id),
            "content": f"chunk-{candidate_id}",
            "source_doc_id": str(source_doc_id) if source_doc_id else None,
            "sibling_count": 0,
        }
    return _Candidate(
        key=(kind, candidate_id),
        hit=SearchHit(
            kind=kind,
            layer=layer,
            kb_id=str(uuid4()),
            source=f"{kind}/{candidate_id}",
            confidence=0.0,
            data=data,
        ),
        native_scores={"structured": structured_score},
    )


def _patch_structured_only(monkeypatch, candidates: list[_Candidate]) -> None:
    """只留结构化一路出候选;其余通道置空,便于单独断言融合与排序。

    图谱一路默认开启(kb_graph_search_enabled),而这些用例传的是假 session,
    真去查图投影会直接 AttributeError。这里一并置空,要测图谱的用例自行覆盖
    ``graph_recall_candidates`` 或显式传 ``graph_enabled``。
    """
    from nicekit.kb import search as module

    async def structured(*_args, **_kwargs):
        return candidates

    async def sparse(*_args, **_kwargs):
        return []

    async def graph(*_args, **_kwargs):
        return []

    async def citations(_session, hits):
        return hits

    monkeypatch.setattr(module, "_structured_candidates", structured)
    monkeypatch.setattr(module, "_sparse_chunk_hits", sparse)
    monkeypatch.setattr(module, "graph_recall_candidates", graph)
    monkeypatch.setattr(module, "_attach_citations", citations)


async def test_graph_channel_is_not_called_when_explicitly_disabled(monkeypatch) -> None:
    from nicekit.kb import search as module

    _patch_structured_only(monkeypatch, [_search_candidate()])

    async def unexpected_graph(*_args, **_kwargs):
        raise AssertionError("graph_enabled=False must not query the graph")

    monkeypatch.setattr(module, "graph_recall_candidates", unexpected_graph)
    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "巴黎",
        embedder=None,
        reranker=None,
        graph_enabled=False,
    )

    assert len(hits) == 1
    assert "graph" not in hits[0].data["scores"]["native"]


async def test_graph_channel_enters_rrf_without_fabricating_citation(monkeypatch) -> None:
    from nicekit.kb import search as module
    from nicekit.kb.graph_search import GraphRecallCandidate

    _patch_structured_only(monkeypatch, [])
    org_id, kb_id, entity_id = uuid4(), uuid4(), uuid4()
    card = KbChunk(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        content="A 型主机 产品卡",
        source_ref=card_source_ref("product", entity_id),
    )

    async def graph(*_args, **kwargs):
        assert kwargs["max_hops"] == 2
        return [
            GraphRecallCandidate(
                chunk=card,
                kind="product",
                entity_id=entity_id,
                score=1.0,
                hops=2,
                edge_ids=(uuid4(), uuid4()),
                predicates=("located_in", "related"),
            )
        ]

    async def restore(*_args, **_kwargs):
        return [
            SearchHit(
                kind="product",
                layer="tenant",
                kb_id=str(kb_id),
                source=f"kb_entities/{entity_id}",
                confidence=1.0,
                data={"id": str(entity_id), "snapshot_id": str(uuid4())},
            )
        ]

    monkeypatch.setattr(module, "graph_recall_candidates", graph)
    monkeypatch.setattr(module, "_restore_card_hits", restore)
    [hit] = await search_kb(
        object(),  # type: ignore[arg-type]
        org_id,
        "A 型主机",
        embedder=None,
        reranker=None,
        graph_enabled=True,
        graph_max_hops=2,
    )

    assert hit.citation is None
    assert hit.data["via"] == "graph"
    assert hit.data["graph_hops"] == 2
    assert hit.data["scores"]["native"]["graph"] == 1.0
    assert hit.data["scores"]["rrf"] == pytest.approx(1 / 61)


# ---- 向量距离阈值(无意义查询防护)-----------------------------------------


def test_filter_vector_hits_drops_over_threshold() -> None:
    near, mid, far = _chunk(), _chunk(), _chunk()
    rows = [(near, 0.30), (mid, 0.62), (far, 0.80)]
    kept = filter_vector_hits(rows, max_distance=0.62)
    # 阈值为闭区间上界:0.62 保留,0.80 丢弃;保持距离升序
    assert kept == [near, mid]


def test_filter_vector_hits_all_far_returns_empty() -> None:
    """乱码查询:全部最近邻都超阈值 → 如实返回空,不编造 top-k。"""
    rows = [(_chunk(), 0.71), (_chunk(), 0.75), (_chunk(), 0.9)]
    assert filter_vector_hits(rows, max_distance=0.62) == []


def test_filter_vector_hits_none_distance_dropped() -> None:
    assert filter_vector_hits([(_chunk(), None)], max_distance=0.62) == []


def test_filter_vector_hits_strictly_reorders_relaxed_candidates() -> None:
    near, mid, far = _chunk(), _chunk(), _chunk()
    rows = [(far, 0.6), (near, 0.2), (mid, 0.4)]
    assert filter_vector_hits(rows, max_distance=0.62) == [near, mid, far]


def test_dense_merge_keeps_versioned_duplicate_distance_and_caps_total() -> None:
    duplicate = _chunk(content="duplicate")
    versioned_only = _chunk(content="versioned")
    legacy_nearest = _chunk(content="legacy-nearest")
    over_limit = _chunk(content="over-limit")

    merged = merge_dense_candidates(
        [(duplicate, 0.5), (versioned_only, 0.3)],
        [(duplicate, 0.2), (legacy_nearest, 0.1), (over_limit, 0.4)],
        max_distance=0.62,
        top_k=4,
    )

    assert merged == [legacy_nearest, versioned_only, over_limit, duplicate]
    assert len(merged) <= 4


def test_scored_dense_merge_filters_distance_before_channel_ranking() -> None:
    near, far = _chunk(), _chunk()
    assert merge_dense_candidates_scored(
        [(far, 0.7), (near, 0.2)],
        [],
        max_distance=0.62,
        top_k=10,
    ) == [(near, 0.2)]


def test_dual_dense_models_collapse_to_one_stable_ranking() -> None:
    shared, old_only, new_only = _chunk(), _chunk(), _chunk()
    merged = merge_dense_rankings(
        [
            [(shared, 0.2), (old_only, 0.3)],
            [(new_only, 0.1), (shared, 0.25)],
        ]
    )

    assert [chunk for chunk, _distance in merged] == [new_only, shared, old_only]
    assert [chunk.id for chunk, _distance in merged].count(shared.id) == 1


async def test_card_and_structured_candidate_cast_one_key_into_global_rrf(monkeypatch) -> None:
    from nicekit.kb import search as module

    org_id, kb_id, entity_id = uuid4(), uuid4(), uuid4()
    structured_hit = SearchHit(
        kind="product",
        layer="tenant",
        kb_id=str(kb_id),
        source=f"kb_entities/{entity_id}",
        confidence=0.0,
        data={"id": str(entity_id), "name": "A 型主机"},
    )
    candidate = _Candidate(
        key=("product", entity_id),
        hit=structured_hit,
        native_scores={"structured": 0.85},
    )
    card = KbChunk(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        content="A 型主机 产品卡",
        source_ref=card_source_ref("product", entity_id),
    )

    async def structured(*_args, **_kwargs):
        return [candidate]

    async def sparse(*_args, **_kwargs):
        return [(card, 0.4)]

    async def no_graph(*_args, **_kwargs):
        return []

    async def no_expiry(*_args, **_kwargs):
        return set()

    async def already_restored(*_args, **_kwargs):
        return []

    async def citations(_session, hits):
        return hits

    class FakeReranker:
        async def rerank(self, *, org_id: UUID, query: str, documents: list[str]):
            assert len(documents) == 1
            return [0.77]

    monkeypatch.setattr(module, "_structured_candidates", structured)
    monkeypatch.setattr(module, "_sparse_chunk_hits", sparse)
    # 图谱一路默认开启,而这里传的是假 session:置空以聚焦卡片/结构化的键合并
    monkeypatch.setattr(module, "graph_recall_candidates", no_graph)
    monkeypatch.setattr(module, "_expired_doc_ids", no_expiry)
    monkeypatch.setattr(module, "_restore_card_hits", already_restored)
    monkeypatch.setattr(module, "_attach_citations", citations)

    [hit] = await search_kb(
        object(),  # type: ignore[arg-type]
        org_id,
        "A 型主机",
        embedder=None,
        reranker=FakeReranker(),
    )

    assert hit.kind == "product"
    assert hit.confidence == 0.77
    assert set(hit.data["scores"]["native"]) == {"structured", "sparse"}
    assert hit.data["scores"]["rrf"] == pytest.approx(2 / 61)
    assert hit.data["scores"]["rerank"] == 0.77


async def test_dense_only_hits_without_lexical_corroboration_are_rejected(monkeypatch) -> None:
    """structured/sparse/must 全空时,仅 dense 语义近邻不构成证据,整体拒答。"""
    from nicekit.kb import search as module

    _patch_structured_only(monkeypatch, [])

    class OneVectorEmbedder:
        label = "fake:one"

        async def embed(self, texts, *, org_id, task=None):
            return [[1.0] + [0.0] * 1023 for _ in texts]

    dense_chunk = _chunk(content="巴黎 chunk 语义近邻")

    async def dense_candidates(*_args, **_kwargs):
        return [(dense_chunk, 0.2)]

    async def no_expiry(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(module, "_versioned_vector_candidates", dense_candidates)
    monkeypatch.setattr(module, "_expired_doc_ids", no_expiry)
    monkeypatch.setattr(module, "_enable_relaxed_vector_scan", no_expiry)

    class NoLegacySession:
        async def execute(self, _stmt):
            class _Empty:
                def all(self):
                    return []

            return _Empty()

    hits = await search_kb(
        NoLegacySession(),  # type: ignore[arg-type]
        uuid4(),
        "雷克雅未克行程",
        embedder=OneVectorEmbedder(),
        reranker=None,
        dual_read_target=None,
    )

    assert hits == []


async def test_restored_card_hit_carries_snapshot_id_for_citations() -> None:
    """卡片语义召回还原的实体必须带 snapshot_id,否则永远附不上 fact_evidence。"""
    from nicekit.kb.search import _restore_card_hits

    org_id, snapshot_id = uuid4(), uuid4()
    row = KbEntity(
        id=uuid4(),
        org_id=org_id,
        kb_id=uuid4(),
        entity_type_key="product",
        name="A 型主机",
        attributes={"name": "A 型主机", "warehouse": "华东仓", "unit_price": 1200},
    )
    row.snapshot_id = snapshot_id

    class OneRowSession:
        async def execute(self, _stmt):
            class _Rows:
                def scalars(self):
                    class _All:
                        def all(self):
                            return [row]

                    return _All()

                def all(self):
                    return []

            return _Rows()

    card = _chunk(content="product card")
    card.snapshot_id = snapshot_id
    [hit] = await _restore_card_hits(
        OneRowSession(),  # type: ignore[arg-type]
        org_id,
        [(card, 1.0, "product", row.id)],
        1.0,
    )

    assert hit.kind == "product"
    assert hit.data["snapshot_id"] == str(snapshot_id)
    assert hit.data["via"] == "semantic_card"
    # B14:实体数据从 KbEntity.attributes 直出(不再有逐类型的字段分支)
    assert hit.data["warehouse"] == "华东仓"
    assert hit.data["unit_price"] == 1200


async def test_search_kb_merges_must_include_channel(monkeypatch) -> None:
    from nicekit.kb import search as module

    _patch_structured_only(monkeypatch, [])
    must_chunk = _chunk(content="卢浮宫周二闭馆")

    async def must_hits(_session, terms, *, top_k, kb_ids):
        assert terms == ("卢浮宫",)
        assert kb_ids is None
        return [(must_chunk, 0.42)]

    async def no_expiry(_session, _doc_ids):
        return set()

    monkeypatch.setattr(module, "_must_include_chunk_hits", must_hits)
    monkeypatch.setattr(module, "_expired_doc_ids", no_expiry)

    [hit] = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "欧洲行程",
        embedder=None,
        reranker=None,
        structured_filters=StructuredSearchQuery(must_include=("卢浮宫",)),
    )

    # 稀疏整句零命中时,必含词通道独立成路进入 RRF
    assert hit.kind == "chunk"
    assert hit.data["id"] == str(must_chunk.id)
    assert hit.data["scores"]["native"] == {"must_include": 0.42}
    assert hit.confidence == pytest.approx(1 / 61)


async def test_search_kb_skips_must_channel_without_terms(monkeypatch) -> None:
    from nicekit.kb import search as module

    _patch_structured_only(monkeypatch, [_search_candidate()])

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("must_include channel must not run without terms")

    monkeypatch.setattr(module, "_must_include_chunk_hits", unexpected)

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        embedder=None,
        reranker=None,
        structured_filters=StructuredSearchQuery(),
    )

    assert len(hits) == 1


async def test_rerank_timeout_falls_back_to_exact_rrf_order(monkeypatch) -> None:
    candidates = [_search_candidate(structured_score=4 - index) for index in range(4)]
    _patch_structured_only(monkeypatch, candidates)

    class TimedOutReranker:
        async def rerank(self, **_kwargs):
            raise RerankError("rerank request timed out")

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=4,
        embedder=None,
        reranker=TimedOutReranker(),
    )

    assert [hit.data["id"] for hit in hits] == [str(item.key[1]) for item in candidates]
    assert all(hit.data["scores"]["rerank"] is None for hit in hits)
    assert [hit.confidence for hit in hits] == pytest.approx(
        [1 / (60 + rank) for rank in range(1, 5)]
    )


async def test_reranked_top_fifty_remain_before_unreranked_rrf_tail(monkeypatch) -> None:
    candidates = [_search_candidate(structured_score=55 - index) for index in range(55)]
    _patch_structured_only(monkeypatch, candidates)

    class ReverseReranker:
        async def rerank(self, *, documents: list[str], **_kwargs):
            assert len(documents) == 50
            return [index / 49 for index in range(50)]

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=55,
        embedder=None,
        reranker=ReverseReranker(),
    )

    expected_prefix = [str(item.key[1]) for item in reversed(candidates[:50])]
    expected_tail = [str(item.key[1]) for item in candidates[50:]]
    assert [hit.data["id"] for hit in hits[:50]] == expected_prefix
    assert [hit.data["id"] for hit in hits[50:]] == expected_tail
    assert hits[49].confidence == 0.0
    assert hits[50].confidence > 0.0


async def test_layer_only_breaks_equal_final_relevance(monkeypatch) -> None:
    platform = _search_candidate(candidate_id=UUID(int=1), layer="platform")
    shared = _search_candidate(candidate_id=UUID(int=2), layer="shared")
    tenant = _search_candidate(candidate_id=UUID(int=3), layer="tenant")
    candidates = [platform, shared, tenant]
    _patch_structured_only(monkeypatch, candidates)

    class FixedReranker:
        def __init__(self, scores: list[float]) -> None:
            self.scores = scores

        async def rerank(self, **_kwargs):
            return self.scores

    unequal = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=3,
        embedder=None,
        reranker=FixedReranker([0.1, 0.5, 0.9]),
    )
    tied = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=3,
        embedder=None,
        reranker=FixedReranker([0.5, 0.5, 0.5]),
    )

    assert [hit.layer for hit in unequal] == ["platform", "shared", "tenant"]
    assert [hit.layer for hit in tied] == ["tenant", "shared", "platform"]


async def test_equal_structured_native_scores_share_rrf_rank_before_layer_tie_break(
    monkeypatch,
) -> None:
    candidates = [
        _search_candidate(candidate_id=UUID(int=1), layer="platform"),
        _search_candidate(candidate_id=UUID(int=2), layer="shared"),
        _search_candidate(candidate_id=UUID(int=3), layer="tenant"),
    ]
    _patch_structured_only(monkeypatch, candidates)

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=3,
        embedder=None,
        reranker=None,
    )

    assert [hit.layer for hit in hits] == ["tenant", "shared", "platform"]
    assert [hit.confidence for hit in hits] == pytest.approx([1 / 61] * 3)


async def test_rrf_layer_tie_break_applies_before_rerank_top_fifty_cutoff(
    monkeypatch,
) -> None:
    platforms = [
        _search_candidate(candidate_id=UUID(int=index), layer="platform") for index in range(1, 51)
    ]
    tenant = _search_candidate(candidate_id=UUID(int=999), layer="tenant")
    _patch_structured_only(monkeypatch, [*platforms, tenant])

    class TiedReranker:
        async def rerank(self, *, documents: list[str], **_kwargs):
            assert len(documents) == 50
            return [0.5] * 50

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=51,
        embedder=None,
        reranker=TiedReranker(),
    )

    by_id = {hit.data["id"]: hit for hit in hits}
    assert by_id[str(tenant.key[1])].data["scores"]["rerank"] == 0.5
    assert sum(hit.data["scores"]["rerank"] is None for hit in hits) == 1
    assert next(hit for hit in hits if hit.data["scores"]["rerank"] is None).layer == "platform"


async def test_same_document_folding_keeps_best_without_score_bonus(monkeypatch) -> None:
    source_doc_id = uuid4()
    first = _search_candidate(kind="chunk", source_doc_id=source_doc_id, structured_score=4.0)
    best = _search_candidate(kind="chunk", source_doc_id=source_doc_id, structured_score=3.0)
    third = _search_candidate(kind="chunk", source_doc_id=source_doc_id, structured_score=2.0)
    other = _search_candidate(kind="chunk", source_doc_id=uuid4(), structured_score=1.0)
    candidates = [first, best, third, other]
    _patch_structured_only(monkeypatch, candidates)

    class FixedReranker:
        async def rerank(self, **_kwargs):
            return [0.2, 0.9, 0.1, 0.8]

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "清迈酒店",
        top_k=4,
        embedder=None,
        reranker=FixedReranker(),
    )

    assert [hit.data["id"] for hit in hits] == [str(best.key[1]), str(other.key[1])]
    assert hits[0].data["sibling_count"] == 2
    assert hits[0].confidence == 0.9
    assert hits[1].data["sibling_count"] == 0


def test_rrf_native_ties_share_rank_and_score_vectors_must_align() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    scores = reciprocal_rank_fusion(
        [[first, second, third]],
        ranking_scores=[[0.9, 0.9, 0.4]],
    )

    assert scores[first] == pytest.approx(1 / 61)
    assert scores[second] == pytest.approx(1 / 61)
    assert scores[third] == pytest.approx(1 / 63)
    with pytest.raises(ValueError, match="align"):
        reciprocal_rank_fusion([[first]], ranking_scores=[[]])


# ---- PostgreSQL FTS sparse query -------------------------------------------


def test_sparse_statement_uses_bound_websearch_rank_scope_and_quarantine() -> None:
    kb_id = uuid4()
    hostile_query = "清迈' OR true -- 亲子 酒店"
    statement = _sparse_chunk_statement(
        hostile_query,
        top_k=20,
        kb_ids=[kb_id],
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "websearch_to_tsquery" in sql
    assert f"'{search_module.get_settings().kb_fts_regconfig}'::regconfig" in sql
    assert "kb_chunks.tsv @@" in sql
    assert "ts_rank_cd" in sql
    assert "ORDER BY sparse_rank DESC" in sql
    assert "kb_chunks.quarantined IS false" in sql
    assert "kb_chunks.kb_id IN" in sql
    assert "knowledge_bases.active_snapshot_id = kb_chunks.snapshot_id" in sql
    assert "source_documents.lifecycle_status =" in sql
    assert "document_revisions.tombstoned_at IS NULL" in sql
    assert hostile_query not in sql
    assert compiled.params["sparse_query"] == hostile_query
    assert kb_id in compiled.params["kb_id_1"]
    assert "ILIKE" not in sql.upper()


def test_search_term_guard_rejects_blank_and_punctuation() -> None:
    assert not _has_search_terms("")
    assert not _has_search_terms(" \t\n!?，。--")
    assert _has_search_terms("清迈")
    assert _has_search_terms("hotel")


def test_controlled_sparse_groups_drop_noise_compounds_and_bound_synonyms(
    monkeypatch,
) -> None:
    # 噪声/同义词表在 SDK 里是空默认 + 注册项(B16),这里注册一份语料词表
    monkeypatch.setattr(
        search_module, "_SPARSE_FALLBACK_NOISE", frozenset({"需要", "最好"})
    )
    monkeypatch.setattr(
        search_module, "_SPARSE_FALLBACK_SYNONYMS", {"买票": ("购票",)}
    )
    airport_groups = _controlled_sparse_groups(
        [
            _SparseLexeme("n", "巴黎戴高乐机场"),
            _SparseLexeme("n", "巴黎"),
            _SparseLexeme("n", "戴高"),
            _SparseLexeme("v", "到"),
            _SparseLexeme("v", "坐"),
            _SparseLexeme("n", "票价"),
            _SparseLexeme("v", "需要"),
        ]
    )
    assert [(group.alternatives, group.phrase, group.required) for group in airport_groups] == [
        ((("巴黎戴高乐机场",), ("巴黎", "戴高")), False, True),
        ((("票价",),), False, False),
    ]
    # B13:领域特判(相邻短语组)已删除,同长度词元一律逐词成组、无 phrase 组
    synonym_groups = _controlled_sparse_groups(
        [
            _SparseLexeme("n", "华东"),
            _SparseLexeme("v", "适合"),
            _SparseLexeme("n", "买票"),
            _SparseLexeme("v", "去"),
            _SparseLexeme("n", "旺季"),
        ]
    )
    assert [
        (group.alternatives, group.phrase, group.required) for group in synonym_groups
    ] == [
        ((("买票",), ("购票",)), False, False),
        ((("华东",),), False, False),
        ((("旺季",),), False, False),
        ((("适合",),), False, False),
    ]
    # 噪声词滤净后只剩单个实词:仍要构造出一个词组(等价于单关键词检索),
    # 否则"专名 + 一串疑问词"这类问句会整条回退通道放弃 → 零召回。
    assert _controlled_sparse_groups(
        [
            _SparseLexeme("n", "巴黎"),
            _SparseLexeme("v", "需要"),
            _SparseLexeme("n", "最好"),
        ]
    ) == (_SparseFallbackGroup(alternatives=(("巴黎",),)),)
    # 全部是噪声词/单字词元:没有任何检索意图,如实返回空,不做全库扫描
    assert (
        _controlled_sparse_groups(
            [
                _SparseLexeme("v", "需要"),
                _SparseLexeme("n", "最好"),
                _SparseLexeme("v", "去"),
            ]
        )
        == ()
    )


def test_controlled_sparse_groups_absorb_single_sublexeme_and_flag_anchor() -> None:
    """卢浮宫/卢浮 收敛为一个锚点组;弱动词与短名词保持 optional。"""
    groups = _controlled_sparse_groups(
        [
            _SparseLexeme("n", "卢浮宫"),
            _SparseLexeme("n", "卢浮"),
            _SparseLexeme("v", "闭馆"),
            _SparseLexeme("v", "参观"),
            _SparseLexeme("n", "时长"),
        ]
    )
    assert [(group.alternatives, group.required) for group in groups] == [
        ((("卢浮宫",), ("卢浮",)), True),
        ((("参观",),), False),
        ((("时长",),), False),
        ((("闭馆",),), False),
    ]


def test_fallback_sparse_statement_is_parameterized_bounded_and_scoped() -> None:
    kb_id = uuid4()
    groups = (
        _SparseFallbackGroup((("第一次",), ("首次",), ("首访",))),
        _SparseFallbackGroup((("巴黎",),)),
        _SparseFallbackGroup((("四星", "酒店"),), phrase=True),
    )
    statement = _fallback_sparse_chunk_statement(
        groups,
        top_k=20,
        kb_ids=[kb_id],
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "websearch_to_tsquery" not in sql
    assert sql.count("plainto_tsquery") == 8
    assert sql.count("phraseto_tsquery") == 2
    # 全部 optional 组:SQL 侧退化为 OR(命中其一),精确 quorum 由 Python 复核
    assert sql.count(" || ") == 8
    assert sql.count(" && ") == 0
    assert "kb_chunks.tsv @@" in sql
    assert "ts_rank_cd" in sql
    assert "kb_chunks.kb_id IN" in sql
    assert "'simple'::regconfig" in sql
    assert compiled.params["sparse_fallback_0_0_0"] == "第一次"
    assert compiled.params["sparse_fallback_0_1_0"] == "首次"
    assert compiled.params["sparse_fallback_0_2_0"] == "首访"
    assert compiled.params["sparse_fallback_2_0"] == "四星 酒店"


def test_compound_fallback_keeps_exact_name_and_bounds_component_branch() -> None:
    groups = (
        _SparseFallbackGroup(
            (("巴黎戴高乐机场",), ("巴黎", "戴高")),
            required=True,
        ),
        _SparseFallbackGroup((("票价",),)),
    )
    compiled = _fallback_sparse_chunk_statement(
        groups,
        top_k=20,
        kb_ids=None,
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    # 锚点组(全名 | 组件 AND)必须命中。唯一的 optional 组"票价"**不进 SQL**:
    # 有锚点时它的 quorum 为 0(见 _fallback_optional_quorum),若仍 && 上去,单个
    # optional 就在 SQL 侧退化成 AND,把候选行提前滤光——正是"WebSearch 什么时候用"
    # 零召回的成因。fallback 本就是 primary 精确 AND 失败后的兜底,这里宽是对的,
    # 精度交给 ts_rank 排序。
    assert sql.count(" || ") == 2
    assert sql.count(" && ") == 2
    assert "sparse_fallback_1_0_0" not in compiled.params
    assert compiled.params["sparse_fallback_0_0_0"] == "巴黎戴高乐机场"
    assert compiled.params["sparse_fallback_0_1_0"] == "巴黎"
    assert compiled.params["sparse_fallback_0_1_1"] == "戴高"


def test_fallback_statement_requires_all_anchors_and_any_optional() -> None:
    """两个锚点 AND;三个 optional 在 SQL 侧 OR 成一支。"""
    groups = (
        _SparseFallbackGroup((("卢浮宫",), ("卢浮",)), required=True),
        _SparseFallbackGroup((("凡尔赛宫",),), required=True),
        _SparseFallbackGroup((("闭馆",),)),
        _SparseFallbackGroup((("参观",),)),
        _SparseFallbackGroup((("时长",),)),
    )
    sql = str(
        _fallback_sparse_chunk_statement(groups, top_k=20, kb_ids=None).compile(
            dialect=postgresql.dialect()
        )
    )
    # (卢浮宫|卢浮) && 凡尔赛宫 && (闭馆 || 参观 || 时长):锚点间 2 个 &&,组内/optional 3 个 ||
    assert sql.count(" && ") == 4
    assert sql.count(" || ") == 6


def _fallback_chunk(content: str) -> KbChunk:
    return KbChunk(id=uuid4(), org_id=uuid4(), kb_id=uuid4(), content=content)


def test_fallback_quorum_enforces_anchors_and_optional_ratio() -> None:
    groups = (
        _SparseFallbackGroup((("卢浮宫",), ("卢浮",)), required=True),
        _SparseFallbackGroup((("闭馆",),)),
        _SparseFallbackGroup((("常规",),)),
        _SparseFallbackGroup((("参观",),)),
        _SparseFallbackGroup((("时长",),)),
    )
    matching = _fallback_chunk("卢浮宫每周二闭馆,常规参观时长约三小时")
    sublexeme_only = _fallback_chunk("卢浮预约参观:闭馆日与时长安排")
    missing_anchor = _fallback_chunk("巴黎地铁常规参观闭馆时长说明")
    below_quorum = _fallback_chunk("卢浮宫简介与馆藏亮点")
    rows = [
        (matching, 0.9),
        (sublexeme_only, 0.8),
        (missing_anchor, 0.7),
        (below_quorum, 0.6),
    ]

    kept = _fallback_quorum_rows(rows, groups, top_k=10)

    # 4 个 optional 需命中 ceil(4*0.6)=3;锚点组可由子词 卢浮 满足(parser 无关的原文复核)
    assert kept == [(matching, 0.9), (sublexeme_only, 0.8)]


def test_fallback_quorum_phrase_group_requires_adjacency() -> None:
    """短语组与 SQL 侧 phraseto 对齐:跨行/跨单元格的子串共现不算命中。"""
    groups = (
        _SparseFallbackGroup((("五星", "酒店"),), phrase=True),
        _SparseFallbackGroup((("巴黎",),)),
    )
    adjacent = _fallback_chunk("巴黎五星 酒店旺季价格")
    cross_row = _fallback_chunk("| 巴黎 | 酒店 | 四星 |\n| 塞维利亚 | Colon | 五星 |")

    kept = _fallback_quorum_rows([(adjacent, 0.9), (cross_row, 0.8)], groups, top_k=10)

    assert kept == [(adjacent, 0.9)]


def test_fallback_quorum_caps_top_k_and_normalizes_fullwidth_text() -> None:
    groups = (
        _SparseFallbackGroup((("票价",),)),
        _SparseFallbackGroup((("RER",),)),
    )
    fullwidth = _fallback_chunk("ＲＥＲ Ｂ 线票价说明")
    rows = [(fullwidth, 0.5), (_fallback_chunk("票价与RER乘车指南"), 0.4)]

    kept = _fallback_quorum_rows(rows, groups, top_k=1)

    # 全角 ＲＥＲ 归一后命中;top_k=1 截断
    assert kept == [(fullwidth, 0.5)]


def test_round_robin_merge_interleaves_terms_and_dedupes() -> None:
    louvre_best, louvre_second = _fallback_chunk("卢浮宫"), _fallback_chunk("卢浮宫导览")
    versailles_best = _fallback_chunk("凡尔赛宫")
    merged = _round_robin_merge(
        [
            [(louvre_best, 0.9), (louvre_second, 0.5)],
            [(versailles_best, 0.8), (louvre_best, 0.7)],
        ],
        top_k=10,
    )
    # 每个必含词的最优命中都排在第二梯队之前;跨词去重
    assert merged == [(louvre_best, 0.9), (versailles_best, 0.8), (louvre_second, 0.5)]
    assert _round_robin_merge([], top_k=5) == []


def test_sparse_merge_prioritizes_primary_deduplicates_and_caps_top_k() -> None:
    primary, fallback_only = _chunk(), _chunk()
    assert _merge_sparse_rows(
        [(primary, 0.2)],
        [(primary, 0.9), (fallback_only, 0.8)],
        top_k=2,
    ) == [(primary, 0.2), (fallback_only, 0.8)]
    assert _merge_sparse_rows(
        [(primary, 0.2)],
        [(fallback_only, 0.8)],
        top_k=1,
    ) == [(primary, 0.2)]


@pytest.mark.parametrize("top_k", [0, 101, True])
def test_search_rejects_invalid_internal_top_k(top_k: int) -> None:
    with pytest.raises(ValueError, match="top_k must be between 1 and 100"):
        _sparse_chunk_statement("清迈", top_k=top_k, kb_ids=None)


def test_vector_candidate_limit_overfetches_before_strict_sort() -> None:
    assert _vector_candidate_limit(1) == 50
    assert _vector_candidate_limit(20) == 60
    assert _vector_candidate_limit(100) == 300


async def test_empty_kb_scope_returns_without_sql() -> None:
    class NoSqlSession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("empty kb scope must not execute SQL")

    assert (
        await search_kb(
            NoSqlSession(),  # type: ignore[arg-type]
            uuid4(),
            "清迈酒店",
            kb_ids=[],
            embedder=None,
        )
        == []
    )


@pytest.mark.parametrize("query", ["", "  \t\n", " ，!? -- "])
async def test_blank_or_punctuation_search_returns_without_sql(query: str) -> None:
    class NoSqlSession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("meaningless query must not execute SQL")

    assert (
        await search_kb(
            NoSqlSession(),  # type: ignore[arg-type]
            uuid4(),
            query,
            embedder=None,
        )
        == []
    )


async def test_direct_search_rejects_oversized_query_without_sql() -> None:
    class NoSqlSession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("oversized query must not execute SQL")

    with pytest.raises(ValueError, match="query must not exceed 1000 characters"):
        await search_kb(
            NoSqlSession(),  # type: ignore[arg-type]
            uuid4(),
            "x" * 1001,
            embedder=None,
        )


def test_rerank_document_is_bounded_and_ignores_arbitrary_nested_payloads() -> None:
    hit = SearchHit(
        kind="product",
        layer="tenant",
        kb_id=str(uuid4()),
        source="kb_entities/1",
        confidence=0.0,
        data={"name": "A 型主机", "content": "x" * 5000, "structure": {"secret": "y"}},
    )

    document = _rerank_document(hit)

    assert len(document) == 4000
    assert "A 型主机" in document
    assert "secret" not in document


def _custom_entity_hit(data: dict) -> SearchHit:
    return SearchHit(
        kind="policy_rule",
        layer="tenant",
        kb_id=str(uuid4()),
        source=f"kb_entities/{uuid4()}",
        confidence=0.5,
        data=data,
    )


def test_rerank_document_serializes_custom_entity_attributes() -> None:
    """M3a 自定义实体:业务属性以 键: 值 追加,系统键与空值不进 rerank 文本。"""
    entity_id = uuid4()
    hit = _custom_entity_hit(
        {
            "id": str(entity_id),
            "entity_type_key": "policy_rule",
            "name": "出口合规细则",
            "办理时长": "5 个工作日",
            "费用": 1200,
            "适用国家": ["法国", "德国"],
            "材料": {"护照": "有效期 6 个月以上"},
            "备注": "  ",
            "内部标记": None,
            "空列表": [],
            "加急": True,
            "snapshot_id": str(uuid4()),
            "via": "semantic_card",
            "stale": True,
        }
    )

    document = _rerank_document(hit)

    lines = document.split("\n")
    assert lines[0] == "policy_rule"
    # 白名单字段(name)优先保留在前,且不因属性展平重复输出
    assert lines[1] == "name: 出口合规细则"
    assert document.count("name:") == 1
    assert "办理时长: 5 个工作日" in document
    assert "费用: 1200" in document
    # 嵌套结构 JSON 序列化(非 ASCII 原样保留)
    assert '适用国家: ["法国", "德国"]' in document
    assert '材料: {"护照": "有效期 6 个月以上"}' in document
    # None/空串/空容器跳过;bool 以 true/false 保留键词面
    assert "备注" not in document
    assert "内部标记" not in document
    assert "空列表" not in document
    assert "加急: true" in document
    # 系统键不进 rerank 文本
    assert str(entity_id) not in document
    assert "snapshot_id" not in document
    assert "via" not in document
    assert "stale" not in document
    assert "entity_type_key" not in document


def test_rerank_document_reads_nested_attributes_dict_shape() -> None:
    """兼容 data.attributes 嵌套形态:属性同样进入序列化文本。"""
    hit = _custom_entity_hit(
        {
            "id": str(uuid4()),
            "attributes": {"name": "冬季滑雪保险", "承保范围": "雪道意外与救援"},
        }
    )

    document = _rerank_document(hit)

    assert "name: 冬季滑雪保险" in document
    assert "承保范围: 雪道意外与救援" in document


def test_rerank_document_truncates_custom_attributes_within_total_budget() -> None:
    """单属性截断 400 字符;属性再多总长仍受 4000 上限,白名单字段保留在头部。"""
    data: dict = {
        "id": str(uuid4()),
        "entity_type_key": "policy_rule",
        "name": "出口合规细则",
        "超长属性": "长" * 1000,
    }
    for index in range(20):
        data[f"属性{index}"] = "值" * 400
    hit = _custom_entity_hit(data)

    document = _rerank_document(hit)

    assert document.startswith("policy_rule\nname: 出口合规细则\n")
    assert f"超长属性: {'长' * 400}\n" in document
    assert "长" * 401 not in document


async def test_graph_only_candidate_never_takes_the_document_slot_from_other_channels(
    monkeypatch,
) -> None:
    """同文档只出一条,图谱独有候选不得顶掉有词面/语义佐证的那一条。

    图谱召回的是**关系证据**所在的切片(它证明两个实体相连),未必是回答问题的
    那一段。真库实测:开图谱后"澜图鉴权服务部署在哪个机房"的代表从含答案的
    沧澜集群#chunk1 被换成了关系证据 #chunk2,recall 直接归零。
    """
    from nicekit.kb import search as module
    from nicekit.kb.graph_search import CHUNK_RESTORE, GraphRecallCandidate

    source_doc_id = uuid4()
    answer = _search_candidate(kind="chunk", source_doc_id=source_doc_id, structured_score=1.0)
    _patch_structured_only(monkeypatch, [answer])

    evidence_chunk = KbChunk(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        source_doc_id=source_doc_id,  # 与答案同文档
        content="关系证据段",
        source_ref="doc.md#chunk9",
    )

    async def graph(*_args, **_kwargs):
        return [
            GraphRecallCandidate(
                chunk=evidence_chunk,
                kind="chunk",
                entity_id=evidence_chunk.id,
                score=99.0,  # 刻意给到远高于答案的分数
                hops=1,
                edge_ids=(uuid4(),),
                predicates=("located_in",),
                restore_mode=CHUNK_RESTORE,
                anchor_entity_id=uuid4(),
            )
        ]

    async def no_expiry(*_args, **_kwargs):
        return set()

    async def no_labels(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(module, "graph_recall_candidates", graph)
    monkeypatch.setattr(module, "_expired_doc_ids", no_expiry)
    monkeypatch.setattr(module, "_graph_path_labels", no_labels)

    hits = await search_kb(
        object(),  # type: ignore[arg-type]
        uuid4(),
        "机房归属",
        top_k=5,
        embedder=None,
        reranker=None,
        graph_enabled=True,
    )

    # 该文档露出的必须仍是有佐证的那一条,图谱证据段只计入 sibling
    assert [hit.data["id"] for hit in hits] == [str(answer.key[1])]
    assert hits[0].data["sibling_count"] == 1
