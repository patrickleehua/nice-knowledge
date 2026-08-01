"""DocType 收敛后的 generic 兜底口径锁定(MIGRATION-PLAN §5.5 B1/B2/B3/B4/B5)。

被测口径:
1. `DocType` 只剩 unclassified/general,`source_documents.doc_type` 是开放字符串;
2. `EXTRACTION_SPECS` 已删除,`_resolve_extraction_spec` **永远**返回
   kb.extract.generic + GenericEntityExtraction + KbEntityType(field_schema 注入);
3. TF 的四个内置 doc_type(hotel/cost/poi/route_template)不再有任何特权分支——
   没注册成实体类型就直接报错,不会退回到"内置契约";
4. 5 张旅游专表不在 SDK 的模型层里。

entity_types 的注册表查询用 monkeypatch 替身,避免起库。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import nicekit.kb.ingestion as ingestion
import nicekit.models.kb as models_kb
from nicekit.domain.kb import GenericEntityExtraction
from nicekit.kb.ingestion import (
    GENERIC_EXTRACTION_TASK,
    _generic_spec_block,
    _resolve_extraction_spec,
)
from nicekit.models.kb import DocType, KbEntityType


def install_entity_types_stub(
    monkeypatch: pytest.MonkeyPatch, *, entity_type: KbEntityType | None
) -> AsyncMock:
    """替换 ingestion 模块级绑定的 get_entity_type(注册表查询,不起库)。"""
    stub = AsyncMock(return_value=entity_type)
    monkeypatch.setattr(ingestion, "get_entity_type", stub)
    return stub

_FIELD_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "amount": {"type": "number"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _entity_type(type_key: str = "component") -> KbEntityType:
    return KbEntityType(
        id=uuid4(),
        org_id=uuid4(),
        type_key=type_key,
        display_name="部件",
        description="装配清单里的一个部件",
        field_schema=_FIELD_SCHEMA,
    )


def _doc(doc_type: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), org_id=uuid4(), kb_id=uuid4(), doc_type=doc_type)


# ---- DocType 收敛 -------------------------------------------------------------


def test_doctype_has_only_unclassified_and_general() -> None:
    assert {member.value for member in DocType} == {"unclassified", "general"}


def test_doc_type_column_is_open_string_without_check_constraint() -> None:
    column = models_kb.SourceDocument.__table__.c.doc_type
    assert column.type.python_type is str
    # doc_type 可存任意已注册实体类型 key,不能有把它锁死在枚举上的 CHECK
    checks = [
        str(constraint.sqltext)
        for constraint in models_kb.SourceDocument.__table__.constraints
        if hasattr(constraint, "sqltext")
    ]
    assert not any("doc_type" in text for text in checks)


def test_extraction_specs_table_is_gone() -> None:
    import nicekit.kb.ingestion as ingestion

    assert not hasattr(ingestion, "EXTRACTION_SPECS")
    assert GENERIC_EXTRACTION_TASK == "kb.extract.generic"


def test_tourism_projection_tables_are_not_in_the_sdk() -> None:
    for removed in ("Destination", "Poi", "HotelPoolEntry", "CostReference", "RouteTemplate"):
        assert not hasattr(models_kb, removed), removed
    tables = set(models_kb.SQLModel.metadata.tables)
    for removed in ("destinations", "pois", "hotel_pools", "cost_references", "route_templates"):
        assert removed not in tables, removed
    # 通用实体存储仍在,行业实体一律走它
    assert "kb_entities" in tables and "kb_entity_types" in tables


# ---- _resolve_extraction_spec 永远走 generic ---------------------------------


async def test_registered_type_resolves_to_generic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_type = _entity_type()
    stub = install_entity_types_stub(monkeypatch, entity_type=entity_type)
    doc = _doc("component")

    task, contract, predicate, resolved = await _resolve_extraction_spec(
        SimpleNamespace(), doc
    )

    assert task == GENERIC_EXTRACTION_TASK
    assert contract is GenericEntityExtraction
    assert predicate == "component"
    assert resolved is entity_type
    stub.assert_awaited_once()
    assert stub.await_args.args[1:] == (doc.org_id, "component")


@pytest.mark.parametrize("legacy", ["hotel", "cost", "poi", "route_template"])
async def test_former_builtin_doc_types_have_no_privileged_contract(
    monkeypatch: pytest.MonkeyPatch,
    legacy: str,
) -> None:
    # TF 内置四类若未注册成实体类型,就和任何陌生 key 一样报错——
    # 不允许悄悄回退到已删除的专用契约
    install_entity_types_stub(monkeypatch, entity_type=None)

    with pytest.raises(ValueError, match="未注册的文档类型"):
        await _resolve_extraction_spec(SimpleNamespace(), _doc(legacy))


async def test_registered_legacy_key_also_goes_through_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 宿主把 hotel 注册成自己的实体类型时,走的仍是 generic 契约
    entity_type = _entity_type("hotel")
    install_entity_types_stub(monkeypatch, entity_type=entity_type)

    task, contract, predicate, resolved = await _resolve_extraction_spec(
        SimpleNamespace(), _doc("hotel")
    )

    assert (task, contract, predicate) == (
        GENERIC_EXTRACTION_TASK,
        GenericEntityExtraction,
        "hotel",
    )
    assert resolved is entity_type


async def test_target_doc_type_overrides_document_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = install_entity_types_stub(monkeypatch, entity_type=_entity_type("supplier"))

    _, _, predicate, _ = await _resolve_extraction_spec(
        SimpleNamespace(), _doc("component"), target_doc_type="supplier"
    )

    assert predicate == "supplier"
    assert stub.await_args.args[2] == "supplier"


async def test_general_doc_type_still_requires_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GENERAL 文档不走结构化抽取(调用方按 doc_type 分流);真调到这里时
    # 也必须有注册记录,不给"内置兜底"
    install_entity_types_stub(monkeypatch, entity_type=None)

    with pytest.raises(ValueError, match="未注册的文档类型"):
        await _resolve_extraction_spec(SimpleNamespace(), _doc(DocType.GENERAL.value))


# ---- field_schema 注入 --------------------------------------------------------


def test_generic_spec_block_injects_type_and_field_schema() -> None:
    block = _generic_spec_block(_entity_type())

    assert block.startswith("ENTITY_TYPE_SPEC:")
    assert "component" in block and "部件" in block
    assert "装配清单里的一个部件" in block
    assert '"additionalProperties": false' in block
    assert '"required": ["name"]' in block
