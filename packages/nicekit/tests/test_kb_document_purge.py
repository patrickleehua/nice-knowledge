from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from nicekit.kb import document_purge
from nicekit.kb.document_purge import (
    DocumentPurgeBlocked,
    DocumentPurgeExecutionError,
    DocumentPurgeForbidden,
    DocumentPurgeInventory,
    DocumentPurgePlanDrift,
    PurgeBlockerCode,
    PurgeObjectCandidate,
    build_document_purge_plan,
)
from nicekit.models.kb import (
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    KbDocumentOperation,
    SourceDocument,
)


def _inventory(**changes: object) -> DocumentPurgeInventory:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    base = DocumentPurgeInventory(
        org_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        lifecycle_status="withdrawn",
        withdrawn_at=now - timedelta(days=31),
        retention_days=30,
        legal_hold_active=False,
        reference_registry_complete=True,
        revision_ids=(uuid4(),),
        chunk_ids=(uuid4(), uuid4()),
        image_asset_ids=(uuid4(),),
        evidence_ids=(uuid4(),),
        exclusive_claim_ids=(uuid4(),),
        shared_claim_ids=(uuid4(),),
        exclusive_entity_ids=(uuid4(),),
        shared_entity_ids=(uuid4(),),
        exclusive_relation_count=1,
        shared_relation_count=1,
        ingest_run_ids=(uuid4(),),
        object_candidates=(PurgeObjectCandidate("org/kb/private-source.pdf", "source"),),
    )
    return replace(base, **changes)


def test_plan_is_deterministic_and_public_summary_redacts_object_keys() -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    inventory = _inventory()

    first = build_document_purge_plan(inventory, now=now)
    second = build_document_purge_plan(inventory, now=now)

    assert first.eligible is True
    assert first.plan_hash == second.plan_hash
    assert first.delete_counts["fact_claims"] == 1
    assert first.retain_counts == {
        "shared_fact_claims": 1,
        "shared_entities": 1,
        "shared_relations": 1,
        "exclusive_entities_for_gc": 1,
        "shared_object_keys": 0,
    }
    assert "private-source.pdf" not in str(first.as_public_dict())
    assert (
        document_purge._manifest_for_plan(first)["objects"][0]["key"] == "org/kb/private-source.pdf"
    )


def test_public_operation_detail_redacts_internal_deletion_manifest() -> None:
    plan = build_document_purge_plan(
        _inventory(),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )
    operation = KbDocumentOperation(
        id=uuid4(),
        org_id=plan.org_id,
        kb_id=plan.kb_id,
        document_id=plan.document_id,
        operation_type=DocumentOperationType.PURGE,
        status=DocumentOperationStatus.PROCESSING,
        stage="object_deletion",
        idempotency_key="purge-operation",
        reason="compliance request",
        impact_summary={"purge": document_purge._manifest_for_plan(plan)},
    )

    detail = document_purge.public_document_purge_operation_detail(operation)

    assert detail["object_status_counts"] == {"pending": 1}
    assert "private-source.pdf" not in str(detail)


def test_plan_returns_stable_retention_and_reference_blockers() -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    inventory = _inventory(
        withdrawn_at=now - timedelta(days=2),
        snapshot_reference_count=2,
        retrieval_reference_count=1,
        business_reference_count=3,
        feedback_or_citation_reference_count=4,
        pinned_entity_count=1,
        manual_or_pending_fact_count=2,
        media_reference_count=5,
        shared_object_key_count=1,
        cross_revision_content_reference_count=1,
    )

    plan = build_document_purge_plan(inventory, now=now)

    assert plan.eligible is False
    assert [blocker.code for blocker in plan.blockers] == sorted(
        (
            PurgeBlockerCode.RETENTION_PERIOD_ACTIVE,
            PurgeBlockerCode.KNOWLEDGE_SNAPSHOT_REFERENCE,
            PurgeBlockerCode.RETRIEVAL_SNAPSHOT_REFERENCE,
            PurgeBlockerCode.BUSINESS_ARTIFACT_REFERENCE,
            PurgeBlockerCode.FEEDBACK_OR_CITATION_REFERENCE,
            PurgeBlockerCode.PINNED_ENTITY_REFERENCE,
            PurgeBlockerCode.MANUAL_OR_PENDING_FACT_REFERENCE,
            PurgeBlockerCode.MEDIA_REFERENCE,
            PurgeBlockerCode.SHARED_OBJECT_KEY_REFERENCE,
            PurgeBlockerCode.CROSS_REVISION_CONTENT_REFERENCE,
        ),
        key=lambda code: code.value,
    )
    retention = next(
        blocker
        for blocker in plan.blockers
        if blocker.code == PurgeBlockerCode.RETENTION_PERIOD_ACTIVE
    )
    assert retention.retry_at == now + timedelta(days=28)


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"lifecycle_status": "active"}, PurgeBlockerCode.DOCUMENT_NOT_WITHDRAWN),
        ({"withdrawn_at": None}, PurgeBlockerCode.RETENTION_STATE_UNAVAILABLE),
        ({"legal_hold_active": True}, PurgeBlockerCode.LEGAL_HOLD_ACTIVE),
        (
            {"reference_registry_complete": False},
            PurgeBlockerCode.REFERENCE_REGISTRY_UNAVAILABLE,
        ),
    ],
)
def test_plan_fails_closed_for_lifecycle_and_governance_state(
    change: dict[str, object],
    expected_code: PurgeBlockerCode,
) -> None:
    plan = build_document_purge_plan(
        _inventory(**change),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert plan.eligible is False
    assert expected_code in {blocker.code for blocker in plan.blockers}


def test_submission_validation_requires_permission_reason_and_current_hash() -> None:
    plan = build_document_purge_plan(
        _inventory(),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    with pytest.raises(DocumentPurgeForbidden):
        document_purge._validate_submission(
            plan,
            authorized=False,
            reason="compliance request",
            expected_plan_hash=plan.plan_hash,
        )
    with pytest.raises(document_purge.DocumentPurgeError):
        document_purge._validate_submission(
            plan,
            authorized=True,
            reason=" ",
            expected_plan_hash=plan.plan_hash,
        )
    with pytest.raises(DocumentPurgePlanDrift):
        document_purge._validate_submission(
            plan,
            authorized=True,
            reason="compliance request",
            expected_plan_hash="0" * 64,
        )
    assert (
        document_purge._validate_submission(
            plan,
            authorized=True,
            reason="  compliance request  ",
            expected_plan_hash=plan.plan_hash,
        )
        == "compliance request"
    )


def test_submission_validation_rejects_blocked_plan_without_creating_work() -> None:
    plan = build_document_purge_plan(
        _inventory(snapshot_reference_count=1),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    with pytest.raises(DocumentPurgeBlocked) as exc_info:
        document_purge._validate_submission(
            plan,
            authorized=True,
            reason="retention cleanup",
            expected_plan_hash=plan.plan_hash,
        )

    assert exc_info.value.plan is plan


async def test_duplicate_submission_reuses_pending_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    kb_id = uuid4()
    document_id = uuid4()
    operation = KbDocumentOperation(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        document_id=document_id,
        operation_type=DocumentOperationType.PURGE,
        status=DocumentOperationStatus.PENDING,
        stage="object_deletion",
        idempotency_key=(
            f"document:{org_id.hex}:{document_id.hex}:purge:{document_purge.PURGE_PLAN_VERSION}"
        ),
        requested_by=uuid4(),
        reason="compliance request",
    )
    document = SourceDocument(
        id=document_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="source.pdf",
        object_key="org/kb/source.pdf",
        sha256="a" * 64,
        doc_type="general",
        lifecycle_status=DocumentLifecycleStatus.PURGE_PENDING,
    )

    class _Session:
        def __init__(self) -> None:
            self.values = iter((document, operation))

        async def scalar(self, _: object) -> object:
            return next(self.values)

    async def unexpected_preview(*_: object, **__: object) -> None:
        raise AssertionError("an accepted duplicate must not rebuild a purge plan")

    monkeypatch.setattr(
        document_purge,
        "preview_document_purge",
        unexpected_preview,
    )
    result = await document_purge.submit_document_purge(
        _Session(),  # type: ignore[arg-type]
        org_id=org_id,
        document_id=document_id,
        actor_id=uuid4(),
        reason="same intent",
        expected_plan_hash="stale-but-already-accepted",
        authorized=True,
    )

    assert result is operation


async def test_object_manifest_is_idempotent_for_deleted_and_missing_objects() -> None:
    objects = [
        {
            "key": "org/kb/already-done.pdf",
            "status": "deleted",
            "attempts": 1,
        },
        {
            "key": "org/kb/already-missing.pdf",
            "status": "pending",
            "attempts": 0,
        },
        {
            "key": "org/kb/present.pdf",
            "status": "pending",
            "attempts": 0,
        },
    ]
    present = {"org/kb/present.pdf"}
    deleted: list[str] = []
    persisted: list[list[str]] = []

    async def object_exists(key: str) -> bool:
        return key in present

    async def delete_object(key: str) -> None:
        deleted.append(key)
        present.remove(key)

    async def persist(items: list[dict[str, object]]) -> None:
        persisted.append([str(item["status"]) for item in items])

    await document_purge._delete_manifest_objects(
        objects,
        object_exists=object_exists,
        delete_object=delete_object,
        persist=persist,
    )

    assert deleted == ["org/kb/present.pdf"]
    assert [item["status"] for item in objects] == [
        "deleted",
        "already_missing",
        "deleted",
    ]
    assert len(persisted) == 2


async def test_object_manifest_records_failure_and_retries_same_item() -> None:
    objects = [
        {
            "key": "org/kb/source.pdf",
            "status": "pending",
            "attempts": 0,
        }
    ]
    present = {"org/kb/source.pdf"}
    fail_delete = True

    async def object_exists(key: str) -> bool:
        return key in present

    async def delete_object(key: str) -> None:
        if fail_delete:
            raise OSError("private provider detail")
        present.remove(key)

    async def persist(_: list[dict[str, object]]) -> None:
        return None

    with pytest.raises(DocumentPurgeExecutionError) as exc_info:
        await document_purge._delete_manifest_objects(
            objects,
            object_exists=object_exists,
            delete_object=delete_object,
            persist=persist,
        )
    assert exc_info.value.code == "PURGE_OBJECT_STORE_FAILED"
    assert objects[0]["status"] == "failed"
    assert objects[0]["error_code"] == "PURGE_OBJECT_STORE_FAILED"

    fail_delete = False
    await document_purge._delete_manifest_objects(
        objects,
        object_exists=object_exists,
        delete_object=delete_object,
        persist=persist,
    )

    assert objects[0]["status"] == "deleted"
    assert objects[0]["attempts"] == 2
    assert "error_code" not in objects[0]


def test_document_force_eligibility_bypasses_reference_and_retention_only() -> None:
    """文档强制清理:保留期/引用类可跳过;法律保留、共享对象、未撤回门禁照拦。"""
    from types import SimpleNamespace

    from nicekit.kb.document_purge import (
        FORCE_BYPASSABLE_BLOCKER_CODES,
        PurgeBlocker,
        PurgeBlockerCode,
        _eligible_with_force,
    )

    bypassable_plan = SimpleNamespace(
        eligible=False,
        blockers=(
            PurgeBlocker(PurgeBlockerCode.RETENTION_PERIOD_ACTIVE, 1),
            PurgeBlocker(PurgeBlockerCode.MANUAL_OR_PENDING_FACT_REFERENCE, 3),
            PurgeBlocker(PurgeBlockerCode.RETRIEVAL_SNAPSHOT_REFERENCE, 2),
        ),
    )
    assert _eligible_with_force(bypassable_plan, force=True) is True
    assert _eligible_with_force(bypassable_plan, force=False) is False

    guarded_plan = SimpleNamespace(
        eligible=False,
        blockers=(
            PurgeBlocker(PurgeBlockerCode.RETENTION_PERIOD_ACTIVE, 1),
            PurgeBlocker(PurgeBlockerCode.LEGAL_HOLD_ACTIVE, 1),
        ),
    )
    assert _eligible_with_force(guarded_plan, force=True) is False

    # 完整性门禁绝不在可跳过清单里
    assert PurgeBlockerCode.LEGAL_HOLD_ACTIVE not in FORCE_BYPASSABLE_BLOCKER_CODES
    assert (
        PurgeBlockerCode.SHARED_OBJECT_KEY_REFERENCE
        not in FORCE_BYPASSABLE_BLOCKER_CODES
    )
    assert (
        PurgeBlockerCode.DOCUMENT_NOT_WITHDRAWN not in FORCE_BYPASSABLE_BLOCKER_CODES
    )
    assert (
        PurgeBlockerCode.CROSS_REVISION_CONTENT_REFERENCE
        not in FORCE_BYPASSABLE_BLOCKER_CODES
    )
