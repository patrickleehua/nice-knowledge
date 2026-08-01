from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nicekit.api.v1 import kb as kb_api
from nicekit.models.kb import CanonicalEntity, FactClaim, FactReviewStatus


def _scalar_result(rows: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = rows
    return result


async def test_canonical_entity_output_maps_complete_governance_state() -> None:
    org_id = uuid4()
    kb_id = uuid4()
    entity_id = uuid4()
    snapshot_id = uuid4()
    actor_id = uuid4()
    survivor_id = uuid4()
    changed_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    entity = CanonicalEntity(
        id=entity_id,
        org_id=org_id,
        kb_id=kb_id,
        entity_type="hotel",
        canonical_name="Retired Hotel",
        metadata_={"source": "governance-test"},
        support_status="unsupported",
        support_status_reason="merged_into_survivor",
        support_status_changed_at=changed_at,
        support_status_snapshot_id=snapshot_id,
        unsupported_at=changed_at,
        is_pinned=True,
        pinned_at=changed_at,
        pinned_by=actor_id,
        pin_reason="retain_for_audit",
        merged_into_entity_id=survivor_id,
        merged_at=changed_at,
        merged_by=actor_id,
        merge_reason="duplicate_registry_identity",
        created_at=changed_at,
        updated_at=changed_at,
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_scalar_result([]))

    [output] = await kb_api._canonical_entity_outputs(session, [entity])

    assert output.model_dump(mode="json") == {
        "id": str(entity_id),
        "org_id": str(org_id),
        "kb_id": str(kb_id),
        "entity_type": "hotel",
        "canonical_name": "Retired Hotel",
        "metadata": {"source": "governance-test"},
        "support_status": "unsupported",
        "support_status_reason": "merged_into_survivor",
        "support_status_changed_at": changed_at.isoformat().replace("+00:00", "Z"),
        "support_status_snapshot_id": str(snapshot_id),
        "unsupported_at": changed_at.isoformat().replace("+00:00", "Z"),
        "is_pinned": True,
        "pinned_at": changed_at.isoformat().replace("+00:00", "Z"),
        "pinned_by": str(actor_id),
        "pin_reason": "retain_for_audit",
        "merged_into_entity_id": str(survivor_id),
        "merged_at": changed_at.isoformat().replace("+00:00", "Z"),
        "merged_by": str(actor_id),
        "merge_reason": "duplicate_registry_identity",
        "aliases": [],
        "created_at": changed_at.isoformat().replace("+00:00", "Z"),
        "updated_at": changed_at.isoformat().replace("+00:00", "Z"),
    }


async def test_fact_claim_output_returns_normalized_relation_object_entity_id() -> None:
    org_id = uuid4()
    kb_id = uuid4()
    subject_entity_id = uuid4()
    object_entity_id = uuid4()
    claim_id = uuid4()
    created_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    claim = FactClaim(
        id=claim_id,
        org_id=org_id,
        kb_id=kb_id,
        subject_entity_id=subject_entity_id,
        object_entity_id=object_entity_id,
        subject_type="canonical_entity",
        subject_id=subject_entity_id,
        predicate="located_in",
        value_json={"target_entity_id": str(object_entity_id)},
        raw_payload={"target_entity_id": str(object_entity_id)},
        review_status=FactReviewStatus.CONFIRMED,
        created_at=created_at,
        updated_at=created_at,
    )
    result = MagicMock()
    result.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    [output] = await kb_api._fact_claim_outputs(session, [claim])
    response = output.model_dump(mode="json")

    assert response["subject_entity_id"] == str(subject_entity_id)
    assert response["object_entity_id"] == str(object_entity_id)
    assert response["effective_payload"]["target_entity_id"] == str(object_entity_id)
