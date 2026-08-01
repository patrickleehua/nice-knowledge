"""七轴权限词表与工具元数据校验(迁移自 TF tests/test_agent_permission_metadata.py)。

TF 的"catalog/metadata/registry 三方完全相等"断言不再适用(SDK 允许多来源
注册),对应语义由 test_agent_tool_registry.py 覆盖。
"""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from nicekit.agent.tools import ToolDef, validate_tool_permission_spec
from nicekit.domain.agent_permission import (
    PermissionDecision,
    PermissionProfile,
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolPermissionSpec,
    ToolReversibility,
    ToolRisk,
)
from nicekit.models.agent_permission import AgentApprovalPolicy
from nicekit.models.chat import ChatSession  # noqa: F401 - 注册 chat_sessions 表


def _permission_spec(**overrides) -> ToolPermissionSpec:
    values = {
        "effect": ToolEffect.WRITE,
        "risk": ToolRisk.ROUTINE,
        "categories": frozenset({ToolCategory.LOCAL_DATA}),
        "reversibility": ToolReversibility.REVERSIBLE,
        "delegation": ToolDelegation.AUTOMATIC,
        "scope": PermissionScope.RESOURCE,
    }
    values.update(overrides)
    return ToolPermissionSpec(**values)


def test_permission_vocabulary_uses_stable_wire_values() -> None:
    assert [profile.value for profile in PermissionProfile] == [
        "request_approval",
        "auto_review",
        "full_access",
        "custom",
    ]
    assert [decision.value for decision in PermissionDecision] == [
        "allow",
        "auto_review",
        "ask_user",
        "deny",
    ]
    assert set(ToolCategory) == {
        ToolCategory.LOCAL_DATA,
        ToolCategory.NETWORK,
        ToolCategory.EXTERNAL_COST,
        ToolCategory.FINANCIAL,
        ToolCategory.DESTRUCTIVE,
        ToolCategory.WORKFLOW,
        ToolCategory.EXPORT,
    }
    # 落库取值不能漂:已签发的授权与组织策略 JSON 都按这些字符串比较
    assert [scope.value for scope in PermissionScope] == [
        "session",
        "resource",
        "organization",
    ]


def test_tool_permission_spec_is_immutable_and_derives_legacy_side_effect() -> None:
    spec = _permission_spec(
        categories={ToolCategory.LOCAL_DATA, ToolCategory.FINANCIAL},
        material_arguments={"scope_id", "amount"},
    )

    assert isinstance(spec.categories, frozenset)
    assert spec.material_arguments == frozenset({"scope_id", "amount"})
    assert spec.side_effect == "write"
    assert _permission_spec(effect=ToolEffect.READ).side_effect == "read"
    with pytest.raises(FrozenInstanceError):
        spec.risk = ToolRisk.CRITICAL  # type: ignore[misc]


def test_creates_scope_root_defaults_to_false_and_must_be_boolean() -> None:
    assert _permission_spec().creates_scope_root is False
    assert _permission_spec(creates_scope_root=True).creates_scope_root is True
    with pytest.raises(TypeError, match="creates_scope_root must be a bool"):
        _permission_spec(creates_scope_root="yes")


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"categories": frozenset()}, "categories cannot be empty"),
        ({"categories": {"local_data"}}, "must contain ToolCategory"),
        ({"risk": "routine"}, "risk must be a ToolRisk"),
        ({"material_arguments": {""}}, "non-empty strings"),
    ],
)
def test_tool_permission_spec_rejects_untyped_or_incomplete_metadata(
    overrides: dict, error: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        _permission_spec(**overrides)


def test_permission_models_and_session_snapshot_columns_are_registered() -> None:
    from sqlmodel import SQLModel

    metadata = SQLModel.metadata
    assert {
        "agent_approval_policies",
        "agent_permission_preferences",
        "agent_permission_grants",
    }.issubset(metadata.tables)
    chat_columns = metadata.tables["chat_sessions"].c
    assert {
        "permission_profile",
        "permission_scope",
        "permission_expires_at",
        "permission_policy_id",
        "permission_policy_version",
        "permission_custom_rules",
        "permission_policy_snapshot",
        "permission_revision",
    }.issubset(chat_columns.keys())

    policy_indexes = {
        index.name for index in metadata.tables["agent_approval_policies"].indexes
    }
    grant_indexes = {
        index.name for index in metadata.tables["agent_permission_grants"].indexes
    }
    assert "uq_agent_approval_policies_active_org" in policy_indexes
    assert "ix_agent_permission_grants_match" in grant_indexes
    assert "ix_agent_permission_grants_active_expiry" in grant_indexes


def test_new_organization_policy_defaults_are_strict() -> None:
    policy = AgentApprovalPolicy(org_id=uuid4(), version=1)

    assert policy.default_profile is PermissionProfile.REQUEST_APPROVAL
    assert policy.allowed_profiles == [PermissionProfile.REQUEST_APPROVAL.value]
    assert policy.reviewer_enabled is False
    assert policy.max_scope is PermissionScope.RESOURCE


async def _noop_executor(_ctx, _args) -> dict:
    return {}


def test_legacy_boolean_tool_definition_gets_reviewable_metadata() -> None:
    tool = ToolDef(
        name="legacy_write",
        description="legacy",
        schema={"type": "object", "properties": {}},
        executor=_noop_executor,
        side_effect="write",
        confirm=True,
    )

    assert tool.permission.effect is ToolEffect.WRITE
    assert tool.permission.risk is ToolRisk.CRITICAL
    assert tool.permission.delegation is ToolDelegation.REVIEWABLE
    assert tool.permission.reversibility is ToolReversibility.IRREVERSIBLE
    assert tool.side_effect == "write"
    assert tool.confirm is True


def test_structured_permission_is_authoritative_for_legacy_side_effect() -> None:
    with pytest.raises(ValueError, match="side_effect conflicts"):
        ToolDef(
            name="conflict",
            description="conflict",
            schema={"type": "object", "properties": {}},
            executor=_noop_executor,
            permission=_permission_spec(effect=ToolEffect.READ),
            side_effect="write",
        )


@pytest.mark.parametrize(
    ("permission", "error"),
    [
        (
            _permission_spec(
                effect=ToolEffect.DELETE,
                risk=ToolRisk.CRITICAL,
                delegation=ToolDelegation.REVIEWABLE,
            ),
            "requires destructive category",
        ),
        (
            _permission_spec(
                effect=ToolEffect.TRANSITION,
                risk=ToolRisk.SENSITIVE,
            ),
            "requires workflow category",
        ),
        (
            _permission_spec(
                categories={ToolCategory.EXTERNAL_COST},
                risk=ToolRisk.SENSITIVE,
            ),
            "external cost requires network",
        ),
        (
            _permission_spec(risk=ToolRisk.CRITICAL),
            "critical risk cannot be automatic",
        ),
        (
            _permission_spec(material_arguments={"missing"}),
            "absent from schema",
        ),
    ],
)
def test_registry_validation_rejects_invalid_permission_combinations(
    permission: ToolPermissionSpec, error: str
) -> None:
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "required": ["known"],
    }

    with pytest.raises(ValueError, match=error):
        validate_tool_permission_spec("unsafe", schema, permission)
