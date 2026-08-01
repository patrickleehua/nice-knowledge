"""长期记忆单测(mock LLM 与 DB 会话,无真实外部服务)。

迁移自 TF tests/test_agent_memory.py。SDK 化后的差异:
- MemoryScope 只内置 org,宿主范围经 register_memory_scopes() 注册,
  这里注册一个示例范围 account 顶掉 TF 的 customer 档;
- 会话 → 作用域 ref 走注入的 ScopeResolver,不再直接查业务表,
  因此 FakeSession 的查询序列少了会话行那一次。

覆盖:抽取管道、去重合并、候选提升规则、低置信丢弃、召回排序与预算、
丢弃原因记录、三个工具、防污染(被否定的记忆不再写也不再召回)。

与 test_compression.py 同口径:DB 用 FakeSession 顶掉,断言落在
memory.py 的纯函数与管道分支上——那才是行为的实际决定处。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import nicekit.agent.builtin_tools  # noqa: F401  (触发内置工具注册)
from nicekit.agent import memory as mem
from nicekit.agent.memory import (
    DEDUPE_SIMILARITY,
    MEMORY_SECTION_MAX_CHARS,
    MIN_WRITE_CONFIDENCE,
    PROMOTION_MIN_CONFIDENCE,
    PROMOTION_MIN_SIGHTINGS,
    MemoryCandidate,
    MemoryValidationError,
    build_transcript,
    coverage,
    extract_candidates,
    find_duplicate,
    ingest_candidate,
    initial_memory_type,
    merged_confidence,
    normalize_candidate,
    parse_extraction_output,
    rank_and_select,
    record_recall_hits,
    render_memory_section,
    score_memory,
    screen_candidate,
    should_promote,
    similarity,
)
from nicekit.agent.tools import ToolContext, ToolError, default_registry
from nicekit.models.chat import ChatMessage
from nicekit.models.memory import (
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    register_memory_scopes,
)

ORG_ID = uuid4()
SESSION_ID = uuid4()
# 宿主注册的示例作用域:TF 里这是内置的 customer 档,SDK 由宿主自定
TEST_SCOPE = "account"
SCOPE_REF = "acct-001"
register_memory_scopes(TEST_SCOPE)


@pytest.fixture(autouse=True)
def _scope_resolver():
    """默认解析出示例作用域;个别用例覆盖成"未绑定"。"""

    async def resolver(_session, _org_id, _session_id):
        return {TEST_SCOPE: SCOPE_REF}

    mem.set_scope_resolver(resolver)
    yield
    mem.set_scope_resolver(None)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def _item(
    title: str,
    content: str,
    *,
    memory_type: str = MemoryType.PREFERENCE.value,
    scope: str = TEST_SCOPE,
    scope_ref_id: str | None = SCOPE_REF,
    status: str = MemoryStatus.ACTIVE.value,
    confidence: float = 0.8,
    hit_count: int = 0,
    sightings: int = 1,
    updated_at: datetime | None = None,
) -> MemoryItem:
    now = updated_at or datetime.now(UTC)
    return MemoryItem(
        id=uuid4(),
        org_id=ORG_ID,
        scope=scope,
        scope_ref_id=scope_ref_id,
        memory_type=memory_type,
        title=title,
        content=content,
        source=f"memory_extraction:{SESSION_ID}",
        confidence=confidence,
        status=status,
        hit_count=hit_count,
        sightings=sightings,
        created_at=now,
        updated_at=now,
    )


class FakeResult:
    def __init__(self, items: list):
        self._items = items

    def scalars(self):
        return self

    def all(self) -> list:
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class FakeSession:
    """按查询顺序依次吐结果;未预置时返回空集(select 的形状由被测代码决定)。"""

    def __init__(self, results: list[list] | None = None):
        self._results = list(results or [])
        self.added: list = []
        self.commits = 0
        self.executed: list = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return FakeResult(self._results.pop(0) if self._results else [])

    def add(self, obj) -> None:
        if obj not in self.added:
            self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeLLM:
    def __init__(self, text: str | None = "", error: Exception | None = None):
        self.calls: list[dict] = []
        self._text = text
        self._error = error

    async def generate_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(text=self._text)


def _candidate(**overrides) -> MemoryCandidate:
    values = {
        "memory_type": MemoryType.PREFERENCE.value,
        "scope": TEST_SCOPE,
        "title": "不接受夜间长途大巴",
        "content": "客户说过老人同行,单程超过三小时的夜间大巴一律不接受。",
        "confidence": 0.8,
        "evidence": "用户原话:我爸妈晚上坐大巴超过三小时受不了",
    }
    values.update(overrides)
    return MemoryCandidate(**values)


# ---------------------------------------------------------------------------
# 分词 / 相似度基础件
# ---------------------------------------------------------------------------


def test_coverage_counts_query_terms_present_in_memory() -> None:
    # 长记忆 vs 短提问:Jaccard 会被长度稀释,覆盖率才是召回该用的口径
    assert coverage("夜间大巴", "客户明确说过不接受夜间大巴,要改高铁") == 1.0
    assert coverage("邮轮", "客户明确说过不接受夜间大巴") == 0.0


def test_similarity_is_symmetric_and_bounded() -> None:
    assert similarity("海景房", "海景房") == 1.0
    assert similarity("海景房", "") == 0.0
    assert 0.0 < similarity("不接受夜间大巴", "不接受长途夜间大巴") < 1.0


# ---------------------------------------------------------------------------
# 候选规整与防污染筛查
# ---------------------------------------------------------------------------


def test_normalize_rejects_unknown_type_and_scope() -> None:
    with pytest.raises(MemoryValidationError):
        normalize_candidate({"memory_type": "gossip", "title": "a", "content": "b"})
    with pytest.raises(MemoryValidationError):
        normalize_candidate(
            {"memory_type": "fact", "scope": "galaxy", "title": "a", "content": "b"}
        )


def test_normalize_requires_title_and_content_and_bounded_confidence() -> None:
    with pytest.raises(MemoryValidationError):
        normalize_candidate({"memory_type": "fact", "title": "", "content": "b"})
    with pytest.raises(MemoryValidationError):
        normalize_candidate(
            {"memory_type": "fact", "title": "a", "content": "b", "confidence": 1.5}
        )


def test_screen_drops_low_confidence() -> None:
    reason = screen_candidate(_candidate(confidence=MIN_WRITE_CONFIDENCE - 0.01))
    assert reason is not None and "低于阈值" in reason


def test_screen_drops_hedged_speculation() -> None:
    """低置信推测:含"可能/也许"且置信度不够高一律不进长期记忆。"""
    reason = screen_candidate(
        _candidate(title="可能偏好海景房", content="客户可能喜欢海景房", confidence=0.7)
    )
    assert reason is not None and "低置信推测" in reason


def test_screen_keeps_hedge_word_when_confidence_is_high() -> None:
    # "可能"也会出现在正当表述里(如"可能过敏"),置信度足够高时不误杀
    assert (
        screen_candidate(
            _candidate(title="海鲜可能过敏", content="客户自述海鲜可能过敏", confidence=0.9)
        )
        is None
    )


def test_screen_drops_one_off_emotion() -> None:
    reason = screen_candidate(
        _candidate(title="客户很生气", content="客户对这次接机迟到很生气", confidence=0.9)
    )
    assert reason is not None and "一次性情绪" in reason


def test_screen_accepts_clean_candidate() -> None:
    assert screen_candidate(_candidate()) is None


# ---------------------------------------------------------------------------
# 候选期与提升规则
# ---------------------------------------------------------------------------


def test_preference_lands_as_candidate_but_facts_land_directly() -> None:
    """偏好要经候选期,约束/决策/事实/风险直接落本型(理由见 memory.py docstring)。"""
    assert (
        initial_memory_type(_candidate(memory_type=MemoryType.PREFERENCE.value))
        == MemoryType.PREFERENCE_CANDIDATE.value
    )
    for direct in (
        MemoryType.CONSTRAINT.value,
        MemoryType.DECISION.value,
        MemoryType.FACT.value,
        MemoryType.RISK.value,
    ):
        assert initial_memory_type(_candidate(memory_type=direct)) == direct


def test_promotion_requires_both_sightings_and_confidence() -> None:
    kind = MemoryType.PREFERENCE_CANDIDATE.value
    assert should_promote(kind, PROMOTION_MIN_SIGHTINGS, PROMOTION_MIN_CONFIDENCE)
    # 次数够但置信不够:一次强表态 + 一次弱表态不算稳定偏好
    assert not should_promote(kind, PROMOTION_MIN_SIGHTINGS, PROMOTION_MIN_CONFIDENCE - 0.01)
    # 置信够但只观察到一次:单次表态成不了长期偏好
    assert not should_promote(kind, PROMOTION_MIN_SIGHTINGS - 1, 0.99)
    # 已是正式偏好/其他类型不再走提升
    assert not should_promote(MemoryType.PREFERENCE.value, 5, 0.99)


def test_merged_confidence_takes_max_then_boosts_by_sightings() -> None:
    assert merged_confidence(0.6, 0.8, 1) == pytest.approx(0.8)
    assert merged_confidence(0.6, 0.8, 2) == pytest.approx(0.9)
    assert merged_confidence(0.95, 0.9, 5) == pytest.approx(0.99)  # 封顶


# ---------------------------------------------------------------------------
# 去重与写入管道
# ---------------------------------------------------------------------------


def test_find_duplicate_matches_same_titled_memory() -> None:
    existing = _item("不接受夜间长途大巴", "老人同行,夜间大巴超过三小时不接受")
    assert find_duplicate(_candidate(title="不接受夜间长途大巴"), [existing]) is existing


def test_find_duplicate_ignores_unrelated_memory() -> None:
    unrelated = _item("偏好米其林餐厅", "客户用餐要求较高,优先安排星级餐厅")
    assert find_duplicate(_candidate(), [unrelated]) is None


def test_dedupe_threshold_is_documented_constant() -> None:
    # 阈值改动会直接影响"同一件事会不会被记成两条",锁死在测试里防止误调
    assert 0.4 <= DEDUPE_SIMILARITY <= 0.8


async def test_ingest_creates_new_row_when_no_duplicate() -> None:
    session = FakeSession([[]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(),
        source="memory_write:x",
        scope_ref_id=SCOPE_REF,
    )
    assert outcome.action == "created" and outcome.written
    written = session.added[0]
    # 偏好先落候选:一次表态不足以成为长期偏好
    assert written.memory_type == MemoryType.PREFERENCE_CANDIDATE.value
    assert written.sightings == 1 and written.scope_ref_id == SCOPE_REF


async def test_ingest_merges_duplicate_instead_of_adding_row() -> None:
    existing = _item(
        "不接受夜间长途大巴",
        "老人同行,不接受夜间大巴",
        memory_type=MemoryType.PREFERENCE_CANDIDATE.value,
        confidence=0.6,
    )
    session = FakeSession([[existing]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(title="不接受夜间长途大巴", confidence=0.65),
        source="memory_extraction:y",
        scope_ref_id=SCOPE_REF,
    )
    # 第二次观察但合并后置信 0.75 已达提升线 → 直接提升(合并的强形式)
    assert outcome.action in {"merged", "promoted"}
    assert outcome.item is existing
    assert existing.sightings == 2
    assert existing.confidence > 0.6
    # 关键:没有新增行,同一条偏好不会在召回里占两个名额
    assert session.added == [existing]


async def test_ingest_promotes_candidate_on_second_confident_sighting() -> None:
    existing = _item(
        "偏好高层海景房",
        "客户明确要求高层海景房",
        memory_type=MemoryType.PREFERENCE_CANDIDATE.value,
        confidence=0.7,
    )
    session = FakeSession([[existing]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(
            title="偏好高层海景房", content="客户再次要求高层海景房", confidence=0.7
        ),
        source="memory_extraction:y",
        scope_ref_id=SCOPE_REF,
    )
    assert outcome.action == "promoted"
    assert existing.memory_type == MemoryType.PREFERENCE.value
    assert existing.sightings == PROMOTION_MIN_SIGHTINGS


async def test_ingest_does_not_promote_when_confidence_stays_low() -> None:
    existing = _item(
        "偏好步行游览",
        "客户提过喜欢步行",
        memory_type=MemoryType.PREFERENCE_CANDIDATE.value,
        confidence=0.5,
    )
    session = FakeSession([[existing]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(title="偏好步行游览", content="客户提过喜欢步行", confidence=0.5),
        source="memory_extraction:y",
        scope_ref_id=SCOPE_REF,
    )
    # 合并后 0.5 + 0.1 = 0.6 < 0.7 提升线:仍停在候选
    assert outcome.action == "merged"
    assert existing.memory_type == MemoryType.PREFERENCE_CANDIDATE.value


async def test_ingest_drops_low_confidence_and_writes_nothing() -> None:
    session = FakeSession([[]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(confidence=0.3),
        source="memory_write:x",
        scope_ref_id=SCOPE_REF,
    )
    assert outcome.action == "dropped" and not outcome.written
    assert "低于阈值" in outcome.reason
    assert session.added == []


async def test_ingest_refuses_to_revive_rejected_memory() -> None:
    """防污染:被用户否定过的结论不得借"新一轮抽取"复活。"""
    rejected = _item(
        "不接受夜间长途大巴",
        "老人同行,不接受夜间大巴\n[已失效] 客户澄清是白天大巴也不接受",
        status=MemoryStatus.REJECTED.value,
    )
    session = FakeSession([[rejected]])
    outcome = await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(title="不接受夜间长途大巴"),
        source="memory_extraction:y",
        scope_ref_id=SCOPE_REF,
    )
    assert outcome.action == "dropped"
    assert "已被否定" in outcome.reason
    assert session.added == []


async def test_ingest_clears_scope_ref_for_org_scope() -> None:
    session = FakeSession([[]])
    await ingest_candidate(
        session,
        org_id=ORG_ID,
        candidate=_candidate(
            scope=MemoryScope.ORG.value,
            memory_type=MemoryType.CONSTRAINT.value,
            title="不接待未成年人单独出行",
            content="本社规定未成年人必须有监护人同行。",
        ),
        source="memory_write:x",
        scope_ref_id=SCOPE_REF,
    )
    # 全社规则与是哪个客户无关,按客户各存一份会让同一条规则重复 N 次
    assert session.added[0].scope_ref_id is None


# ---------------------------------------------------------------------------
# 召回:排序、预算、丢弃原因
# ---------------------------------------------------------------------------


def test_score_ranks_topic_match_above_irrelevant_memory() -> None:
    relevant = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    irrelevant = _item("偏好米其林餐厅", "用餐要求较高")
    hit, _ = score_memory(relevant, query_text="夜间大巴怎么安排")
    miss, _ = score_memory(irrelevant, query_text="夜间大巴怎么安排")
    assert hit > miss


def test_score_prefers_constraints_over_unpromoted_candidates() -> None:
    """同样命中话题时,硬约束该排在未提升的候选之前。"""
    constraint = _item(
        "预算上限每人一万",
        "客户明确预算上限每人一万",
        memory_type=MemoryType.CONSTRAINT.value,
    )
    candidate = _item(
        "预算上限每人一万",
        "客户明确预算上限每人一万",
        memory_type=MemoryType.PREFERENCE_CANDIDATE.value,
    )
    high, _ = score_memory(constraint, query_text="预算上限")
    low, _ = score_memory(candidate, query_text="预算上限")
    assert high > low


def test_score_decays_with_age() -> None:
    fresh = _item("夜间大巴", "不接受夜间大巴")
    stale = _item(
        "夜间大巴",
        "不接受夜间大巴",
        updated_at=datetime.now(UTC) - timedelta(days=400),
    )
    fresh_score, _ = score_memory(fresh, query_text="夜间大巴")
    stale_score, _ = score_memory(stale, query_text="夜间大巴")
    assert fresh_score > stale_score


def test_rank_drops_irrelevant_with_reason() -> None:
    relevant = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    irrelevant = _item("偏好米其林餐厅", "用餐要求较高")
    result = rank_and_select([relevant, irrelevant], query_text="夜间大巴怎么安排")
    assert [item.id for item in result.selected] == [relevant.id]
    dropped = {row.memory_id: row.reason for row in result.dropped}
    assert dropped[str(irrelevant.id)] == "与本轮话题无关"


def test_rank_respects_char_budget_and_records_reason() -> None:
    items = [
        _item(f"夜间大巴安排{index}", "老人同行,夜间大巴不接受;" * 6)
        for index in range(5)
    ]
    result = rank_and_select(items, query_text="夜间大巴", budget_chars=300)
    assert 0 < len(result.selected) < len(items)
    assert any(row.reason == "超出字符预算" for row in result.dropped)
    # 选中项渲染后必须真的落在预算内
    assert len(render_memory_section(result.selected, max_chars=10_000)) <= 300 + 200


def test_rank_respects_item_cap() -> None:
    items = [
        _item(f"夜间大巴{index}", "夜间大巴不接受") for index in range(12)
    ]
    result = rank_and_select(
        items, query_text="夜间大巴", budget_chars=100_000, max_items=3
    )
    assert len(result.selected) == 3
    assert any(row.reason == "超出条数上限" for row in result.dropped)


def test_rank_never_selects_rejected_memory() -> None:
    """防污染闭环:被否定的记忆不再进任何一轮 prompt。"""
    rejected = _item(
        "不接受夜间大巴",
        "老人同行,夜间大巴一律不接受",
        status=MemoryStatus.REJECTED.value,
    )
    result = rank_and_select([rejected], query_text="夜间大巴怎么安排")
    assert result.selected == []
    assert result.dropped[0].reason.startswith("状态 rejected")


def test_snapshot_notes_carry_selected_and_dropped() -> None:
    relevant = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    irrelevant = _item("偏好米其林餐厅", "用餐要求较高")
    notes = rank_and_select(
        [relevant, irrelevant], query_text="夜间大巴"
    ).snapshot_notes()
    joined = " ".join(notes)
    assert "memory_recall_selected:1 条" in joined
    assert "memory_recall_dropped" in joined and "偏好米其林餐厅" in joined


# ---------------------------------------------------------------------------
# 命中计数
# ---------------------------------------------------------------------------


async def test_record_recall_hits_preserves_updated_at() -> None:
    """命中计数不得刷新 updated_at,否则新鲜度分会形成滚雪球反馈。"""
    from sqlalchemy.dialects import postgresql

    session = FakeSession()
    await record_recall_hits(session, [uuid4(), uuid4()])
    sql = str(session.executed[0].compile(dialect=postgresql.dialect()))
    assert "hit_count=(memory_items.hit_count + " in sql
    assert "updated_at=memory_items.updated_at" in sql


async def test_record_recall_hits_is_noop_without_ids() -> None:
    session = FakeSession()
    await record_recall_hits(session, [])
    assert session.executed == []


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_section_is_empty_without_items() -> None:
    assert render_memory_section([]) == ""


def test_render_section_labels_type_scope_and_source() -> None:
    text = render_memory_section(
        [_item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")]
    )
    assert "【相关记忆】" in text
    assert "以用户当下说法为准" in text  # 冲突裁决必须写进 prompt
    assert f"[偏好·{TEST_SCOPE}]" in text
    assert "来源 memory_extraction" in text


def test_render_section_respects_hard_cap() -> None:
    items = [_item(f"记忆{index}", "内容" * 100) for index in range(20)]
    assert len(render_memory_section(items)) <= MEMORY_SECTION_MAX_CHARS


# ---------------------------------------------------------------------------
# 抽取管道
# ---------------------------------------------------------------------------


def test_parse_extraction_output_handles_fenced_and_chatty_json() -> None:
    payload = '{"candidates": [{"memory_type": "fact", "title": "a", "content": "b"}]}'
    assert parse_extraction_output(payload)[0]["title"] == "a"
    assert parse_extraction_output(f"好的,结果如下:\n```json\n{payload}\n```")[0]["title"] == "a"
    assert parse_extraction_output("我无法完成") == []
    assert parse_extraction_output("") == []


def test_build_transcript_truncates_tool_rows() -> None:
    rows = [
        ChatMessage(
            id=uuid4(),
            org_id=ORG_ID,
            session_id=SESSION_ID,
            sequence=1,
            role="user",
            content="客户不接受夜间大巴",
        ),
        ChatMessage(
            id=uuid4(),
            org_id=ORG_ID,
            session_id=SESSION_ID,
            sequence=2,
            role="tool",
            tool_name="kb_search",
            content="x" * 5000,
        ),
    ]
    text = build_transcript(rows)
    assert "[user] 客户不接受夜间大巴" in text
    assert "[工具 kb_search]" in text
    # 工具输出是数据备份不是记忆素材,喂全文只会让模型把检索结果当客户偏好
    assert len(text) < 2000


def _extract_session(messages: list[ChatMessage], siblings: list | None = None):
    """extract_candidates 的查询顺序:消息行 → 去重同 scope 行。

    作用域解析已改走 ScopeResolver(不查库),比 TF 少一次会话行查询。
    """
    return FakeSession([messages, siblings or []])


def _messages(count: int = 6) -> list[ChatMessage]:
    return [
        ChatMessage(
            id=uuid4(),
            org_id=ORG_ID,
            session_id=SESSION_ID,
            sequence=index + 1,
            role="user" if index % 2 == 0 else "assistant",
            content=f"第 {index} 轮对话内容,客户谈到出行安排。",
        )
        for index in range(count)
    ]


async def test_extract_skips_when_conversation_too_short() -> None:
    session = _extract_session(_messages(2))
    llm = FakeLLM()
    report = await extract_candidates(session, ORG_ID, SESSION_ID, llm)
    assert report.skipped_reason == "消息过少,无需抽取"
    assert llm.calls == []  # 不烧一次注定没产出的模型调用


async def test_extract_writes_org_scope_candidate_without_project() -> None:
    """会话没绑项目时仍能沉淀 org 范围记忆(不依赖客户标识)。"""
    session = _extract_session(_messages())
    llm = FakeLLM(
        '{"candidates": [{"memory_type": "constraint", "scope": "org",'
        ' "title": "不接待未成年人单独出行", "content": "本社规定未成年人须有监护人同行。",'
        ' "confidence": 0.9, "evidence": "用户原话:我们社不接未成年人单独出行"}]}'
    )
    report = await extract_candidates(session, ORG_ID, SESSION_ID, llm)
    assert report.candidates == 1 and report.created == 1
    assert session.added[0].scope == MemoryScope.ORG.value
    assert session.commits == 1
    # 抽取复用默认会话路由、无工具、system 用 memory_extraction prompt
    # (prompt 资源包随 P2c 交付,未就位时用内置兜底指令 —— 两者都要求原文佐证)
    assert llm.calls[0]["tools"] == []
    assert llm.calls[0]["task"] == mem.MEMORY_TASK
    assert "原文摘录" in llm.calls[0]["system"] or "防污染" in llm.calls[0]["system"]


async def test_extract_drops_candidate_without_evidence() -> None:
    """抽取器必须给对话原文佐证,给不出来的按脑补处理。"""
    session = _extract_session(_messages())
    llm = FakeLLM(
        '{"candidates": [{"memory_type": "constraint", "scope": "org",'
        ' "title": "偏好豪华酒店", "content": "预算高所以偏好豪华酒店。", "confidence": 0.9}]}'
    )
    report = await extract_candidates(session, ORG_ID, SESSION_ID, llm)
    assert report.created == 0
    assert any("缺少对话原文佐证" in row for row in report.dropped)
    assert session.added == []


async def test_extract_drops_candidate_with_invalid_fields() -> None:
    session = _extract_session(_messages())
    llm = FakeLLM('{"candidates": [{"memory_type": "gossip", "title": "a", "content": "b"}]}')
    report = await extract_candidates(session, ORG_ID, SESSION_ID, llm)
    assert report.created == 0 and report.dropped
    assert "字段非法" in report.dropped[0]


async def test_extract_survives_model_failure() -> None:
    """抽取失败绝不上抛:少沉淀一次无碍,炸掉一次对话不可接受。"""
    session = _extract_session(_messages())
    report = await extract_candidates(
        session, ORG_ID, SESSION_ID, FakeLLM(error=RuntimeError("provider down"))
    )
    assert report.skipped_reason is not None and "模型调用失败" in report.skipped_reason
    assert session.added == []


async def test_extract_survives_non_json_output() -> None:
    session = _extract_session(_messages())
    report = await extract_candidates(
        session, ORG_ID, SESSION_ID, FakeLLM("这段对话里没有值得记的东西")
    )
    assert report.candidates == 0 and report.created == 0


async def test_schedule_memory_extraction_swallows_errors(monkeypatch) -> None:
    """后台任务与 schedule_compression 同口径:异常吞掉只记日志。"""
    monkeypatch.setattr(mem, "org_session", lambda factory, org_id: FakeSession())

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mem, "extract_candidates", _boom)
    task = mem.schedule_memory_extraction(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=FakeLLM()
    )
    await task  # 不抛


# ---------------------------------------------------------------------------
# 工具:memory_search / memory_write / memory_forget
# ---------------------------------------------------------------------------


class _FakeChatSession:
    def __init__(self, scope_type=None, scope_id=None):
        self.id = SESSION_ID
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.org_id = ORG_ID


def _ctx(session: FakeSession, **scope) -> ToolContext:
    return ToolContext(
        session=session,
        org_id=ORG_ID,
        user_id=uuid4(),
        role="org_admin",
        chat_session=_FakeChatSession(**scope),
    )


def test_memory_tools_are_registered_with_expected_gating() -> None:
    assert default_registry.require("memory_search").side_effect == "read"
    assert default_registry.require("memory_write").side_effect == "write"
    # 删记忆是高影响:confirm=True 人在环
    assert default_registry.require("memory_forget").side_effect == "write"
    assert default_registry.require("memory_forget").confirm is True
    assert default_registry.require("memory_write").confirm is False


async def test_memory_search_returns_public_view() -> None:
    match = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    session = FakeSession([[match]])
    result = await default_registry.require("memory_search").executor(
        _ctx(session), {"query": "夜间大巴", "scope": None}
    )
    assert result["count"] == 1
    row = result["memories"][0]
    assert row["memory_id"] == str(match.id) and row["title"] == match.title
    assert "embedding" not in row


async def test_memory_search_rejects_empty_query_and_bad_scope() -> None:
    with pytest.raises(ToolError):
        await default_registry.require("memory_search").executor(
            _ctx(FakeSession()), {"query": " ", "scope": None}
        )
    with pytest.raises(ToolError):
        await default_registry.require("memory_search").executor(
            _ctx(FakeSession()), {"query": "x", "scope": "galaxy"}
        )


async def test_memory_write_goes_through_the_same_pipeline() -> None:
    """agent 主动写入同样过防污染 + 去重,没有捷径。"""
    session = FakeSession([[]])  # 只有去重查询,写 org 范围
    result = await default_registry.require("memory_write").executor(
        _ctx(session),
        {
            "type": "constraint",
            "scope": "org",
            "title": "不接待未成年人单独出行",
            "content": "本社规定未成年人须有监护人同行。",
        },
    )
    assert result["ok"] is True and result["action"] == "created"
    assert session.commits == 1


async def test_memory_write_is_blocked_by_rejected_duplicate() -> None:
    rejected = _item(
        "不接待未成年人单独出行",
        "本社规定未成年人须有监护人同行",
        scope=MemoryScope.ORG.value,
        scope_ref_id=None,
        status=MemoryStatus.REJECTED.value,
    )
    session = FakeSession([[rejected]])
    result = await default_registry.require("memory_write").executor(
        _ctx(session),
        {
            "type": "constraint",
            "scope": "org",
            "title": "不接待未成年人单独出行",
            "content": "本社规定未成年人须有监护人同行。",
        },
    )
    # 不抛错:被管道拒绝是正常结果,原因如实回给模型
    assert result["ok"] is False and "已被否定" in result["reason"]


async def test_memory_write_rejects_unbound_host_scope() -> None:
    # ScopeResolver 解析不出该范围的 ref 时,非 org 范围的写入一律拒绝
    async def empty_resolver(_session, _org_id, _session_id):
        return {}

    mem.set_scope_resolver(empty_resolver)
    session = FakeSession([[]])
    with pytest.raises(ToolError, match="没有可归属的"):
        await default_registry.require("memory_write").executor(
            _ctx(session),
            {
                "type": "preference",
                "scope": TEST_SCOPE,
                "title": "偏好靠窗座位",
                "content": "对方明确要求靠窗座位。",
            },
        )


async def test_memory_write_rejects_invalid_type() -> None:
    with pytest.raises(ToolError):
        await default_registry.require("memory_write").executor(
            _ctx(FakeSession()),
            {"type": "gossip", "scope": "org", "title": "a", "content": "b"},
        )


async def test_memory_forget_soft_deletes_with_reason() -> None:
    item = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    session = FakeSession([[item]])
    result = await default_registry.require("memory_forget").executor(
        _ctx(session), {"memory_id": str(item.id), "reason": "客户已澄清可以接受白天大巴"}
    )
    assert result["status"] == MemoryStatus.REJECTED.value
    # 软删:行还在,失效依据写进正文(下一轮抽取据此拦截同一条脏记忆)
    assert item.status == MemoryStatus.REJECTED.value
    assert "[已失效] 客户已澄清" in item.content
    assert session.commits == 1


async def test_memory_forget_requires_reason_and_existing_row() -> None:
    item = _item("a", "b")
    with pytest.raises(ToolError, match="reason"):
        await default_registry.require("memory_forget").executor(
            _ctx(FakeSession([[item]])), {"memory_id": str(item.id), "reason": " "}
        )
    with pytest.raises(ToolError, match="不存在"):
        await default_registry.require("memory_forget").executor(
            _ctx(FakeSession([[]])), {"memory_id": str(uuid4()), "reason": "过时了"}
        )


async def test_memory_forget_is_idempotent_guarded() -> None:
    item = _item("a", "b", status=MemoryStatus.REJECTED.value)
    with pytest.raises(ToolError, match="已经被标记为失效"):
        await default_registry.require("memory_forget").executor(
            _ctx(FakeSession([[item]])), {"memory_id": str(item.id), "reason": "过时了"}
        )


async def test_forgotten_memory_is_never_recalled_again() -> None:
    """端到端的防污染闭环:forget → 之后的召回里不再出现。"""
    item = _item("不接受夜间大巴", "老人同行,夜间大巴一律不接受")
    await default_registry.require("memory_forget").executor(
        _ctx(FakeSession([[item]])), {"memory_id": str(item.id), "reason": "客户已澄清"}
    )
    assert rank_and_select([item], query_text="夜间大巴怎么安排").selected == []
