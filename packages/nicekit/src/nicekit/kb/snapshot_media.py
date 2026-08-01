"""Frozen knowledge-media projection and activation validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from minio.error import S3Error
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.domain.kb_media import ImageSourceCitation, NormalizedBBox
from nicekit.kb import storage
from nicekit.kb.image_assets import (
    KbImageAssetUnavailableError,
    original_object_key,
    resolve_image_object,
    thumbnail_object_key,
)
from nicekit.kb.image_validation import validate_kb_image
from nicekit.kb.retrieval_projection import (
    IMAGE_SNAPSHOT_META_KEY,
    image_chunk_snapshot_meta,
)
from nicekit.models.kb import (
    DocumentRevision,
    KbChunk,
    KbImageAsset,
    KbSnapshotImageAsset,
    KnowledgeSnapshot,
    RevisionStatus,
)

if TYPE_CHECKING:
    from nicekit.kb.snapshot import SnapshotBuildContext

_HASH = re.compile(r"^[0-9a-f]{64}$")
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})
_IMAGE_PROJECTION_SCHEMA = "kb-snapshot-media-v1"


class SnapshotMediaProjectionError(RuntimeError):
    """Bounded media gate failure safe for API and operational diagnostics."""

    def __init__(self, code: str, *, asset_id: UUID | None = None) -> None:
        self.code = code
        self.asset_id = asset_id
        suffix = f":{asset_id}" if asset_id is not None else ""
        super().__init__(f"media_projection_invalid:{code}{suffix}")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _citation_payload(
    *,
    asset_id: UUID,
    revision_id: UUID,
    source_doc_id: UUID,
    source_sha256: str,
    image_sha256: str,
    page: int | None,
    slide: int | None,
    bbox: Mapping[str, float] | None,
) -> dict[str, object]:
    return {
        "asset_id": str(asset_id),
        "revision_id": str(revision_id),
        "source_doc_id": str(source_doc_id),
        "source_sha256": source_sha256,
        "image_sha256": image_sha256,
        "page": page,
        "slide": slide,
        "bbox": dict(bbox) if bbox is not None else None,
    }


def citation_fingerprint(
    *,
    asset_id: UUID,
    revision_id: UUID,
    source_doc_id: UUID,
    source_sha256: str,
    image_sha256: str,
    page: int | None,
    slide: int | None,
    bbox: Mapping[str, float] | None,
) -> str:
    return _canonical_sha256(
        _citation_payload(
            asset_id=asset_id,
            revision_id=revision_id,
            source_doc_id=source_doc_id,
            source_sha256=source_sha256,
            image_sha256=image_sha256,
            page=page,
            slide=slide,
            bbox=bbox,
        )
    )


def _asset_manifest_entry(asset: KbImageAsset) -> dict[str, object]:
    try:
        frozen_meta = image_chunk_snapshot_meta(asset)
    except Exception as exc:
        raise SnapshotMediaProjectionError(
            "invalid_enrichment",
            asset_id=asset.id,
        ) from exc
    return {
        "asset_id": str(asset.id),
        "revision_id": str(asset.revision_id),
        "enrichment_fingerprint": frozen_meta["enrichment_fingerprint"],
        "extraction_fingerprint": asset.extraction_fingerprint,
        "ocr_fingerprint": asset.ocr_fingerprint,
        "caption_fingerprint": asset.caption_fingerprint,
        "config_fingerprint": asset.config_fingerprint,
        "asset_lock_version": asset.lock_version,
    }


async def image_projection_source_manifest(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    revision_ids: Sequence[UUID],
) -> dict[str, object]:
    """Freeze the accepted image source set into snapshot configuration."""

    assets: list[KbImageAsset] = []
    if revision_ids:
        assets = list(
            (
                await session.scalars(
                    select(KbImageAsset)
                    .where(
                        KbImageAsset.org_id == org_id,
                        KbImageAsset.kb_id == kb_id,
                        KbImageAsset.revision_id.in_(revision_ids),
                        KbImageAsset.enrichment_status == "succeeded",
                        KbImageAsset.review_status == "accepted",
                    )
                    .order_by(KbImageAsset.id)
                )
            ).all()
        )
    entries = [_asset_manifest_entry(asset) for asset in assets]
    return {
        "schema_version": _IMAGE_PROJECTION_SCHEMA,
        "accepted_assets": entries,
        "accepted_asset_count": len(entries),
        "accepted_asset_manifest_hash": _canonical_sha256(entries),
        "config_fingerprints": sorted(
            {
                str(entry["config_fingerprint"])
                for entry in entries
                if entry["config_fingerprint"] is not None
            }
        ),
    }


def _require_complete_asset(asset: KbImageAsset) -> None:
    original = (
        asset.original_object_key,
        asset.content_type,
        asset.size_bytes,
        asset.width,
        asset.height,
    )
    thumbnail = (
        asset.thumbnail_object_key,
        asset.thumbnail_sha256,
        asset.thumbnail_content_type,
        asset.thumbnail_size_bytes,
        asset.thumbnail_width,
        asset.thumbnail_height,
    )
    if (
        any(value is None for value in original)
        or any(value is None for value in thumbnail)
        or _HASH.fullmatch(asset.image_sha256) is None
        or _HASH.fullmatch(asset.extraction_fingerprint) is None
    ):
        raise SnapshotMediaProjectionError(
            "incomplete_asset_metadata",
            asset_id=asset.id,
        )


def _projection_values(
    *,
    context: SnapshotBuildContext,
    asset: KbImageAsset,
    revision: DocumentRevision,
    chunk: KbChunk,
) -> dict[str, object]:
    _require_complete_asset(asset)
    assert asset.original_object_key is not None
    assert asset.content_type is not None
    assert asset.width is not None
    assert asset.height is not None
    assert asset.thumbnail_object_key is not None
    assert asset.thumbnail_sha256 is not None
    assert asset.thumbnail_content_type is not None
    assert asset.thumbnail_size_bytes is not None
    assert asset.thumbnail_width is not None
    assert asset.thumbnail_height is not None

    image_meta = _validated_image_chunk_meta(chunk, asset_id=asset.id)
    return {
        "id": uuid5(context.snapshot_id, f"snapshot-image:{asset.id}"),
        "org_id": context.org_id,
        "kb_id": context.kb_id,
        "snapshot_id": context.snapshot_id,
        "image_asset_id": asset.id,
        "image_chunk_id": chunk.id,
        "source_doc_id": asset.doc_id,
        "revision_id": asset.revision_id,
        "source_sha256": revision.sha256,
        "source_occurrence": asset.source_occurrence,
        "parser_name": asset.parser_name,
        "parser_item_ref": asset.parser_item_ref,
        "page": asset.page,
        "slide": asset.slide,
        "reading_order": asset.reading_order,
        "bbox": asset.bbox,
        "image_sha256": asset.image_sha256,
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "original_object_key_fingerprint": _key_fingerprint(
            asset.original_object_key
        ),
        "thumbnail_object_key_fingerprint": _key_fingerprint(
            asset.thumbnail_object_key
        ),
        "thumbnail_sha256": asset.thumbnail_sha256,
        "thumbnail_content_type": asset.thumbnail_content_type,
        "thumbnail_size_bytes": asset.thumbnail_size_bytes,
        "thumbnail_width": asset.thumbnail_width,
        "thumbnail_height": asset.thumbnail_height,
        "extraction_fingerprint": asset.extraction_fingerprint,
        "ocr_fingerprint": asset.ocr_fingerprint,
        "caption_fingerprint": asset.caption_fingerprint,
        "config_fingerprint": asset.config_fingerprint,
        "enrichment_fingerprint": image_meta["enrichment_fingerprint"],
        "citation_fingerprint": citation_fingerprint(
            asset_id=asset.id,
            revision_id=asset.revision_id,
            source_doc_id=asset.doc_id,
            source_sha256=revision.sha256,
            image_sha256=asset.image_sha256,
            page=asset.page,
            slide=asset.slide,
            bbox=asset.bbox,
        ),
        "accepted_at_build": True,
        "asset_lock_version": asset.lock_version,
    }


def _validated_image_chunk_meta(
    chunk: KbChunk,
    *,
    asset_id: UUID,
) -> dict[str, object]:
    meta = chunk.meta
    image_meta = meta.get(IMAGE_SNAPSHOT_META_KEY) if isinstance(meta, dict) else None
    if (
        not isinstance(image_meta, dict)
        or set(image_meta)
        != {"alt_text", "caption", "ocr_text", "enrichment_fingerprint"}
        or not isinstance(image_meta.get("alt_text"), str)
        or not image_meta["alt_text"].strip()
        or not isinstance(image_meta.get("enrichment_fingerprint"), str)
        or _HASH.fullmatch(image_meta["enrichment_fingerprint"]) is None
    ):
        raise SnapshotMediaProjectionError(
            "invalid_image_chunk_metadata",
            asset_id=asset_id,
        )
    return image_meta


def _source_manifest_entries(snapshot: KnowledgeSnapshot) -> list[dict[str, object]]:
    source = snapshot.config_manifest.get("image_projection")
    if (
        not isinstance(source, dict)
        or source.get("schema_version") != _IMAGE_PROJECTION_SCHEMA
        or not isinstance(source.get("accepted_assets"), list)
    ):
        raise SnapshotMediaProjectionError("missing_source_manifest")
    entries = source["accepted_assets"]
    if not all(isinstance(entry, dict) for entry in entries):
        raise SnapshotMediaProjectionError("invalid_source_manifest")
    if source.get("accepted_asset_count") != len(entries):
        raise SnapshotMediaProjectionError("invalid_source_manifest")
    if source.get("accepted_asset_manifest_hash") != _canonical_sha256(entries):
        raise SnapshotMediaProjectionError("invalid_source_manifest")
    return entries


def _row_manifest(row: KbSnapshotImageAsset) -> dict[str, object]:
    return {
        "asset_id": str(row.image_asset_id),
        "chunk_id": str(row.image_chunk_id),
        "revision_id": str(row.revision_id),
        "source_doc_id": str(row.source_doc_id),
        "source_sha256": row.source_sha256,
        "image_sha256": row.image_sha256,
        "enrichment_fingerprint": row.enrichment_fingerprint,
        "citation_fingerprint": row.citation_fingerprint,
    }


def _projection_stats(rows: Sequence[KbSnapshotImageAsset]) -> dict[str, object]:
    accepted_assets = [_row_manifest(row) for row in rows]
    chunks = [
        {
            "chunk_id": str(row.image_chunk_id),
            "asset_id": str(row.image_asset_id),
            "enrichment_fingerprint": row.enrichment_fingerprint,
        }
        for row in rows
    ]
    associations = [
        {
            "asset_id": str(row.image_asset_id),
            "chunk_id": str(row.image_chunk_id),
            "citation_fingerprint": row.citation_fingerprint,
        }
        for row in rows
    ]
    config_fingerprints = sorted(
        {row.config_fingerprint for row in rows if row.config_fingerprint is not None}
    )
    enrichment_fingerprints = sorted(
        {row.enrichment_fingerprint for row in rows}
    )
    return {
        "accepted_asset_count": len(rows),
        "image_chunk_count": len(chunks),
        "association_count": len(associations),
        "accepted_assets": accepted_assets,
        "image_chunks": chunks,
        "associations": associations,
        "accepted_asset_manifest_hash": _canonical_sha256(accepted_assets),
        "image_chunk_manifest_hash": _canonical_sha256(chunks),
        "association_manifest_hash": _canonical_sha256(associations),
        "config_fingerprints": config_fingerprints,
        "enrichment_fingerprints": enrichment_fingerprints,
    }


async def _verify_stored_variant(
    *,
    asset: KbImageAsset,
    row: KbSnapshotImageAsset,
    variant: str,
) -> None:
    if variant == "original":
        key = asset.original_object_key
        expected_key_fingerprint = row.original_object_key_fingerprint
        expected_sha256 = row.image_sha256
        expected_size = row.size_bytes
        expected_content_type = row.content_type
        expected_width = row.width
        expected_height = row.height
        expected_key = original_object_key(
            row.org_id,
            row.kb_id,
            row.image_sha256,
            row.content_type,
        )
    else:
        key = asset.thumbnail_object_key
        expected_key_fingerprint = row.thumbnail_object_key_fingerprint
        expected_sha256 = row.thumbnail_sha256
        expected_size = row.thumbnail_size_bytes
        expected_content_type = row.thumbnail_content_type
        expected_width = row.thumbnail_width
        expected_height = row.thumbnail_height
        expected_key = thumbnail_object_key(
            row.org_id,
            row.kb_id,
            row.thumbnail_sha256,
        )
    if (
        key is None
        or not hmac.compare_digest(_key_fingerprint(key), expected_key_fingerprint)
        or not hmac.compare_digest(key, expected_key)
    ):
        raise SnapshotMediaProjectionError(
            "object_key_mismatch",
            asset_id=row.image_asset_id,
        )
    try:
        data = await storage.get_object(key)
    except S3Error as exc:
        code = "missing_object" if exc.code in _MISSING_OBJECT_CODES else "object_unavailable"
        raise SnapshotMediaProjectionError(code, asset_id=row.image_asset_id) from None
    except FileNotFoundError:
        raise SnapshotMediaProjectionError(
            "missing_object",
            asset_id=row.image_asset_id,
        ) from None
    except Exception:
        raise SnapshotMediaProjectionError(
            "object_unavailable",
            asset_id=row.image_asset_id,
        ) from None
    if len(data) != expected_size:
        raise SnapshotMediaProjectionError(
            "object_size_mismatch",
            asset_id=row.image_asset_id,
        )
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise SnapshotMediaProjectionError(
            "object_hash_mismatch",
            asset_id=row.image_asset_id,
        )
    try:
        validated = validate_kb_image(data)
    except ValueError:
        raise SnapshotMediaProjectionError(
            "invalid_object_bytes",
            asset_id=row.image_asset_id,
        ) from None
    if (
        validated.content_type != expected_content_type
        or validated.width != expected_width
        or validated.height != expected_height
    ):
        raise SnapshotMediaProjectionError(
            "object_metadata_mismatch",
            asset_id=row.image_asset_id,
        )


def _frozen_values_match(
    row: KbSnapshotImageAsset,
    asset: KbImageAsset,
    revision: DocumentRevision,
    chunk: KbChunk,
    *,
    include_mutable_enrichment: bool,
) -> bool:
    if asset.original_object_key is None or asset.thumbnail_object_key is None:
        return False
    expected = {
        "source_doc_id": asset.doc_id,
        "revision_id": asset.revision_id,
        "source_sha256": revision.sha256,
        "source_occurrence": asset.source_occurrence,
        "parser_name": asset.parser_name,
        "parser_item_ref": asset.parser_item_ref,
        "page": asset.page,
        "slide": asset.slide,
        "reading_order": asset.reading_order,
        "bbox": asset.bbox,
        "image_sha256": asset.image_sha256,
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "original_object_key_fingerprint": _key_fingerprint(
            asset.original_object_key
        ),
        "thumbnail_object_key_fingerprint": _key_fingerprint(
            asset.thumbnail_object_key
        ),
        "thumbnail_sha256": asset.thumbnail_sha256,
        "thumbnail_content_type": asset.thumbnail_content_type,
        "thumbnail_size_bytes": asset.thumbnail_size_bytes,
        "thumbnail_width": asset.thumbnail_width,
        "thumbnail_height": asset.thumbnail_height,
        "extraction_fingerprint": asset.extraction_fingerprint,
    }
    if include_mutable_enrichment:
        expected.update(
            {
                "ocr_fingerprint": asset.ocr_fingerprint,
                "caption_fingerprint": asset.caption_fingerprint,
                "config_fingerprint": asset.config_fingerprint,
                "asset_lock_version": asset.lock_version,
            }
        )
    return all(getattr(row, field) == value for field, value in expected.items()) and (
        row.image_chunk_id == chunk.id
    )


async def _validate_rows(
    session: AsyncSession,
    *,
    snapshot: KnowledgeSnapshot,
    rows: Sequence[KbSnapshotImageAsset],
    validate_objects: bool,
    require_current_acceptance: bool,
) -> None:
    source_entries = _source_manifest_entries(snapshot)
    source_by_id = {
        str(entry.get("asset_id")): entry
        for entry in source_entries
        if isinstance(entry.get("asset_id"), str)
    }
    if len(source_by_id) != len(source_entries):
        raise SnapshotMediaProjectionError("invalid_source_manifest")
    if set(source_by_id) != {str(row.image_asset_id) for row in rows}:
        raise SnapshotMediaProjectionError("media_membership_mismatch")

    revision_ids = {
        UUID(str(entry["revision_id"]))
        for entry in snapshot.revision_manifest
        if isinstance(entry, dict) and "revision_id" in entry
    }
    all_image_chunks = list(
        (
            await session.scalars(
                select(KbChunk)
                .where(
                    KbChunk.org_id == snapshot.org_id,
                    KbChunk.kb_id == snapshot.kb_id,
                    KbChunk.snapshot_id == snapshot.id,
                    KbChunk.content_kind == "image",
                )
                .order_by(KbChunk.id)
            )
        ).all()
    )
    chunk_by_id = {chunk.id: chunk for chunk in all_image_chunks}
    if set(chunk_by_id) != {row.image_chunk_id for row in rows}:
        raise SnapshotMediaProjectionError("image_chunk_membership_mismatch")

    asset_ids = [row.image_asset_id for row in rows]
    assets = list(
        (
            await session.scalars(
                select(KbImageAsset)
                .where(
                    KbImageAsset.org_id == snapshot.org_id,
                    KbImageAsset.kb_id == snapshot.kb_id,
                    KbImageAsset.id.in_(asset_ids),
                )
                .order_by(KbImageAsset.id)
                .with_for_update()
            )
        ).all()
    )
    asset_by_id = {asset.id: asset for asset in assets}
    revisions = list(
        (
            await session.scalars(
                select(DocumentRevision)
                .where(
                    DocumentRevision.org_id == snapshot.org_id,
                    DocumentRevision.kb_id == snapshot.kb_id,
                    DocumentRevision.id.in_({row.revision_id for row in rows}),
                )
                .order_by(DocumentRevision.id)
                .with_for_update()
            )
        ).all()
    )
    revision_by_id = {revision.id: revision for revision in revisions}

    for row in rows:
        asset = asset_by_id.get(row.image_asset_id)
        chunk = chunk_by_id.get(row.image_chunk_id)
        revision = revision_by_id.get(row.revision_id)
        if asset is None:
            raise SnapshotMediaProjectionError(
                "missing_asset",
                asset_id=row.image_asset_id,
            )
        if not row.accepted_at_build or (
            require_current_acceptance
            and (
                asset.enrichment_status != "succeeded"
                or asset.review_status != "accepted"
            )
        ):
            raise SnapshotMediaProjectionError(
                "asset_unaccepted",
                asset_id=row.image_asset_id,
            )
        if revision is None or revision.id not in revision_ids:
            raise SnapshotMediaProjectionError(
                "revision_not_in_snapshot",
                asset_id=row.image_asset_id,
            )
        if (
            revision.doc_id != row.source_doc_id
            or revision.tombstoned_at is not None
            or str(revision.status) == RevisionStatus.TOMBSTONED.value
        ):
            raise SnapshotMediaProjectionError(
                "invalid_revision",
                asset_id=row.image_asset_id,
            )
        if chunk is None:
            raise SnapshotMediaProjectionError(
                "missing_image_chunk",
                asset_id=row.image_asset_id,
            )
        if (
            chunk.quarantined
            or chunk.image_asset_id != asset.id
            or chunk.revision_id != revision.id
            or chunk.source_doc_id != revision.doc_id
            or chunk.snapshot_id != snapshot.id
            or not chunk.content.strip()
        ):
            raise SnapshotMediaProjectionError(
                "invalid_image_chunk",
                asset_id=row.image_asset_id,
            )
        if not _frozen_values_match(
            row,
            asset,
            revision,
            chunk,
            include_mutable_enrichment=require_current_acceptance,
        ):
            raise SnapshotMediaProjectionError(
                "asset_frozen_state_mismatch",
                asset_id=row.image_asset_id,
            )
        image_meta = _validated_image_chunk_meta(chunk, asset_id=asset.id)
        if row.enrichment_fingerprint != image_meta["enrichment_fingerprint"]:
            raise SnapshotMediaProjectionError(
                "enrichment_fingerprint_mismatch",
                asset_id=row.image_asset_id,
            )
        if require_current_acceptance:
            expected_image_meta = image_chunk_snapshot_meta(asset)
            if image_meta != expected_image_meta:
                raise SnapshotMediaProjectionError(
                    "enrichment_fingerprint_mismatch",
                    asset_id=row.image_asset_id,
                )
            source_entry = source_by_id[str(asset.id)]
            if source_entry != _asset_manifest_entry(asset):
                raise SnapshotMediaProjectionError(
                    "accepted_source_state_mismatch",
                    asset_id=row.image_asset_id,
                )
        expected_citation_fingerprint = citation_fingerprint(
            asset_id=asset.id,
            revision_id=revision.id,
            source_doc_id=revision.doc_id,
            source_sha256=revision.sha256,
            image_sha256=asset.image_sha256,
            page=asset.page,
            slide=asset.slide,
            bbox=asset.bbox,
        )
        if not hmac.compare_digest(
            row.citation_fingerprint,
            expected_citation_fingerprint,
        ):
            raise SnapshotMediaProjectionError(
                "invalid_citation",
                asset_id=row.image_asset_id,
            )
        try:
            ImageSourceCitation(
                asset_id=asset.id,
                revision_id=revision.id,
                source_doc_id=revision.doc_id,
                source_sha256=revision.sha256,
                image_sha256=asset.image_sha256,
                page=asset.page,
                slide=asset.slide,
                bbox=(
                    NormalizedBBox.model_validate(asset.bbox)
                    if asset.bbox is not None
                    else None
                ),
                quote_text=chunk.content[:4000],
            )
        except ValueError:
            raise SnapshotMediaProjectionError(
                "invalid_citation",
                asset_id=row.image_asset_id,
            ) from None

        if validate_objects:
            await _verify_stored_variant(asset=asset, row=row, variant="original")
            await _verify_stored_variant(asset=asset, row=row, variant="thumbnail")
            try:
                original = await resolve_image_object(
                    session,
                    request_org_id=snapshot.org_id,
                    asset_id=asset.id,
                    variant="original",
                    snapshot_id=snapshot.id,
                    allow_candidate_snapshot=True,
                )
                thumbnail = await resolve_image_object(
                    session,
                    request_org_id=snapshot.org_id,
                    asset_id=asset.id,
                    variant="thumbnail",
                    snapshot_id=snapshot.id,
                    allow_candidate_snapshot=True,
                )
            except KbImageAssetUnavailableError:
                raise SnapshotMediaProjectionError(
                    "unauthorized_media_route",
                    asset_id=row.image_asset_id,
                ) from None
            if (
                original.sha256 != row.image_sha256
                or thumbnail.sha256 != row.thumbnail_sha256
            ):
                raise SnapshotMediaProjectionError(
                    "media_route_integrity_mismatch",
                    asset_id=row.image_asset_id,
                )


async def validate_snapshot_media_projection(
    session: AsyncSession,
    snapshot: KnowledgeSnapshot,
    *,
    validate_objects: bool = True,
    require_current_acceptance: bool = True,
) -> None:
    """Validate frozen membership before any active pointer mutation."""

    required = snapshot.config_manifest.get("required_projection_builders")
    if not isinstance(required, list) or not any(
        isinstance(item, dict) and item.get("name") == "snapshot_media"
        for item in required
    ):
        return
    await session.execute(
        select(func.set_config("app.build_snapshot_id", str(snapshot.id), True))
    )

    rows = list(
        (
            await session.scalars(
                select(KbSnapshotImageAsset)
                .where(
                    KbSnapshotImageAsset.org_id == snapshot.org_id,
                    KbSnapshotImageAsset.kb_id == snapshot.kb_id,
                    KbSnapshotImageAsset.snapshot_id == snapshot.id,
                )
                .order_by(KbSnapshotImageAsset.image_asset_id)
                .with_for_update()
            )
        ).all()
    )
    expected_stats = _projection_stats(rows)
    projections = snapshot.build_stats.get("projections")
    result = projections.get("snapshot_media") if isinstance(projections, dict) else None
    stats = result.get("stats") if isinstance(result, dict) else None
    if stats != expected_stats:
        raise SnapshotMediaProjectionError("media_build_stats_mismatch")
    await _validate_rows(
        session,
        snapshot=snapshot,
        rows=rows,
        validate_objects=validate_objects,
        require_current_acceptance=require_current_acceptance,
    )


class SnapshotMediaProjectionBuilder:
    """Materialize accepted assets from exact retrieval image chunk metadata."""

    name = "snapshot_media"
    version = "1"

    async def build(
        self,
        session: AsyncSession,
        context: SnapshotBuildContext,
    ) -> Mapping[str, Any]:
        snapshot = await session.get(KnowledgeSnapshot, context.snapshot_id)
        if snapshot is None:
            raise SnapshotMediaProjectionError("missing_snapshot")
        source_entries = _source_manifest_entries(snapshot)
        source_asset_ids = [UUID(str(entry["asset_id"])) for entry in source_entries]
        rows: list[tuple[KbImageAsset, DocumentRevision, KbChunk]] = []
        if source_asset_ids:
            rows = list(
                (
                    await session.execute(
                        select(KbImageAsset, DocumentRevision, KbChunk)
                        .join(
                            DocumentRevision,
                            and_(
                                DocumentRevision.id == KbImageAsset.revision_id,
                                DocumentRevision.org_id == KbImageAsset.org_id,
                                DocumentRevision.kb_id == KbImageAsset.kb_id,
                            ),
                        )
                        .join(
                            KbChunk,
                            and_(
                                KbChunk.image_asset_id == KbImageAsset.id,
                                KbChunk.revision_id == KbImageAsset.revision_id,
                                KbChunk.org_id == KbImageAsset.org_id,
                                KbChunk.kb_id == KbImageAsset.kb_id,
                                KbChunk.snapshot_id == context.snapshot_id,
                                KbChunk.content_kind == "image",
                            ),
                        )
                        .where(
                            KbImageAsset.id.in_(source_asset_ids),
                            KbImageAsset.org_id == context.org_id,
                            KbImageAsset.kb_id == context.kb_id,
                        )
                        .order_by(KbImageAsset.id)
                        .with_for_update(of=(KbImageAsset, DocumentRevision, KbChunk))
                    )
                ).all()
            )
        if [asset.id for asset, _revision, _chunk in rows] != source_asset_ids:
            raise SnapshotMediaProjectionError("missing_image_chunk")

        values = [
            _projection_values(
                context=context,
                asset=asset,
                revision=revision,
                chunk=chunk,
            )
            for asset, revision, chunk in rows
        ]
        existing = list(
            (
                await session.scalars(
                    select(KbSnapshotImageAsset)
                    .where(KbSnapshotImageAsset.snapshot_id == context.snapshot_id)
                    .order_by(KbSnapshotImageAsset.image_asset_id)
                )
            ).all()
        )
        if existing:
            expected = [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"id"}
                }
                for row in values
            ]
            actual = [
                {
                    key: getattr(row, key)
                    for key in expected[0]
                }
                for row in existing
            ] if expected else []
            if actual != expected:
                raise SnapshotMediaProjectionError("existing_membership_mismatch")
        else:
            session.add_all(KbSnapshotImageAsset(**value) for value in values)
            await session.flush()
            existing = list(
                (
                    await session.scalars(
                        select(KbSnapshotImageAsset)
                        .where(KbSnapshotImageAsset.snapshot_id == context.snapshot_id)
                        .order_by(KbSnapshotImageAsset.image_asset_id)
                    )
                ).all()
            )

        await _validate_rows(
            session,
            snapshot=snapshot,
            rows=existing,
            validate_objects=True,
            require_current_acceptance=True,
        )
        return _projection_stats(existing)


__all__ = [
    "SnapshotMediaProjectionBuilder",
    "SnapshotMediaProjectionError",
    "citation_fingerprint",
    "image_projection_source_manifest",
    "validate_snapshot_media_projection",
]
