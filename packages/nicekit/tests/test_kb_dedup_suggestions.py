"""实体归一合并候选推荐:信号分层打分 + 编排(FakeSession,无 DB)。"""

from uuid import uuid4

from nicekit.kb.dedup_suggestions import (
    MAX_CONFIDENCE,
    cosine_similarity,
    levenshtein,
    name_tokens,
    score_candidate_pair,
    score_name_pair,
    semantic_bonus,
    suggest_merge_candidates,
)
from nicekit.models.kb import CanonicalEntity, EntityAlias, KbChunk

# ---- 词元与编辑距离 ---------------------------------------------------------


def test_name_tokens_expands_cjk_bigrams_and_keeps_latin_words() -> None:
    assert name_tokens("香格里拉") == {"香格", "格里", "里拉"}
    assert name_tokens("shangri-la paris") == {"shangri-la", "paris"}
    assert name_tokens("巴黎 shangri-la") == {"巴黎", "shangri-la"}
    assert name_tokens("塔") == {"塔"}


def test_levenshtein_basic_cases() -> None:
    assert levenshtein("hilton", "hilton") == 0
    assert levenshtein("hilton", "hillton") == 1
    assert levenshtein("", "abc") == 3
    assert levenshtein("kitten", "sitting") == 3


# ---- 强信号(0.9+)-----------------------------------------------------------


def test_canonical_name_match_scores_highest() -> None:
    score, reasons = score_candidate_pair(
        "hilton bangkok", "hilton bangkok", frozenset(), frozenset()
    )
    assert score == 0.98
    assert "canonical_name_match" in reasons


def test_shared_alias_is_strong_signal() -> None:
    score, reasons = score_candidate_pair(
        "香格里拉大酒店",
        "shangri-la paris",
        frozenset({"香格里拉大酒店", "shangri-la paris"}),
        frozenset({"shangri-la paris"}),
    )
    assert score == 0.95
    assert any(r.startswith("shared_alias:") for r in reasons)


def test_canonical_name_as_other_alias_is_strong_signal() -> None:
    score, reasons = score_candidate_pair(
        "香格里拉大酒店",
        "shangri-la paris",
        frozenset(),
        frozenset({"香格里拉大酒店"}),
    )
    assert score == 0.92
    assert "canonical_is_alias" in reasons


# ---- 中信号(0.6 ~ 0.88)----------------------------------------------------


def test_chinese_substring_is_medium_signal() -> None:
    score, reasons = score_name_pair("香格里拉", "巴黎香格里拉大酒店")
    assert 0.6 <= score < 0.9
    assert "name_substring" in reasons


def test_latin_token_overlap_is_medium_signal() -> None:
    score, reasons = score_name_pair("shangri-la paris", "shangri-la hotel paris")
    assert 0.6 <= score < 0.9
    assert any(r.startswith("token_overlap:") for r in reasons)


def test_latin_edit_distance_is_medium_signal() -> None:
    score, reasons = score_name_pair("hilton bangkok", "hilton bangkkok")
    assert 0.6 <= score < 0.9
    assert any(r.startswith("edit_distance:") for r in reasons)


def test_edit_distance_not_used_for_cjk_names() -> None:
    _score, reasons = score_name_pair("北京烤鸭", "北京烤鹅")
    assert not any(r.startswith("edit_distance:") for r in reasons)


def test_unrelated_names_score_zero() -> None:
    score, reasons = score_candidate_pair("东京塔", "巴黎铁塔", frozenset(), frozenset())
    assert score < 0.6
    assert reasons == [] or not any(r.startswith("shared_alias") for r in reasons)


def test_strong_signal_outranks_medium_signal() -> None:
    strong, _ = score_candidate_pair(
        "a hotel", "b hotel", frozenset({"same"}), frozenset({"same"})
    )
    medium, _ = score_candidate_pair(
        "shangri-la paris", "shangri-la hotel paris", frozenset(), frozenset()
    )
    assert strong > medium >= 0.6


# ---- 语义信号 ----------------------------------------------------------------


def test_cosine_similarity_and_bonus_tiers() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) is None
    assert cosine_similarity([1.0], [1.0, 0.0]) is None
    assert semantic_bonus(0.95) == 0.08
    assert semantic_bonus(0.85) == 0.05
    assert semantic_bonus(0.75) == 0.02
    assert semantic_bonus(0.5) == 0.0


# ---- 编排(FakeSession,无 DB)----------------------------------------------


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> list:
        return list(self._rows)

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """按 select 的主实体分发预置结果(where/order 由测试数据自洽保证)。"""

    def __init__(
        self,
        entities: list[CanonicalEntity],
        alias_rows: list[tuple],
        chunk_rows: list[tuple],
    ) -> None:
        self._entities = entities
        self._alias_rows = alias_rows
        self._chunk_rows = chunk_rows

    async def execute(self, stmt) -> _FakeResult:
        desc = stmt.column_descriptions[0]
        model = desc.get("entity") or desc.get("type")
        if model is CanonicalEntity:
            return _FakeResult(self._entities)
        if model is EntityAlias:
            return _FakeResult(self._alias_rows)
        if model is KbChunk:
            return _FakeResult(self._chunk_rows)
        raise AssertionError(f"unexpected statement: {stmt}")


def _entity(org_id, kb_id, entity_type: str, name: str) -> CanonicalEntity:
    return CanonicalEntity(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        entity_type=entity_type,
        canonical_name=name,
    )


async def test_suggest_merge_candidates_full_flow() -> None:
    org_id, kb_id = uuid4(), uuid4()
    zh = _entity(org_id, kb_id, "hotel", "香格里拉大酒店")
    en = _entity(org_id, kb_id, "hotel", "Shangri-La Paris")
    poi = _entity(org_id, kb_id, "poi", "东京塔")
    # zh 别名更丰富(canonical + 英文别名)→ 应作为保留目标 target
    alias_rows = [
        (zh.id, "香格里拉大酒店"),
        (zh.id, "shangri-la paris"),
        (en.id, "shangri-la paris"),
        (poi.id, "东京塔"),
    ]
    chunk_rows = [
        (f"hotel:{zh.id}", [1.0, 0.0]),
        (f"hotel:{en.id}", [1.0, 0.0]),
    ]
    session = _FakeSession([zh, en, poi], alias_rows, chunk_rows)

    suggestions = await suggest_merge_candidates(
        session, org_id=org_id, kb_id=kb_id
    )

    assert len(suggestions) == 1
    [suggestion] = suggestions
    assert suggestion.source.id == en.id
    assert suggestion.target.id == zh.id
    # 强信号 0.95 + 语义加分 0.08 → 封顶 0.99
    assert suggestion.confidence == MAX_CONFIDENCE
    assert any(r.startswith("shared_alias:") for r in suggestion.reasons)
    assert any(r.startswith("semantic_similarity:") for r in suggestion.reasons)


async def test_suggest_skips_semantic_when_card_vector_missing() -> None:
    org_id, kb_id = uuid4(), uuid4()
    zh = _entity(org_id, kb_id, "hotel", "香格里拉大酒店")
    en = _entity(org_id, kb_id, "hotel", "Shangri-La Paris")
    alias_rows = [
        (zh.id, "香格里拉大酒店"),
        (zh.id, "shangri-la paris"),
        (en.id, "shangri-la paris"),
    ]
    # 仅一方有向量卡片 → 语义信号跳过且不报错
    session = _FakeSession([zh, en], alias_rows, [(f"hotel:{zh.id}", [1.0, 0.0])])

    [suggestion] = await suggest_merge_candidates(
        session, org_id=org_id, kb_id=kb_id
    )

    assert suggestion.confidence == 0.95
    assert not any(
        r.startswith("semantic_similarity:") for r in suggestion.reasons
    )


async def test_suggest_never_pairs_across_entity_types() -> None:
    org_id, kb_id = uuid4(), uuid4()
    hotel = _entity(org_id, kb_id, "hotel", "东京塔酒店")
    poi = _entity(org_id, kb_id, "poi", "东京塔")
    alias_rows = [(hotel.id, "东京塔酒店"), (poi.id, "东京塔")]
    session = _FakeSession([hotel, poi], alias_rows, [])

    assert await suggest_merge_candidates(session, org_id=org_id, kb_id=kb_id) == []


async def test_suggest_orders_by_confidence_and_honors_limit() -> None:
    org_id, kb_id = uuid4(), uuid4()
    a1 = _entity(org_id, kb_id, "hotel", "Hilton Bangkok")
    a2 = _entity(org_id, kb_id, "hotel", "hilton bangkok")  # 强:标准名一致
    b1 = _entity(org_id, kb_id, "hotel", "Mandarin Oriental Hotel")
    b2 = _entity(org_id, kb_id, "hotel", "Mandarin Oriental")  # 中:子串/词元
    alias_rows = [
        (a1.id, "hilton bangkok"),
        (a2.id, "hilton bangkok"),
        (b1.id, "mandarin oriental hotel"),
        (b2.id, "mandarin oriental"),
    ]
    session = _FakeSession([a1, a2, b1, b2], alias_rows, [])

    suggestions = await suggest_merge_candidates(session, org_id=org_id, kb_id=kb_id)
    assert len(suggestions) == 2
    assert suggestions[0].confidence > suggestions[1].confidence
    assert {suggestions[0].source.id, suggestions[0].target.id} == {a1.id, a2.id}

    limited = await suggest_merge_candidates(
        session, org_id=org_id, kb_id=kb_id, limit=1
    )
    assert len(limited) == 1
    assert limited[0].confidence == suggestions[0].confidence


async def test_suggest_returns_empty_for_single_entity() -> None:
    org_id, kb_id = uuid4(), uuid4()
    only = _entity(org_id, kb_id, "hotel", "唯一酒店")
    session = _FakeSession([only], [], [])

    assert await suggest_merge_candidates(session, org_id=org_id, kb_id=kb_id) == []
