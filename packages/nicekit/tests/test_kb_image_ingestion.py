from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nicekit.domain.kb_media import ImageEnrichmentStatus, ImageReviewStatus
from nicekit.kb import image_ingestion
from nicekit.kb.image_ingestion import (
    image_enrichment_run_identity,
    persist_image_candidates,
    summarize_image_assets,
)
from nicekit.kb.parsers.base import ParsedImageCandidate


def _asset(
    enrichment_status: ImageEnrichmentStatus,
    review_status: ImageReviewStatus = ImageReviewStatus.NEEDS_REVIEW,
):
    return SimpleNamespace(
        enrichment_status=enrichment_status,
        review_status=review_status,
    )


def _candidate(source_occurrence: str) -> ParsedImageCandidate:
    return ParsedImageCandidate(
        content=b"image-bytes",
        source_occurrence=source_occurrence,
        filename="source.png",
        content_type="image/png",
    )


def test_image_stage_projection_distinguishes_all_operator_states() -> None:
    assert summarize_image_assets([]).state == "completed"
    assert summarize_image_assets([]).publishable
    assert summarize_image_assets(
        [_asset(ImageEnrichmentStatus.PENDING)]
    ).state == "pending"
    assert summarize_image_assets(
        [_asset(ImageEnrichmentStatus.PROCESSING)]
    ).state == "processing"
    assert summarize_image_assets(
        [_asset(ImageEnrichmentStatus.SUCCEEDED)]
    ).state == "needs_review"
    completed = summarize_image_assets(
        [
            _asset(
                ImageEnrichmentStatus.SUCCEEDED,
                ImageReviewStatus.ACCEPTED,
            ),
            _asset(
                ImageEnrichmentStatus.SUCCEEDED,
                ImageReviewStatus.EXCLUDED,
            ),
        ]
    )
    assert completed.state == "completed"
    assert completed.completed == 2
    assert completed.publishable


def test_image_stage_failure_takes_precedence_and_blocks_publishability() -> None:
    projection = summarize_image_assets(
        [
            _asset(ImageEnrichmentStatus.PENDING),
            _asset(ImageEnrichmentStatus.PROCESSING),
            _asset(ImageEnrichmentStatus.FAILED),
        ]
    )

    assert projection.state == "failed"
    assert projection.failed == 1
    assert not projection.publishable


def test_image_enrichment_run_identity_covers_occurrence_and_configuration() -> None:
    revision_id = uuid4()
    fingerprint = "a" * 64

    first = image_enrichment_run_identity(
        revision_id=revision_id,
        source_occurrence="page:1/picture:1",
        config_fingerprint=fingerprint,
    )
    replay = image_enrichment_run_identity(
        revision_id=revision_id,
        source_occurrence="page:1/picture:1",
        config_fingerprint=fingerprint,
    )
    other_occurrence = image_enrichment_run_identity(
        revision_id=revision_id,
        source_occurrence="page:1/picture:2",
        config_fingerprint=fingerprint,
    )
    other_config = image_enrichment_run_identity(
        revision_id=revision_id,
        source_occurrence="page:1/picture:1",
        config_fingerprint="b" * 64,
    )

    assert first == replay
    assert first != other_occurrence
    assert first != other_config
    stage, idempotency_key = first
    assert stage.startswith("image_enrich:")
    assert len(stage) <= 50
    assert len(idempotency_key) <= 200


async def test_duplicate_source_occurrence_fails_before_any_object_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(image_ingestion, "_persist_candidate", persist)

    with pytest.raises(ValueError, match="duplicate image source_occurrence"):
        await persist_image_candidates(
            SimpleNamespace(),
            doc=SimpleNamespace(),
            revision=SimpleNamespace(),
            ingest_run_id=uuid4(),
            parser_name="docling",
            candidates=(
                _candidate("page:1/picture:1"),
                _candidate("page:1/picture:1"),
            ),
        )

    persist.assert_not_awaited()


async def test_enrichment_event_is_not_duplicated_for_same_outcome() -> None:
    asset = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        enrichment_status=ImageEnrichmentStatus.SUCCEEDED,
        review_status=ImageReviewStatus.NEEDS_REVIEW,
        config_fingerprint="a" * 64,
    )
    first_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 1]),
        execute=AsyncMock(),
    )
    await image_ingestion._record_enrichment_event(
        first_session,
        asset=asset,
        action="enrichment_completed",
        outcome=f"succeeded:{'b' * 64}",
        result_fingerprint="b" * 64,
    )
    first_session.execute.assert_awaited_once()

    replay_session = SimpleNamespace(
        scalar=AsyncMock(return_value=uuid4()),
        execute=AsyncMock(),
    )
    await image_ingestion._record_enrichment_event(
        replay_session,
        asset=asset,
        action="enrichment_completed",
        outcome=f"succeeded:{'b' * 64}",
        result_fingerprint="b" * 64,
    )
    replay_session.execute.assert_not_awaited()
