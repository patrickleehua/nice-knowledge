"""runtime/bootstrap.py:装配期注册与 seed 收敛(§5.9 A2/A3)+ 写角色统一注册(A13)。

seed 部分用假会话验证"调了哪些 ensure_*、计数怎么汇总";真库效果由 demo seed
与冒烟覆盖。
"""

from uuid import uuid4

import pytest

from nicekit.core.config import get_settings
from nicekit.kb import ports as kb_ports
from nicekit.runtime import bootstrap
from nicekit.runtime.bootstrap import (
    AGENT_DEFAULT_TASK,
    bootstrap_platform,
    build_agent_default_route,
    install_default_ports,
)
from nicekit.tenancy.roles import (
    register_roles,
    register_write_roles,
    reset_registered_roles,
    reset_write_roles,
    write_roles,
)


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def execute(self, _statement):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()

    def add(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


# ---------------------------------------------------------------------------
# 注册半边(A2/A3)
# ---------------------------------------------------------------------------


def test_install_default_ports_wires_builtin_tools_and_incident_recorder() -> None:
    kb_ports.set_incident_recorder(None)
    install_default_ports(force=True)

    from nicekit.agent.tools import default_registry
    from nicekit.llm.prompt_catalog import BUILTIN_PROMPT_CATALOG
    from nicekit.operations.incidents import SqlIncidentRecorder

    # A2:builtin_tools 只在这里被显式 import 一次
    assert "kb_search" in default_registry.names()
    # A3:KB 任务目录条目进内置目录
    assert any(task.startswith("kb.") for task in BUILTIN_PROMPT_CATALOG)
    assert isinstance(kb_ports.get_incident_recorder(), SqlIncidentRecorder)


def test_install_default_ports_is_idempotent() -> None:
    install_default_ports(force=True)
    install_default_ports()  # 二次调用不该因工具重名报错
    install_default_ports()


def test_kb_notify_roles_follow_write_role_registry() -> None:
    try:
        register_roles("editor")
        register_write_roles("editor")
        install_default_ports(force=True)
        assert "editor" in kb_ports.kb_notify_roles()
    finally:
        reset_write_roles()
        reset_registered_roles()
        install_default_ports(force=True)


# ---------------------------------------------------------------------------
# 写角色注册表(A13)
# ---------------------------------------------------------------------------


def test_write_roles_builtin_and_registered() -> None:
    try:
        assert write_roles() == ("platform_admin", "org_admin")
        register_roles("reviewer")
        register_write_roles("reviewer")
        assert write_roles() == ("platform_admin", "org_admin", "reviewer")
        register_write_roles("org_admin")  # 内置写角色重复注册是 no-op
        assert write_roles().count("org_admin") == 1
    finally:
        reset_write_roles()
        reset_registered_roles()


def test_write_roles_reject_unregistered_name() -> None:
    with pytest.raises(ValueError, match="未注册的角色"):
        register_write_roles("ghost")


async def test_require_write_role_reads_registry_at_request_time() -> None:
    """A13 的关键:允许集合不在 import 期定格,宿主装配后已定义的端点立刻放行。"""
    from fastapi import HTTPException

    from nicekit.api.deps import OrgContext, require_write_role

    checker = require_write_role()
    ctx = OrgContext(user_id=uuid4(), org_id=uuid4(), role="editor")
    with pytest.raises(HTTPException) as excinfo:
        await checker(ctx)
    assert excinfo.value.status_code == 403

    try:
        register_roles("editor")
        register_write_roles("editor")
        assert await checker(ctx) is ctx
    finally:
        reset_write_roles()
        reset_registered_roles()


# ---------------------------------------------------------------------------
# seed 半边
# ---------------------------------------------------------------------------


def test_agent_default_route_needs_configured_model(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_default_model", "", raising=False)
    assert build_agent_default_route(settings) is None

    monkeypatch.setattr(settings, "llm_default_model", "gpt-x", raising=False)
    monkeypatch.setattr(settings, "llm_fallback_provider", "anthropic", raising=False)
    monkeypatch.setattr(settings, "llm_fallback_model", "claude-x", raising=False)
    route = build_agent_default_route(settings)
    assert route is not None
    assert route.task == AGENT_DEFAULT_TASK
    assert route.fallback_chain == [{"provider": "anthropic", "model": "claude-x"}]


async def test_bootstrap_platform_aggregates_seed_counts(monkeypatch) -> None:
    calls: list[str] = []

    async def _entity_types(session, org_id=None, specs=None) -> int:
        calls.append("entity_types")
        assert specs == [{"type_key": "product"}]
        return 2

    async def _kb_prompts(session) -> int:
        calls.append("kb_prompts")
        return 11

    async def _kb_answer(session) -> int:
        calls.append("kb_answer")
        return 1

    async def _agent_route(session) -> int:
        calls.append("agent_route")
        return 1

    async def _agent_card(session, org_id=None) -> int:
        calls.append("agent_card")
        return 1

    import nicekit.agent.seed as agent_seed
    import nicekit.kb.answer_seed as answer_seed
    import nicekit.kb.entity_types as entity_types
    import nicekit.kb.prompts_seed as prompts_seed

    monkeypatch.setattr(entity_types, "ensure_entity_types", _entity_types)
    monkeypatch.setattr(prompts_seed, "ensure_kb_prompts", _kb_prompts)
    monkeypatch.setattr(answer_seed, "ensure_kb_answer_route", _kb_answer)
    monkeypatch.setattr(agent_seed, "ensure_default_agent_card", _agent_card)
    monkeypatch.setattr(bootstrap, "ensure_agent_default_route", _agent_route)

    session = _Session()
    report = await bootstrap_platform(session, entity_type_specs=[{"type_key": "product"}])

    assert calls == [
        "entity_types",
        "kb_prompts",
        "kb_answer",
        "agent_route",
        "agent_card",
    ]
    assert report.as_dict() == {
        "entity_types": 2,
        "kb_prompts": 11,
        "kb_answer_route": 1,
        "agent_default_route": 1,
        "agent_cards": 1,
        "prompt_resource_issues": [],
    }
    assert session.commits == 1
