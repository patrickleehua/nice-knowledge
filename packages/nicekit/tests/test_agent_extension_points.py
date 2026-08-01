"""SDK ↔ 宿主的注入点(MIGRATION-PLAN §4 扩展点协议)。

覆盖本波新增的四个注入点:RoleChecker、material 参数追加、
management 的 known_tools/registry provider、terminal 的资源终结回调。
"""

from uuid import uuid4

import pytest

from nicekit.agent.permissions import actions, management
from nicekit.agent.terminal import (
    TerminalContext,
    register_terminal_finalizer,
    reset_terminal_finalizers,
    terminalize_agent_run,
)
from nicekit.agent.tools import (
    ToolContext,
    ToolError,
    ToolRegistry,
    check_role,
    require_role,
    set_role_checker,
)
from nicekit.models.chat import ChatRunInputStatus, ChatSession


def _ctx(role: str) -> ToolContext:
    return ToolContext(
        session=object(),  # type: ignore[arg-type]
        org_id=uuid4(),
        user_id=uuid4(),
        role=role,
        chat_session=ChatSession(org_id=uuid4(), user_id=uuid4(), agent_card_id=uuid4()),
    )


# ------------------------------------------------------------- RoleChecker


def test_default_role_checker_compares_names() -> None:
    set_role_checker(None)
    assert check_role(_ctx("org_admin"), "org_admin") is True
    assert check_role(_ctx("member"), "org_admin") is False


def test_injected_role_checker_can_model_hierarchies() -> None:
    ranks = {"member": 0, "org_admin": 1, "platform_admin": 2}

    def checker(ctx: ToolContext, role: str) -> bool:
        return ranks.get(str(ctx.role), -1) >= ranks.get(role, 99)

    set_role_checker(checker)
    try:
        assert check_role(_ctx("platform_admin"), "org_admin") is True
        require_role(_ctx("platform_admin"), "org_admin", "无权限")
        with pytest.raises(ToolError, match="无权限"):
            require_role(_ctx("member"), "org_admin", "无权限")
    finally:
        set_role_checker(None)
    assert check_role(_ctx("platform_admin"), "org_admin") is False


# ------------------------------------------------ material 参数(指纹口径)


def test_builtin_material_arguments_drop_business_specific_names() -> None:
    actions.reset_material_arguments()
    names = actions.material_argument_names()

    assert {"amount", "currency", "n", "size", "status"}.issubset(names)
    assert "offer_index" not in names
    assert "unit_cost" not in names


def test_hosts_can_append_material_arguments() -> None:
    actions.reset_material_arguments()
    try:
        actions.register_material_arguments("Unit_Cost", "seats")
        # 大小写归一,便于宿主直接抄 schema 字段名
        assert {"unit_cost", "seats"}.issubset(actions.material_argument_names())
    finally:
        actions.reset_material_arguments()
    assert "seats" not in actions.material_argument_names()


# ------------------------------------------------ management 的工具名来源


def test_hard_rules_validation_uses_the_injected_tool_names() -> None:
    with pytest.raises(management.PermissionManagementError) as excinfo:
        management.validate_hard_rules(
            {"denied_tools": ["host_tool"]},
            known_tools=lambda: {"sdk_tool"},
        )
    assert excinfo.value.code == "unknown_tool"

    normalized = management.validate_hard_rules(
        {"denied_tools": ["host_tool"], "tool_decisions": {"host_tool": "deny"}},
        known_tools=lambda: {"host_tool"},
    )
    assert normalized["denied_tools"] == ["host_tool"]
    assert normalized["tool_decisions"] == {"host_tool": "deny"}


def test_registry_provider_supplies_default_known_tools() -> None:
    registry = ToolRegistry("host")

    async def executor(_ctx, _args) -> dict:
        return {}

    from nicekit.agent.tools import ToolDef

    registry.add(
        ToolDef(
            name="host_tool",
            description="宿主工具",
            schema={"type": "object", "properties": {}},
            executor=executor,
            side_effect="read",
        )
    )
    management.set_tool_registry_provider(lambda: registry)
    try:
        assert management.validate_hard_rules({"denied_tools": ["host_tool"]}) == {
            "denied_tools": ["host_tool"]
        }
        with pytest.raises(management.PermissionManagementError, match="未知工具"):
            management.validate_hard_rules({"denied_tools": ["absent"]})
    finally:
        management.set_tool_registry_provider(None)


def test_scope_validation_rejects_scope_tier_without_a_binding() -> None:
    from nicekit.domain.agent_permission import PermissionScope

    management.validate_scope(
        PermissionScope.RESOURCE, PermissionScope.ORGANIZATION, scope_id=uuid4()
    )
    with pytest.raises(management.PermissionManagementError) as excinfo:
        management.validate_scope(
            PermissionScope.RESOURCE, PermissionScope.ORGANIZATION, scope_id=None
        )
    assert excinfo.value.code == "scope_requires_binding"


# ------------------------------------------------------- terminal finalizer


class _Result:
    def __init__(self, *, one=None, rows=None, scalar=None):
        self._one = one
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._one

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *results: _Result):
        self.results = list(results)
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _query):
        return self.results.pop(0)

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture(autouse=True)
def _clean_finalizers():
    reset_terminal_finalizers()
    yield
    reset_terminal_finalizers()


async def test_terminalization_passes_resources_to_registered_finalizers() -> None:
    run_id = uuid4()
    chat = ChatSession(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_card_id=uuid4(),
        active_run_id=run_id,
        plan=[{"id": "s1", "title": "步骤", "status": "in_progress", "note": None}],
    )
    seen: list[TerminalContext] = []

    async def finalizer(_session, context: TerminalContext) -> None:
        seen.append(context)

    register_terminal_finalizer(finalizer)
    session = _Session(_Result(one=chat), _Result(rows=[]), _Result(scalar=4))

    result = await terminalize_agent_run(
        session,  # type: ignore[arg-type]
        chat_session_id=chat.id,
        run_id=run_id,
        stop="cancelled",
        reason="用户取消",
        cancellable_resources=("job-1", "job-2"),
    )

    assert result.committed is True
    assert session.committed is True
    assert [context.cancellable_resources for context in seen] == [("job-1", "job-2")]
    assert seen[0].stop == "cancelled"
    assert seen[0].reason == "用户取消"
    assert seen[0].org_id == chat.org_id
    # 计划收敛与会话状态复位照旧
    assert result.plan is not None and result.plan[0]["status"] == "failed"
    assert chat.active_run_id is None and chat.cancel_requested is False


async def test_finalizer_failure_does_not_block_session_convergence() -> None:
    run_id = uuid4()
    chat = ChatSession(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_card_id=uuid4(),
        active_run_id=run_id,
    )

    async def boom(_session, _context) -> None:
        raise RuntimeError("宿主收尾失败")

    register_terminal_finalizer(boom)
    session = _Session(_Result(one=chat), _Result(rows=[]), _Result(scalar=0))

    result = await terminalize_agent_run(
        session,  # type: ignore[arg-type]
        chat_session_id=chat.id,
        run_id=run_id,
        stop="error",
        reason="worker 崩溃",
    )

    assert result.committed is True
    assert chat.active_run_id is None


async def test_terminalization_is_a_noop_when_the_run_already_converged() -> None:
    chat = ChatSession(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_card_id=uuid4(),
        active_run_id=None,
    )
    calls: list[str] = []

    async def finalizer(_session, _context) -> None:
        calls.append("called")

    register_terminal_finalizer(finalizer)
    session = _Session(_Result(one=chat))

    result = await terminalize_agent_run(
        session,  # type: ignore[arg-type]
        chat_session_id=chat.id,
        run_id=uuid4(),
        stop="cancelled",
        reason="重复终结",
    )

    assert result.committed is False
    assert calls == []
    assert session.rolled_back is True


async def test_queued_inputs_are_skipped_not_dropped(monkeypatch) -> None:
    from nicekit.models.chat import ChatRunInput

    run_id = uuid4()
    chat = ChatSession(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_card_id=uuid4(),
        active_run_id=run_id,
    )
    queued = ChatRunInput(
        id=uuid4(),
        org_id=chat.org_id,
        session_id=chat.id,
        run_id=run_id,
        user_id=chat.user_id,
        content="排队中的补充",
        position=1,
    )
    session = _Session(_Result(one=chat), _Result(rows=[queued]), _Result(scalar=2))

    result = await terminalize_agent_run(
        session,  # type: ignore[arg-type]
        chat_session_id=chat.id,
        run_id=run_id,
        stop="cancelled",
        reason="用户取消",
    )

    assert queued.status == ChatRunInputStatus.SKIPPED.value
    assert [event["type"] for event in result.queue_events] == ["input.skipped"]
    assert result.queue_events[0]["reason"] == "run_cancelled"
