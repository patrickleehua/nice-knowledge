"""Sanitized image inventory serialization tests."""

from datetime import UTC, datetime
from uuid import uuid4

from nicekit.domain.kb_media import ImageEnrichmentStatus, ImageReviewStatus
from nicekit.kb.image_inventory import serialize_image_asset
from nicekit.models.kb import (
    DocType,
    DocumentRevision,
    KbImageAsset,
    SourceDocument,
)


def test_failed_asset_without_media_metadata_has_no_urls_or_storage_details() -> None:
    now = datetime.now(UTC)
    org_id, kb_id, doc_id, revision_id, asset_id = (uuid4() for _ in range(5))
    document = SourceDocument(
        id=doc_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="unsupported-image.pdf",
        object_key="private/source-object",
        sha256="a" * 64,
        doc_type=DocType.GENERAL,
    )
    revision = DocumentRevision(
        id=revision_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_no=3,
        sha256="b" * 64,
        original_object_key="private/revision-object",
    )
    asset = KbImageAsset(
        id=asset_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_id=revision_id,
        source_occurrence="page:2/picture:4",
        parser_name="docling",
        page=2,
        image_sha256="c" * 64,
        size_bytes=128,
        extraction_fingerprint="d" * 64,
        enrichment_status=ImageEnrichmentStatus.FAILED,
        review_status=ImageReviewStatus.NEEDS_REVIEW,
        failure_code="unsupported_image",
        failure_detail="provider payload and private/source-object",
        lock_version=2,
        created_at=now,
        updated_at=now,
    )

    result = serialize_image_asset(
        asset,
        document,
        revision,
        snapshot_count=1,
        image_chunk_count=2,
        evidence_span_count=3,
    )
    payload = result.model_dump(mode="json")

    assert payload["source_filename"] == "unsupported-image.pdf"
    assert payload["revision_number"] == 3
    assert payload["content_url"] is None
    assert payload["thumbnail_url"] is None
    assert payload["content_type"] is None
    assert payload["failure_code"] == "unsupported_image"
    assert payload["usage"] == {
        "snapshot_count": 1,
        "image_chunk_count": 2,
        "evidence_span_count": 3,
        "business_artifact_count": 0,
        "business_artifact_support": "unsupported",
    }
    assert "failure_detail" not in payload
    assert "object_key" not in payload
    assert "private/source-object" not in str(payload)


def test_stored_asset_projects_only_authenticated_application_urls() -> None:
    now = datetime.now(UTC)
    org_id, kb_id, doc_id, revision_id, asset_id = (uuid4() for _ in range(5))
    document = SourceDocument(
        id=doc_id,
        org_id=org_id,
        kb_id=kb_id,
        filename="brochure.pdf",
        object_key="private/source-object",
        sha256="a" * 64,
        doc_type=DocType.GENERAL,
    )
    revision = DocumentRevision(
        id=revision_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_no=1,
        sha256="b" * 64,
        original_object_key="private/revision-object",
    )
    asset = KbImageAsset(
        id=asset_id,
        org_id=org_id,
        kb_id=kb_id,
        doc_id=doc_id,
        revision_id=revision_id,
        source_occurrence="page:1/picture:1",
        parser_name="docling",
        page=1,
        image_sha256="c" * 64,
        size_bytes=512,
        content_type="image/png",
        width=640,
        height=480,
        original_object_key="private/original-object",
        thumbnail_object_key="private/thumbnail-object",
        thumbnail_sha256="d" * 64,
        thumbnail_content_type="image/webp",
        thumbnail_size_bytes=128,
        thumbnail_width=320,
        thumbnail_height=240,
        extraction_fingerprint="e" * 64,
        enrichment_status=ImageEnrichmentStatus.SUCCEEDED,
        review_status=ImageReviewStatus.NEEDS_REVIEW,
        lock_version=1,
        created_at=now,
        updated_at=now,
    )

    payload = serialize_image_asset(
        asset,
        document,
        revision,
        snapshot_count=0,
        image_chunk_count=0,
        evidence_span_count=0,
    ).model_dump(mode="json")

    assert payload["content_url"] == (
        f"/api/v1/kb/image-assets/{asset_id}/content"
    )
    assert payload["thumbnail_url"] == (
        f"/api/v1/kb/image-assets/{asset_id}/thumbnail"
    )
    assert "private/original-object" not in str(payload)
    assert "private/thumbnail-object" not in str(payload)
