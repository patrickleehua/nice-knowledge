"""P2c 四个 agent 相关 router 的接口层专测(chat / agent_permissions / memory / icron)。

**不用 `TestClient(app)`**:TF 踩过坑——它会触发 lifespan,而 lifespan 里的 DB
后台任务在测试环境会把进程挂住。这里一律现搭一个只挂本 router 的轻量 app,
用 httpx 的 ASGITransport 直接打,依赖注入全部 override 成替身。

覆盖重点是本波真正改动的部分:scope 泛化(project_id/customer_id → scope_type/
scope_id)、角色字符串化、工具目录改读 ToolRegistry;不重复测 P2a/P2b 已覆盖的
服务层逻辑。
"""

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nicekit.api import deps
from nicekit.api.v1 import agent_permissions as permissions_api
from nicekit.api.v1 import chat as chat_api
from nicekit.api.v1 import icron as icron_api
from nicekit.api.v1 import memory as memory_api
from nicekit.domain.agent_permission import PermissionScope
from nicekit.llm.registry import ResolvedRoute
from nicekit.models.chat import ChatSessionOriginMode
from nicekit.models.llm_provider import LlmProvider
from nicekit.models.memory import MemoryItem, MemoryScope, register_memory_scopes

ORG_ID = uuid4()
USER_ID = uuid4()


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, results: list | None = None) -> None:
        self._results = list(results or [])
        self.committed = 0

    async def execute(self, _statement):
        return self._results.pop(0) if self._results else _FakeResult([])

    def add(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _client(router, *, session: _FakeSession | None = None, role: str = "member"):
    """只挂一个 router 的轻量 app(不建 lifespan,不碰真实 DB/Redis)。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    ctx = deps.OrgContext(user_id=USER_ID, org_id=ORG_ID, role=role)
    app.dependency_overrides[deps.get_org_context] = lambda: ctx
    app.dependency_overrides[deps.get_org_session] = lambda: session or _FakeSession()
    app.dependency_overrides[deps.get_session] = lambda: session or _FakeSession()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# chat:请求契约(scope 泛化)
# ---------------------------------------------------------------------------


def test_session_create_requires_scope_pair() -> None:
    assert chat_api.SessionCreate().scope_id is None
    ok = chat_api.SessionCreate(scope_type="ticket", scope_id=uuid4())
    assert ok.scope_type == "ticket"
    with pytest.raises(ValidationError):
        chat_api.SessionCreate(scope_type="ticket")
    with pytest.raises(ValidationError):
        chat_api.SessionCreate(scope_id=uuid4())


def test_session_create_rejects_legacy_business_fields() -> None:
    """extra=forbid:旧前端传 project_id/customer_id 必须显式 422,而不是被静默吞掉。"""
    with pytest.raises(ValidationError):
        chat_api.SessionCreate(project_id=uuid4())
    with pytest.raises(ValidationError):
        chat_api.SessionCreate(customer_id=uuid4())


def test_message_context_switches_scope_as_a_pair() -> None:
    ctx = chat_api.MessageContextIn(scope_type="ticket", scope_id=uuid4())
    assert ctx.scope_type == "ticket"
    with pytest.raises(ValidationError):
        chat_api.MessageContextIn(scope_id=uuid4())


def test_message_in_allows_only_one_interaction_action() -> None:
    with pytest.raises(ValidationError):
        chat_api.MessageIn(
            content="",
            confirm={"tool_call_id": "c1", "approved": True},
            continue_plan={"step_id": "s1"},
        )
    with pytest.raises(ValidationError):
        chat_api.MessageIn(content="带内容", continue_plan={"step_id": "s1"})


def test_message_in_normalizes_runtime_tools() -> None:
    body = chat_api.MessageIn(content="查一下", runtime_tools=[" web_search ", "web_search", ""])
    assert body.runtime_tools == ["web_search"]


def test_session_out_projects_scope_and_redacts_pending() -> None:
    scope_id = uuid4()
    payload = {
        "id": uuid4(),
        "agent_card_id": uuid4(),
        "scope_type": "ticket",
        "scope_id": scope_id,
        "origin_mode": ChatSessionOriginMode.SCOPED,
        "title": "新对话",
        "status": "active",
        "pending_confirmation": {
            "tool_call_id": "c1",
            "name": "danger",
            "input": {"secret": "raw"},
            "display_input": {"secret": "***"},
        },
        "pending_user_input": None,
        "plan": None,
        "created_at": None,
        "updated_at": None,
    }
    out = chat_api.SessionOut.model_validate(payload)
    assert out.scope_type == "ticket" and out.scope_id == scope_id
    # 可执行原始参数绝不出接口
    assert out.pending_confirmation["input"] == {"secret": "***"}


async def test_thinking_levels_endpoint_is_settings_driven() -> None:
    async with _client(chat_api.router) as client:
        response = await client.get("/api/v1/chat/thinking-levels")
    assert response.status_code == 200
    body = response.json()
    assert body["levels"] and "value" in body["levels"][0]
    assert "default" in body


async def test_runtime_tools_endpoint_reads_the_tool_registry(monkeypatch) -> None:
    import nicekit.agent.builtin_tools  # noqa: F401 - A2:显式 import 才注册

    card = _resolved_card(tools=("kb_search",))

    async def _resolve_card(_session, _org_id, _card_id):
        return card

    monkeypatch.setattr(chat_api, "resolve_card", _resolve_card)
    async with _client(chat_api.router) as client:
        response = await client.get("/api/v1/chat/runtime-tools")
    assert response.status_code == 200
    body = response.json()
    assert body["notice"].startswith("仅本轮生效")
    names = {item["name"] for item in body["tools"]}
    # 候选只含只读、可临时开启、且不在卡白名单里的工具
    assert "kb_search" not in names
    assert "kb_list" in names
    assert "skills_list" not in names  # runtime_grantable=False


async def test_runtime_tools_endpoint_404_when_card_missing(monkeypatch) -> None:
    async def _resolve_card(_session, _org_id, _card_id):
        raise LookupError("agent 卡不存在或已停用")

    monkeypatch.setattr(chat_api, "resolve_card", _resolve_card)
    async with _client(chat_api.router) as client:
        response = await client.get("/api/v1/chat/runtime-tools")
    assert response.status_code == 404


def _resolved_card(**values):
    from nicekit.agent.service import ResolvedAgentCard

    defaults = {
        "card_id": uuid4(),
        "version_id": uuid4(),
        "name": "assistant",
        "system_prompt": "",
        "model_task": "agent.default",
        "max_turns": 8,
        "timeout_seconds": 300,
        "tools": (),
        "mcp_bindings": (),
        "skills": (),
    }
    return ResolvedAgentCard(**{**defaults, **values})


async def test_runtime_model_catalog_uses_configured_providers(monkeypatch) -> None:
    """搬自 TF tests/test_chat_runtime_models.py:只有能真正构造出来的模型才可选。"""

    async def fake_route(_session, _task, _org_id):
        return ResolvedRoute(
            primary_provider="openai",
            primary_model="route-model",
            fallback_chain=[{"provider": "custom", "model": "fallback-model"}],
            max_tokens=4096,
            timeout_seconds=60,
        )

    monkeypatch.setattr(chat_api, "resolve_route", fake_route)
    monkeypatch.setattr(
        chat_api,
        "env_provider_credentials",
        lambda: {"openai": ("openai", "env-key", "")},
    )
    session = _FakeSession(
        [
            _FakeResult(
                [
                    LlmProvider(name="openai", protocol="openai", models=["inventory-model"]),
                    LlmProvider(
                        name="custom",
                        protocol="openai",
                        api_key="custom-key",
                        models=["custom-model"],
                    ),
                    LlmProvider(
                        name="disabled",
                        protocol="openai",
                        api_key="disabled-key",
                        enabled=False,
                        models=["hidden-model"],
                    ),
                    LlmProvider(name="unconfigured", protocol="openai", models=["hidden"]),
                ]
            )
        ]
    )
    catalog = await chat_api._runtime_models_for_task(session, ORG_ID, "agent.default")

    assert (catalog.default.provider, catalog.default.model) == ("openai", "route-model")
    assert {(option.provider, option.model) for option in catalog.models} == {
        ("openai", "route-model"),
        ("custom", "fallback-model"),
    }


# ---------------------------------------------------------------------------
# agent_permissions:三档 scope
# ---------------------------------------------------------------------------


def test_org_policy_default_max_scope_is_resource_not_project() -> None:
    body = permissions_api.OrganizationPolicyUpdateIn(
        expected_version=0,
        default_profile="request_approval",
        allowed_profiles=["request_approval"],
    )
    assert body.max_scope is PermissionScope.RESOURCE

    empty = permissions_api._policy_out(None)
    assert empty.max_scope is PermissionScope.RESOURCE
    assert empty.version == 0


def test_session_permission_state_carries_scope_pair() -> None:
    scope_id = uuid4()
    state = permissions_api.SessionPermissionStateOut(
        session_id=uuid4(),
        revision=1,
        profile="request_approval",
        scope=PermissionScope.RESOURCE,
        expires_at=None,
        active_run=False,
        scope_type="ticket",
        scope_id=scope_id,
        custom_rules={},
        policy_snapshot={},
        profile_options=[],
        organization=permissions_api.OrganizationConstraintsOut(
            policy_id=None,
            policy_version=0,
            is_enabled=True,
            shadow_evaluation=False,
            max_scope=PermissionScope.RESOURCE,
            max_grant_ttl_seconds=28800,
            max_full_access_ttl_seconds=3600,
            reviewer_enabled=False,
            reviewer_eligible_categories=[],
            reviewer_eligible_tools=[],
            denied_categories=[],
            denied_tools=[],
            user_required_categories=[],
            user_required_tools=[],
        ),
        grants=[],
        pending_decision=None,
        reviewer_overrides=[],
    )
    assert state.scope_type == "ticket" and state.scope_id == scope_id


async def test_tool_catalog_lists_registry_metadata() -> None:
    import nicekit.agent.builtin_tools  # noqa: F401 - A2:显式 import 才注册

    async with _client(permissions_api.router, role="org_admin") as client:
        response = await client.get("/api/v1/org/agent-permissions/tool-catalog")
    assert response.status_code == 200
    items = {item["name"]: item for item in response.json()}
    assert "kb_search" in items
    assert items["kb_search"]["effect"] == "read"
    assert items["kb_search"]["label"]


async def test_tool_catalog_rejects_plain_member() -> None:
    async with _client(permissions_api.router, role="member") as client:
        response = await client.get("/api/v1/org/agent-permissions/tool-catalog")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# memory:scope 泛化 + 展示名解析扩展点
# ---------------------------------------------------------------------------


async def test_memory_scopes_endpoint_reflects_registered_scopes() -> None:
    register_memory_scopes("ticket")
    async with _client(memory_api.router) as client:
        response = await client.get("/api/v1/memory/scopes")
    body = response.json()
    assert "org" in body["scopes"] and "ticket" in body["scopes"]
    assert "preference" in body["types"]
    assert "rejected" in body["statuses"]


async def test_memory_list_rejects_unregistered_scope() -> None:
    async with _client(memory_api.router) as client:
        response = await client.get("/api/v1/memory", params={"scope": "no_such_scope"})
    assert response.status_code == 422


async def test_memory_scope_label_resolver_is_pluggable() -> None:
    item = MemoryItem(
        id=uuid4(),
        org_id=ORG_ID,
        scope="ticket",
        scope_ref_id="t-1",
        memory_type="fact",
        title="标题",
        content="正文",
        source="memory_write",
        confidence=0.9,
    )

    async def _resolver(_session, _org_id, refs):
        assert refs == [("ticket", "t-1")]
        return {"t-1": "工单 #1"}

    try:
        memory_api.set_scope_label_resolver(_resolver)
        labels = await memory_api._scope_labels(_FakeSession(), ORG_ID, [item])
        assert labels == {"t-1": "工单 #1"}
    finally:
        memory_api.set_scope_label_resolver(None)
    assert await memory_api._scope_labels(_FakeSession(), ORG_ID, [item]) == {}


async def test_memory_scope_label_failure_never_breaks_the_list() -> None:
    item = MemoryItem(
        id=uuid4(),
        org_id=ORG_ID,
        scope="ticket",
        scope_ref_id="t-1",
        memory_type="fact",
        title="标题",
        content="正文",
        source="memory_write",
        confidence=0.9,
    )

    async def _boom(_session, _org_id, _refs):
        raise RuntimeError("宿主查表炸了")

    try:
        memory_api.set_scope_label_resolver(_boom)
        assert await memory_api._scope_labels(_FakeSession(), ORG_ID, [item]) == {}
    finally:
        memory_api.set_scope_label_resolver(None)


async def test_memory_org_scope_needs_no_label_lookup() -> None:
    item = MemoryItem(
        id=uuid4(),
        org_id=ORG_ID,
        scope=MemoryScope.ORG.value,
        scope_ref_id=None,
        memory_type="constraint",
        title="标题",
        content="正文",
        source="memory_extraction",
        confidence=0.9,
    )

    async def _never(_session, _org_id, _refs):  # pragma: no cover - 不该被调用
        raise AssertionError("org 范围没有指向对象,不该走解析")

    try:
        memory_api.set_scope_label_resolver(_never)
        assert await memory_api._scope_labels(_FakeSession(), ORG_ID, [item]) == {}
    finally:
        memory_api.set_scope_label_resolver(None)


# ---------------------------------------------------------------------------
# icron:scope 泛化 + 调度预览
# ---------------------------------------------------------------------------


def test_icron_task_in_requires_scope_pair() -> None:
    ok = icron_api.TaskIn(
        name="每日巡检",
        instruction="检查一下",
        schedule_kind="interval",
        interval_seconds=3600,
        scope_type="ticket",
        scope_id=uuid4(),
    )
    assert ok.scope_type == "ticket"
    with pytest.raises(ValidationError):
        icron_api.TaskIn(
            name="x", instruction="y", schedule_kind="manual", scope_type="ticket"
        )
    with pytest.raises(ValidationError):
        icron_api.TaskIn(
            name="x", instruction="y", schedule_kind="manual", project_id=uuid4()
        )


async def test_icron_schedule_preview_returns_upcoming_runs() -> None:
    async with _client(icron_api.router) as client:
        response = await client.post(
            "/api/v1/icron/schedule/preview",
            json={"schedule_kind": "interval", "interval_seconds": 3600},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["upcoming"]) == 5
    assert body["schedule_summary"]


async def test_icron_schedule_preview_rejects_bad_schedule() -> None:
    async with _client(icron_api.router) as client:
        response = await client.post(
            "/api/v1/icron/schedule/preview",
            json={"schedule_kind": "cron", "cron_expr": "not a cron"},
        )
    assert response.status_code == 422


async def test_icron_manual_schedule_has_no_upcoming() -> None:
    async with _client(icron_api.router) as client:
        response = await client.post(
            "/api/v1/icron/schedule/preview", json={"schedule_kind": "manual"}
        )
    assert response.json()["upcoming"] == []
