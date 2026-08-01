"""Authenticated KB image delivery API contract."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nicekit.api.v1 import kb_media
from nicekit.kb.image_assets import (
    KbImageAssetUnavailableError,
    ResolvedImageObject,
)

# TF 经验:不 import 组装好的 app(也不用 with TestClient(app)——会触发
# lifespan 卡死)。轻量 FastAPI 只挂本用例需要的 router,依赖全部由
# dependency_overrides 假注入。
app = FastAPI()
app.include_router(kb_media.router, prefix="/api/v1")


def _resolved() -> ResolvedImageObject:
    return ResolvedImageObject(
        asset_id=uuid4(),
        data=b"verified-image",
        content_type="image/webp",
        filename="kb-image-safe.webp",
        sha256="a" * 64,
    )


def test_media_routes_require_authentication() -> None:
    client = TestClient(app)
    asset_id = uuid4()

    assert client.get(f"/api/v1/kb/image-assets/{asset_id}/content").status_code == 401
    assert client.get(f"/api/v1/kb/image-assets/{asset_id}/thumbnail").status_code == 401


async def test_authorized_content_has_private_security_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _resolved()

    async def resolve(*args, **kwargs):
        return image

    monkeypatch.setattr(kb_media, "resolve_image_object", resolve)

    response = await kb_media.get_image_content(
        image.asset_id,
        SimpleNamespace(org_id=uuid4()),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert response.body == image.data
    assert response.media_type == "image/webp"
    assert response.headers["etag"] == image.etag
    assert response.headers["cache-control"].startswith("private")
    assert response.headers["content-disposition"] == (
        'inline; filename="kb-image-safe.webp"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["vary"] == "Authorization"
    assert "object_key" not in response.headers


async def test_if_none_match_returns_304_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _resolved()

    async def resolve(*args, **kwargs):
        return image

    monkeypatch.setattr(kb_media, "resolve_image_object", resolve)

    response = await kb_media.get_image_thumbnail(
        image.asset_id,
        SimpleNamespace(org_id=uuid4()),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        if_none_match=f'W/{image.etag}',
    )

    assert response.status_code == 304
    assert response.body == b""
    assert response.headers["etag"] == image.etag
    assert response.headers["cache-control"].startswith("private")


async def test_unavailable_asset_returns_uniform_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*args, **kwargs):
        raise KbImageAssetUnavailableError("object_prefix_mismatch")

    monkeypatch.setattr(kb_media, "resolve_image_object", unavailable)

    with pytest.raises(HTTPException) as caught:
        await kb_media.get_image_content(
            uuid4(),
            SimpleNamespace(org_id=uuid4()),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == "图片资产不存在"
    assert "prefix" not in str(caught.value.detail)
