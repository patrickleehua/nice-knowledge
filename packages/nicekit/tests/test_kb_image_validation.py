import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from nicekit.kb import image_validation
from nicekit.kb.image_validation import (
    KbImageValidationError,
    validate_kb_image,
)


@pytest.fixture(autouse=True)
def _configured_image_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        image_validation,
        "get_settings",
        lambda: SimpleNamespace(
            kb_image_max_bytes=20 * 1024 * 1024,
            kb_image_max_dimension=20_000,
            kb_image_max_pixels=50_000_000,
        ),
    )


def _image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (16, 12),
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("image_format", "content_type", "extension"),
    [
        ("PNG", "image/png", "png"),
        ("JPEG", "image/jpeg", "jpg"),
        ("WEBP", "image/webp", "webp"),
    ],
)
def test_validate_kb_image_detects_trusted_metadata(
    image_format: str,
    content_type: str,
    extension: str,
) -> None:
    data = _image_bytes(image_format)
    validated = validate_kb_image(data)

    assert validated.data == data
    assert validated.content_type == content_type
    assert validated.extension == extension
    assert validated.size_bytes == len(data)
    assert (validated.width, validated.height) == (16, 12)
    assert validated.sha256 == hashlib.sha256(data).hexdigest()


def test_validate_kb_image_rejects_empty_and_oversized_payloads() -> None:
    with pytest.raises(KbImageValidationError) as empty:
        validate_kb_image(b"")
    assert empty.value.code == "empty_image"

    data = _image_bytes()
    with pytest.raises(KbImageValidationError) as oversized:
        validate_kb_image(data, max_bytes=len(data) - 1)
    assert oversized.value.code == "image_too_large"
    assert oversized.value.size_bytes == len(data)
    assert oversized.value.sha256 == hashlib.sha256(data).hexdigest()


def test_validate_kb_image_rejects_malformed_and_unsupported_bytes() -> None:
    malformed = b"not-an-image"
    with pytest.raises(KbImageValidationError) as caught:
        validate_kb_image(malformed)
    assert caught.value.code == "malformed_image"

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="BMP")
    with pytest.raises(KbImageValidationError) as unsupported:
        validate_kb_image(buffer.getvalue())
    assert unsupported.value.code == "unsupported_format"


def test_validate_kb_image_enforces_dimension_and_pixel_limits() -> None:
    data = _image_bytes(size=(20, 10))
    with pytest.raises(KbImageValidationError) as dimension:
        validate_kb_image(data, max_dimension=19)
    assert dimension.value.code == "invalid_dimensions"

    with pytest.raises(KbImageValidationError) as pixels:
        validate_kb_image(data, max_pixels=199)
    assert pixels.value.code == "invalid_dimensions"


def test_is_decorative_image_filters_icon_sized_illustrations() -> None:
    """项目符号/箭头这类图标不值得 OCR 与 VLM caption:任一边或面积低于下限即判定装饰性。"""
    # 实测样本:13x15、20x13 这类碎片 OCR 命中恒为空
    assert image_validation.is_decorative_image(
        13, 15, min_dimension=64, min_pixels=16_384
    )
    # 边长达标但面积过小(细长分隔线)
    assert image_validation.is_decorative_image(
        400, 40, min_dimension=64, min_pixels=16_384
    )
    # 正文插图必须保留
    assert not image_validation.is_decorative_image(
        616, 355, min_dimension=64, min_pixels=16_384
    )
    # 阈值置 0 表示关闭过滤,任何尺寸都不算装饰性
    assert not image_validation.is_decorative_image(
        4, 4, min_dimension=0, min_pixels=0
    )
