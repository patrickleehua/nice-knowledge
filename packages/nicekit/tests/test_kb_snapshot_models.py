from nicekit.models.kb import (
    KnowledgeBase,
    KnowledgeSnapshot,
    OutboxEvent,
    OutboxStatus,
    SnapshotStatus,
)


def test_snapshot_and_outbox_model_contract() -> None:
    assert KnowledgeSnapshot.__tablename__ == "knowledge_snapshots"
    assert OutboxEvent.__tablename__ == "outbox_events"
    assert {status.value for status in SnapshotStatus} == {
        "building",
        "ready",
        "active",
        "retired",
        "failed",
    }
    assert {status.value for status in OutboxStatus} == {
        "pending",
        "processing",
        "published",
        "dead_letter",
    }


def test_active_snapshot_pointer_is_scoped_to_its_knowledge_base() -> None:
    constraint = next(
        constraint
        for constraint in KnowledgeBase.__table__.foreign_key_constraints
        if constraint.name == "fk_knowledge_bases_active_snapshot"
    )
    assert [column.name for column in constraint.columns] == [
        "id",
        "active_snapshot_id",
    ]
    assert [element.target_fullname for element in constraint.elements] == [
        "knowledge_snapshots.kb_id",
        "knowledge_snapshots.id",
    ]
    assert constraint.ondelete is None


def test_snapshot_and_outbox_constraints_and_partial_indexes() -> None:
    constraint_names = {
        constraint.name
        for model in (KnowledgeSnapshot, OutboxEvent)
        for constraint in model.__table__.constraints
    }
    assert {
        "uq_knowledge_snapshot_fingerprints",
        "uq_knowledge_snapshot_kb_id_id",
        "ck_knowledge_snapshot_revision_set_hash",
        "ck_knowledge_snapshot_embedding_fingerprint",
        "ck_knowledge_snapshot_config_fingerprint",
        "uq_outbox_event_idempotency_key",
        "ck_outbox_event_claim_state",
        "ck_outbox_event_published_state",
        "ck_outbox_event_dead_letter_state",
    } <= constraint_names

    indexes = {
        index.name: index
        for model in (KnowledgeSnapshot, OutboxEvent)
        for index in model.__table__.indexes
    }
    for name in (
        "uq_knowledge_snapshots_active_kb",
        "ix_knowledge_snapshots_build_queue",
        "ix_outbox_events_pending",
        "ix_outbox_events_processing",
    ):
        assert indexes[name].dialect_options["postgresql"]["where"] is not None


def test_snapshot_and_outbox_foreign_keys_never_cascade() -> None:
    for model in (KnowledgeSnapshot, OutboxEvent):
        for column in model.__table__.columns:
            for foreign_key in column.foreign_keys:
                assert foreign_key.ondelete is None
