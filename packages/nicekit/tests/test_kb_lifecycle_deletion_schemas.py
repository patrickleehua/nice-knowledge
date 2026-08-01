from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nicekit.api.v1.kb import (
    KnowledgeBaseDeletionPreviewOut,
    KnowledgeBaseLifecycleAuditDetail,
    KnowledgeBaseLifecycleAuditEvent,
    KnowledgeBaseLifecycleBlockerCode,
    KnowledgeBaseLifecycleBlockerOut,
    KnowledgeBaseLifecycleErrorCode,
    KnowledgeBaseLifecycleErrorOut,
    KnowledgeBaseLifecycleOperationOut,
)


def test_preview_schema_uses_stable_public_blockers() -> None:
    blocker = KnowledgeBaseLifecycleBlockerOut(
        code=KnowledgeBaseLifecycleBlockerCode.EXTERNAL_SCOPE_REFERENCE,
        count=1,
        identifiers=["workspace:example"],
        resolution_hint="确认由宿主解除外部业务引用后重试",
    )
    preview = KnowledgeBaseDeletionPreviewOut(
        version="kb-deletion-v1",
        kb_id=uuid4(),
        kb_name="供应商知识库",
        owner_org_id=uuid4(),
        lifecycle_status="active",
        consumption_epoch=3,
        complete=True,
        allowed_actions=["archive"],
        blockers=[blocker],
        impact_counts={"external_references": 1},
        plan_hash="a" * 64,
        expires_at=datetime.now(UTC),
    )
    assert preview.blockers[0].code == "EXTERNAL_SCOPE_REFERENCE"
    assert preview.plan_hash == "a" * 64

    with pytest.raises(ValidationError):
        KnowledgeBaseDeletionPreviewOut(
            version="kb-deletion-v1",
            kb_id=uuid4(),
            kb_name="供应商知识库",
            owner_org_id=uuid4(),
            lifecycle_status="active",
            consumption_epoch=0,
            complete=True,
            plan_hash="not-a-hash",
            expires_at=None,
        )


def test_operation_schema_exposes_only_sanitized_error_contract() -> None:
    operation = KnowledgeBaseLifecycleOperationOut(
        id=uuid4(),
        kb_id=uuid4(),
        operation_type="purge",
        status="failed",
        phase="delete_objects",
        attempt_count=2,
        impact_summary={"objects": 3},
        last_error_code="object_store_unavailable",
        error_message="对象存储暂时不可用",
        retryable=True,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=None,
    )
    payload = operation.model_dump()
    assert payload["last_error_code"] == "object_store_unavailable"
    assert "object_key" not in payload

    with pytest.raises(ValidationError):
        KnowledgeBaseLifecycleOperationOut(
            **{
                **payload,
                "last_error_code": "provider;secret=raw",
            }
        )


def test_error_and_audit_schemas_keep_stable_codes() -> None:
    error = KnowledgeBaseLifecycleErrorOut(
        code=KnowledgeBaseLifecycleErrorCode.DELETION_PLAN_STALE,
        message="删除计划已变化，请重新预检",
    )
    assert error.code == "deletion_plan_stale"

    detail = KnowledgeBaseLifecycleAuditDetail(
        event_code=KnowledgeBaseLifecycleAuditEvent.ARCHIVED,
        previous_lifecycle_status="active",
        lifecycle_status="archived",
        previous_consumption_epoch=4,
        consumption_epoch=5,
        plan_hash="b" * 64,
        reason="业务资料已停用",
    )
    assert detail.event_code == "kb.lifecycle.archived"
    assert detail.consumption_epoch == 5
