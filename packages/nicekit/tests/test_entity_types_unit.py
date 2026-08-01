"""实体类型注册表单测:schema 校验 / 过滤字段声明 / 属性强校验 / 卡片渲染 / 默认 seed。

由 TF ``tests/test_entity_types_unit.py`` 搬运适配:原用例的 ``fleet_price``
(车队价格)语料换成领域中性的 ``product``,断言口径不变;内置九类行业定义的
自检改为 SDK 默认 seed(只有 ``concept`` 兜底)的自检。
"""

import pytest

from nicekit.kb.entity_types import (
    DEFAULT_ENTITY_TYPE_SPECS,
    FALLBACK_TYPE_KEY,
    EntityTypeInvalid,
    EntityValidationError,
    render_entity_card,
    validate_entity_attributes,
    validate_field_schema,
    validate_filterable_fields,
)
from nicekit.models.kb import KbEntityType

PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "warehouse": {"type": "string"},
        "sku": {"type": "string"},
        "quantity": {"type": "integer", "minimum": 1},
        "tier": {"type": "string", "enum": ["basic", "plus", "pro"]},
        "unit_price": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
    },
    "required": ["name", "sku", "quantity", "tier"],
    "additionalProperties": False,
}


def _product_type(**overrides) -> KbEntityType:
    defaults = dict(
        org_id=None,
        type_key="product",
        display_name="产品",
        field_schema=PRODUCT_SCHEMA,
        filterable_fields=[
            {"field": "warehouse", "type": "text", "label": "仓库"},
            {"field": "tier", "type": "text", "label": "档位"},
            {"field": "unit_price", "type": "number", "label": "单价"},
        ],
        card_template="产品:{name}({warehouse}),{sku} {quantity} 件,"
        "{tier} {unit_price} {currency}/件。",
    )
    defaults.update(overrides)
    return KbEntityType(**defaults)


# ---- field_schema 校验 ----


def test_valid_schema_passes() -> None:
    validate_field_schema(PRODUCT_SCHEMA)


def test_schema_must_be_object() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_field_schema({"type": "array"})


def test_schema_requires_non_empty_properties() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_field_schema({"type": "object", "properties": {}})


def test_schema_requires_string_name_property() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_field_schema(
            {
                "type": "object",
                "properties": {"name": {"type": "integer"}},
                "required": ["name"],
            }
        )


def test_schema_requires_name_in_required() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_field_schema(
            {"type": "object", "properties": {"name": {"type": "string"}}}
        )


def test_schema_must_be_valid_json_schema() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_field_schema(
            {
                "type": "object",
                "properties": {"name": {"type": "string"}, "x": {"type": "nope"}},
                "required": ["name"],
            }
        )


# ---- filterable_fields 校验 ----


def test_filterable_fields_accepts_declared_text_field() -> None:
    validate_filterable_fields([{"field": "warehouse", "type": "text"}], PRODUCT_SCHEMA)


def test_filterable_field_must_exist_in_schema() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_filterable_fields([{"field": "ghost", "type": "text"}], PRODUCT_SCHEMA)


def test_filterable_field_type_is_restricted() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_filterable_fields(
            [{"field": "warehouse", "type": "uuid"}], PRODUCT_SCHEMA
        )


def test_filterable_field_cannot_repeat() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_filterable_fields(
            [
                {"field": "warehouse", "type": "text"},
                {"field": "warehouse", "type": "text"},
            ],
            PRODUCT_SCHEMA,
        )


def test_filterable_field_requires_field_key() -> None:
    with pytest.raises(EntityTypeInvalid):
        validate_filterable_fields([{"type": "text"}], PRODUCT_SCHEMA)


# ---- 属性强校验 ----


def test_valid_attributes_return_stripped_name() -> None:
    name = validate_entity_attributes(
        _product_type(),
        {
            "name": " 精选礼盒 ",
            "warehouse": "华东仓",
            "sku": "SKU-100",
            "quantity": 19,
            "tier": "pro",
            "unit_price": 1800,
            "currency": "CNY",
        },
    )
    assert name == "精选礼盒"


def test_attribute_constraint_violation_is_rejected() -> None:
    entity_type = _product_type()
    with pytest.raises(EntityValidationError):
        validate_entity_attributes(
            entity_type,
            {"name": "x", "sku": "SKU-1", "quantity": 0, "tier": "pro"},
        )


def test_unknown_attribute_is_rejected() -> None:
    entity_type = _product_type()
    with pytest.raises(EntityValidationError):
        validate_entity_attributes(
            entity_type,
            {
                "name": "x",
                "sku": "SKU-1",
                "quantity": 10,
                "tier": "pro",
                "ghost": 1,
            },
        )


def test_blank_name_is_rejected() -> None:
    with pytest.raises(EntityValidationError):
        validate_entity_attributes(
            _product_type(
                field_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                }
            ),
            {"name": "   "},
        )


# ---- 卡片渲染 ----


def test_card_template_renders_and_missing_fields_become_blank() -> None:
    text = render_entity_card(
        _product_type(),
        {
            "name": "精选礼盒",
            "warehouse": "华东仓",
            "sku": "SKU-100",
            "quantity": 19,
            "tier": "pro",
            "unit_price": None,
            "currency": None,
        },
    )
    assert "产品:精选礼盒(华东仓)" in text
    assert "SKU-100 19 件" in text


def test_card_fallback_without_template_and_list_join() -> None:
    entity_type = _product_type(card_template=None)
    text = render_entity_card(
        entity_type, {"name": "礼盒A", "quantity": 19, "tags": ["a", "b"]}
    )
    assert text.startswith("产品:礼盒A")
    assert "quantity:19" in text
    assert "a、b" in text  # 列表值顿号拼接


# ---- 默认 seed 自检(SDK 不内置任何领域类型) ----


def test_default_specs_only_seed_the_domain_free_fallback() -> None:
    keys = [spec["type_key"] for spec in DEFAULT_ENTITY_TYPE_SPECS]
    assert keys == [FALLBACK_TYPE_KEY]


def test_default_specs_are_self_consistent() -> None:
    for spec in DEFAULT_ENTITY_TYPE_SPECS:
        validate_field_schema(spec["field_schema"])
        validate_filterable_fields(spec["filterable_fields"], spec["field_schema"])
