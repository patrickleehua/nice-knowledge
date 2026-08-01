from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nicekit.domain.kb_media import (
    ImageEnrichmentStatus,
    ImageReviewStatus,
    ImageSourceCitation,
    KbImageAssetRead,
    KbImageAssetUsageSummary,
    KnowledgeMediaReference,
    NormalizedBBox,
)
from nicekit.kb.parsers.base import ParsedDocument, ParsedImageCandidate


def _citation(asset_id=None) -> ImageSourceCitation:
    asset_id = asset_id or uuid4()
    return ImageSourceCitation(
        asset_id=asset_id,
        revision_id=uuid4(),
        source_doc_id=uuid4(),
        source_sha256="a" * 64,
        image_sha256="b" * 64,
        page=2,
        bbox=NormalizedBBox(left=0.1, top=0.2, right=0.8, bottom=0.9),
        quote_text="美泉宫外观",
    )


def test_structured_parsed_document_retains_image_candidates() -> None:
    candidate = ParsedImageCandidate(
        content=b"image-bytes",
        source_occurrence="docling:pictures/0",
        filename="picture-1.png",
        content_type="image/png",
        page=1,
        bbox=(0.1, 0.2, 0.8, 0.9),
        reading_order=0,
        parser_item_ref="#/pictures/0",
    )

    parsed = ParsedDocument(
        markdown="正文",
        parser_name="docling",
        structured_json={"pictures": [{"self_ref": "#/pictures/0"}]},
        images=(candidate,),
    )

    assert parsed.structured_json is not None
    assert parsed.images == (candidate,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page", 0),
        ("slide", 0),
        ("bbox", (-0.1, 0.0, 0.5, 0.5)),
        ("bbox", (0.5, 0.5, 0.4, 0.9)),
    ],
)
def test_image_candidate_rejects_invalid_source_position(field: str, value: object) -> None:
    kwargs = {
        "content": b"image-bytes",
        "source_occurrence": "fixture:1",
        "filename": "fixture.png",
        "content_type": "image/png",
        field: value,
    }
    with pytest.raises(ValueError):
        ParsedImageCandidate(**kwargs)


def test_media_reference_contains_stable_ids_and_application_urls_only() -> None:
    citation = _citation()
    media = KnowledgeMediaReference(
        asset_id=citation.asset_id,
        alt_text="美泉宫外观",
        content_type="image/jpeg",
        width=1200,
        height=801,
        page=2,
        bbox=citation.bbox,
        citation=citation,
        thumbnail_url=f"/api/v1/kb/image-assets/{citation.asset_id}/thumbnail",
        content_url=f"/api/v1/kb/image-assets/{citation.asset_id}/content",
    )

    payload = media.model_dump(mode="json")
    assert payload["asset_id"] == str(citation.asset_id)
    assert "object_key" not in payload
    assert "data" not in payload
    assert payload["thumbnail_url"].startswith("/api/v1/")
    assert payload["content_url"].startswith("/api/v1/")


def test_media_reference_rejects_mismatched_citation_asset() -> None:
    with pytest.raises(ValidationError, match="asset IDs must match"):
        KnowledgeMediaReference(
            asset_id=uuid4(),
            alt_text="来源图片",
            content_type="image/png",
            width=640,
            height=480,
            citation=_citation(),
            thumbnail_url="/api/v1/kb/image-assets/a/thumbnail",
            content_url="/api/v1/kb/image-assets/a/content",
        )


def _asset_read(**overrides: object) -> KbImageAssetRead:
    asset_id = uuid4()
    values: dict[str, object] = {
        "asset_id": asset_id,
        "kb_id": uuid4(),
        "source_document_id": uuid4(),
        "source_filename": "source.pdf",
        "revision_id": uuid4(),
        "revision_number": 1,
        "revision_sha256": "d" * 64,
        "source_occurrence": "docling:#/pictures/0",
        "parser_name": "docling",
        "page": 1,
        "image_sha256": "a" * 64,
        "size_bytes": 512,
        "content_type": "image/png",
        "width": 100,
        "height": 80,
        "thumbnail_sha256": "b" * 64,
        "thumbnail_content_type": "image/webp",
        "thumbnail_size_bytes": 128,
        "thumbnail_width": 100,
        "thumbnail_height": 80,
        "content_url": f"/api/v1/kb/image-assets/{asset_id}/content",
        "thumbnail_url": f"/api/v1/kb/image-assets/{asset_id}/thumbnail",
        "extraction_fingerprint": "c" * 64,
        "enrichment_status": ImageEnrichmentStatus.SUCCEEDED,
        "review_status": ImageReviewStatus.NEEDS_REVIEW,
        "failure_code": None,
        "lock_version": 1,
        "usage": KbImageAssetUsageSummary(
            snapshot_count=0,
            image_chunk_count=0,
            evidence_span_count=0,
        ),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return KbImageAssetRead.model_validate(values)


def test_image_asset_read_contract_excludes_storage_and_raw_failure_details() -> None:
    payload = _asset_read(
        enrichment_status=ImageEnrichmentStatus.FAILED,
        failure_code="caption_provider_unavailable",
    ).model_dump(mode="json")

    assert payload["source_occurrence"] == "docling:#/pictures/0"
    assert payload["content_url"].startswith("/api/v1/")
    assert payload["failure_code"] == "caption_provider_unavailable"
    assert "object_key" not in payload
    assert "failure_detail" not in payload
    assert "data" not in payload


def test_image_asset_read_requires_complete_original_metadata() -> None:
    with pytest.raises(ValidationError, match="original media metadata must be complete"):
        _asset_read(content_url=None)
