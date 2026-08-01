"""Bounded validation for knowledge-base image payloads."""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from nicekit.core.config import get_settings

_FORMAT_METADATA = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}


@dataclass(frozen=True, slots=True)
class ValidatedKbImage:
    data: bytes
    content_type: str
    extension: str
    size_bytes: int
    width: int
    height: int
    sha256: str


class KbImageValidationError(ValueError):
    """Browser-safe validation failure with stable diagnostic metadata."""

    def __init__(self, code: str, message: str, *, data: bytes) -> None:
        super().__init__(message)
        self.code = code
        self.size_bytes = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()


def validate_kb_image(
    data: bytes,
    *,
    max_bytes: int | None = None,
    max_dimension: int | None = None,
    max_pixels: int | None = None,
) -> ValidatedKbImage:
    """Decode a PNG/JPEG/WebP payload and derive trusted metadata.

    File names and caller-supplied MIME types are deliberately ignored.
    """

    settings = get_settings()
    byte_limit = (
        settings.kb_image_max_bytes if max_bytes is None else max_bytes
    )
    dimension_limit = (
        settings.kb_image_max_dimension
        if max_dimension is None
        else max_dimension
    )
    pixel_limit = (
        settings.kb_image_max_pixels if max_pixels is None else max_pixels
    )

    if not data:
        raise KbImageValidationError("empty_image", "image payload is empty", data=data)
    if len(data) > byte_limit:
        raise KbImageValidationError(
            "image_too_large",
            "image payload exceeds the configured byte limit",
            data=data,
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                image_format = (source.format or "").upper()
                width, height = source.size
                if image_format not in _FORMAT_METADATA:
                    raise KbImageValidationError(
                        "unsupported_format",
                        "image format is not PNG, JPEG, or WebP",
                        data=data,
                    )
                if (
                    width <= 0
                    or height <= 0
                    or width > dimension_limit
                    or height > dimension_limit
                    or width * height > pixel_limit
                ):
                    raise KbImageValidationError(
                        "invalid_dimensions",
                        "image dimensions exceed the configured limits",
                        data=data,
                    )
                source.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
    except KbImageValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as exc:
        raise KbImageValidationError(
            "malformed_image",
            "image payload cannot be decoded",
            data=data,
        ) from exc

    content_type, extension = _FORMAT_METADATA[image_format]
    return ValidatedKbImage(
        data=data,
        content_type=content_type,
        extension=extension,
        size_bytes=len(data),
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def is_decorative_image(
    width: int,
    height: int,
    *,
    min_dimension: int | None = None,
    min_pixels: int | None = None,
) -> bool:
    """判定插图是否为装饰性图标(项目符号/箭头/分隔线),这类图不值得 OCR 与 caption。"""
    settings = get_settings()
    dimension_floor = (
        settings.kb_image_min_dimension if min_dimension is None else min_dimension
    )
    pixel_floor = settings.kb_image_min_pixels if min_pixels is None else min_pixels
    if dimension_floor and min(width, height) < dimension_floor:
        return True
    return bool(pixel_floor and width * height < pixel_floor)


__all__ = [
    "KbImageValidationError",
    "ValidatedKbImage",
    "is_decorative_image",
    "validate_kb_image",
]
