"""对象存储:MinIO/S3,key 按 {org_id}/ 前缀隔离。"""

import io
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import anyio
from minio import Minio
from minio.error import S3Error

from nicekit.core.config import get_settings


@dataclass(frozen=True, slots=True)
class ObjectIdentity:
    exists: bool
    version_id: str | None
    etag: str | None


class ObjectIdentityChanged(RuntimeError):
    """Raised when a key no longer identifies the preflight object."""


@lru_cache
def _client() -> Minio:
    s = get_settings()
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
    )


@lru_cache
def _ensure_bucket() -> bool:
    """进程内一次性:外部 MinIO(非 compose init)不保证 bucket 预建。"""
    s = get_settings()
    client = _client()
    if not client.bucket_exists(s.minio_bucket):
        client.make_bucket(s.minio_bucket)
    return True


def kb_object_key(org_id: UUID, doc_id: UUID, filename: str) -> str:
    return f"{org_id}/kb/{doc_id}/{filename}"


def kb_revision_object_key(org_id: UUID, doc_id: UUID, revision_id: UUID, filename: str) -> str:
    return f"{org_id}/kb/{doc_id}/revisions/{revision_id}/{filename}"


def kb_markdown_key(org_id: UUID, kb_id: UUID, doc_id: UUID) -> str:
    """统一 Markdown 中间表示(KB-1)的落档 key,org 前缀隔离与原文件一致。"""
    return f"{org_id}/kb/{kb_id}/markdown/{doc_id}.md"


def kb_revision_markdown_key(
    org_id: UUID, kb_id: UUID, doc_id: UUID, revision_id: UUID, attempt_id: str
) -> str:
    """Attempt-scoped staging artifact; only a fenced DB pointer makes it visible."""
    return f"{org_id}/kb/{kb_id}/staging/{doc_id}/{revision_id}/{attempt_id}/document.md"


def kb_revision_structured_json_key(
    org_id: UUID, kb_id: UUID, doc_id: UUID, revision_id: UUID, attempt_id: str
) -> str:
    """Lossless parser artifact staged alongside its derived Markdown."""
    return f"{org_id}/kb/{kb_id}/staging/{doc_id}/{revision_id}/{attempt_id}/document.json"


def kb_revision_chunks_key(
    org_id: UUID, kb_id: UUID, doc_id: UUID, revision_id: UUID, attempt_id: str
) -> str:
    """Chunk staging artifact consumed by the later snapshot materializer."""
    return f"{org_id}/kb/{kb_id}/staging/{doc_id}/{revision_id}/{attempt_id}/chunks.json"


async def put_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    s = get_settings()

    def _put() -> None:
        _ensure_bucket()
        _client().put_object(
            s.minio_bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )

    await anyio.to_thread.run_sync(_put)


async def bucket_exists() -> bool:
    """Check the configured private bucket without creating or mutating it."""
    s = get_settings()
    return bool(
        await anyio.to_thread.run_sync(
            _client().bucket_exists,
            s.minio_bucket,
            abandon_on_cancel=True,
        )
    )


async def get_object(key: str) -> bytes:
    s = get_settings()

    def _get() -> bytes:
        resp = _client().get_object(s.minio_bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    return await anyio.to_thread.run_sync(_get)


async def object_exists(key: str) -> bool:
    """Return whether an object exists without downloading its content."""

    settings = get_settings()

    def _stat() -> bool:
        try:
            _client().stat_object(settings.minio_bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise
        return True

    return bool(await anyio.to_thread.run_sync(_stat))


async def stat_object_identity(key: str) -> ObjectIdentity:
    """Capture the immutable identity used by a purge manifest."""

    settings = get_settings()

    def _stat() -> ObjectIdentity:
        try:
            result = _client().stat_object(settings.minio_bucket, key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return ObjectIdentity(False, None, None)
            raise
        return ObjectIdentity(
            True,
            getattr(result, "version_id", None),
            getattr(result, "etag", None),
        )

    return await anyio.to_thread.run_sync(_stat)


async def remove_object_if_unchanged(
    key: str,
    version_id: str | None,
    etag: str | None,
    expected_missing: bool,
) -> None:
    """Delete only the object version captured by deletion preflight."""

    settings = get_settings()

    def _remove() -> None:
        try:
            current = _client().stat_object(
                settings.minio_bucket,
                key,
            )
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return
            raise
        if expected_missing:
            raise ObjectIdentityChanged("object appeared after deletion preflight")
        current_version = getattr(current, "version_id", None)
        current_etag = getattr(current, "etag", None)
        if version_id is not None and current_version != version_id:
            raise ObjectIdentityChanged("object version changed after deletion preflight")
        if etag is not None and current_etag != etag:
            raise ObjectIdentityChanged("object etag changed after deletion preflight")
        _client().remove_object(
            settings.minio_bucket,
            key,
            version_id=version_id,
        )

    await anyio.to_thread.run_sync(_remove)


async def remove_object(key: str) -> None:
    """Delete an object idempotently.

    S3-compatible DELETE already treats an absent key as success. Keeping this
    small wrapper lets purge workers retry after a partial object-store phase.
    """

    settings = get_settings()

    def _remove() -> None:
        try:
            _client().remove_object(settings.minio_bucket, key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise

    await anyio.to_thread.run_sync(_remove)
