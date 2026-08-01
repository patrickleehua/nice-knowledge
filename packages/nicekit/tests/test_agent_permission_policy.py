"""权限决策矩阵与 canonical action(迁移自 TF tests/test_agent_permission_policy.py)。

TF 版直接拿 44 个业务工具当被测对象;SDK 里没有业务工具,改为就地构造声明式
ToolDef。作用域断言跟随 scope 泛化:project_id → scope_id,
cross_project → cross_scope,general_project_mutation → general_scope_mutation。
"""

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest

from nicekit.agent.permissions.actions import (
    CanonicalToolAction,
    ResolvedActionScope,
    ResolvedResource,
    ResourceResolutionError,
    canonicalize_tool_action,
    register_resource_resolver,
    reset_resource_resolvers,
)
from nicekit.agent.permissions.policy import (
    CapabilityBoundary,
    EffectivePermissionPolicy,
    ResolvedPermissionGrant,
    evaluate_tool_action,
    evaluate_tool_action_with_shadow,
    load_effective_policy,
    policy_snapshot_payload,
)
from nicekit.agent.tools import ToolDef
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
from nicekit.models.agent_permission import AgentApprovalPolicy, AgentPermissionGrant
from nicekit.models.chat import ChatSession, ChatSessionOriginMode


@pytest.fixture(autouse=True)
def _clean_resolvers():
    reset_resource_resolvers()
    yield
    reset_resource_resolvers()


async def _noop(_ctx, _args) -> dict:
    return {}


def _chat(*, scope_id: UUID | None = None) -> ChatSession:
    return ChatSession(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_card_id=uuid4(),
        scope_type="case" if scope_id is not None else None,
        scope_id=scope_id,
        origin_mode=(
            ChatSessionOriginMode.SCOPED
            if scope_id is not None
            else ChatSessionOriginMode.GENERAL
        ),
    )


def _policy(
    profile: PermissionProfile,
    *,
    scope_id: UUID | None = None,
    **overrides,
) -> EffectivePermissionPolicy:
    values = {
        "org_id": uuid4(),
        "user_id": uuid4(),
        "session_id": uuid4(),
        "scope_id": scope_id,
        "policy_id": uuid4(),
        "policy_version": 3,
        "profile": profile,
        "allowed_profiles": frozenset(PermissionProfile),
        "scope": PermissionScope.RESOURCE if scope_id else PermissionScope.SESSION,
        "expires_at": (
            datetime.now(UTC) + timedelta(hours=1)
            if profile is PermissionProfile.FULL_ACCESS
            else None
        ),
    }
    values.update(overrides)
    return EffectivePermissionPolicy(**values)


def _action(
    tool_name: str,
    *,
    mode: str = "scoped",
    bound_scope_id: UUID | None = None,
    target_scope_id: UUID | None = None,
    creates_scope_root: bool = False,
    material_fingerprint: str = "a" * 64,
    resolution_error: str | None = None,
) -> CanonicalToolAction:
    return CanonicalToolAction(
        tool_name=tool_name,
        sanitized_arguments={},
        argument_hash="b" * 64,
        action_hash="c" * 64,
        material_arguments={},
        material_fingerprint=material_fingerprint,
        scope=ResolvedActionScope(
            org_id=uuid4(),
            session_id=uuid4(),
            mode=mode,  # type: ignore[arg-type]
            bound_scope_id=bound_scope_id,
            target_scope_id=target_scope_id,
            resource_type="case" if target_scope_id else None,
            resource_id=target_scope_id,
            creates_scope_root=creates_scope_root,
            resolution_error=resolution_error,
        ),
    )


def _tool(
    *,
    name: str,
    effect: ToolEffect = ToolEffect.WRITE,
    risk: ToolRisk = ToolRisk.SENSITIVE,
    categories: set[ToolCategory] | None = None,
    delegation: ToolDelegation = ToolDelegation.REVIEWABLE,
    reversibility: ToolReversibility = ToolReversibility.IRREVERSIBLE,
    creates_scope_root: bool = False,
    material_arguments: frozenset[str] = frozenset(),
    secret_arguments: frozenset[str] = frozenset(),
    schema: dict | None = None,
) -> ToolDef:
    return ToolDef(
        name=name,
        description=name,
        schema=schema or {"type": "object", "properties": {}},
        executor=_noop,
        permission=ToolPermissionSpec(
            effect=effect,
            risk=risk,
            categories=frozenset(categories or {ToolCategory.LOCAL_DATA}),
            reversibility=reversibility,
            delegation=delegation,
            scope=PermissionScope.RESOURCE,
            material_arguments=material_arguments,
            secret_arguments=secret_arguments,
            creates_scope_root=creates_scope_root,
        ),
    )


# 常用被测工具
ROUTINE_READ = _tool(
    name="routine_read",
    effect=ToolEffect.READ,
    risk=ToolRisk.ROUTINE,
    categories={ToolCategory.LOCAL_DATA},
    delegation=ToolDelegation.AUTOMATIC,
    reversibility=ToolReversibility.REVERSIBLE,
)
PAID_NETWORK = _tool(
    name="paid_network",
    effect=ToolEffect.WRITE,
    risk=ToolRisk.SENSITIVE,
    categories={ToolCategory.NETWORK, ToolCategory.EXTERNAL_COST},
    delegation=ToolDelegation.REVIEWABLE,
    reversibility=ToolReversibility.IRREVERSIBLE,
    material_arguments=frozenset({"n", "size"}),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "n": {"type": ["integer", "null"]},
            "size": {"type": ["string", "null"]},
        },
    },
)
CRITICAL_WRITE = _tool(
    name="critical_write",
    risk=ToolRisk.CRITICAL,
    categories={ToolCategory.FINANCIAL},
    delegation=ToolDelegation.REVIEWABLE,
)
SCOPE_MUTATION = _tool(name="scope_mutation", categories={ToolCategory.LOCAL_DATA})
SCOPE_CREATE = _tool(
    name="scope_create",
    categories={ToolCategory.LOCAL_DATA},
    creates_scope_root=True,
)


# ---------------------------------------------------------------- canonical


async def test_canonical_action_is_deterministic_redacted_and_secret_sensitive() -> None:
    tool = _tool(
        name="secure_tool",
        risk=ToolRisk.SENSITIVE,
        reversibility=ToolReversibility.REVERSIBLE,
        secret_arguments=frozenset({"private"}),
        schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "payload": {"type": "object"},
                "private": {"type": "string"},
            },
        },
    )
    scope_id = uuid4()
    chat = _chat(scope_id=scope_id)
    resolved = ResolvedResource(scope_id=scope_id, resource_type="case", resource_id=scope_id)
    first = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=tool,
        arguments={
            "private": "one",
            "payload": {"token": "nested-secret", "name": "safe"},
            "case_id": str(scope_id),
        },
        resolved_scope=resolved,
    )
    reordered = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=tool,
        arguments={
            "case_id": str(scope_id),
            "payload": {"name": "safe", "token": "nested-secret"},
            "private": "one",
        },
        resolved_scope=resolved,
    )
    changed_secret = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=tool,
        arguments={
            "private": "two",
            "payload": {"token": "nested-secret", "name": "safe"},
            "case_id": str(scope_id),
        },
        resolved_scope=resolved,
    )

    assert first.action_hash == reordered.action_hash
    assert first.sanitized_arguments["private"] == "[REDACTED]"
    assert first.sanitized_arguments["payload"]["token"] == "[REDACTED]"
    assert first.argument_hash != changed_secret.argument_hash
    assert len(first.action_hash) == len(first.material_fingerprint) == 64


async def test_material_fingerprint_ignores_non_material_free_text() -> None:
    chat = _chat()
    first = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=PAID_NETWORK,
        arguments={"prompt": "A", "n": 1, "size": "1024x1024"},
    )
    second = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=PAID_NETWORK,
        arguments={"prompt": "B", "n": 1, "size": "1024x1024"},
    )

    assert first.argument_hash != second.argument_hash
    assert first.material_fingerprint == second.material_fingerprint
    assert first.material_arguments == {"n": 1, "size": "1024x1024"}


async def test_registered_resolver_decides_the_target_scope() -> None:
    bound_scope = uuid4()
    other_scope = uuid4()
    chat = _chat(scope_id=bound_scope)

    class _Resolver:
        async def resolve_scope(self, _session, _tool_name, arguments):
            return other_scope if arguments.get("case_id") else None

    register_resource_resolver(_Resolver())
    cross = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=SCOPE_MUTATION,
        arguments={"case_id": str(other_scope)},
    )
    inherited = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=SCOPE_MUTATION,
        arguments={},
    )

    assert cross.scope.cross_scope is True
    assert cross.scope.target_scope_id == other_scope
    # 无 resolver 命中时回落到会话绑定的作用域
    assert inherited.scope.target_scope_id == bound_scope
    assert inherited.scope.cross_scope is False


async def test_resolver_failure_is_a_fail_closed_resolution_error() -> None:
    chat = _chat(scope_id=uuid4())

    class _Resolver:
        async def resolve_scope(self, _session, _tool_name, _arguments):
            raise ResourceResolutionError("resource_not_visible")

    register_resource_resolver(_Resolver())
    hidden = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=SCOPE_MUTATION,
        arguments={"case_id": str(uuid4())},
    )

    assert hidden.scope.resolution_error == "resource_not_visible"
    assert hidden.scope.target_scope_id is None
    evaluation = evaluate_tool_action(
        _policy(PermissionProfile.FULL_ACCESS), SCOPE_MUTATION, hidden
    )
    assert evaluation.decision is PermissionDecision.DENY
    assert evaluation.reason_code == "resource_not_visible"


async def test_creates_scope_root_comes_from_tool_metadata() -> None:
    chat = _chat()
    creating = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=SCOPE_CREATE,
        arguments={},
    )
    mutating = await canonicalize_tool_action(
        object(),  # type: ignore[arg-type]
        org_id=chat.org_id,
        chat_session=chat,
        tool=SCOPE_MUTATION,
        arguments={},
    )

    assert creating.scope.creates_scope_root is True
    assert mutating.scope.creates_scope_root is False


# ------------------------------------------------------------ decision flow


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        (CapabilityBoundary(agent_tool_allowed=False), "agent_tool_not_allowed"),
        (CapabilityBoundary(service_ready=False), "service_unavailable"),
        (CapabilityBoundary(tenant_allowed=False), "tenant_boundary"),
        (CapabilityBoundary(role_allowed=False), "role_not_allowed"),
        (CapabilityBoundary(business_allowed=False), "business_rule_denied"),
        (CapabilityBoundary(budget_available=False), "budget_exhausted"),
    ],
)
def test_capability_boundary_always_precedes_profiles(
    boundary: CapabilityBoundary, reason: str
) -> None:
    scope_id = uuid4()
    evaluation = evaluate_tool_action(
        _policy(PermissionProfile.FULL_ACCESS, scope_id=scope_id),
        SCOPE_MUTATION,
        _action(
            "scope_mutation",
            bound_scope_id=scope_id,
            target_scope_id=scope_id,
        ),
        capability=boundary,
    )

    assert evaluation.decision is PermissionDecision.DENY
    assert evaluation.reason_code == reason


def test_agent_forbidden_and_user_required_cannot_be_granted_by_full_access() -> None:
    forbidden = _tool(
        name="forbidden",
        risk=ToolRisk.CRITICAL,
        categories={ToolCategory.FINANCIAL},
        delegation=ToolDelegation.AGENT_FORBIDDEN,
    )
    user_required = _tool(
        name="human",
        risk=ToolRisk.CRITICAL,
        categories={ToolCategory.FINANCIAL},
        delegation=ToolDelegation.USER_REQUIRED,
    )
    policy = _policy(PermissionProfile.FULL_ACCESS)

    assert evaluate_tool_action(policy, forbidden, _action("forbidden")).decision is (
        PermissionDecision.DENY
    )
    assert evaluate_tool_action(policy, user_required, _action("human")).decision is (
        PermissionDecision.ASK_USER
    )


def test_request_and_compatibility_profiles_keep_distinct_semantics() -> None:
    normal = _policy(PermissionProfile.REQUEST_APPROVAL)
    compatibility = _policy(
        PermissionProfile.REQUEST_APPROVAL,
        compatibility_mode=True,
    )

    assert evaluate_tool_action(
        normal, ROUTINE_READ, _action("routine_read")
    ).decision is PermissionDecision.ALLOW
    assert evaluate_tool_action(
        normal, SCOPE_CREATE, _action("scope_create", creates_scope_root=True)
    ).decision is PermissionDecision.ASK_USER
    assert evaluate_tool_action(
        compatibility, ROUTINE_READ, _action("routine_read")
    ).decision is PermissionDecision.ALLOW
    assert evaluate_tool_action(
        compatibility, PAID_NETWORK, _action("paid_network")
    ).decision is PermissionDecision.ASK_USER


def test_shadow_mode_records_configured_outcome_while_enforcing_legacy_gates() -> None:
    policy = _policy(PermissionProfile.FULL_ACCESS, shadow_evaluation=True)

    for tool in (ROUTINE_READ, PAID_NETWORK, CRITICAL_WRITE):
        action = _action(tool.name)
        comparison = evaluate_tool_action_with_shadow(policy, tool, action)
        configured = evaluate_tool_action(policy, tool, action)

        assert comparison.shadow == configured
        expected = (
            PermissionDecision.ALLOW
            if tool.permission.delegation is ToolDelegation.AUTOMATIC and not tool.confirm
            else PermissionDecision.ASK_USER
        )
        assert comparison.enforced.decision is expected, tool.name
        # differs 同时看 decision / reason_code / source:合规模式下即使结论相同,
        # 依据不同也要被记下来(否则灰度期看不出策略真正的影响面)
        assert comparison.differs is (
            (
                comparison.enforced.decision,
                comparison.enforced.reason_code,
                comparison.enforced.source,
            )
            != (configured.decision, configured.reason_code, configured.source)
        )


def test_disabled_shadow_mode_enforces_the_configured_outcome_directly() -> None:
    policy = _policy(PermissionProfile.FULL_ACCESS, shadow_evaluation=False)
    comparison = evaluate_tool_action_with_shadow(
        policy, PAID_NETWORK, _action("paid_network")
    )
    assert comparison.shadow is None
    assert comparison.enforced.decision is PermissionDecision.ALLOW


def test_auto_review_routes_only_eligible_reviewable_actions() -> None:
    eligible = _policy(
        PermissionProfile.AUTO_REVIEW,
        reviewer_enabled=True,
        reviewer_eligible_categories=frozenset({ToolCategory.EXTERNAL_COST}),
    )
    ineligible = _policy(PermissionProfile.AUTO_REVIEW, reviewer_enabled=True)

    approved_route = evaluate_tool_action(eligible, PAID_NETWORK, _action("paid_network"))
    fallback = evaluate_tool_action(ineligible, PAID_NETWORK, _action("paid_network"))

    assert approved_route.decision is PermissionDecision.AUTO_REVIEW
    assert approved_route.reviewer_eligible is True
    assert fallback.decision is PermissionDecision.ASK_USER
    assert fallback.reason_code == "reviewer_not_eligible"


def test_critical_action_requires_user_unless_explicitly_reviewer_eligible() -> None:
    default = _policy(PermissionProfile.AUTO_REVIEW, reviewer_enabled=True)
    eligible = _policy(
        PermissionProfile.AUTO_REVIEW,
        reviewer_enabled=True,
        reviewer_eligible_tools=frozenset({"critical_write"}),
    )

    assert evaluate_tool_action(
        default, CRITICAL_WRITE, _action("critical_write")
    ).decision is PermissionDecision.ASK_USER
    assert evaluate_tool_action(
        eligible, CRITICAL_WRITE, _action("critical_write")
    ).decision is PermissionDecision.AUTO_REVIEW


def test_organization_rules_precede_grants_and_profile() -> None:
    grant = ResolvedPermissionGrant(
        id=uuid4(),
        tool_name="paid_network",
        category=None,
        scope=PermissionScope.SESSION,
        session_id=None,
        scope_type=None,
        scope_id=None,
        material_fingerprint="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    policy = _policy(
        PermissionProfile.FULL_ACCESS,
        denied_categories=frozenset({ToolCategory.EXTERNAL_COST}),
        grants=(grant,),
    )

    evaluation = evaluate_tool_action(policy, PAID_NETWORK, _action("paid_network"))
    assert evaluation.decision is PermissionDecision.DENY
    assert evaluation.source == "organization"


def test_exact_scoped_grant_allows_only_matching_material_action() -> None:
    session_id = uuid4()
    scope_id = uuid4()
    grant = ResolvedPermissionGrant(
        id=uuid4(),
        tool_name="critical_write",
        category=None,
        scope=PermissionScope.RESOURCE,
        session_id=session_id,
        scope_type="case",
        scope_id=scope_id,
        material_fingerprint="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    policy = _policy(
        PermissionProfile.REQUEST_APPROVAL,
        scope_id=scope_id,
        session_id=session_id,
        grants=(grant,),
    )
    matching = _action(
        "critical_write", bound_scope_id=scope_id, target_scope_id=scope_id
    )
    changed = _action(
        "critical_write",
        bound_scope_id=scope_id,
        target_scope_id=scope_id,
        material_fingerprint="d" * 64,
    )

    approved = evaluate_tool_action(policy, CRITICAL_WRITE, matching)
    assert approved.decision is PermissionDecision.ALLOW
    assert approved.grant_id == grant.id
    assert evaluate_tool_action(
        policy, CRITICAL_WRITE, changed
    ).decision is PermissionDecision.ASK_USER


def test_expired_grant_does_not_match() -> None:
    grant = ResolvedPermissionGrant(
        id=uuid4(),
        tool_name="paid_network",
        category=None,
        scope=PermissionScope.ORGANIZATION,
        session_id=None,
        scope_type=None,
        scope_id=None,
        material_fingerprint="a" * 64,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    policy = _policy(PermissionProfile.REQUEST_APPROVAL, grants=(grant,))

    assert evaluate_tool_action(
        policy, PAID_NETWORK, _action("paid_network")
    ).decision is PermissionDecision.ASK_USER


def test_custom_rules_use_strictest_category_and_fail_missing_to_ask() -> None:
    complete = _policy(
        PermissionProfile.CUSTOM,
        custom_rules=MappingProxyType(
            {
                ToolCategory.LOCAL_DATA: PermissionDecision.ALLOW,
                ToolCategory.FINANCIAL: PermissionDecision.DENY,
            }
        ),
    )
    missing = _policy(
        PermissionProfile.CUSTOM,
        custom_rules=MappingProxyType({ToolCategory.LOCAL_DATA: PermissionDecision.ALLOW}),
    )

    assert evaluate_tool_action(
        complete, CRITICAL_WRITE, _action("critical_write")
    ).decision is PermissionDecision.DENY
    missing_result = evaluate_tool_action(
        missing, PAID_NETWORK, _action("paid_network")
    )
    assert missing_result.decision is PermissionDecision.ASK_USER
    assert missing_result.reason_code == "custom_missing_rule"


def test_scoped_and_general_mode_scope_rules_are_non_bypassable() -> None:
    bound_scope = uuid4()
    other_scope = uuid4()
    full_access = _policy(PermissionProfile.FULL_ACCESS, scope_id=bound_scope)
    cross_scope = _action(
        "scope_mutation", bound_scope_id=bound_scope, target_scope_id=other_scope
    )
    general_existing = _action(
        "scope_mutation", mode="general", target_scope_id=other_scope
    )
    general_create = _action(
        "scope_create", mode="general", creates_scope_root=True
    )
    general_read = _action("routine_read", mode="general", target_scope_id=other_scope)

    cross = evaluate_tool_action(full_access, SCOPE_MUTATION, cross_scope)
    assert cross.decision is PermissionDecision.DENY
    assert cross.reason_code == "scope_mismatch"

    mutation = evaluate_tool_action(
        _policy(PermissionProfile.FULL_ACCESS), SCOPE_MUTATION, general_existing
    )
    assert mutation.decision is PermissionDecision.ASK_USER
    assert mutation.reason_code == "general_scope_mutation"

    # 新建作用域根正是通用会话该干的事;只读同样不受这条规则约束
    assert evaluate_tool_action(
        _policy(PermissionProfile.FULL_ACCESS), SCOPE_CREATE, general_create
    ).decision is PermissionDecision.ALLOW
    assert evaluate_tool_action(
        _policy(PermissionProfile.FULL_ACCESS), ROUTINE_READ, general_read
    ).decision is PermissionDecision.ALLOW


# ----------------------------------------------------------------- loading


class _Result:
    def __init__(self, *, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.one

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, *results: _Result):
        self.results = list(results)

    async def execute(self, _query):
        return self.results.pop(0)


async def test_effective_policy_loading_intersects_org_session_expiry_and_grants() -> None:
    now = datetime.now(UTC)
    chat = _chat(scope_id=uuid4())
    chat.permission_profile = PermissionProfile.FULL_ACCESS
    chat.permission_scope = PermissionScope.ORGANIZATION
    chat.permission_expires_at = now - timedelta(seconds=1)
    policy_row = AgentApprovalPolicy(
        id=uuid4(),
        org_id=chat.org_id,
        version=7,
        default_profile=PermissionProfile.REQUEST_APPROVAL,
        allowed_profiles=[profile.value for profile in PermissionProfile],
        hard_rules={"category_decisions": {"financial": "ask_user"}},
        reviewer_enabled=True,
        reviewer_eligible_categories=["external_cost"],
        max_scope=PermissionScope.RESOURCE,
        is_active=True,
    )
    active_grant = AgentPermissionGrant(
        id=uuid4(),
        org_id=chat.org_id,
        user_id=chat.user_id,
        session_id=chat.id,
        scope_type=chat.scope_type,
        scope_id=chat.scope_id,
        tool_name="critical_write",
        scope=PermissionScope.RESOURCE,
        action_fingerprint="e" * 64,
        policy_version=7,
        created_by=chat.user_id,
        expires_at=now + timedelta(hours=1),
    )
    expired_grant = active_grant.model_copy(
        update={"id": uuid4(), "expires_at": now - timedelta(seconds=1)}
    )
    session = _Session(
        _Result(one=policy_row),
        _Result(rows=[active_grant, expired_grant]),
    )

    effective = await load_effective_policy(
        session,  # type: ignore[arg-type]
        org_id=chat.org_id,
        user_id=chat.user_id,
        chat_session=chat,
        now=now,
    )

    assert effective.profile is PermissionProfile.REQUEST_APPROVAL
    assert effective.scope is PermissionScope.RESOURCE
    assert effective.scope_id == chat.scope_id
    assert effective.policy_version == 7
    assert [grant.id for grant in effective.grants] == [active_grant.id]
    assert effective.grants[0].scope_id == chat.scope_id
    assert effective.organization_category_decisions[ToolCategory.FINANCIAL] is (
        PermissionDecision.ASK_USER
    )


async def test_missing_org_policy_loads_strict_compatibility_state() -> None:
    chat = _chat()
    effective = await load_effective_policy(
        _Session(_Result(one=None)),  # type: ignore[arg-type]
        org_id=chat.org_id,
        user_id=chat.user_id,
        chat_session=chat,
    )

    assert effective.policy_id is None
    assert effective.profile is PermissionProfile.REQUEST_APPROVAL
    assert effective.allowed_profiles == frozenset({PermissionProfile.REQUEST_APPROVAL})
    assert effective.compatibility_mode is True


def test_policy_snapshot_contains_only_bounded_non_secret_state() -> None:
    policy = _policy(
        PermissionProfile.AUTO_REVIEW,
        custom_rules=MappingProxyType({ToolCategory.LOCAL_DATA: PermissionDecision.ALLOW}),
    )

    snapshot = policy_snapshot_payload(policy)

    assert snapshot["policy_id"] == str(policy.policy_id)
    assert snapshot["profile"] == "auto_review"
    assert "custom_rules" not in snapshot
    assert "organization_tool_decisions" not in snapshot
    assert "arguments" not in snapshot
