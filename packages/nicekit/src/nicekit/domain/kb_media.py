"""Stable contracts for governed knowledge-base media.

These DTOs intentionally expose application URLs and immutable identifiers only.
Object-storage keys and binary payloads are persistence details and must never
enter retrieval snapshots, Agent events, or browser JSON.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImageEnrichmentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ImageReviewStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    EXCLUDED = "excluded"
    REJECTED = "rejected"


class ImageAssetIssueKind(StrEnum):
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    OVERSIZED = "oversized"
    MISSING_OBJECT = "missing_object"
    PROVIDER_FAILED = "provider_failed"
    UNRESOLVED = "unresolved"


class ImageReviewAction(StrEnum):
    ACCEPT = "accept"
    EDIT_AND_ACCEPT = "edit_and_accept"
    REJECT = "reject"
    EXCLUDE = "exclude"


class NormalizedBBox(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_area(self) -> Self:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("bbox must have positive area")
        return self


class ImageSourceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["image_source"] = "image_source"
    asset_id: UUID
    revision_id: UUID
    source_doc_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    bbox: NormalizedBBox | None = None
    quote_text: str = Field(max_length=4000)


class KnowledgeMediaReferenceSnapshot(BaseModel):
    """URL-free media metadata safe to persist in retrieval state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["image"] = "image"
    asset_id: UUID
    snapshot_id: UUID | None = None
    alt_text: str = Field(min_length=1, max_length=2000)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    bbox: NormalizedBBox | None = None
    citation: ImageSourceCitation

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.citation.asset_id != self.asset_id:
            raise ValueError("media reference and citation asset IDs must match")
        return self


class KnowledgeMediaReference(KnowledgeMediaReferenceSnapshot):
    """Response-time media reference with authenticated application routes."""

    thumbnail_url: str = Field(min_length=1, max_length=500, pattern=r"^/api/v1/")
    content_url: str = Field(min_length=1, max_length=500, pattern=r"^/api/v1/")


def project_authenticated_media_reference(
    value: KnowledgeMediaReferenceSnapshot | dict,
) -> KnowledgeMediaReference:
    """Project fresh application routes without persisting URL-bearing state."""

    snapshot = KnowledgeMediaReferenceSnapshot.model_validate(value)
    base_url = f"/api/v1/kb/image-assets/{snapshot.asset_id}"
    snapshot_query = (
        f"?snapshot_id={snapshot.snapshot_id}" if snapshot.snapshot_id is not None else ""
    )
    return KnowledgeMediaReference(
        **snapshot.model_dump(),
        thumbnail_url=f"{base_url}/thumbnail{snapshot_query}",
        content_url=f"{base_url}/content{snapshot_query}",
    )


class SelectedKnowledgeMedia(KnowledgeMediaReferenceSnapshot):
    """Immutable, URL-free media selection stored on a business record.

    New selections must name the exact active knowledge snapshot from which the
    evidence was selected. Historical text-only records remain compatible
    because the surrounding business field defaults to an empty list.
    """

    snapshot_id: UUID


def selected_knowledge_media_from_reference(
    value: KnowledgeMediaReferenceSnapshot | dict,
) -> SelectedKnowledgeMedia:
    """Canonicalize a retrieval media reference for URL-free persistence."""

    if isinstance(value, KnowledgeMediaReferenceSnapshot):
        payload = value.model_dump()
    else:
        payload = dict(value)
    payload.pop("thumbnail_url", None)
    payload.pop("content_url", None)
    return SelectedKnowledgeMedia.model_validate(payload)


class KbImageAssetUsageSummary(BaseModel):
    """Current persisted references to one governed image occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_count: int = Field(ge=0)
    image_chunk_count: int = Field(ge=0)
    evidence_span_count: int = Field(ge=0)
    business_artifact_count: int = Field(default=0, ge=0)
    business_artifact_support: Literal["unsupported"] = "unsupported"


class KbImageAssetRead(BaseModel):
    """Sanitized image-asset state for admin and maintenance APIs.

    Storage keys and provider failure payloads are intentionally absent. Media
    bytes are addressed only through authenticated application URLs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID
    kb_id: UUID
    source_document_id: UUID
    source_filename: str = Field(min_length=1, max_length=500)
    revision_id: UUID
    revision_number: int = Field(ge=1)
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ingest_run_id: UUID | None = None
    source_occurrence: str = Field(min_length=1, max_length=500)
    parser_name: str = Field(min_length=1, max_length=50)
    parser_item_ref: str | None = Field(default=None, max_length=500)
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    bbox: NormalizedBBox | None = None
    reading_order: int | None = Field(default=None, ge=0)
    surrounding_text: str | None = Field(default=None, max_length=4000)

    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    content_type: Literal["image/png", "image/jpeg", "image/webp"] | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    thumbnail_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    thumbnail_content_type: Literal["image/png", "image/jpeg", "image/webp"] | None = None
    thumbnail_size_bytes: int | None = Field(default=None, gt=0)
    thumbnail_width: int | None = Field(default=None, gt=0)
    thumbnail_height: int | None = Field(default=None, gt=0)
    content_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^/api/v1/",
    )
    thumbnail_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^/api/v1/",
    )

    ocr_text: str | None = Field(default=None, max_length=100_000)
    caption: str | None = Field(default=None, max_length=10_000)
    ocr_provider: str | None = Field(default=None, max_length=100)
    ocr_model: str | None = Field(default=None, max_length=200)
    caption_provider: str | None = Field(default=None, max_length=100)
    caption_model: str | None = Field(default=None, max_length=200)
    extraction_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ocr_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    caption_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    config_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    enrichment_status: ImageEnrichmentStatus
    review_status: ImageReviewStatus
    failure_code: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[a-z0-9_]+$",
    )
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    review_source: str | None = Field(default=None, max_length=50)
    review_note: str | None = Field(default=None, max_length=10_000)
    lock_version: int = Field(ge=1)
    usage: KbImageAssetUsageSummary
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_media_metadata(self) -> Self:
        original = (self.content_type, self.width, self.height, self.content_url)
        if any(value is not None for value in original) and not all(
            value is not None for value in original
        ):
            raise ValueError("original media metadata must be complete")
        thumbnail = (
            self.thumbnail_sha256,
            self.thumbnail_content_type,
            self.thumbnail_size_bytes,
            self.thumbnail_width,
            self.thumbnail_height,
            self.thumbnail_url,
        )
        if any(value is not None for value in thumbnail) and not all(
            value is not None for value in thumbnail
        ):
            raise ValueError("thumbnail media metadata must be complete")
        if self.page is not None and self.slide is not None:
            raise ValueError("page and slide are mutually exclusive")
        if (self.enrichment_status is ImageEnrichmentStatus.FAILED) != (
            self.failure_code is not None
        ):
            raise ValueError("failure code must match failed enrichment state")
        if (
            self.review_status is ImageReviewStatus.ACCEPTED
            and self.enrichment_status is not ImageEnrichmentStatus.SUCCEEDED
        ):
            raise ValueError("accepted image enrichment must have succeeded")
        return self


class KbImageAssetPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[KbImageAssetRead]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class KbImageEnrichmentReadinessRead(BaseModel):
    """Safe configuration readiness for the KB settings workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    ready: bool
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    ocr_provider: str = Field(min_length=1, max_length=100)
    ocr_model: str = Field(min_length=1, max_length=200)
    caption_provider: str | None = Field(default=None, max_length=100)
    caption_model: str | None = Field(default=None, max_length=200)
    selection_source: Literal["platform_default", "kb_override"] = "platform_default"
    capability_source: Literal[
        "manual",
        "provider",
        "registry",
        "unclassified",
    ] | None = None
    registry_revision: str | None = Field(default=None, max_length=100)
    acceptance_policy: Literal["human_review_required"] = "human_review_required"
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class KbImageAssetAuditState(BaseModel):
    """Allowlisted image state suitable for organization-scoped audit display."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caption: str | None = Field(default=None, max_length=10_000)
    ocr_text: str | None = Field(default=None, max_length=100_000)
    enrichment_status: str | None = Field(default=None, max_length=30)
    review_status: str | None = Field(default=None, max_length=30)
    failure_code: str | None = Field(default=None, max_length=100)
    review_source: str | None = Field(default=None, max_length=50)
    reviewed_by: UUID | None = None
    lock_version: int | None = Field(default=None, ge=1)
    image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    config_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    result_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class KbImageAssetAuditEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_version: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=100)
    actor_kind: Literal["human", "policy", "system", "provider"]
    actor_user_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)
    before_state: KbImageAssetAuditState | None = None
    after_state: KbImageAssetAuditState | None = None
    created_at: datetime


class KbImageAssetAuditPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[KbImageAssetAuditEventRead]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class KbImageBulkReviewRequest(BaseModel):
    """按筛选条件批量审核。不传 asset_ids 即对整个筛选结果生效(跨页)。

    刻意只放开 accept/exclude:批量拒绝会静默丢内容,仍要求逐张确认。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "exclude"]
    reason: str = Field(min_length=1, max_length=500)
    asset_ids: list[UUID] | None = None
    document_id: UUID | None = None
    review_status: ImageReviewStatus | None = None
    enrichment_status: ImageEnrichmentStatus | None = None
    max_assets: int = Field(default=500, ge=1, le=2000)


class KbImageBulkReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    matched: int = Field(ge=0)
    updated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    truncated: bool = False


class KbImageReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ImageReviewAction
    expected_lock_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    caption: str | None = Field(default=None, max_length=10_000)
    ocr_text: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def validate_edits(self) -> Self:
        has_edit = self.caption is not None or self.ocr_text is not None
        if self.action is ImageReviewAction.EDIT_AND_ACCEPT and not has_edit:
            raise ValueError("edit_and_accept requires caption or ocr_text")
        if self.action is not ImageReviewAction.EDIT_AND_ACCEPT and has_edit:
            raise ValueError("caption and ocr_text require edit_and_accept")
        return self


class KbImageRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lock_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class KbImageRetryAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID
    run_id: UUID
    lock_version: int = Field(ge=1)
    status: Literal["queued"] = "queued"
