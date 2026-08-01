"""Content-addressed KB image storage and fail-closed resolution tests."""

import io
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from minio.error import S3Error
from PIL import Image

from nicekit.kb import image_assets
from nicekit.kb.image_assets import (
    KbImageAssetUnavailableError,
    KbImageObjectIntegrityError,
)
from nicekit.kb.image_validation import validate_kb_image


@pytest.fixture(autouse=True)
def _active_kb_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    async def capture(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(
        image_assets,
        "capture_active_knowledge_base_lease",
        capture,
    )


def _image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (800, 400),
    color=(20, 80, 160, 255),
) -> bytes:
    mode = "RGBA" if image_format in {"PNG", "WEBP"} else "RGB"
    image = Image.new(mode, size, color=color if mode == "RGBA" else color[:3])
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _missing() -> S3Error:
    return S3Error(Mock(), "NoSuchKey", "missing", None, None, None)


@pytest.mark.parametrize("image_format", ("PNG", "JPEG", "WEBP"))
def test_thumbnail_is_bounded_webp_and_deterministic(
    image_format: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        image_assets,
        "get_settings",
        lambda: SimpleNamespace(
            kb_image_thumbnail_max_dimension=256,
            kb_image_max_bytes=20 * 1024 * 1024,
            kb_image_max_dimension=20_000,
            kb_image_max_pixels=50_000_000,
        ),
    )
    original = validate_kb_image(_image_bytes(image_format))

    first = image_assets.create_thumbnail(original)
    second = image_assets.create_thumbnail(original)

    assert first.content_type == "image/webp"
    assert max(first.width, first.height) <= 256
    assert first.data == second.data
    assert first.sha256 == second.sha256


def test_content_addressed_paths_are_hash_scoped_and_deterministic() -> None:
    org_id, kb_id = uuid4(), uuid4()
    sha256 = "ab" + ("0" * 62)

    original = image_assets.original_object_key(org_id, kb_id, sha256, "image/png")
    thumbnail = image_assets.thumbnail_object_key(org_id, kb_id, sha256)

    assert original == (
        f"{org_id}/kb/{kb_id}/image-assets/originals/sha256/ab/{sha256}.png"
    )
    assert thumbnail == (
        f"{org_id}/kb/{kb_id}/image-assets/thumbnails/sha256/ab/{sha256}.webp"
    )
    assert ".." not in original and ".." not in thumbnail


async def test_store_image_objects_is_idempotent_and_verifies_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects: dict[str, bytes] = {}
    puts: list[tuple[str, str]] = []

    async def get_object(key: str) -> bytes:
        if key not in objects:
            raise _missing()
        return objects[key]

    async def put_object(key: str, data: bytes, content_type: str) -> None:
        puts.append((key, content_type))
        objects[key] = data

    monkeypatch.setattr(image_assets.storage, "get_object", get_object)
    monkeypatch.setattr(image_assets.storage, "put_object", put_object)
    original = validate_kb_image(_image_bytes())
    org_id, kb_id = uuid4(), uuid4()

    first = await image_assets.store_image_objects(
        org_id=org_id,
        kb_id=kb_id,
        image=original,
    )
    second = await image_assets.store_image_objects(
        org_id=org_id,
        kb_id=kb_id,
        image=original,
    )

    assert first == second
    assert len(puts) == 2
    assert len(objects) == 2
    assert first.original_object_key in objects
    assert first.thumbnail_object_key in objects


async def test_store_refuses_to_overwrite_corrupt_existing_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = validate_kb_image(_image_bytes())
    puts: list[str] = []

    async def get_object(key: str) -> bytes:
        return b"corrupt"

    async def put_object(key: str, data: bytes, content_type: str) -> None:
        puts.append(key)

    monkeypatch.setattr(image_assets.storage, "get_object", get_object)
    monkeypatch.setattr(image_assets.storage, "put_object", put_object)

    with pytest.raises(KbImageObjectIntegrityError) as caught:
        await image_assets.store_image_objects(
            org_id=uuid4(),
            kb_id=uuid4(),
            image=original,
        )

    assert caught.value.code == "object_size_mismatch"
    assert puts == []


def _asset_for(data: bytes):
    validated = validate_kb_image(data)
    org_id, kb_id, asset_id = uuid4(), uuid4(), uuid4()
    return SimpleNamespace(
        id=asset_id,
        org_id=org_id,
        kb_id=kb_id,
        image_sha256=validated.sha256,
        size_bytes=validated.size_bytes,
        content_type=validated.content_type,
        width=validated.width,
        height=validated.height,
        original_object_key=image_assets.original_object_key(
            org_id,
            kb_id,
            validated.sha256,
            validated.content_type,
        ),
        thumbnail_object_key=None,
        thumbnail_sha256=None,
        thumbnail_content_type=None,
        thumbnail_size_bytes=None,
        thumbnail_width=None,
        thumbnail_height=None,
    )


async def test_resolve_visible_asset_returns_integrity_checked_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _image_bytes()
    asset = _asset_for(data)

    async def load_asset(*args, **kwargs):
        return asset

    async def get_object(key: str) -> bytes:
        assert key == asset.original_object_key
        return data

    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(image_assets.storage, "get_object", get_object)

    resolved = await image_assets.resolve_image_object(
        object(),  # type: ignore[arg-type]
        request_org_id=uuid4(),
        asset_id=asset.id,
        variant="original",
    )

    assert resolved.data == data
    assert resolved.content_type == "image/png"
    assert resolved.etag == f'"{asset.image_sha256}"'
    assert resolved.filename == f"kb-image-{asset.id}.png"


async def test_resolve_cross_tenant_or_invisible_asset_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_asset(*args, **kwargs):
        return None

    async def unexpected(key: str) -> bytes:
        raise AssertionError("storage must not be queried")

    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(image_assets.storage, "get_object", unexpected)

    with pytest.raises(KbImageAssetUnavailableError):
        await image_assets.resolve_image_object(
            object(),  # type: ignore[arg-type]
            request_org_id=uuid4(),
            asset_id=uuid4(),
            variant="original",
        )


async def test_resolve_holds_lifecycle_lock_before_object_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _image_bytes()
    asset = _asset_for(data)
    captured: dict = {}

    async def load_asset(*_args, **_kwargs):
        return asset

    async def capture(session, **kwargs):
        captured.update({"session": session, **kwargs})
        return SimpleNamespace()

    async def get_object(_key: str) -> bytes:
        return data

    session = object()
    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(
        image_assets,
        "capture_active_knowledge_base_lease",
        capture,
    )
    monkeypatch.setattr(image_assets.storage, "get_object", get_object)

    await image_assets.resolve_image_object(
        session,  # type: ignore[arg-type]
        request_org_id=uuid4(),
        asset_id=asset.id,
        variant="original",
    )

    assert captured == {
        "session": session,
        "kb_id": asset.kb_id,
        "owner_org_id": asset.org_id,
        "lock": True,
    }


async def test_resolve_rejects_prefix_mismatch_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset_for(_image_bytes())
    asset.original_object_key = f"{uuid4()}/other-tenant.png"

    async def load_asset(*args, **kwargs):
        return asset

    async def unexpected(key: str) -> bytes:
        raise AssertionError("storage must not be queried")

    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(image_assets.storage, "get_object", unexpected)

    with pytest.raises(KbImageAssetUnavailableError):
        await image_assets.resolve_image_object(
            object(),  # type: ignore[arg-type]
            request_org_id=uuid4(),
            asset_id=asset.id,
            variant="original",
        )


@pytest.mark.parametrize("failure", ("missing", "hash"))
async def test_resolve_missing_or_hash_mismatched_object_fails_closed(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset_for(_image_bytes())

    async def load_asset(*args, **kwargs):
        return asset

    async def get_object(key: str) -> bytes:
        if failure == "missing":
            raise _missing()
        return _image_bytes(color=(200, 10, 10, 255))

    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(image_assets.storage, "get_object", get_object)

    with pytest.raises(KbImageAssetUnavailableError):
        await image_assets.resolve_image_object(
            object(),  # type: ignore[arg-type]
            request_org_id=uuid4(),
            asset_id=asset.id,
            variant="original",
        )


async def test_requested_snapshot_must_be_active_and_contain_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset_for(_image_bytes())

    async def load_asset(*args, **kwargs):
        return asset

    async def snapshot_allows(*args, **kwargs):
        return False

    async def unexpected(key: str) -> bytes:
        raise AssertionError("storage must not be queried")

    monkeypatch.setattr(image_assets, "_load_visible_asset", load_asset)
    monkeypatch.setattr(image_assets, "_snapshot_allows_asset", snapshot_allows)
    monkeypatch.setattr(image_assets.storage, "get_object", unexpected)

    with pytest.raises(KbImageAssetUnavailableError):
        await image_assets.resolve_image_object(
            object(),  # type: ignore[arg-type]
            request_org_id=uuid4(),
            asset_id=asset.id,
            variant="original",
            snapshot_id=uuid4(),
        )
