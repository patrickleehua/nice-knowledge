"""HTTP contracts for privileged, reference-aware document purge operations."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb
from nicekit.kb.document_purge import (
    DocumentPurgePlan,
    DocumentPurgePlanDrift,
    PurgeObjectCandidate,
)
from nicekit.models.kb import (
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    KbDocumentOperation,
    OutboxEvent,
    OutboxStatus,
    SourceDocument,
)
from nicekit.models.tenancy import AuditLog, Role

# TF 经验:不 import 组装好的 app(也不用 with TestClient(app)——会触发
# lifespan 卡死)。轻量 FastAPI 只挂本用例需要的 router,依赖全部由
# dependency_overrides 假注入。
app = FastAPI()
app.include_router(kb.router, prefix="/api/v1")


def _purge_plan(*, org_id, document_id) -> DocumentPurgePlan:
    return DocumentPurgePlan(
        version="kb-document-purge/v1",
        org_id=org_id,
        kb_id=uuid4(),
        document_id=document_id,
        plan_hash="a" * 64,
        eligible=True,
        blockers=(),
        delete_counts={"objects": 1, "revisions": 1},
        retain_counts={"shared_entities": 2},
        retention_deadline=datetime.now(UTC),
        revision_ids=(uuid4(),),
        chunk_ids=(uuid4(),),
        image_asset_ids=(),
        evidence_ids=(),
        exclusive_claim_ids=(),
        ingest_run_ids=(),
        object_candidates=(
            PurgeObjectCandidate(
                key="private/org/source-document.pdf",
                category="original",
            ),
        ),
    )


def _purge_operation(
    *,
    org_id,
    document_id,
    status=DocumentOperationStatus.PENDING,
    retryable=False,
) -> KbDocumentOperation:
    return KbDocumentOperation(
        id=uuid4(),
        org_id=org_id,
        kb_id=uuid4(),
        document_id=document_id,
        operation_type=DocumentOperationType.PURGE,
        status=status,
        stage="planned",
        idempotency_key=f"purge:{document_id}",
        requested_by=uuid4(),
        reason="compliance retention elapsed",
        impact_summary={
            "purge": {
                "version": "kb-document-purge/v1",
                "plan_hash": "a" * 64,
                "phase": "planned",
                "summary": {
                    "delete_counts": {"objects": 1},
                    "retain_counts": {"shared_entities": 2},
                },
                "objects": [
                    {
                        "key": "private/org/source-document.pdf",
                        "status": "pending",
                    }
                ],
            }
        },
        attempts=3,
        retryable=retryable,
        last_error_code=("PURGE_OBJECT_DELETE_FAILED" if retryable else None),
        last_error=("Permanent purge did not complete." if retryable else None),
        failed_at=(datetime.now(UTC) if retryable else None),
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def purge_client():
    org_id, user_id = uuid4(), uuid4()
    role = {"value": Role.ORG_ADMIN}

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.scalar_results: list[object] = []
            self.commits = 0
            self.rollbacks = 0

        def add(self, value) -> None:
            self.added.append(value)

        def add_all(self, values) -> None:
            self.added.extend(values)

        async def scalar(self, _statement):
            if self.scalar_results:
                return self.scalar_results.pop(0)
            return next(
                (value for value in reversed(self.added) if isinstance(value, OutboxEvent)),
                None,
            )

        async def execute(self, _statement):
            class Scalars:
                @staticmethod
                def all():
                    return []

            class Result:
                @staticmethod
                def scalars():
                    return Scalars()

                @staticmethod
                def all():
                    return []

            return Result()

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = FakeSession()

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
        yield TestClient(app), session, role, org_id, user_id
    finally:
        app.dependency_overrides.pop(get_org_context, None)
        app.dependency_overrides.pop(get_org_session, None)


def test_purge_preview_returns_only_safe_plan_fields(
    purge_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _, org_id, _ = purge_client
    document_id = uuid4()
    plan = _purge_plan(org_id=org_id, document_id=document_id)

    async def preview(_session, **kwargs):
        assert kwargs == {"org_id": org_id, "document_id": document_id}
        return plan

    monkeypatch.setattr(kb, "preview_document_purge", preview)
    response = client.get(f"/api/v1/kb/documents/{document_id}/purge-preview")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "version": "kb-document-purge/v1",
        "document_id": str(document_id),
        "plan_hash": "a" * 64,
        "eligible": True,
        "blockers": [],
        "delete_counts": {"objects": 1, "revisions": 1},
        "retain_counts": {"shared_entities": 2},
        "retention_deadline": plan.retention_deadline.isoformat().replace("+00:00", "Z"),
    }
    assert "private/org" not in response.text
    assert str(plan.revision_ids[0]) not in response.text


def test_document_legal_hold_is_admin_audited_and_blocks_by_persistent_state(
    purge_client,
) -> None:
    client, session, role, org_id, user_id = purge_client
    document = SourceDocument(
        id=uuid4(),
        org_id=org_id,
        kb_id=uuid4(),
        filename="source.pdf",
        object_key="org/kb/source.pdf",
        sha256="a" * 64,
        doc_type="general",
        lifecycle_status=DocumentLifecycleStatus.WITHDRAWN,
        created_at=datetime.now(UTC),
    )
    session.scalar_results.append(document)
    applied = client.post(
        f"/api/v1/kb/documents/{document.id}/legal-hold",
        json={"reason": "  regulatory case 42  "},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["legal_hold_active"] is True
    assert document.legal_hold_by == user_id
    assert document.legal_hold_reason == "regulatory case 42"

    role["value"] = Role.MEMBER
    denied = client.post(
        f"/api/v1/kb/documents/{document.id}/legal-hold/release",
        json={"reason": "case closed"},
    )
    assert denied.status_code == 403

    role["value"] = Role.ORG_ADMIN
    session.scalar_results.append(document)
    released = client.post(
        f"/api/v1/kb/documents/{document.id}/legal-hold/release",
        json={"reason": "case closed"},
    )
    assert released.status_code == 200, released.text
    assert released.json()["legal_hold_active"] is False
    assert document.legal_hold_at is None

    audits = [value for value in session.added if isinstance(value, AuditLog)]
    assert [audit.action for audit in audits] == [
        "kb.document.legal_hold.applied",
        "kb.document.legal_hold.released",
    ]


def test_purge_preview_and_submit_require_admin(
    purge_client,
) -> None:
    client, _, role, _, _ = purge_client
    role["value"] = Role.MEMBER
    document_id = uuid4()

    assert client.get(f"/api/v1/kb/documents/{document_id}/purge-preview").status_code == 403
    assert (
        client.post(
            f"/api/v1/kb/documents/{document_id}/purge",
            json={
                "expected_plan_hash": "a" * 64,
                "reason": "retention elapsed",
                "confirm_irreversible": True,
            },
        ).status_code
        == 403
    )


def test_purge_submit_validates_hash_reason_and_confirmation(
    purge_client,
) -> None:
    client, _, _, _, _ = purge_client
    endpoint = f"/api/v1/kb/documents/{uuid4()}/purge"

    assert (
        client.post(
            endpoint,
            json={
                "expected_plan_hash": "A" * 64,
                "reason": "retention elapsed",
                "confirm_irreversible": True,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint,
            json={
                "expected_plan_hash": "a" * 64,
                "reason": "   ",
                "confirm_irreversible": True,
            },
        ).status_code
        == 422
    )
    unconfirmed = client.post(
        endpoint,
        json={
            "expected_plan_hash": "a" * 64,
            "reason": "retention elapsed",
            "confirm_irreversible": False,
        },
    )
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["detail"]["code"] == "PURGE_CONFIRMATION_REQUIRED"


def test_duplicate_purge_submit_reuses_operation_and_dispatch(
    purge_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session, _, org_id, user_id = purge_client
    document_id = uuid4()
    operation = _purge_operation(org_id=org_id, document_id=document_id)

    async def submit(_session, **kwargs):
        assert kwargs["actor_id"] == user_id
        assert kwargs["authorized"] is True
        return operation

    monkeypatch.setattr(kb, "submit_document_purge", submit)
    body = {
        "expected_plan_hash": "a" * 64,
        "reason": "  compliance retention elapsed  ",
        "confirm_irreversible": True,
    }
    first = client.post(f"/api/v1/kb/documents/{document_id}/purge", json=body)
    second = client.post(f"/api/v1/kb/documents/{document_id}/purge", json=body)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] == str(operation.id)
    dispatches = [value for value in session.added if isinstance(value, OutboxEvent)]
    assert len(dispatches) == 1
    assert dispatches[0].payload["operation_id"] == str(operation.id)
    assert "private/org/source-document.pdf" not in first.text


def test_plan_drift_returns_latest_safe_plan(
    purge_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _, org_id, _ = purge_client
    document_id = uuid4()
    plan = _purge_plan(org_id=org_id, document_id=document_id)

    async def submit(*_args, **_kwargs):
        raise DocumentPurgePlanDrift(plan)

    monkeypatch.setattr(kb, "submit_document_purge", submit)
    response = client.post(
        f"/api/v1/kb/documents/{document_id}/purge",
        json={
            "expected_plan_hash": "b" * 64,
            "reason": "retention elapsed",
            "confirm_irreversible": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PURGE_PLAN_DRIFT"
    assert response.json()["detail"]["plan"]["plan_hash"] == "a" * 64
    assert "private/org" not in response.text


def test_purge_status_is_admin_only_and_redacts_manifest(
    purge_client,
) -> None:
    client, session, role, org_id, _ = purge_client
    operation = _purge_operation(org_id=org_id, document_id=uuid4())
    session.scalar_results.append(operation)

    response = client.get(f"/api/v1/kb/document-operations/{operation.id}")

    assert response.status_code == 200, response.text
    assert response.json()["attempts"] == 3
    assert response.json()["impact_summary"]["object_status_counts"] == {"pending": 1}
    assert "private/org" not in response.text

    role["value"] = Role.MEMBER
    session.scalar_results.append(operation)
    denied = client.get(f"/api/v1/kb/document-operations/{operation.id}")
    assert denied.status_code == 403


def test_failed_purge_retry_reuses_operation_and_creates_new_dispatch(
    purge_client,
) -> None:
    client, session, _, org_id, _ = purge_client
    operation = _purge_operation(
        org_id=org_id,
        document_id=uuid4(),
        status=DocumentOperationStatus.FAILED,
        retryable=True,
    )
    prior = OutboxEvent(
        id=uuid4(),
        org_id=org_id,
        kb_id=operation.kb_id,
        aggregate_type="source_document",
        aggregate_id=operation.document_id,
        event_type="document.purge.requested",
        idempotency_key=(f"document:{org_id.hex}:{operation.document_id.hex}:purge-dispatch:0"),
        payload={
            "document_id": str(operation.document_id),
            "operation_id": str(operation.id),
        },
        status=OutboxStatus.PUBLISHED,
        published_at=datetime.now(UTC),
    )
    session.scalar_results.extend([operation, operation, prior])

    response = client.post(f"/api/v1/kb/document-operations/{operation.id}/retry")

    assert response.status_code == 202, response.text
    assert response.json()["id"] == str(operation.id)
    assert response.json()["status"] == "pending"
    assert response.json()["attempts"] == 3
    dispatches = [value for value in session.added if isinstance(value, OutboxEvent)]
    assert len(dispatches) == 1
    assert dispatches[0].idempotency_key.endswith("purge-dispatch:1")
    assert dispatches[0].payload["operation_id"] == str(operation.id)
    assert operation.impact_summary["purge_dispatch_generation"] == 1
