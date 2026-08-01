import json
from collections import Counter
from uuid import uuid4, uuid5

import pytest

from nicekit.kb.embedding import (
    EmbeddingUnavailableError,
    chunk_embedding_text,
    embedding_content_hash,
)
from nicekit.kb.projections import ProjectionPayloadError
from nicekit.kb.retrieval_projection import (
    _chunk_anchor_contains_evidence,
    _citation_pairs,
    _ensure_chunk_embeddings,
    _entity_card_content,
    _parse_staged_chunks,
    retrieval_chunk_id,
)
from nicekit.models.kb import EvidenceSpan, FactClaim, KbChunk


def _artifact(**overrides) -> bytes:
    item = {
        "chunk_index": 0,
        "content": "清迈亲子酒店",
        "embedding": [0.1, 0.2],
        "embedding_model": "siliconflow:BAAI/bge-m3",
        "end_line": 4,
        "heading_path": "清迈 > 酒店",
        "meta": None,
        "page": 1,
        "quarantined": False,
        "source_ref": "guide.md#chunk0",
        "start_line": 2,
    }
    item.update(overrides)
    return json.dumps([item], ensure_ascii=False).encode()


def test_retrieval_generation_model_has_tenant_fks_and_gc_safe_evidence() -> None:
    constraints = {constraint.name: constraint for constraint in KbChunk.__table__.constraints}
    assert [column.name for column in constraints["fk_kb_chunk_revision_tenant"].columns] == [
        "revision_id",
        "org_id",
        "kb_id",
    ]
    assert [column.name for column in constraints["fk_kb_chunk_snapshot_tenant"].columns] == [
        "org_id",
        "kb_id",
        "snapshot_id",
    ]
    assert KbChunk.__table__.c.revision_id.nullable
    assert KbChunk.__table__.c.snapshot_id.nullable
    chunk_fk = next(iter(EvidenceSpan.__table__.c.chunk_id.foreign_keys))
    assert chunk_fk.ondelete == "SET NULL"
    anchor = next(
        constraint
        for constraint in EvidenceSpan.__table__.constraints
        if constraint.name == "ck_evidence_span_anchor"
    )
    assert "chunk_id" not in str(anchor.sqltext)


def test_staged_chunk_contract_is_strict_and_ids_are_deterministic() -> None:
    parsed = _parse_staged_chunks(_artifact(), expected_count=1)
    assert parsed[0]["content"] == "清迈亲子酒店"
    snapshot_id, revision_id = uuid4(), uuid4()
    assert retrieval_chunk_id(snapshot_id, revision_id, 0) == retrieval_chunk_id(
        snapshot_id, revision_id, 0
    )
    assert retrieval_chunk_id(snapshot_id, revision_id, 0) != retrieval_chunk_id(
        snapshot_id, revision_id, 1
    )


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (_artifact(chunk_index=1), "indexes"),
        (_artifact(embedding=[float("nan")]), "finite"),
        (_artifact(unexpected=True), "fields"),
        (b"not-json", "UTF-8 JSON"),
    ],
)
def test_corrupt_staged_chunk_artifact_fails_build(artifact: bytes, message: str) -> None:
    with pytest.raises(ProjectionPayloadError, match=message):
        _parse_staged_chunks(artifact, expected_count=1)


def test_staged_chunk_count_must_match_run_stats() -> None:
    with pytest.raises(ProjectionPayloadError, match="count"):
        _parse_staged_chunks(_artifact(), expected_count=2)


def test_confirmed_claim_card_contains_searchable_fields_only() -> None:
    claim = FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="source_document",
        subject_id=uuid4(),
        predicate="product",
        value_json={
            "name": "A 型主机",
            "summary": "工业级主机",
            "private": {"x": 1},
        },
        raw_payload={},
    )

    content = _entity_card_content(claim)

    assert "A 型主机" in content and "summary: 工业级主机" in content
    assert "private" not in content


def test_wiki_claim_card_contains_title_and_published_body() -> None:
    claim = FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="wiki_page",
        subject_id=uuid4(),
        predicate="wiki_page",
        value_json={
            "title": "巴黎交通",
            "page_type": "topic",
            "content_markdown": "地铁与 RER 换乘说明",
        },
        raw_payload={},
    )

    content = _entity_card_content(claim)

    assert "title: 巴黎交通" in content
    assert "content_markdown: 地铁与 RER 换乘说明" in content


def _deriving_claim(
    predicate: str,
    value: dict,
    corrected: dict | None = None,
) -> FactClaim:
    return FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="source_document",
        subject_id=uuid4(),
        predicate=predicate,
        value_json=value,
        corrected_payload=corrected,
        raw_payload={},
    )


def test_projection_citation_refs_are_strict_and_stably_sorted() -> None:
    claim_a, claim_b, evidence_a, evidence_b = uuid4(), uuid4(), uuid4(), uuid4()
    expected = tuple(
        sorted(
            ((claim_a, evidence_a), (claim_b, evidence_b)),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        )
    )
    assert _citation_pairs(
        {
            "citation_refs": [
                {
                    "fact_claim_id": str(claim_b),
                    "evidence_span_id": str(evidence_b),
                },
                {
                    "fact_claim_id": str(claim_a),
                    "evidence_span_id": str(evidence_a),
                },
            ]
        }
    ) == expected
    with pytest.raises(ProjectionPayloadError, match="fields"):
        _citation_pairs({"citation_refs": [{"fact_claim_id": str(claim_a)}]})


def test_staged_chunk_citation_anchor_requires_contained_lines_and_compatible_page() -> None:
    evidence = EvidenceSpan(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        fact_claim_id=uuid4(),
        revision_id=uuid4(),
        page=2,
        start_line=3,
        end_line=4,
        quote_text="exact anchor",
    )

    assert _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": 2, "end_line": 5}, evidence
    )
    assert _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": 1, "end_line": 4}, evidence
    )
    assert not _chunk_anchor_contains_evidence(
        {"page": 3, "start_line": 2, "end_line": 5}, evidence
    )
    assert not _chunk_anchor_contains_evidence(
        {"page": None, "start_line": 2, "end_line": 5}, evidence
    )
    assert not _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": 4, "end_line": 5}, evidence
    )
    assert not _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": None, "end_line": None}, evidence
    )
    assert not _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": 1, "end_line": 5},
        evidence.model_copy(update={"start_line": None, "end_line": None}),
    )
    assert _chunk_anchor_contains_evidence(
        {"page": 2, "start_line": 2, "end_line": 5},
        evidence.model_copy(update={"page": None}),
    )


_DIM = 4
_LABEL = "test:snapshot-embed"


class _FakeResult:
    def __init__(self, rows: list[tuple[str, list[float]]]):
        self._rows = rows

    def all(self) -> list[tuple[str, list[float]]]:
        return self._rows


class _FakeSession:
    """只回应版本行复用查询的最小 session 桩。"""

    def __init__(self, rows: list[tuple[str, list[float]]] | None = None):
        self.rows = rows or []
        self.execute_calls = 0

    async def execute(self, _statement) -> _FakeResult:
        self.execute_calls += 1
        return _FakeResult(self.rows)


class _FakeEmbedder:
    def __init__(self, vector: list[float] | None = None, error: Exception | None = None):
        self.batches: list[list[str]] = []
        self._vector = vector if vector is not None else [0.5] * _DIM
        self._error = error

    async def embed(
        self, texts: list[str], *, org_id, task: str | None = None
    ) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._error is not None:
            raise self._error
        return [list(self._vector) for _ in texts]


def _chunk_row(content: str, *, quarantined: bool = False) -> dict:
    return {
        "id": uuid4(),
        "org_id": uuid4(),
        "kb_id": uuid4(),
        "content": content,
        "embedding": None,
        "embedding_model": None,
        "quarantined": quarantined,
        "heading_path": None,
    }


async def _run_fill(
    chunk_rows: list[dict],
    *,
    session: _FakeSession | None = None,
    embedding_rows: list[dict] | None = None,
    factory=None,
    counters: Counter | None = None,
) -> list[dict]:
    embedding_rows = embedding_rows if embedding_rows is not None else []
    await _ensure_chunk_embeddings(
        session or _FakeSession(),  # type: ignore[arg-type]
        org_id=uuid4(),
        kb_id=uuid4(),
        chunk_rows=chunk_rows,
        embedding_rows=embedding_rows,
        provider="test",
        model="snapshot-embed",
        dim=_DIM,
        expected_label=_LABEL,
        counters=counters if counters is not None else Counter(),
        embedding_service_factory=factory,
    )
    return embedding_rows


async def test_ensure_embeddings_reuses_content_addressed_vectors_without_api_calls() -> None:
    db_reused = _chunk_row("库内既有版本行命中")
    in_build_reused = _chunk_row("本次构建内已嵌入的同内容")
    quarantined = _chunk_row("隔离行不参与嵌入", quarantined=True)
    db_hash = embedding_content_hash(chunk_embedding_text(db_reused["content"], None))
    in_build_hash = embedding_content_hash(
        chunk_embedding_text(in_build_reused["content"], None)
    )
    session = _FakeSession(rows=[(db_hash, [0.1] * _DIM)])
    seeded = [
        {
            "id": uuid4(),
            "chunk_id": uuid4(),
            "content_hash": in_build_hash,
            "embedding": [0.2] * _DIM,
        }
    ]
    counters: Counter = Counter()

    def _forbidden_factory(_config):
        raise AssertionError("复用命中时不得触发嵌入 API")

    embedding_rows = await _run_fill(
        [db_reused, in_build_reused, quarantined],
        session=session,
        embedding_rows=seeded,
        factory=_forbidden_factory,
        counters=counters,
    )

    assert db_reused["embedding"] == [0.1] * _DIM
    assert db_reused["embedding_model"] == _LABEL
    assert in_build_reused["embedding"] == [0.2] * _DIM
    assert quarantined["embedding"] is None and quarantined["embedding_model"] is None
    assert counters["reused_embeddings"] == 2
    assert counters["generated_embeddings"] == 0
    new_rows = embedding_rows[1:]
    assert {row["content_hash"] for row in new_rows} == {db_hash, in_build_hash}
    assert all(row["provider"] == "test" and row["dim"] == _DIM for row in new_rows)
    assert new_rows[0]["id"] == uuid5(
        db_reused["id"], f"embedding:test:snapshot-embed:{_DIM}"
    )


async def test_ensure_embeddings_incrementally_embeds_deduplicated_new_content() -> None:
    first = _chunk_row("全新内容 A")
    duplicate = _chunk_row("全新内容 A")
    second = _chunk_row("全新内容 B")
    embedder = _FakeEmbedder(vector=[0.3] * _DIM)
    counters: Counter = Counter()

    embedding_rows = await _run_fill(
        [first, duplicate, second],
        factory=lambda config: embedder,
        counters=counters,
    )

    assert len(embedder.batches) == 1
    assert sorted(embedder.batches[0]) == ["全新内容 A", "全新内容 B"]
    assert first["embedding"] == duplicate["embedding"] == second["embedding"] == [0.3] * _DIM
    assert all(row["embedding_model"] == _LABEL for row in (first, duplicate, second))
    assert counters["reused_embeddings"] == 0
    assert counters["generated_embeddings"] == 3
    assert len(embedding_rows) == 3


async def test_ensure_embeddings_fails_build_when_embedding_service_unavailable() -> None:
    unavailable = _FakeEmbedder(error=EmbeddingUnavailableError("embedding 服务不可用"))
    with pytest.raises(EmbeddingUnavailableError):
        await _run_fill([_chunk_row("嵌不出来的内容")], factory=lambda config: unavailable)

    with pytest.raises(ProjectionPayloadError, match="dimension"):
        await _run_fill(
            [_chunk_row("维度不符的内容")],
            factory=lambda config: _FakeEmbedder(vector=[0.4] * (_DIM + 1)),
        )


async def test_ensure_embeddings_default_factory_requires_provider_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nicekit.kb import retrieval_projection as module

    monkeypatch.setattr(
        module,
        "provider_model_endpoint",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(ProjectionPayloadError, match="provider model"):
        await _run_fill([_chunk_row("没有凭证时必须失败")], factory=None)


async def test_ensure_embeddings_skips_when_all_rows_already_embedded() -> None:
    embedded = _chunk_row("已带向量")
    embedded["embedding"] = [0.6] * _DIM
    embedded["embedding_model"] = _LABEL
    session = _FakeSession()

    def _forbidden_factory(_config):
        raise AssertionError("无缺失时不得触发嵌入")

    embedding_rows = await _run_fill(
        [embedded], session=session, factory=_forbidden_factory
    )

    assert session.execute_calls == 0
    assert embedding_rows == []
