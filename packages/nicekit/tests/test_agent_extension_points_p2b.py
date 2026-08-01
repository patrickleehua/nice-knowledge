"""P2b 新增扩展点的专测(MIGRATION-PLAN §4)。

覆盖本波把 TF 硬依赖拆成注入点的五处:
- `capabilities.notify.Notifier`(替代直连 Notification 表);
- `agent.memory.ScopeResolver`(替代 import 业务 customer 服务);
- MCP 凭证解密与工具能力元数据推导(fail-closed 走 write);
- `compression.SUMMARY_TASK` / `memory.MEMORY_TASK` 可配(默认 agent.default);
- `icron.NOTIFY_LINK` 可配 + 定时任务按 scope_type/scope_id 建会话。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.agent import compression, icron
from nicekit.agent import memory as mem
from nicekit.agent.mcp_manager import McpToolSpec, decrypt_mapping, mcp_tool_permission
from nicekit.agent.sub_agents import scope_marker
from nicekit.capabilities import notify as notify_module
from nicekit.core.secretbox import get_secret_box
from nicekit.domain.agent_permission import ToolEffect
from nicekit.models.chat import ChatSessionOriginMode
from nicekit.models.icron import IcronScheduleKind, IcronTask, IcronTaskStatus
from nicekit.models.memory import register_memory_scopes

ORG_ID = uuid4()
USER_ID = uuid4()


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify(self, session, **kwargs):
        self.calls.append(kwargs)
        return []


async def test_notify_facade_delegates_to_the_registered_notifier() -> None:
    recorder = _RecordingNotifier()
    notify_module.set_notifier(recorder)
    try:
        await notify_module.notify(
            None,
            org_id=ORG_ID,
            user_ids=[USER_ID],
            kind="demo.event",
            title="标题",
            body="正文",
            link="/app/demo",
        )
    finally:
        notify_module.set_notifier(None)
    assert recorder.calls[0]["kind"] == "demo.event"
    assert recorder.calls[0]["user_ids"] == [USER_ID]
    # 复位后回到默认 SQL 实现
    assert isinstance(notify_module.get_notifier(), notify_module.SqlNotifier)


async def test_sql_notifier_dedupes_recipients_and_does_not_commit() -> None:
    class _Session:
        def __init__(self):
            self.added: list = []
            self.commits = 0

        def add_all(self, rows):
            self.added.extend(rows)

        async def commit(self):  # pragma: no cover - 触发即失败
            self.commits += 1

    session = _Session()
    rows = await notify_module.SqlNotifier().notify(
        session,
        org_id=ORG_ID,
        user_ids=[USER_ID, USER_ID],
        kind="demo.event",
        title="标题",
        body="正文",
        email=False,
    )
    assert len(rows) == 1  # 保序去重
    assert session.commits == 0  # 事务边界属于调用方


async def test_sql_notifier_returns_empty_without_recipients() -> None:
    assert (
        await notify_module.SqlNotifier().notify(
            None, org_id=ORG_ID, user_ids=[], kind="k", title="t", body="b"
        )
        == []
    )


# ---------------------------------------------------------------------------
# ScopeResolver
# ---------------------------------------------------------------------------


async def test_scope_resolver_defaults_to_org_only() -> None:
    mem.set_scope_resolver(None)
    assert await mem.resolve_scope_refs(None, ORG_ID, uuid4()) == {}


async def test_scope_resolver_drops_unregistered_and_empty_scopes() -> None:
    register_memory_scopes("ticket")

    async def resolver(_session, _org_id, _session_id):
        return {"ticket": "T-1", "unregistered": "X", "org": "ignored", "ticket2": ""}

    mem.set_scope_resolver(resolver)
    try:
        refs = await mem.resolve_scope_refs(None, ORG_ID, uuid4())
    finally:
        mem.set_scope_resolver(None)
    # 未注册范围与空 ref 被丢弃;org 天然没有指向对象
    assert refs == {"ticket": "T-1"}


async def test_scope_resolver_failure_never_breaks_the_conversation() -> None:
    async def broken(_session, _org_id, _session_id):
        raise RuntimeError("宿主查询挂了")

    mem.set_scope_resolver(broken)
    try:
        assert await mem.resolve_scope_refs(None, ORG_ID, uuid4()) == {}
    finally:
        mem.set_scope_resolver(None)


def test_memory_scope_label_falls_back_to_the_scope_name() -> None:
    assert mem.scope_label("org") == "全组织"
    assert mem.scope_label("ticket") == "ticket"
    mem.register_scope_label("ticket", "该工单")
    try:
        assert mem.scope_label("ticket") == "该工单"
    finally:
        mem._SCOPE_LABEL.pop("ticket", None)


# ---------------------------------------------------------------------------
# 作用域注入(sub_agents)
# ---------------------------------------------------------------------------


def test_scope_marker_is_empty_without_a_binding() -> None:
    assert scope_marker(None) == ""
    assert scope_marker(SimpleNamespace(scope_type=None, scope_id=None)) == ""
    # 半绑定状态同样不注入(scope_type 有值但没有 id)
    assert scope_marker(SimpleNamespace(scope_type="ticket", scope_id=None)) == ""


def test_scope_marker_renders_generic_scope_line() -> None:
    scope_id = uuid4()
    marker = scope_marker(SimpleNamespace(scope_type="ticket", scope_id=scope_id))
    assert marker == f"[当前作用域 ticket:{scope_id}]"


# ---------------------------------------------------------------------------
# MCP:凭证解密 + 能力元数据
# ---------------------------------------------------------------------------


def test_decrypt_mapping_restores_ciphertext_and_passes_plaintext_through() -> None:
    box = get_secret_box()
    raw = {"Authorization": box.encrypt("Bearer secret"), "X-Plain": "keep"}
    got = decrypt_mapping(raw, server_name="demo", field_name="headers")
    assert got == {"Authorization": "Bearer secret", "X-Plain": "keep"}


def test_decrypt_mapping_drops_undecryptable_entries(monkeypatch) -> None:
    """解不开的键宁可缺一个 header,也不把密文原样发出去。"""
    from nicekit.core import secretbox

    class _Broken:
        def decrypt(self, token: str) -> str:
            if token.startswith("enc:"):
                raise secretbox.SecretBoxError("key 不匹配")
            return token

    monkeypatch.setattr(
        "nicekit.agent.mcp_manager.get_secret_box", lambda: _Broken()
    )
    got = decrypt_mapping(
        {"bad": "enc:zzz", "good": "ok"}, server_name="demo", field_name="env"
    )
    assert got == {"good": "ok"}


def _server(**kwargs):
    return SimpleNamespace(name="demo", **kwargs)


def test_mcp_tool_permission_is_write_by_default() -> None:
    permission = mcp_tool_permission(_server(), "anything")
    assert permission.effect is ToolEffect.WRITE


def test_mcp_tool_permission_reads_protocol_annotations() -> None:
    spec = McpToolSpec(name="lookup", description="", schema={}, read_only_hint=True)
    assert mcp_tool_permission(_server(), spec).effect is ToolEffect.READ
    written = McpToolSpec(name="write", description="", schema={}, read_only_hint=False)
    assert mcp_tool_permission(_server(), written).effect is ToolEffect.WRITE


def test_mcp_tool_permission_falls_back_to_server_tool_metadata() -> None:
    server = _server(tool_metadata={"lookup": {"effect": "read"}})
    spec = McpToolSpec(name="lookup", description="", schema={}, read_only_hint=None)
    assert mcp_tool_permission(server, spec).effect is ToolEffect.READ
    other = McpToolSpec(name="mutate", description="", schema={}, read_only_hint=None)
    assert mcp_tool_permission(server, other).effect is ToolEffect.WRITE


# ---------------------------------------------------------------------------
# LLM task 名可配
# ---------------------------------------------------------------------------


def test_summary_and_memory_tasks_default_to_agent_default() -> None:
    assert compression.SUMMARY_TASK == "agent.default"
    assert mem.MEMORY_TASK == "agent.default"


def test_summary_and_memory_tasks_are_configurable() -> None:
    compression.set_summary_task("host.summary")
    mem.set_memory_task("host.memory")
    try:
        assert compression.SUMMARY_TASK == "host.summary"
        assert mem.MEMORY_TASK == "host.memory"
    finally:
        compression.set_summary_task(None)
        mem.set_memory_task(None)
    assert compression.SUMMARY_TASK == compression.DEFAULT_SUMMARY_TASK
    assert mem.MEMORY_TASK == mem.DEFAULT_MEMORY_TASK


def test_summary_prompt_keeps_the_precision_rule_without_domain_wording() -> None:
    prompt = compression.SUMMARY_SYSTEM_PROMPT
    assert "精确引用" in prompt  # "保留关键事实与数字"的语义不许丢
    for word in ("旅行社", "酒店", "报价", "行程"):
        assert word not in prompt


# ---------------------------------------------------------------------------
# icron:通知链接可配 + 作用域建会话
# ---------------------------------------------------------------------------


def test_notify_link_is_configurable() -> None:
    task_id = uuid4()
    assert icron.notification_link(task_id) == f"/app/icron?task={task_id}"
    icron.set_notify_link("/console/jobs")
    try:
        assert icron.notification_link(task_id) == f"/console/jobs?task={task_id}"
    finally:
        icron.set_notify_link(None)
    assert icron.NOTIFY_LINK == icron.DEFAULT_NOTIFY_LINK


@pytest.mark.parametrize(
    ("scope_id", "expected_mode"),
    [
        (None, ChatSessionOriginMode.GENERAL),
        (uuid4(), ChatSessionOriginMode.SCOPED),
    ],
)
async def test_icron_run_session_uses_scope_fields(
    monkeypatch, scope_id, expected_mode
) -> None:
    """定时任务开的会话按 scope_type/scope_id 绑定,origin_mode 随之推导。"""
    import sys
    import types

    task = IcronTask(
        id=uuid4(),
        org_id=ORG_ID,
        created_by=USER_ID,
        name="每日巡检",
        instruction="汇总昨日异常。",
        schedule_kind=IcronScheduleKind.CRON.value,
        cron_expr="0 9 * * *",
        status=IcronTaskStatus.ACTIVE.value,
        scope_type="ticket" if scope_id else None,
        scope_id=scope_id,
    )

    card_id = uuid4()
    service_stub = types.ModuleType("nicekit.agent.service")

    async def resolve_card(_session, _org_id, _card_id):
        return SimpleNamespace(card_id=card_id)

    service_stub.resolve_card = resolve_card
    monkeypatch.setitem(sys.modules, "nicekit.agent.service", service_stub)

    from nicekit.agent.permissions import management, policy
    from nicekit.domain.agent_permission import PermissionProfile, PermissionScope

    async def defaults(_session, *, org_id, user_id, scope_id):
        return PermissionProfile.REQUEST_APPROVAL, PermissionScope.SESSION, {}, None

    async def effective(_session, *, org_id, user_id, chat_session):
        return SimpleNamespace(policy_id=None, policy_version=None)

    monkeypatch.setattr(management, "new_session_permission_defaults", defaults)
    monkeypatch.setattr(policy, "load_effective_policy", effective)
    monkeypatch.setattr(policy, "policy_snapshot_payload", lambda _e: {})

    class _Session:
        def __init__(self):
            self.added: list = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            return None

    session = _Session()
    chat = await icron._create_run_session(session, task)

    assert chat.scope_type == task.scope_type
    assert chat.scope_id == scope_id
    assert chat.origin_mode is expected_mode
    assert chat.title == "每日巡检"
