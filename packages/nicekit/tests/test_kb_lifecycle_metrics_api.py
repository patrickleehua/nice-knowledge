"""GET /kb/lifecycle/metrics 聚合契约。

路由尚未在 router.py 注册(主控接线),故测试用本地 FastAPI 应用挂载该 router;
客户端构造沿用现有 KB 接口测试写法:TestClient(app) + dependency_overrides + 假
session(不得用 with TestClient(app),lifespan 会卡死)。

假 session 按 SQL 语句涉及的表分派预置行:knowledge_bases 分组计数行、
audit_logs 审计行、knowledge_base_lifecycle_operations 操作行。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb_lifecycle_metrics
from nicekit.models.kb import (
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecyclePhase,
    KnowledgeBaseLifecycleStatus,
)
from nicekit.models.tenancy import Role

METRICS_URL = "/api/v1/kb/lifecycle/metrics"


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _MetricsSession:
    """只读假 session:按查询涉及的表返回预置行。"""

    def __init__(
        self,
        kb_rows: list[tuple] | None = None,
        audit_rows: list[tuple] | None = None,
        operation_rows: list[tuple] | None = None,
    ) -> None:
        self.kb_rows = kb_rows or []
        self.audit_rows = audit_rows or []
        self.operation_rows = operation_rows or []

    async def execute(self, statement) -> _Result:
        sql = str(statement)
        if "audit_logs" in sql:
            return _Result(self.audit_rows)
        if "knowledge_base_lifecycle_operations" in sql:
            return _Result(self.operation_rows)
        assert "knowledge_bases" in sql, f"unexpected query: {sql}"
        return _Result(self.kb_rows)


@pytest.fixture
def metrics_client():
    org_id, user_id = uuid4(), uuid4()
    role = {"value": Role.ORG_ADMIN}
    holder = {"session": _MetricsSession()}

    app = FastAPI()
    app.include_router(kb_lifecycle_metrics.router, prefix="/api/v1")

    async def context_override() -> OrgContext:
        return OrgContext(user_id=user_id, org_id=org_id, role=role["value"])

    async def session_override():
        yield holder["session"]

    app.dependency_overrides[get_org_context] = context_override
    app.dependency_overrides[get_org_session] = session_override
    yield TestClient(app), holder, role, org_id


def test_empty_org_returns_all_zero_metrics(metrics_client) -> None:
    client, _, _, _ = metrics_client

    response = client.get(METRICS_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kb_status_counts"] == {
        "active": 0,
        "archived": 0,
        "purge_pending": 0,
        "purged": 0,
        "total": 0,
    }
    assert payload["lifecycle_activity"] == {
        "archives_30d": 0,
        "restores_30d": 0,
        "archives_90d": 0,
        "restores_90d": 0,
    }
    operations = payload["purge_operations"]
    assert operations["total"] == 0
    assert operations["failed_count"] == 0
    assert operations["dead_letter_count"] == 0
    assert operations["completed_count"] == 0
    assert operations["avg_completion_seconds"] is None
    # 状态/阶段分布必须给全零枚举键,前端不做缺键兜底
    assert operations["status_counts"] == {
        status.value: 0 for status in KnowledgeBaseLifecycleOperationStatus
    }
    assert operations["phase_counts"] == {
        phase.value: 0 for phase in KnowledgeBaseLifecyclePhase
    }


def test_aggregates_archive_restore_and_completed_operations(
    metrics_client,
) -> None:
    client, holder, _, _ = metrics_client
    now = datetime.now(UTC)
    started = now - timedelta(days=3)
    holder["session"] = _MetricsSession(
        kb_rows=[
            # DB 返回裸字符串,内存对象可能是 StrEnum,两种都要吃得下
            (KnowledgeBaseLifecycleStatus.ACTIVE, 2),
            ("archived", 1),
            ("purged", 1),
        ],
        audit_rows=[
            ("kb.archive", now - timedelta(days=5)),
            ("kb.archive", now - timedelta(days=45)),
            ("kb.restore", now - timedelta(days=10)),
            ("kb.restore", now - timedelta(days=80)),
        ],
        operation_rows=[
            ("completed", "completed", started, started + timedelta(seconds=100)),
            ("completed", "completed", started, started + timedelta(seconds=200)),
            (
                KnowledgeBaseLifecycleOperationStatus.FAILED,
                KnowledgeBaseLifecyclePhase.DELETE_OBJECTS,
                started,
                None,
            ),
            ("dead_letter", "revalidate_and_quiesce", started, None),
        ],
    )

    response = client.get(METRICS_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kb_status_counts"] == {
        "active": 2,
        "archived": 1,
        "purge_pending": 0,
        "purged": 1,
        "total": 4,
    }
    assert payload["lifecycle_activity"] == {
        "archives_30d": 1,
        "restores_30d": 1,
        "archives_90d": 2,
        "restores_90d": 2,
    }
    operations = payload["purge_operations"]
    assert operations["total"] == 4
    assert operations["status_counts"] == {
        "pending": 0,
        "processing": 0,
        "completed": 2,
        "failed": 1,
        "dead_letter": 1,
    }
    assert operations["phase_counts"] == {
        "revalidate_and_quiesce": 1,
        "delete_objects": 1,
        "cleanup_metadata": 0,
        "finalize_audit_shell": 0,
        "completed": 2,
    }
    assert operations["failed_count"] == 1
    assert operations["dead_letter_count"] == 1
    assert operations["completed_count"] == 2
    assert operations["avg_completion_seconds"] == 150.0


def test_non_admin_role_is_forbidden(metrics_client) -> None:
    client, _, role, _ = metrics_client
    role["value"] = Role.MEMBER

    response = client.get(METRICS_URL)

    assert response.status_code == 403
