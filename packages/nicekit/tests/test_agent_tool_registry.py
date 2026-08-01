"""ToolRegistry 新语义专测(替代 TF 的模块级 TOOLS + 三方相等断言)。

覆盖:装饰器登记、缺元数据报错、重名报错、多来源 merge/combined、
schemas 白名单投影、as_dict 视图不可回写、confirm/runtime_grantable 派生。
"""

import pytest

from nicekit.agent.tools import (
    DEFAULT_TOOL_CATEGORY,
    ToolDef,
    ToolRegistrationError,
    ToolRegistry,
    default_registry,
    tool_permission,
    validate_tool_permission_spec,
)
from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolPermissionSpec,
    ToolReversibility,
    ToolRisk,
)

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def _read_permission(**overrides) -> ToolPermissionSpec:
    values = {
        "effect": ToolEffect.READ,
        "risk": ToolRisk.ROUTINE,
        "categories": frozenset({ToolCategory.LOCAL_DATA}),
        "reversibility": ToolReversibility.REVERSIBLE,
        "delegation": ToolDelegation.AUTOMATIC,
        "scope": PermissionScope.ORGANIZATION,
    }
    values.update(overrides)
    return ToolPermissionSpec(**values)


async def _noop(_ctx, _args) -> dict:
    return {}


def _registry(name: str = "test") -> ToolRegistry:
    registry = ToolRegistry(name)

    @registry.register(
        "probe",
        "探测",
        SCHEMA,
        permission=_read_permission(),
        label="探测",
        category="通用",
    )
    async def probe(_ctx, _args) -> dict:
        return {"ok": True}

    return registry


# ---------- 登记 ----------


def test_register_decorator_returns_function_and_records_metadata() -> None:
    registry = ToolRegistry("t")

    @registry.register(
        "echo",
        "回显",
        SCHEMA,
        permission=_read_permission(),
        label="回显工具",
        category="通用",
        stage="research",
        emit_start_progress=False,
        runtime_grantable=False,
    )
    async def echo(_ctx, args) -> dict:
        return args

    assert echo.__name__ == "echo"  # 装饰器返回原函数,不包一层
    tool = registry.require("echo")
    assert tool.label == "回显工具"
    assert tool.category == "通用"
    assert tool.stage == "research"
    assert tool.emit_start_progress is False
    assert tool.runtime_grantable is False
    assert tool.side_effect == "read"
    assert tool.confirm is False  # automatic delegation 不需要确认
    assert "echo" in registry and len(registry) == 1


def test_confirm_defaults_follow_delegation() -> None:
    registry = ToolRegistry("t")
    registry.register(
        "reviewable",
        "需复核",
        SCHEMA,
        permission=_read_permission(
            effect=ToolEffect.WRITE,
            risk=ToolRisk.SENSITIVE,
            reversibility=ToolReversibility.IRREVERSIBLE,
            delegation=ToolDelegation.REVIEWABLE,
        ),
    )(_noop)

    assert registry.require("reviewable").confirm is True
    assert registry.confirm_required_names() == frozenset({"reviewable"})
    assert registry.runtime_grantable_names() == frozenset({"reviewable"})


def test_default_label_and_category_fall_back() -> None:
    registry = ToolRegistry("t")
    registry.register("bare", "无显示名", SCHEMA, permission=_read_permission())(_noop)
    tool = registry.require("bare")

    assert tool.label == ""
    assert tool.display_label == "bare"
    assert tool.category == DEFAULT_TOOL_CATEGORY


# ---------- 校验:缺元数据 / 坏元数据 ----------


def test_registration_without_permission_metadata_fails_closed() -> None:
    registry = ToolRegistry("t")
    with pytest.raises(ToolRegistrationError, match="missing permission metadata"):
        registry.register("unclassified", "无权限声明", SCHEMA, permission=None)  # type: ignore[arg-type]


def test_registration_rejects_side_effect_conflicting_with_permission() -> None:
    registry = ToolRegistry("t")
    with pytest.raises(ToolRegistrationError, match="side_effect conflicts"):
        registry.register(
            "conflict",
            "冲突",
            SCHEMA,
            permission=_read_permission(),
            side_effect="write",
        )


def test_registration_rejects_material_argument_absent_from_schema() -> None:
    registry = ToolRegistry("t")
    with pytest.raises(ValueError, match="absent from schema"):
        registry.register(
            "bad_material",
            "坏元数据",
            SCHEMA,
            permission=_read_permission(material_arguments=frozenset({"missing"})),
        )


def test_add_validates_dynamically_built_tools() -> None:
    registry = ToolRegistry("t")
    with pytest.raises(ValueError, match="schema properties are required"):
        registry.add(
            ToolDef(
                name="mcp_tool",
                description="动态工具",
                schema={"type": "object"},
                executor=_noop,
                side_effect="read",
            )
        )


def test_creates_scope_root_requires_a_write_effect() -> None:
    with pytest.raises(ValueError, match="cannot create a scope root"):
        validate_tool_permission_spec(
            "reader",
            SCHEMA,
            _read_permission(creates_scope_root=True),
        )


# ---------- 重名 ----------


def test_duplicate_registration_raises_instead_of_silently_overwriting() -> None:
    registry = _registry()
    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register("probe", "重名", SCHEMA, permission=_read_permission())(_noop)


def test_merge_reports_every_conflicting_name() -> None:
    left = _registry("left")
    right = _registry("right")

    with pytest.raises(ToolRegistrationError, match=r"\['probe'\]"):
        left.merge(right)
    # 冲突时不做部分合并
    assert left.names() == frozenset({"probe"})


# ---------- 多来源合并 ----------


def test_combined_merges_multiple_sources_without_touching_them() -> None:
    sdk = _registry("sdk")
    host = ToolRegistry("host")
    host.register("host_tool", "宿主工具", SCHEMA, permission=_read_permission())(_noop)

    combined = ToolRegistry.combined(sdk, host, name="app")

    assert combined.name == "app"
    assert combined.names() == frozenset({"probe", "host_tool"})
    assert sdk.names() == frozenset({"probe"})
    assert host.names() == frozenset({"host_tool"})


def test_copy_is_independent_of_the_source_registry() -> None:
    source = _registry()
    clone = source.copy(name="clone")
    clone.register("extra", "额外", SCHEMA, permission=_read_permission())(_noop)

    assert "extra" not in source
    assert source.unregister("probe") is not None
    assert "probe" in clone


# ---------- 读取投影 ----------


def test_schemas_projects_only_allowed_names_in_order() -> None:
    registry = _registry()
    registry.register("second", "第二个", SCHEMA, permission=_read_permission())(_noop)

    schemas = registry.schemas(["second", "unknown", "probe"])

    assert [item["name"] for item in schemas] == ["second", "probe"]
    assert schemas[0]["schema"] is SCHEMA


def test_as_dict_is_a_snapshot_not_a_live_handle() -> None:
    registry = _registry()
    view = registry.as_dict()
    view.pop("probe")

    assert "probe" in registry


def test_require_reports_the_registry_name_on_miss() -> None:
    registry = _registry("sdk")
    assert registry.get("nope") is None
    with pytest.raises(KeyError, match="sdk"):
        registry.require("nope")


def test_tool_permission_helper_builds_the_seven_axes() -> None:
    spec = tool_permission(
        ToolEffect.WRITE,
        ToolRisk.SENSITIVE,
        {ToolCategory.NETWORK, ToolCategory.EXTERNAL_COST},
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.REVIEWABLE,
        PermissionScope.SESSION,
        ("query",),
        creates_scope_root=True,
    )

    assert spec.categories == frozenset(
        {ToolCategory.NETWORK, ToolCategory.EXTERNAL_COST}
    )
    assert spec.material_arguments == frozenset({"query"})
    assert spec.creates_scope_root is True
    assert spec.side_effect == "write"


def test_default_registry_is_a_registry_instance_for_sdk_builtins() -> None:
    assert isinstance(default_registry, ToolRegistry)
    assert default_registry.name == "nicekit"
