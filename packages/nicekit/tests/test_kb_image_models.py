from nicekit.domain.kb_media import ImageEnrichmentStatus, ImageReviewStatus
from nicekit.models.kb import (
    EvidenceSpan,
    KbChunk,
    KbImageAsset,
    KbImageAssetEvent,
    KbSnapshotImageAsset,
)


def _constraint_names(model) -> set[str | None]:
    return {constraint.name for constraint in model.__table__.constraints}


def _foreign_key(model, name: str):
    return next(
        constraint
        for constraint in model.__table__.foreign_key_constraints
        if constraint.name == name
    )


def test_image_asset_models_use_stable_table_and_status_contracts() -> None:
    assert KbImageAsset.__tablename__ == "kb_image_assets"
    assert KbImageAssetEvent.__tablename__ == "kb_image_asset_events"
    assert KbSnapshotImageAsset.__tablename__ == "kb_snapshot_image_assets"
    assert {status.value for status in ImageEnrichmentStatus} == {
        "pending",
        "processing",
        "succeeded",
        "failed",
    }
    assert {status.value for status in ImageReviewStatus} == {
        "needs_review",
        "accepted",
        "excluded",
        "rejected",
    }


def test_image_asset_constraints_preserve_occurrences_and_tenant_identity() -> None:
    names = _constraint_names(KbImageAsset)
    assert {
        "uq_kb_image_asset_tenant_identity",
        "uq_kb_image_asset_revision_identity",
        "uq_kb_image_asset_revision_occurrence",
        "fk_kb_image_asset_revision_tenant",
        "ck_kb_image_asset_sha256",
        "ck_kb_image_asset_bbox",
        "ck_kb_image_asset_original_metadata",
        "ck_kb_image_asset_thumbnail_metadata",
        "ck_kb_image_asset_enrichment_status",
        "ck_kb_image_asset_review_status",
        "ck_kb_image_asset_failure_state",
        "ck_kb_image_asset_accepted_state",
    } <= names

    revision_fk = _foreign_key(KbImageAsset, "fk_kb_image_asset_revision_tenant")
    assert [column.name for column in revision_fk.columns] == [
        "revision_id",
        "org_id",
        "kb_id",
    ]
    assert [element.target_fullname for element in revision_fk.elements] == [
        "document_revisions.id",
        "document_revisions.org_id",
        "document_revisions.kb_id",
    ]


def test_image_chunk_and_evidence_associations_are_revision_scoped() -> None:
    chunk_fk = _foreign_key(KbChunk, "fk_kb_chunk_image_asset_revision")
    evidence_fk = _foreign_key(
        EvidenceSpan,
        "fk_evidence_span_image_asset_revision",
    )
    for foreign_key in (chunk_fk, evidence_fk):
        assert [column.name for column in foreign_key.columns] == [
            "image_asset_id",
            "org_id",
            "kb_id",
            "revision_id",
        ]
        assert [element.target_fullname for element in foreign_key.elements] == [
            "kb_image_assets.id",
            "kb_image_assets.org_id",
            "kb_image_assets.kb_id",
            "kb_image_assets.revision_id",
        ]

    assert KbChunk.__table__.c.content_kind.server_default is not None
    assert {
        "ck_kb_chunk_content_kind",
        "ck_kb_chunk_image_asset_state",
    } <= _constraint_names(KbChunk)


def test_snapshot_membership_and_events_are_tenant_scoped() -> None:
    assert {
        "uq_kb_snapshot_image_asset_membership",
        "fk_kb_snapshot_image_asset_snapshot_tenant",
        "fk_kb_snapshot_image_asset_asset_tenant",
        "ck_kb_snapshot_image_asset_enrichment_fingerprint",
    } <= _constraint_names(KbSnapshotImageAsset)
    assert {
        "uq_kb_image_asset_event_version",
        "uq_kb_image_asset_event_idempotency_key",
        "fk_kb_image_asset_event_asset_tenant",
    } <= _constraint_names(KbImageAssetEvent)


def test_nullable_image_json_fields_bind_python_none_as_sql_null() -> None:
    assert KbImageAsset.__table__.c.bbox.type.none_as_null is True
    assert KbImageAssetEvent.__table__.c.before_state.type.none_as_null is True
    assert KbImageAssetEvent.__table__.c.after_state.type.none_as_null is True
