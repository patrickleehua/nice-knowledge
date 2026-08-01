"""生命周期管理台三个只读聚合端点的契约测试。

完全仿照 test_kb_lifecycle_metrics_api.py:本地 FastAPI 应用挂载 router +
dependency_overrides + 假 session(不得用 with TestClient(app),lifespan 会卡死)。

假 session 按 SQL 涉及的表分派预置行:
- knowledge_bases → board 的库行;
- knowledge_base_lifecycle_operations → KB purge operation 行;
- kb_document_operations → 文档操作行。
settings 通过 monkeypatch kb_lifecycle_metrics.get_settings 固定,保证断言确定性。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb_lifecycle_metrics
from nicekit.kb.purge_due import PurgeDueCandidate
from nicekit.models.tenancy import Role

BOARD_URL = "/api/v1/kb/lifecycle/board"
OPERATIONS_URL = "/api/v1/kb/lifecycle/operations"
PURGE_DUE_URL = "/api/v1/kb/lifecycle/purge-due"


def _dt(value: str) -> datetime:
    """响应里的 ISO 时间串转回 datetime(pydantic 对 UTC 序列化为 Z 后缀)。"""
    return datetime.fromisoformat(value)


class _Settings:
    """端点只读取这两个配置项,固定值让保留期断言可复现。"""

    kb_document_purge_retention_days = 30
    kb_lifecycle_purge_worker_enabled = False


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _ConsoleSession:
    """只读假 session:按查询涉及的表返回预置行。

    注意分派顺序:document 队列查询同时 join knowledge_bases / source_documents,
    kb purge 队列查询同时 join knowledge_bases,必须先按操作表名分派。
    """

    def __init__(
        self,
        kb_rows: list[tuple] | None = None,
        kb_operation_rows: list[tuple] | None = None,
        document_operation_rows: list[tuple] | None = None,
    ) -> None:
        self.kb_rows = kb_rows or []
        self.kb_operation_rows = kb_operation_rows or []
        self.document_operation_rows = document_operation_rows or []

    async def execute(self, statement) -> _Result:
        sql = str(statement)
        if "kb_document_operations" in sql:
            return _Result(self.document_operation_rows)
        if "knowledge_base_lifecycle_operations" in sql:
            return _Result(self.kb_operation_rows)
        assert "knowledge_bases" in sql, f"unexpected query: {sql}"
        return _Result(self.kb_rows)


@pytest.fixture
def console_client(monkeypatch):
    org_id, user_id = uuid4(), uuid4()
    role = {"value": Role.ORG_ADMIN}
    holder = {"session": _ConsoleSession()}
    monkeypatch.setattr(kb_lifecycle_metrics, "get_settings", lambda: _Settings)

    app = FastAPI()
    app.include_router(kb_lifecycle_metrics.router, prefix="/api/v1")

    async def context_override() -> OrgContext:
        return OrgContext(user_id=user_id, org_id=org_id, role=role["value"])

    async def session_override():
        yield holder["session"]

    app.dependency_overrides[get_org_context] = context_override
    app.dependency_overrides[get_org_session] = session_override
    yield TestClient(app), holder, role, org_id


# ---- board ----------------------------------------------------------------


def test_board_empty_org(console_client) -> None:
    client, _, _, _ = console_client

    response = client.get(BOARD_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"] == []
    assert payload["purge_worker_enabled"] is False
    assert payload["retention_days"] == 30
    assert payload["generated_at"]


def test_board_archived_due_kb_sorted_first_with_latest_operation(
    console_client,
) -> None:
    client, holder, _, _ = console_client
    now = datetime.now(UTC)
    due_kb_id, active_kb_id, op_id = uuid4(), uuid4(), uuid4()
    archived_at = now - timedelta(days=40)
    requested_at = now - timedelta(days=2)
    holder["session"] = _ConsoleSession(
        kb_rows=[
            # (id, name, lifecycle_status, archived_at, purged_at, created_at)
            (
                active_kb_id,
                "活跃库",
                "active",
                None,
                None,
                now - timedelta(days=100),
            ),
            (
                due_kb_id,
                "到期归档库",
                "archived",
                archived_at,
                None,
                now - timedelta(days=200),
            ),
        ],
        kb_operation_rows=[
            # (kb_id, id, status, phase, created_at, completed_at, last_error_message)
            (
                due_kb_id,
                op_id,
                "failed",
                "delete_objects",
                requested_at,
                None,
                "boom",
            ),
        ],
    )

    response = client.get(BOARD_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["name"] for item in payload["items"]] == ["到期归档库", "活跃库"]

    due_item = payload["items"][0]
    assert due_item["kb_id"] == str(due_kb_id)
    assert due_item["lifecycle_status"] == "archived"
    assert due_item["purge_due"] is True
    assert _dt(due_item["archived_at"]) == archived_at
    assert due_item["purged_at"] is None
    # retention_due_at = archived_at + 30 天(_Settings 固定)
    assert _dt(due_item["retention_due_at"]) == archived_at + timedelta(days=30)
    latest = due_item["latest_operation"]
    assert latest["id"] == str(op_id)
    assert latest["status"] == "failed"
    assert latest["phase"] == "delete_objects"
    assert _dt(latest["requested_at"]) == requested_at
    assert latest["completed_at"] is None
    assert latest["last_error_message"] == "boom"

    active_item = payload["items"][1]
    assert active_item["purge_due"] is False
    assert active_item["retention_due_at"] is None
    assert active_item["latest_operation"] is None


# ---- operations -----------------------------------------------------------


def test_operations_merges_two_kinds_and_marks_retryable(console_client) -> None:
    client, holder, _, _ = console_client
    now = datetime.now(UTC)
    kb_id = uuid4()
    kb_op_retryable, kb_op_blocked = uuid4(), uuid4()
    doc_op_purge, doc_op_withdrawal, doc_op_reingestion = uuid4(), uuid4(), uuid4()
    holder["session"] = _ConsoleSession(
        kb_operation_rows=[
            # (id, kb_id, kb_name, status, phase, created_at, completed_at,
            #  last_error_message, last_error_code)
            (
                kb_op_retryable,
                kb_id,
                "旅行库",
                "dead_letter",
                "delete_objects",
                now - timedelta(minutes=1),
                None,
                "对象删除失败",
                "object_delete_failed",
            ),
            # 错误码不在可重试清单 → 不可重试(不得放宽 retry 端点准入)
            (
                kb_op_blocked,
                kb_id,
                "旅行库",
                "failed",
                "revalidate_and_quiesce",
                now - timedelta(minutes=5),
                None,
                "引用未清",
                "blockers_present",
            ),
        ],
        document_operation_rows=[
            # (id, kb_id, kb_name, filename, operation_type, status, stage,
            #  created_at, completed_at, last_error, retryable)
            (
                doc_op_purge,
                kb_id,
                "旅行库",
                "行程.pdf",
                "purge",
                "failed",
                "delete_objects",
                now - timedelta(minutes=2),
                None,
                "minio 超时",
                True,
            ),
            # purge 但行上 retryable 标记为假 → 不可重试
            (
                doc_op_withdrawal,
                kb_id,
                "旅行库",
                "酒店.docx",
                "withdrawal",
                "dead_letter",
                None,
                now - timedelta(minutes=3),
                None,
                "投递失败",
                False,
            ),
            # reingestion 的 retry 端点直接 409 → 恒不可重试
            (
                doc_op_reingestion,
                kb_id,
                "旅行库",
                "报价.xlsx",
                "reingestion",
                "failed",
                "parse",
                now - timedelta(minutes=4),
                None,
                "解析失败",
                True,
            ),
        ],
    )

    response = client.get(OPERATIONS_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["purge_worker_enabled"] is False
    # 合并后按 requested_at 倒序
    assert [item["id"] for item in payload["items"]] == [
        str(kb_op_retryable),
        str(doc_op_purge),
        str(doc_op_withdrawal),
        str(doc_op_reingestion),
        str(kb_op_blocked),
    ]

    by_id = {item["id"]: item for item in payload["items"]}
    kb_item = by_id[str(kb_op_retryable)]
    assert kb_item["kind"] == "kb_purge"
    assert kb_item["kb_name"] == "旅行库"
    assert kb_item["target"] == "旅行库"
    assert kb_item["phase"] == "delete_objects"
    assert kb_item["retryable"] is True
    assert by_id[str(kb_op_blocked)]["retryable"] is False

    purge_item = by_id[str(doc_op_purge)]
    assert purge_item["kind"] == "document"
    assert purge_item["target"] == "行程.pdf"
    # 操作类型并入 phase:类型:stage
    assert purge_item["phase"] == "purge:delete_objects"
    assert purge_item["last_error_message"] == "minio 超时"
    assert purge_item["retryable"] is True

    withdrawal_item = by_id[str(doc_op_withdrawal)]
    assert withdrawal_item["phase"] == "withdrawal"  # 无 stage 时只留类型
    assert withdrawal_item["retryable"] is True  # withdrawal 终态失败即可重试

    assert by_id[str(doc_op_reingestion)]["phase"] == "reingestion:parse"
    assert by_id[str(doc_op_reingestion)]["retryable"] is False


def test_operations_limit_applies_after_merge(console_client) -> None:
    client, holder, _, _ = console_client
    now = datetime.now(UTC)
    kb_id = uuid4()
    holder["session"] = _ConsoleSession(
        kb_operation_rows=[
            (
                uuid4(),
                kb_id,
                "旅行库",
                "completed",
                "completed",
                now - timedelta(minutes=index + 1),
                now - timedelta(minutes=index + 1) + timedelta(seconds=30),
                None,
                None,
            )
            for index in range(3)
        ],
        document_operation_rows=[
            (
                uuid4(),
                kb_id,
                "旅行库",
                f"doc-{index}.pdf",
                "withdrawal",
                "completed",
                None,
                now - timedelta(seconds=index),
                now,
                None,
                False,
            )
            for index in range(3)
        ],
    )

    response = client.get(OPERATIONS_URL, params={"limit": 4})

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 4
    # 文档操作更新(秒级)排在 KB 操作(分钟级)之前
    assert [item["kind"] for item in items] == [
        "document",
        "document",
        "document",
        "kb_purge",
    ]


def test_operations_limit_out_of_range_rejected(console_client) -> None:
    client, _, _, _ = console_client

    assert client.get(OPERATIONS_URL, params={"limit": 0}).status_code == 422
    assert client.get(OPERATIONS_URL, params={"limit": 201}).status_code == 422


# ---- purge-due ------------------------------------------------------------


def test_purge_due_passes_through_find_purge_due(console_client, monkeypatch) -> None:
    client, _, _, org_id = console_client
    now = datetime.now(UTC)
    kb_id = uuid4()
    candidate = PurgeDueCandidate(
        kb_id=kb_id,
        org_id=org_id,
        name="到期归档库",
        archived_at=now - timedelta(days=45),
        due_at=now - timedelta(days=15),
        plan_hash="a" * 64,
        preview_complete=True,
        # blocker 带 identifiers / resolution_hint 等内部键,响应只准留 code+count
        blockers=(
            {
                "code": "LEGAL_HOLD_ACTIVE",
                "count": 2,
                "identifiers": [str(uuid4())],
                "resolution_hint": "先解除法务保留",
            },
        ),
    )
    captured = {}

    async def fake_find_purge_due(session):
        captured["session"] = session
        return [candidate]

    monkeypatch.setattr(
        kb_lifecycle_metrics, "find_purge_due", fake_find_purge_due
    )

    response = client.get(PURGE_DUE_URL)

    assert response.status_code == 200, response.text
    assert "session" in captured  # 确认端点把会话透传给 find_purge_due
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kb_id"] == str(kb_id)
    assert item["name"] == "到期归档库"
    assert _dt(item["archived_at"]) == candidate.archived_at
    assert _dt(item["due_at"]) == candidate.due_at
    assert item["plan_hash"] == "a" * 64
    assert item["preview_complete"] is True
    # 内部键(identifiers / resolution_hint)必须被裁掉,只留 code+count
    assert item["blockers"] == [{"code": "LEGAL_HOLD_ACTIVE", "count": 2}]


# ---- 权限 -----------------------------------------------------------------


@pytest.mark.parametrize("url", [BOARD_URL, OPERATIONS_URL, PURGE_DUE_URL])
def test_non_admin_role_is_forbidden(console_client, url) -> None:
    client, _, role, _ = console_client
    role["value"] = Role.MEMBER

    response = client.get(url)

    assert response.status_code == 403
