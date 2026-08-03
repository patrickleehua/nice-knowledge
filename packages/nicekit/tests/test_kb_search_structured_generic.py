"""结构化召回泛化重写的契约锁定(MIGRATION-PLAN §5.5 B9-B16、§8 风险 1)。

被测口径:
1. `StructuredFilter` 只认 `eq/min/max` 三个算子,字段必须在目标
   `KbEntityType.filterable_fields` 里声明,未声明一律拒绝(防 JSONB 探测);
2. `_structured_candidates` 是**单一泛化路径**:按 `type_key` 分组查 `KbEntity`,
   声明式字段进 SQL 硬过滤,词面打分落在 `name` 与类型声明的 text 属性上;
3. 类型过滤 / 数值 / 文本 / 日期 / 多字段组合 / 空结果 / RRF 融合逐项覆盖;
4. 语料是非旅游的 `product` / `policy` 两个类型(SDK 不内置任何领域词表)。

全部用例离线:SQL 语句捕获在假 session 里断言编译结果,不起数据库。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from nicekit.kb import search as search_module
from nicekit.kb.search import (
    PAGE_KIND,
    STRUCTURED_FILTER_OPS,
    SearchHit,
    StructuredFilter,
    StructuredFilterError,
    StructuredSearchQuery,
    _Candidate,
    _declared_text_fields,
    _structured_candidates,
    reciprocal_rank_fusion,
    search_kb,
    structured_search_manifest,
)
from nicekit.models.kb import KbEntity, KbEntityType

PRODUCT_TYPE = KbEntityType(
    id=uuid4(),
    org_id=None,
    type_key="product",
    display_name="产品",
    is_builtin=True,
    field_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "warehouse": {"type": "string"},
            "tier": {"type": "string"},
            "unit_price": {"type": ["number", "null"]},
            "listed_on": {"type": ["string", "null"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    filterable_fields=[
        {"field": "warehouse", "type": "text", "label": "仓库"},
        {"field": "tier", "type": "text", "label": "档位"},
        {"field": "unit_price", "type": "number", "label": "单价"},
        {"field": "listed_on", "type": "date", "label": "上架日"},
    ],
    card_template="产品:{name}({warehouse})",
)

POLICY_TYPE = KbEntityType(
    id=uuid4(),
    org_id=None,
    type_key="policy",
    display_name="制度",
    is_builtin=True,
    field_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "scope": {"type": ["string", "null"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    filterable_fields=[{"field": "scope", "type": "text", "label": "适用范围"}],
    card_template=None,
)

_TYPES = {"product": PRODUCT_TYPE, "policy": POLICY_TYPE}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: [row[0] for row in self._rows])


class _CapturingSession:
    """记录每条被执行的语句并按顺序回放预置行。"""

    def __init__(self, rows_by_call=()):
        self.statements = []
        self._rows = list(rows_by_call)

    async def execute(self, statement):
        self.statements.append(statement)
        rows = self._rows.pop(0) if self._rows else []
        return _Result(rows)

    def sql(self, index: int) -> str:
        compiled = self.statements[index].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
        return " ".join(str(compiled).split())


def _entity(type_key: str, name: str, attributes: dict, **overrides) -> KbEntity:
    row = KbEntity(
        id=overrides.pop("id", uuid4()),
        org_id=overrides.pop("org_id", uuid4()),
        kb_id=overrides.pop("kb_id", uuid4()),
        entity_type_key=type_key,
        name=name,
        attributes=attributes,
        source_doc_id=overrides.pop("source_doc_id", None),
    )
    row.snapshot_id = overrides.pop("snapshot_id", uuid4())
    return row


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """把实体类型注册表替换为两个非旅游类型(不起库)。"""

    async def load(_session, _org_id):
        return dict(_TYPES)

    async def get_type(_session, _org_id, type_key):
        return _TYPES.get(type_key)

    async def no_expiry(_session, _doc_ids):
        return set()

    monkeypatch.setattr(search_module, "load_custom_entity_types", load)
    monkeypatch.setattr(search_module, "get_entity_type", get_type)
    monkeypatch.setattr(search_module, "_expired_doc_ids", no_expiry)


# ---------------------------------------------------------------------------
# StructuredFilter 契约
# ---------------------------------------------------------------------------


def test_filter_ops_are_closed_set() -> None:
    assert STRUCTURED_FILTER_OPS == ("eq", "min", "max")
    with pytest.raises(StructuredFilterError):
        StructuredFilter(field="unit_price", op="like", value="x")
    with pytest.raises(StructuredFilterError):
        StructuredFilter(field="  ", value="x")


def test_filter_lookup_key_maps_to_entity_lookup_shape() -> None:
    assert StructuredFilter("warehouse").lookup_key == "warehouse"
    assert StructuredFilter("unit_price", "min", 10).lookup_key == "unit_price__min"
    assert StructuredFilter("unit_price", "max", 10).lookup_key == "unit_price__max"


def test_undeclared_field_is_rejected_for_explicit_types() -> None:
    query = StructuredSearchQuery(
        type_keys=("product",),
        filters=(StructuredFilter("secret_margin", value=1),),
    )
    with pytest.raises(StructuredFilterError, match="secret_margin"):
        query.filters_for(PRODUCT_TYPE)


def test_undeclared_field_only_skips_the_type_when_no_type_is_pinned() -> None:
    query = StructuredSearchQuery(filters=(StructuredFilter("warehouse", value="东仓"),))
    assert query.filters_for(PRODUCT_TYPE) is not None
    # policy 没声明 warehouse → 该类型本轮不参与召回,而不是整体报错
    assert query.filters_for(POLICY_TYPE) is None


def test_text_field_rejects_range_operator() -> None:
    query = StructuredSearchQuery(
        type_keys=("product",),
        filters=(StructuredFilter("warehouse", "min", "东仓"),),
    )
    with pytest.raises(StructuredFilterError, match="不支持范围过滤"):
        query.filters_for(PRODUCT_TYPE)


def test_declared_text_fields_drive_scoring_and_exclude_number_and_date() -> None:
    assert _declared_text_fields(PRODUCT_TYPE) == ("warehouse", "tier")
    assert _declared_text_fields(POLICY_TYPE) == ("scope",)


def test_structured_manifest_is_domain_free() -> None:
    manifest = structured_search_manifest()
    assert manifest["schema_version"] == "generic-entity-filters-v1"
    assert manifest["entity_source"] == "kb_entities"
    assert manifest["grouping"] == "by_type_key"
    assert manifest["filter_ops"] == list(STRUCTURED_FILTER_OPS)
    assert manifest["filter_field_types"] == ["text", "number", "date"]
    assert manifest["page_channel"]["kind"] == PAGE_KIND
    serialized = repr(manifest)
    for word in ("hotel", "cost", "poi", "route_template", "destination"):
        assert word not in serialized


# ---------------------------------------------------------------------------
# 泛化召回:分组 / 过滤 / 打分
# ---------------------------------------------------------------------------


async def test_unpinned_query_groups_one_statement_per_registered_type() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session, uuid4(), "主机", top_k=5, kb_ids=None, filters=None
    )
    # policy + product 各一条,外加 wiki 页面通道
    assert len(session.statements) == 3
    assert "kb_entities.entity_type_key = 'policy'" in session.sql(0)
    assert "kb_entities.entity_type_key = 'product'" in session.sql(1)
    assert "FROM kb_pages" in session.sql(2)


async def test_type_filter_limits_recall_to_the_named_types() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(type_keys=("product",)),
    )
    assert len(session.statements) == 2  # product + wiki 页面
    assert "kb_entities.entity_type_key = 'product'" in session.sql(0)
    assert "policy" not in session.sql(0)


async def test_unknown_type_key_is_rejected() -> None:
    session = _CapturingSession()
    with pytest.raises(StructuredFilterError, match="未注册的实体类型"):
        await _structured_candidates(
            session,
            uuid4(),
            "主机",
            top_k=5,
            kb_ids=None,
            filters=StructuredSearchQuery(type_keys=("ghost",)),
        )


async def test_text_filter_compiles_to_jsonb_equality() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(
            type_keys=("product",),
            filters=(StructuredFilter("warehouse", value="东仓"),),
        ),
    )
    sql = session.sql(0)
    assert "(kb_entities.attributes ->> 'warehouse') = '东仓'" in sql


async def test_numeric_range_filter_casts_to_numeric() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(
            type_keys=("product",),
            filters=(
                StructuredFilter("unit_price", "min", 100),
                StructuredFilter("unit_price", "max", 900),
            ),
        ),
    )
    sql = session.sql(0)
    assert "CAST((kb_entities.attributes ->> 'unit_price') AS NUMERIC) >= 100.0" in sql
    assert "CAST((kb_entities.attributes ->> 'unit_price') AS NUMERIC) <= 900.0" in sql


async def test_date_range_filter_stays_textual_iso_comparison() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(
            type_keys=("product",),
            filters=(StructuredFilter("listed_on", "min", "2026-01-01"),),
        ),
    )
    sql = session.sql(0)
    assert "(kb_entities.attributes ->> 'listed_on') >= '2026-01-01'" in sql


async def test_multi_field_filters_are_conjunctive() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(
            type_keys=("product",),
            filters=(
                StructuredFilter("warehouse", value="东仓"),
                StructuredFilter("tier", value="pro"),
                StructuredFilter("unit_price", "min", 100),
            ),
        ),
    )
    sql = session.sql(0)
    assert "(kb_entities.attributes ->> 'warehouse') = '东仓'" in sql
    assert "(kb_entities.attributes ->> 'tier') = 'pro'" in sql
    assert "CAST((kb_entities.attributes ->> 'unit_price') AS NUMERIC) >= 100.0" in sql
    # 三条声明式条件串成 AND(彼此之间没有 OR 松绑)
    assert (
        "(kb_entities.attributes ->> 'warehouse') = '东仓' "
        "AND (kb_entities.attributes ->> 'tier') = 'pro' "
        "AND CAST((kb_entities.attributes ->> 'unit_price') AS NUMERIC) >= 100.0"
    ) in sql


async def test_scoring_covers_name_and_declared_text_attributes_only() -> None:
    session = _CapturingSession()
    await _structured_candidates(
        session,
        uuid4(),
        "主机",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(type_keys=("product",)),
    )
    sql = session.sql(0)
    assert "lower(kb_entities.name) = '主机'" in sql
    assert "kb_entities.attributes ->> 'warehouse'" in sql
    assert "kb_entities.attributes ->> 'tier'" in sql
    # number/date 字段只做过滤,不参与模糊词面打分
    assert "lower(kb_entities.attributes ->> 'unit_price')" not in sql


async def test_kb_scope_is_pushed_into_every_group() -> None:
    kb_id = uuid4()
    session = _CapturingSession()
    await _structured_candidates(
        session, uuid4(), "主机", top_k=5, kb_ids=[kb_id], filters=None
    )
    for index in range(len(session.statements)):
        assert "kb_id IN" in session.sql(index)


async def test_rows_become_candidates_with_attributes_flattened() -> None:
    org_id = uuid4()
    product = _entity(
        "product",
        "A 型主机",
        {"name": "A 型主机", "warehouse": "东仓", "unit_price": 1200},
        org_id=org_id,
    )
    policy = _entity("policy", "退换货制度", {"name": "退换货制度"}, org_id=org_id)
    session = _CapturingSession(
        rows_by_call=[[(policy, 0.6)], [(product, 0.9)], []]
    )

    candidates = await _structured_candidates(
        session, org_id, "主机", top_k=5, kb_ids=None, filters=None
    )

    assert [candidate.key[0] for candidate in candidates] == ["product", "policy"]
    top = candidates[0]
    assert top.hit.kind == "product"
    assert top.hit.source == f"kb_entities/{product.id}"
    assert top.hit.data["entity_type_key"] == "product"
    assert top.hit.data["warehouse"] == "东仓"  # attributes 直出
    assert top.hit.data["unit_price"] == 1200
    assert top.native_scores == {"structured": 0.9}


async def test_must_include_marks_the_hit_and_enters_scoring() -> None:
    org_id = uuid4()
    product = _entity("product", "A 型主机", {"name": "A 型主机"}, org_id=org_id)
    session = _CapturingSession(rows_by_call=[[(product, 1.0)], []])

    candidates = await _structured_candidates(
        session,
        org_id,
        "主机怎么选",
        top_k=5,
        kb_ids=None,
        filters=StructuredSearchQuery(
            type_keys=("product",), must_include=("A 型主机",)
        ),
    )

    assert candidates[0].hit.data["must_include_hit"] is True
    assert "0.2" in session.sql(0)  # must_include 加成进了打分表达式


async def test_empty_registry_yields_only_the_page_channel(monkeypatch) -> None:
    async def empty(_session, _org_id):
        return {}

    monkeypatch.setattr(search_module, "load_custom_entity_types", empty)
    session = _CapturingSession()
    candidates = await _structured_candidates(
        session, uuid4(), "主机", top_k=5, kb_ids=None, filters=None
    )
    assert candidates == []
    assert len(session.statements) == 1
    assert "FROM kb_pages" in session.sql(0)


# ---------------------------------------------------------------------------
# RRF 融合与空结果
# ---------------------------------------------------------------------------


def test_rrf_fuses_structured_and_sparse_rankings_by_rank_not_score() -> None:
    a, b = ("product", uuid4()), ("policy", uuid4())
    scores = reciprocal_rank_fusion([[a, b], [b, a]])
    # 两路各排一次第一、一次第二 → 完全并列
    assert scores[a] == pytest.approx(scores[b])
    assert scores[a] == pytest.approx(1 / 61 + 1 / 62)


async def test_structured_hit_and_card_hit_collapse_onto_one_rrf_key(
    monkeypatch,
) -> None:
    org_id, kb_id, entity_id = uuid4(), uuid4(), uuid4()
    candidate = _Candidate(
        key=("product", entity_id),
        hit=SearchHit(
            kind="product",
            layer="tenant",
            kb_id=str(kb_id),
            source=f"kb_entities/{entity_id}",
            confidence=0.0,
            data={"id": str(entity_id), "name": "A 型主机"},
        ),
        native_scores={"structured": 0.85},
    )

    async def structured(*_args, **_kwargs):
        return [candidate]

    async def no_sparse(*_args, **_kwargs):
        return []

    async def citations(_session, hits):
        return hits

    monkeypatch.setattr(search_module, "_structured_candidates", structured)
    monkeypatch.setattr(search_module, "_sparse_chunk_hits", no_sparse)
    # 图谱一路默认开启,而这里传的是假 session:置空以聚焦结构化候选的融合
    monkeypatch.setattr(search_module, "graph_recall_candidates", no_sparse)
    monkeypatch.setattr(search_module, "_attach_citations", citations)

    [hit] = await search_kb(
        object(),  # type: ignore[arg-type]
        org_id,
        "A 型主机",
        embedder=None,
        reranker=None,
    )
    assert hit.kind == "product"
    assert hit.data["scores"]["native"] == {"structured": 0.85}
    assert hit.data["scores"]["rrf"] == pytest.approx(1 / 61)


async def test_all_channels_empty_returns_empty_without_fabricating(
    monkeypatch,
) -> None:
    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(search_module, "_structured_candidates", nothing)
    monkeypatch.setattr(search_module, "_sparse_chunk_hits", nothing)
    monkeypatch.setattr(search_module, "graph_recall_candidates", nothing)

    async def boom(*_args, **_kwargs):
        raise AssertionError("空结果不应触发引用装配")

    monkeypatch.setattr(search_module, "_attach_citations", boom)

    assert (
        await search_kb(
            object(),  # type: ignore[arg-type]
            uuid4(),
            "查无此物",
            embedder=None,
            reranker=None,
        )
        == []
    )
