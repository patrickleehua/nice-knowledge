"""scope 泛化后的表结构与词表(MIGRATION-PLAN §5.4"4 条硬 FK 泛化")。

铁律:SDK 的表不得引用宿主业务表。这里逐张核对——会话 / 授权 / 定时任务
都只保留 scope_type + scope_id 两列且**没有外键**;记忆范围只内置 org,
其余靠注册。
"""

import pytest
from sqlmodel import SQLModel

from nicekit.models.agent_permission import AgentPermissionGrant  # noqa: F401
from nicekit.models.chat import (
    SCOPE_TYPE_MAX_LENGTH,
    ChatSession,
    ChatSessionOriginMode,
)
from nicekit.models.icron import IcronTask  # noqa: F401
from nicekit.models.memory import (
    MemoryItem,
    MemoryScope,
    MemoryScopeError,
    known_memory_scopes,
    normalize_memory_scope,
    register_memory_scopes,
    reset_registered_memory_scopes,
)

SCOPED_TABLES = ("chat_sessions", "agent_permission_grants", "icron_tasks")


@pytest.fixture(autouse=True)
def _clean_memory_scopes():
    reset_registered_memory_scopes()
    yield
    reset_registered_memory_scopes()


@pytest.mark.parametrize("table_name", SCOPED_TABLES)
def test_scope_columns_replace_business_foreign_keys(table_name: str) -> None:
    table = SQLModel.metadata.tables[table_name]

    assert {"scope_type", "scope_id"}.issubset(table.c.keys())
    assert "project_id" not in table.c
    assert "customer_id" not in table.c
    # scope_* 两列一律无外键:引用宿主业务表会把 SDK schema 钉死在某个宿主上
    assert not table.c["scope_type"].foreign_keys
    assert not table.c["scope_id"].foreign_keys
    assert table.c["scope_type"].type.length == SCOPE_TYPE_MAX_LENGTH
    assert table.c["scope_type"].nullable and table.c["scope_id"].nullable


def test_no_sdk_table_references_a_host_business_table() -> None:
    forbidden = {"projects", "customers", "itineraries", "quotes", "render_jobs"}
    referenced = {
        key.column.table.name
        for table in SQLModel.metadata.tables.values()
        for key in table.foreign_keys
    }
    assert forbidden.isdisjoint(referenced)


def test_origin_mode_vocabulary_is_general_or_scoped() -> None:
    assert [mode.value for mode in ChatSessionOriginMode] == ["general", "scoped"]
    constraint = next(
        item
        for item in SQLModel.metadata.tables["chat_sessions"].constraints
        if item.name == "ck_chat_sessions_origin_mode"
    )
    assert "'general'" in str(constraint.sqltext)
    assert "'scoped'" in str(constraint.sqltext)


def test_new_session_defaults_to_an_unbound_general_conversation() -> None:
    from uuid import uuid4

    chat = ChatSession(org_id=uuid4(), user_id=uuid4(), agent_card_id=uuid4())

    assert chat.origin_mode is ChatSessionOriginMode.GENERAL
    assert chat.scope_type is None and chat.scope_id is None


def test_grant_scope_binding_check_targets_scope_id() -> None:
    table = SQLModel.metadata.tables["agent_permission_grants"]
    constraint = next(
        item
        for item in table.constraints
        if item.name == "ck_agent_permission_grants_scope_binding"
    )
    assert "scope_id IS NOT NULL" in str(constraint.sqltext)


# ---------------------------------------------------------------- memory


def test_memory_scope_vocabulary_only_builtin_is_org() -> None:
    assert [scope.value for scope in MemoryScope] == ["org"]
    assert known_memory_scopes() == frozenset({"org"})
    assert MemoryItem.model_fields["scope"].default == "org"


def test_registered_scopes_extend_the_vocabulary() -> None:
    register_memory_scopes("case", "account")

    assert known_memory_scopes() == frozenset({"org", "case", "account"})
    assert normalize_memory_scope("case") == "case"
    # 幂等:重复注册与注册内置项都是 no-op
    register_memory_scopes("case", "org")
    assert known_memory_scopes() == frozenset({"org", "case", "account"})


def test_unknown_scope_fails_closed() -> None:
    with pytest.raises(MemoryScopeError, match="未知记忆范围"):
        normalize_memory_scope("case")
    with pytest.raises(MemoryScopeError, match="未知记忆范围"):
        normalize_memory_scope(None)


@pytest.mark.parametrize("name", ["Case", "1case", "case-x", "x" * 21, ""])
def test_illegal_scope_names_are_rejected_at_registration(name: str) -> None:
    with pytest.raises(MemoryScopeError, match="非法记忆范围名"):
        register_memory_scopes(name)


def test_reset_clears_only_registered_scopes() -> None:
    register_memory_scopes("case")
    reset_registered_memory_scopes()
    assert known_memory_scopes() == frozenset({"org"})
