"""Private storage and authorized resolution for governed KB image assets."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from minio.error import S3Error
from PIL import Image, ImageOps
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.kb import ports, storage
from nicekit.kb.effective_scope import live_document_revision_filter
from nicekit.kb.eligibility import capture_active_knowledge_base_lease
from nicekit.kb.image_validation import ValidatedKbImage, validate_kb_image
from nicekit.models.kb import (
    DocumentRevision,
    KbImageAsset,
    KbShare,
    KbSnapshotImageAsset,
    KnowledgeBase,
    KnowledgeBaseLifecycleStatus,
    KnowledgeSnapshot,
    SnapshotStatus,
    SourceDocument,
)

logger = logging.getLogger(__name__)

ImageVariant = Literal["original", "thumbnail"]

_HASH = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_TYPE_EXTENSION = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})
_THUMBNAIL_CONTENT_TYPE = "image/webp"
_THUMBNAIL_EXTENSION = "webp"
_THUMBNAIL_QUALITY = 82
_THUMBNAIL_METHOD = 6


class KbImageObjectIntegrityError(RuntimeError):
    """Sanitized object-integrity failure safe for operational classification."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KbImageAssetUnavailableError(LookupError):
    """Asset is absent, unauthorized, ineligible, or fails closed validation."""

    def __init__(self, code: str = "image_asset_unavailable") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredImageObjects:
    original: ValidatedKbImage
    original_object_key: str
    thumbnail: ValidatedKbImage
    thumbnail_object_key: str


@dataclass(frozen=True, slots=True)
class ResolvedImageObject:
    asset_id: UUID
    data: bytes
    content_type: str
    filename: str
    sha256: str

    @property
    def etag(self) -> str:
        return f'"{self.sha256}"'


def image_asset_prefix(org_id: UUID, kb_id: UUID) -> str:
    return f"{org_id}/kb/{kb_id}/image-assets/"


def original_object_key(
    org_id: UUID,
    kb_id: UUID,
    sha256: str,
    content_type: str,
) -> str:
    extension = _extension_for(content_type)
    _require_sha256(sha256)
    return (
        f"{image_asset_prefix(org_id, kb_id)}originals/sha256/"
        f"{sha256[:2]}/{sha256}.{extension}"
    )


def thumbnail_object_key(
    org_id: UUID,
    kb_id: UUID,
    sha256: str,
) -> str:
    _require_sha256(sha256)
    return (
        f"{image_asset_prefix(org_id, kb_id)}thumbnails/sha256/"
        f"{sha256[:2]}/{sha256}.{_THUMBNAIL_EXTENSION}"
    )


def _require_sha256(value: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")


def _extension_for(content_type: str) -> str:
    try:
        return _CONTENT_TYPE_EXTENSION[content_type]
    except KeyError as exc:
        raise ValueError("unsupported image content type") from exc


def create_thumbnail(image: ValidatedKbImage) -> ValidatedKbImage:
    """Create deterministic, orientation-corrected WebP bytes within the configured edge."""
    max_dimension = get_settings().kb_image_thumbnail_max_dimension
    with Image.open(io.BytesIO(image.data)) as source:
        thumbnail = ImageOps.exif_transpose(source)
        thumbnail.thumbnail(
            (max_dimension, max_dimension),
            resample=Image.Resampling.LANCZOS,
        )
        if "A" in thumbnail.getbands() or "transparency" in thumbnail.info:
            thumbnail = thumbnail.convert("RGBA")
        else:
            thumbnail = thumbnail.convert("RGB")
        output = io.BytesIO()
        thumbnail.save(
            output,
            format="WEBP",
            quality=_THUMBNAIL_QUALITY,
            method=_THUMBNAIL_METHOD,
        )
    return validate_kb_image(
        output.getvalue(),
        max_dimension=max_dimension,
        max_pixels=max_dimension * max_dimension,
    )


def _is_missing_object(exc: S3Error) -> bool:
    return exc.code in _MISSING_OBJECT_CODES


def _verify_bytes(data: bytes, *, expected_sha256: str, expected_size: int) -> None:
    if len(data) != expected_size:
        raise KbImageObjectIntegrityError("object_size_mismatch")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
        raise KbImageObjectIntegrityError("object_hash_mismatch")


async def _ensure_content_addressed_object(
    key: str,
    image: ValidatedKbImage,
) -> None:
    try:
        existing = await storage.get_object(key)
    except S3Error as exc:
        if not _is_missing_object(exc):
            raise
    else:
        _verify_bytes(
            existing,
            expected_sha256=image.sha256,
            expected_size=image.size_bytes,
        )
        return

    await storage.put_object(key, image.data, image.content_type)
    try:
        persisted = await storage.get_object(key)
    except S3Error as exc:
        raise KbImageObjectIntegrityError("object_missing_after_write") from exc
    _verify_bytes(
        persisted,
        expected_sha256=image.sha256,
        expected_size=image.size_bytes,
    )


async def store_image_objects(
    *,
    org_id: UUID,
    kb_id: UUID,
    image: ValidatedKbImage,
) -> StoredImageObjects:
    """Idempotently persist validated original and thumbnail by their content hashes."""
    thumbnail = create_thumbnail(image)
    original_key = original_object_key(
        org_id,
        kb_id,
        image.sha256,
        image.content_type,
    )
    thumbnail_key = thumbnail_object_key(org_id, kb_id, thumbnail.sha256)
    await _ensure_content_addressed_object(original_key, image)
    await _ensure_content_addressed_object(thumbnail_key, thumbnail)
    return StoredImageObjects(
        original=image,
        original_object_key=original_key,
        thumbnail=thumbnail,
        thumbnail_object_key=thumbnail_key,
    )


def visible_kb_filter(request_org_id: UUID):
    platform_org_id = get_settings().platform_org_id
    shared = exists(
        select(KbShare.id).where(
            KbShare.kb_id == KnowledgeBase.id,
            KbShare.grantee_org_id == request_org_id,
        )
    )
    return and_(
        KnowledgeBase.lifecycle_status == KnowledgeBaseLifecycleStatus.ACTIVE.value,
        or_(
            KnowledgeBase.org_id == request_org_id,
            KnowledgeBase.org_id == platform_org_id,
            shared,
        ),
    )


async def _load_visible_asset(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    asset_id: UUID,
) -> KbImageAsset | None:
    return await session.scalar(
        select(KbImageAsset)
        .join(
            KnowledgeBase,
            and_(
                KnowledgeBase.id == KbImageAsset.kb_id,
                KnowledgeBase.org_id == KbImageAsset.org_id,
            ),
        )
        .join(
            DocumentRevision,
            and_(
                DocumentRevision.id == KbImageAsset.revision_id,
                DocumentRevision.doc_id == KbImageAsset.doc_id,
                DocumentRevision.org_id == KbImageAsset.org_id,
                DocumentRevision.kb_id == KbImageAsset.kb_id,
            ),
        )
        .join(
            SourceDocument,
            and_(
                SourceDocument.id == KbImageAsset.doc_id,
                SourceDocument.id == DocumentRevision.doc_id,
                SourceDocument.org_id == KbImageAsset.org_id,
                SourceDocument.kb_id == KbImageAsset.kb_id,
            ),
        )
        .where(
            KbImageAsset.id == asset_id,
            live_document_revision_filter(),
            visible_kb_filter(request_org_id),
        )
        .limit(1)
    )


async def _snapshot_allows_asset(
    session: AsyncSession,
    *,
    asset: KbImageAsset,
    snapshot_id: UUID,
    allow_candidate_snapshot: bool,
) -> bool:
    membership = await session.scalar(
        select(KbSnapshotImageAsset.id)
        .join(
            KnowledgeSnapshot,
            and_(
                KnowledgeSnapshot.id == KbSnapshotImageAsset.snapshot_id,
                KnowledgeSnapshot.org_id == KbSnapshotImageAsset.org_id,
                KnowledgeSnapshot.kb_id == KbSnapshotImageAsset.kb_id,
            ),
        )
        .join(
            KnowledgeBase,
            and_(
                KnowledgeBase.id == KnowledgeSnapshot.kb_id,
                KnowledgeBase.org_id == KnowledgeSnapshot.org_id,
            ),
        )
        .where(
            KbSnapshotImageAsset.snapshot_id == snapshot_id,
            KbSnapshotImageAsset.image_asset_id == asset.id,
            KbSnapshotImageAsset.org_id == asset.org_id,
            KbSnapshotImageAsset.kb_id == asset.kb_id,
            *(
                (
                    KnowledgeSnapshot.status.in_(
                        (
                            SnapshotStatus.BUILDING.value,
                            SnapshotStatus.READY.value,
                            SnapshotStatus.ACTIVE.value,
                            SnapshotStatus.RETIRED.value,
                        )
                    ),
                )
                if allow_candidate_snapshot
                else (
                    KnowledgeSnapshot.status == SnapshotStatus.ACTIVE.value,
                    KnowledgeBase.active_snapshot_id == snapshot_id,
                    KnowledgeBase.lifecycle_status == "active",
                )
            ),
        )
        .limit(1)
    )
    return membership is not None


def _variant_metadata(
    asset: KbImageAsset,
    variant: ImageVariant,
) -> tuple[str, str, str, int, int, int] | None:
    if variant == "original":
        values = (
            asset.original_object_key,
            asset.image_sha256,
            asset.content_type,
            asset.size_bytes,
            asset.width,
            asset.height,
        )
    else:
        values = (
            asset.thumbnail_object_key,
            asset.thumbnail_sha256,
            asset.thumbnail_content_type,
            asset.thumbnail_size_bytes,
            asset.thumbnail_width,
            asset.thumbnail_height,
        )
    if any(value is None for value in values):
        return None
    key, sha256, content_type, size, width, height = values
    return (
        str(key),
        str(sha256),
        str(content_type),
        int(size),
        int(width),
        int(height),
    )


def _expected_object_key(
    asset: KbImageAsset,
    *,
    variant: ImageVariant,
    sha256: str,
    content_type: str,
) -> str:
    if variant == "original":
        return original_object_key(asset.org_id, asset.kb_id, sha256, content_type)
    if content_type != _THUMBNAIL_CONTENT_TYPE:
        raise ValueError("thumbnail content type is not WebP")
    return thumbnail_object_key(asset.org_id, asset.kb_id, sha256)


async def _record_integrity_failure(
    session: AsyncSession,
    asset: KbImageAsset,
    code: str,
) -> None:
    logger.warning(
        "KB image object consistency failure",
        extra={"asset_id": str(asset.id), "reason_code": code},
    )
    # Unit callers may use a minimal sentinel session. Real request/worker
    # sessions durably upsert the sanitized incident in the current transaction.
    if not isinstance(session, AsyncSession):
        return
    # 事件表属 operations 子系统(MIGRATION-PLAN A1),走 ports.IncidentRecorder;
    # 未注册实现时静默跳过——登记是诊断,不改变业务结果。
    await ports.record_incident(
        session,
        org_id=asset.org_id,
        kb_id=asset.kb_id,
        image_asset_id=asset.id,
        category="object_metadata_inconsistency",
        code=code,
    )


async def resolve_image_object(
    session: AsyncSession,
    *,
    request_org_id: UUID,
    asset_id: UUID,
    variant: ImageVariant,
    snapshot_id: UUID | None = None,
    allow_candidate_snapshot: bool = False,
    lock_lifecycle: bool = True,
) -> ResolvedImageObject:
    asset = await _load_visible_asset(
        session,
        request_org_id=request_org_id,
        asset_id=asset_id,
    )
    if asset is None:
        raise KbImageAssetUnavailableError("asset_unavailable")
    if (
        await capture_active_knowledge_base_lease(
            session,
            kb_id=asset.kb_id,
            owner_org_id=asset.org_id,
            lock=lock_lifecycle,
        )
        is None
    ):
        raise KbImageAssetUnavailableError("asset_unavailable")
    if snapshot_id is not None and not await _snapshot_allows_asset(
        session,
        asset=asset,
        snapshot_id=snapshot_id,
        allow_candidate_snapshot=allow_candidate_snapshot,
    ):
        raise KbImageAssetUnavailableError("snapshot_unavailable")

    metadata = _variant_metadata(asset, variant)
    if metadata is None:
        raise KbImageAssetUnavailableError("object_metadata_incomplete")
    key, sha256, content_type, size, width, height = metadata
    try:
        expected_key = _expected_object_key(
            asset,
            variant=variant,
            sha256=sha256,
            content_type=content_type,
        )
    except ValueError as exc:
        await _record_integrity_failure(session, asset, "invalid_object_metadata")
        raise KbImageAssetUnavailableError("invalid_object_metadata") from exc
    expected_prefix = image_asset_prefix(asset.org_id, asset.kb_id)
    if not key.startswith(expected_prefix) or not hmac.compare_digest(key, expected_key):
        await _record_integrity_failure(session, asset, "object_prefix_mismatch")
        raise KbImageAssetUnavailableError("object_prefix_mismatch")

    try:
        data = await storage.get_object(key)
    except S3Error as exc:
        code = "object_missing" if _is_missing_object(exc) else "object_storage_unavailable"
        await _record_integrity_failure(session, asset, code)
        raise KbImageAssetUnavailableError(code) from exc
    try:
        _verify_bytes(data, expected_sha256=sha256, expected_size=size)
        validated = validate_kb_image(data)
    except (KbImageObjectIntegrityError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_object_bytes")
        await _record_integrity_failure(session, asset, str(code))
        raise KbImageAssetUnavailableError(str(code)) from exc
    if (
        validated.content_type != content_type
        or validated.width != width
        or validated.height != height
    ):
        await _record_integrity_failure(session, asset, "object_metadata_mismatch")
        raise KbImageAssetUnavailableError("object_metadata_mismatch")

    extension = _extension_for(content_type)
    return ResolvedImageObject(
        asset_id=asset.id,
        data=data,
        content_type=content_type,
        filename=f"kb-image-{asset.id}.{extension}",
        sha256=sha256,
    )


__all__ = [
    "ImageVariant",
    "KbImageAssetUnavailableError",
    "KbImageObjectIntegrityError",
    "ResolvedImageObject",
    "StoredImageObjects",
    "create_thumbnail",
    "image_asset_prefix",
    "original_object_key",
    "resolve_image_object",
    "store_image_objects",
    "thumbnail_object_key",
    "visible_kb_filter",
]
