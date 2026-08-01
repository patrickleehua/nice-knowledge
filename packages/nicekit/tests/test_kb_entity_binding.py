from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from nicekit.kb import entity_binding
from nicekit.models.kb import CanonicalEntity, FactClaim, GraphPredicate


def _relation_claim(*, org_id, kb_id, payload, object_entity_id=None):
    return FactClaim(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        subject_type="source_document",
        subject_id=uuid4(),
        object_entity_id=object_entity_id,
        predicate=GraphPredicate.LOCATED_IN,
        value_json=payload,
        raw_payload=payload,
    )


def _entity(*, org_id, kb_id, entity_type, name):
    return CanonicalEntity(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        entity_type=entity_type,
        canonical_name=name,
    )


async def test_relation_binding_writes_authoritative_endpoint_foreign_keys(
    monkeypatch,
) -> None:
    org_id, kb_id = uuid4(), uuid4()
    source = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="organization",
        name="法国领馆",
    )
    target = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="destination",
        name="上海",
    )
    resolved = iter((source, target))

    async def fake_resolve(*_args, **_kwargs):
        return next(resolved)

    monkeypatch.setattr(entity_binding, "resolve_entity", fake_resolve)
    session = MagicMock()
    session.flush = AsyncMock()
    claim = _relation_claim(
        org_id=org_id,
        kb_id=kb_id,
        payload={
            "source_name": "法国领馆",
            "source_type_key": "organization",
            "target_name": "上海",
            "target_type_key": "destination",
        },
    )

    assert await entity_binding.bind_claim_entities(session, claim, org_id=org_id) is None
    assert claim.subject_entity_id == source.id
    assert claim.object_entity_id == target.id
    assert claim.corrected_payload["target_entity_id"] == str(target.id)
    session.add.assert_called_once_with(claim)
    session.flush.assert_awaited_once()


async def test_relation_binding_rejects_conflicting_existing_object_endpoint(
    monkeypatch,
) -> None:
    org_id, kb_id = uuid4(), uuid4()
    source = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="organization",
        name="法国领馆",
    )
    target = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="destination",
        name="上海",
    )
    conflicting_target = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="destination",
        name="北京",
    )
    resolved = iter((source, target))

    async def fake_resolve(*_args, **_kwargs):
        return next(resolved)

    monkeypatch.setattr(entity_binding, "resolve_entity", fake_resolve)
    session = MagicMock()
    session.flush = AsyncMock()
    claim = _relation_claim(
        org_id=org_id,
        kb_id=kb_id,
        object_entity_id=conflicting_target.id,
        payload={
            "source_name": "法国领馆",
            "target_name": "上海",
        },
    )

    error = await entity_binding.bind_claim_entities(session, claim, org_id=org_id)

    assert error is not None and "object_entity_id" in error
    assert claim.subject_entity_id is None
    assert claim.object_entity_id == conflicting_target.id
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_relation_binding_rejects_cross_tenant_claim_before_resolution(
    monkeypatch,
) -> None:
    org_id = uuid4()
    resolve = AsyncMock()
    monkeypatch.setattr(entity_binding, "resolve_entity", resolve)
    session = MagicMock()
    session.flush = AsyncMock()
    claim = _relation_claim(
        org_id=uuid4(),
        kb_id=uuid4(),
        payload={"source_name": "A", "target_name": "B"},
    )

    error = await entity_binding.bind_claim_entities(session, claim, org_id=org_id)

    assert error is not None and "租户" in error
    resolve.assert_not_awaited()
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_relation_binding_accepts_existing_subject_and_legacy_target_id() -> None:
    org_id, kb_id = uuid4(), uuid4()
    source = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="organization",
        name="法国领馆",
    )
    target = _entity(
        org_id=org_id,
        kb_id=kb_id,
        entity_type="destination",
        name="上海",
    )
    session = MagicMock()
    session.get = AsyncMock(side_effect=(source, target))
    session.flush = AsyncMock()
    claim = _relation_claim(
        org_id=org_id,
        kb_id=kb_id,
        payload={"target_entity_id": str(target.id)},
    )
    claim.subject_entity_id = source.id

    assert await entity_binding.bind_claim_entities(session, claim, org_id=org_id) is None
    assert claim.subject_entity_id == source.id
    assert claim.object_entity_id == target.id
    assert claim.corrected_payload == {"target_entity_id": str(target.id)}
    session.add.assert_called_once_with(claim)
    session.flush.assert_awaited_once()
