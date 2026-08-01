"""P3c 波次的 KB 契约回归(路由面 + 跨波次接线 A6/A7/A8/B20/B21/B29)。

全部离线:只读 router 元数据与纯函数,不碰 DB/MinIO/LLM。
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from nicekit.api.v1 import kb as kb_api
from nicekit.kb import projections, wiki_gen
from nicekit.kb.search import StructuredFilter, StructuredSearchQuery
from nicekit.kb.snapshot import projection_builders


def _paths() -> set[str]:
    return {route.path for route in kb_api.router.routes}


# ---- B29:五类行业专属实体 CRUD 已删除,只留通用 wiki 页 ----------------------


@pytest.mark.parametrize(
    "removed", ["destinations", "pois", "hotels", "costs", "routes"]
)
def test_industry_entity_crud_endpoints_are_gone(removed: str) -> None:
    assert not any(path.startswith(f"/kb/{removed}") for path in _paths())


def test_only_generic_page_projection_crud_remains() -> None:
    assert set(kb_api._ENTITIES) == {"pages"}
    assert {"/kb/pages", "/kb/pages/{item_id}", "/kb/pages/{item_id}/move"} <= _paths()


def test_entity_card_rebuild_endpoint_is_gone() -> None:
    """B8:entity_cards 整文件删除,全库重建端点随之下线。"""
    assert not any("entity-cards" in path for path in _paths())


def test_route_diagnostics_endpoint_is_gone() -> None:
    assert not any("route-diagnostics" in path for path in _paths())


# ---- A8:lifecycle 契约 -------------------------------------------------------


def test_lifecycle_blocker_codes_follow_external_scope_contract() -> None:
    codes = {member.value for member in kb_api.KnowledgeBaseLifecycleBlockerCode}
    assert "EXTERNAL_SCOPE_REFERENCE" in codes
    assert "CURRENT_PROJECT_REFERENCE" not in codes
    assert "HISTORICAL_RETRIEVAL_REFERENCE" not in codes


def test_archive_body_uses_external_unlink_acknowledgement() -> None:
    fields = kb_api.KbArchiveBody.model_fields
    assert "acknowledge_external_unlink" in fields
    assert "confirm_project_unlinks" not in fields


def test_deletion_preview_out_drops_business_reference_list() -> None:
    fields = kb_api.KnowledgeBaseDeletionPreviewOut.model_fields
    assert "project_references" not in fields
    assert "external_reference_count" not in fields  # 计数走 impact_counts
    detail_fields = kb_api.KnowledgeBaseLifecycleAuditDetail.model_fields
    assert "unlinked_external_count" in detail_fields
    assert "unlinked_project_count" not in detail_fields


# ---- B9-B14:search/answer 的声明式结构化过滤契约 -----------------------------


def test_structured_query_compiles_to_service_contract() -> None:
    body = kb_api.SearchBody.model_validate(
        {
            "query": "结算口径",
            "structured": {
                "type_keys": ["equipment"],
                "filters": [
                    {"field": "grade", "op": "eq", "value": "A"},
                    {"field": "unit_price", "op": "min", "value": 100},
                ],
                "match_terms": ["台账"],
                "must_include": ["编号"],
            },
        }
    )
    query = body.structured.to_query()

    assert isinstance(query, StructuredSearchQuery)
    assert query.type_keys == ("equipment",)
    assert query.filters == (
        StructuredFilter(field="grade", op="eq", value="A"),
        StructuredFilter(field="unit_price", op="min", value=100),
    )
    assert query.match_terms == ("台账",)
    assert query.must_include == ("编号",)


def test_empty_structured_block_means_no_structured_filter() -> None:
    body = kb_api.SearchBody.model_validate({"query": "x", "structured": {}})
    assert body.structured.to_query() is None
    assert kb_api.SearchBody.model_validate({"query": "x"}).structured is None


def test_illegal_operator_is_rejected_as_422() -> None:
    body = kb_api.StructuredQueryIn.model_validate(
        {"filters": [{"field": "grade", "op": "eq", "value": "A"}]}
    )
    object.__setattr__(body.filters[0], "op", "like")  # 绕过入参校验模拟脏数据
    with pytest.raises(HTTPException) as excinfo:
        body.to_query()
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "invalid_structured_filter"


def test_industry_search_parameters_are_gone() -> None:
    """TF 的 hotel_star / route_days 等行业过滤参数不得残留在请求契约里。"""
    for model in (kb_api.SearchBody, kb_api.KnowledgeAnswerBody, kb_api.StructuredQueryIn):
        assert not {
            name for name in model.model_fields
            if any(token in name for token in ("hotel", "star", "route", "quote"))
        }


# ---- A6:wiki 常量归位 --------------------------------------------------------


def test_wiki_constants_have_a_single_source_of_truth() -> None:
    from nicekit.kb import wiki_review

    assert projections.PAGE_ORIGIN_LLM is wiki_gen.PAGE_ORIGIN_LLM
    assert projections.PAGE_DRAFT_PENDING is wiki_gen.PAGE_DRAFT_PENDING
    assert (
        projections.WIKI_PUBLICATION_STATUS_KEY is wiki_gen.WIKI_PUBLICATION_STATUS_KEY
    )
    assert wiki_review.PAGE_DRAFT_PENDING is wiki_gen.PAGE_DRAFT_PENDING
    assert wiki_gen.PAGE_ORIGIN_LLM == "llm"
    assert wiki_gen.PAGE_DRAFT_PENDING == "pending_review"
    assert wiki_gen.WIKI_PUBLICATION_STATUS_KEY == "_wiki_publication_status"


# ---- B20:page_type 开放集合 --------------------------------------------------


def test_page_types_are_open_by_default_and_registrable() -> None:
    assert wiki_gen.valid_page_types() is None
    assert wiki_gen.normalize_page_type("anything_goes") == "anything_goes"
    assert wiki_gen.normalize_page_type("  ") == wiki_gen.DEFAULT_PAGE_TYPE
    try:
        wiki_gen.set_valid_page_types(["concept", " topic "])
        assert wiki_gen.valid_page_types() == frozenset({"concept", "topic"})
        assert wiki_gen.normalize_page_type("concept") == "concept"
        assert wiki_gen.normalize_page_type("anything_goes") is None
        wiki_gen.set_valid_page_types([])  # 空白名单 = 回到开放
        assert wiki_gen.valid_page_types() is None
    finally:
        wiki_gen.set_valid_page_types(None)


# ---- A7:媒体投影成为发布门禁 ------------------------------------------------


def test_media_projection_is_a_required_snapshot_builder() -> None:
    required = {item["name"] for item in projection_builders.required_manifest()}
    assert required == {"graph", "retrieval", "snapshot_media", "sql", "wiki"}


def test_snapshot_loads_media_module_and_review_settlement_sees_image_stage() -> None:
    from nicekit.kb import image_ingestion, snapshot, snapshot_media

    assert snapshot._snapshot_media() is snapshot_media
    assert callable(image_ingestion.revision_image_stage)


# ---- B21:lint 实体白名单只查通用实体表 --------------------------------------


async def test_lint_entity_whitelist_reads_only_kb_entity() -> None:
    from nicekit.kb.lint import run_structural_lint
    from nicekit.models.kb import KbEntity, KbPage

    queried: list[object] = []

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def scalars(self):
            return iter(self._rows)

    class _Session:
        async def execute(self, stmt):
            entity = stmt.column_descriptions[0].get("entity")
            queried.append(entity)
            if entity is KbPage:
                return _Result([(uuid4(), "台账总览", "见 [[设备甲]] 与 [[缺失页]]。")])
            return _Result(["设备甲"])

    issues, stats = await run_structural_lint(_Session(), uuid4())

    assert queried == [KbPage, KbEntity]
    broken = [issue for issue in issues if issue.type == "broken_link"]
    assert len(broken) == 1 and "缺失页" in broken[0].message
    assert stats["broken_links"] == 1
