"""HTTP contracts for knowledge-base archive, deletion, and purge."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb as kb_api
from nicekit.kb.lifecycle import KnowledgeBaseLifecycleError
from nicekit.models.kb import (
    KnowledgeBase,
    KnowledgeBaseLifecycleOperation,
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecyclePhase,
    KnowledgeBaseLifecycleStatus,
)
from nicekit.models.tenancy import Role

# TF 经验:不 import 组装好的 app(也不用 with TestClient(app)——会触发
# lifespan 卡死)。轻量 FastAPI 只挂本用例需要的 router,依赖全部由
# dependency_overrides 假注入。
app = FastAPI()
app.include_router(kb_api.router, prefix="/api/v1")


class _Scalars:
    def all(self) -> list[object]:
        return []


class _Result:
    def scalars(self) -> _Scalars:
        return _Scalars()


class _Session:
    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base
        self.commits = 0
        self.rollbacks = 0

    async def get(self, model, identity, **_kwargs):
        if model is KnowledgeBase and identity == self.knowledge_base.id:
            return self.knowledge_base
        return None

    async def execute(self, _statement):
        return _Result()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, _value) -> None:
        return None


@pytest.fixture
def lifecycle_client():
    org_id, user_id, kb_id = uuid4(), uuid4(), uuid4()
    role = {"value": Role.ORG_ADMIN}
    knowledge_base = KnowledgeBase(
        id=kb_id,
        org_id=org_id,
        name="供应商知识库",
        created_by=user_id,
        created_at=datetime.now(UTC),
    )
    session = _Session(knowledge_base)

    async def context_override() -> OrgContext:
        return OrgContext(
            user_id=user_id,
            org_id=org_id,
            role=role["value"],
        )

    async def session_override():
        yield session

    app.dependency_overrides[get_org_context] = context_override
    app.dependency_overrides[get_org_session] = session_override
    try:
        yield TestClient(app), session, role, knowledge_base, user_id
    finally:
        app.dependency_overrides.pop(get_org_context, None)
        app.dependency_overrides.pop(get_org_session, None)


def _preview(knowledge_base: KnowledgeBase) -> dict:
    return {
        "version": "kb-deletion-v1",
        "kb_id": str(knowledge_base.id),
        "name": knowledge_base.name,
        "owner_org_id": str(knowledge_base.org_id),
        "lifecycle_status": "active",
        "consumption_epoch": 0,
        "complete": True,
        "allowed_actions": ["hard_delete"],
        "hard_delete_eligible": True,
        "plan_hash": "a" * 64,
        "expires_at": datetime.now(UTC).isoformat(),
        "counts": {
            "tables": {},
            "external_references": 0,
            "business_references": 0,
            "feedback_references": 0,
            "pinned_entities": 0,
            "objects": 0,
            "shared_objects": 0,
            "legal_holds": 0,
            "manual_facts": 0,
            "ingest_runs": 0,
            "document_operations": 0,
            "embedding_reindex_jobs": 0,
        },
        "external_reference_count": 0,
        "blockers": [],
        "latest_operation": None,
        "_manifest": {
            "objects": ["private/secret-source.pdf"],
            "content_tables": {},
        },
    }


def _operation(knowledge_base: KnowledgeBase) -> KnowledgeBaseLifecycleOperation:
    return KnowledgeBaseLifecycleOperation(
        id=uuid4(),
        org_id=knowledge_base.org_id,
        kb_id=knowledge_base.id,
        plan_hash="b" * 64,
        idempotency_key="purge-request-1",
        requested_by=knowledge_base.created_by,
        status=KnowledgeBaseLifecycleOperationStatus.PENDING,
        phase=KnowledgeBaseLifecyclePhase.REVALIDATE_AND_QUIESCE,
        manifest_summary={"object_count": 1},
        created_at=datetime.now(UTC),
    )


def test_preview_uses_safe_frontend_contract(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _, knowledge_base, _ = lifecycle_client

    async def preview(_session, **kwargs):
        assert kwargs == {
            "org_id": knowledge_base.org_id,
            "kb_id": knowledge_base.id,
        }
        return _preview(knowledge_base)

    monkeypatch.setattr(kb_api, "preview_knowledge_base_deletion", preview)
    response = client.get(
        f"/api/v1/kb/bases/{knowledge_base.id}/deletion-preview"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kb_name"] == knowledge_base.name
    assert payload["allowed_actions"] == ["empty_delete"]
    assert payload["owner_org_id"] == str(knowledge_base.org_id)
    assert "private/secret-source.pdf" not in response.text
    assert "_manifest" not in payload


def test_preview_exposes_registered_reference_blockers(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _, knowledge_base, _ = lifecycle_client
    preview_payload = _preview(knowledge_base)
    preview_payload["allowed_actions"] = ["archive"]
    preview_payload["blockers"] = [
        {
            "code": "protected_knowledge_snapshot_references",
            "count": 1,
            "message": "受保护知识快照仍被引用",
            "ids": [str(uuid4())],
        },
        {
            "code": "feedback_or_citation_references",
            "count": 2,
            "message": "反馈或引用记录仍在使用",
        },
    ]

    async def preview(_session, **_kwargs):
        return preview_payload

    monkeypatch.setattr(kb_api, "preview_knowledge_base_deletion", preview)
    response = client.get(
        f"/api/v1/kb/bases/{knowledge_base.id}/deletion-preview"
    )

    assert response.status_code == 200, response.text
    assert [item["code"] for item in response.json()["blockers"]] == [
        "KNOWLEDGE_SNAPSHOT_REFERENCE",
        "FEEDBACK_OR_CITATION_REFERENCE",
    ]


def test_preview_serializes_retention_blocker_with_retry_at(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归:archived+保留期 blocker 携带 retry_at,响应模型 extra="forbid"
    曾因缺该字段把预检响应打成 500(前端只看到 Failed to fetch)。"""
    client, _, _, knowledge_base, _ = lifecycle_client
    preview_payload = _preview(knowledge_base)
    preview_payload["allowed_actions"] = ["restore"]
    retry_at = datetime(2026, 8, 28, 11, 21, 15, tzinfo=UTC).isoformat()
    preview_payload["blockers"] = [
        {
            "code": "retention_period_active",
            "count": 1,
            "message": "归档保留期未过",
            "retry_at": retry_at,
        }
    ]

    async def preview(_session, **_kwargs):
        return preview_payload

    monkeypatch.setattr(kb_api, "preview_knowledge_base_deletion", preview)
    response = client.get(
        f"/api/v1/kb/bases/{knowledge_base.id}/deletion-preview"
    )

    assert response.status_code == 200, response.text
    blocker = response.json()["blockers"][0]
    assert blocker["code"] == "RETENTION_PERIOD_ACTIVE"
    assert blocker["retry_at"] == retry_at


def test_archive_restore_and_purge_accept_frontend_request_contract(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session, _, knowledge_base, user_id = lifecycle_client
    received: dict[str, dict] = {}

    async def archive(_session, **kwargs):
        received["archive"] = kwargs
        knowledge_base.lifecycle_status = KnowledgeBaseLifecycleStatus.ARCHIVED
        return knowledge_base

    async def restore(_session, **kwargs):
        received["restore"] = kwargs
        knowledge_base.lifecycle_status = KnowledgeBaseLifecycleStatus.ACTIVE
        return knowledge_base

    operation = _operation(knowledge_base)

    async def purge(_session, **kwargs):
        received["purge"] = kwargs
        return operation

    monkeypatch.setattr(kb_api, "archive_knowledge_base", archive)
    monkeypatch.setattr(kb_api, "restore_knowledge_base", restore)
    monkeypatch.setattr(kb_api, "submit_knowledge_base_purge", purge)
    monkeypatch.setattr(
        kb_api,
        "_require_own_kb",
        AsyncMock(side_effect=AssertionError("lifecycle service owns the row lock")),
    )

    archived = client.post(
        f"/api/v1/kb/bases/{knowledge_base.id}/archive",
        json={
            "expected_plan_hash": "a" * 64,
            "reason": "资料停止使用",
            "acknowledge_external_unlink": True,
        },
    )
    restored = client.post(f"/api/v1/kb/bases/{knowledge_base.id}/restore")
    purged = client.post(
        f"/api/v1/kb/bases/{knowledge_base.id}/purge",
        headers={"Idempotency-Key": "purge-request-1"},
        json={
            "expected_plan_hash": "b" * 64,
            "confirmation_name": knowledge_base.name,
            "reason": "保留期已结束",
        },
    )

    assert archived.status_code == 200, archived.text
    assert restored.status_code == 200, restored.text
    assert purged.status_code == 202, purged.text
    assert received["archive"] == {
        "org_id": knowledge_base.org_id,
        "kb_id": knowledge_base.id,
        "actor_id": user_id,
        "reason": "资料停止使用",
        "expected_plan_hash": "a" * 64,
        "acknowledge_external_unlink": True,
    }
    assert received["restore"]["reason"] == "manual restore"
    assert received["purge"]["name_confirmation"] == knowledge_base.name
    assert received["purge"]["idempotency_key"] == "purge-request-1"
    assert session.commits == 3


def test_empty_delete_requires_and_normalizes_if_match(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session, _, knowledge_base, _ = lifecycle_client
    received: dict[str, str] = {}

    async def delete_empty(_session, **kwargs):
        received["plan_hash"] = kwargs["expected_plan_hash"]

    monkeypatch.setattr(
        kb_api,
        "hard_delete_empty_knowledge_base",
        delete_empty,
    )
    missing = client.delete(f"/api/v1/kb/bases/{knowledge_base.id}")
    deleted = client.delete(
        f"/api/v1/kb/bases/{knowledge_base.id}",
        headers={"If-Match": '"%s"' % ("c" * 64)},
    )

    assert missing.status_code == 428
    assert deleted.status_code == 204, deleted.text
    assert received["plan_hash"] == '"%s"' % ("c" * 64)
    assert session.commits == 1


def test_lifecycle_actions_reject_non_admin_and_non_owner(
    lifecycle_client,
) -> None:
    client, session, role, knowledge_base, _ = lifecycle_client
    role["value"] = Role.MEMBER
    assert (
        client.get(
            f"/api/v1/kb/bases/{knowledge_base.id}/deletion-preview"
        ).status_code
        == 403
    )

    role["value"] = Role.ORG_ADMIN
    session.knowledge_base.org_id = uuid4()
    response = client.get(
        f"/api/v1/kb/bases/{knowledge_base.id}/deletion-preview"
    )
    assert response.status_code == 403


def test_plan_stale_error_is_structured_and_rolls_back(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session, _, knowledge_base, _ = lifecycle_client

    async def archive(*_args, **_kwargs):
        raise KnowledgeBaseLifecycleError(
            "deletion_plan_stale",
            "删除影响已经变化",
            preview=_preview(knowledge_base),
        )

    monkeypatch.setattr(kb_api, "archive_knowledge_base", archive)
    response = client.post(
        f"/api/v1/kb/bases/{knowledge_base.id}/archive",
        json={
            "expected_plan_hash": "a" * 64,
            "reason": "资料停止使用",
            "acknowledge_external_unlink": False,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "deletion_plan_stale"
    assert response.json()["detail"]["preview"]["kb_name"] == knowledge_base.name
    assert session.rollbacks == 1


def test_archived_list_query_uses_public_parameter_name() -> None:
    parameters = app.openapi()["paths"]["/api/v1/kb/bases"]["get"]["parameters"]
    assert any(
        parameter["name"] == "lifecycle_status"
        and parameter["schema"]["default"] == "active"
        for parameter in parameters
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader",
    [
        kb_api.list_document_revisions,
        kb_api.get_document,
        kb_api.get_document_ingestion_status,
        kb_api.list_document_chunks,
    ],
)
async def test_document_id_reads_fail_before_content_when_kb_is_not_active(
    reader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id, kb_id = uuid4(), uuid4()
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id=doc_id, kb_id=kb_id))
    session.execute = AsyncMock()
    gate = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="知识库不存在或不可用")
    )
    monkeypatch.setattr(kb_api, "_require_visible_active_kb", gate)

    with pytest.raises(HTTPException) as exc_info:
        await reader(doc_id, session)

    assert exc_info.value.status_code == 404
    gate.assert_awaited_once_with(session, kb_id)
    session.execute.assert_not_awaited()
