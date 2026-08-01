"""Image generation configuration, validation, storage, and usage metering."""

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlparse
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.capabilities.imagegen.base import (
    MAX_IMAGE_BYTES,
    SIZES,
    ImageBlob,
    ImageGenOutcome,
    ImageGenProvider,
    ImageGenQuery,
)
from nicekit.capabilities.imagegen.openai_compat import OpenAICompatImageProvider
from nicekit.core.config import get_settings
from nicekit.kb.storage import put_object
from nicekit.llm.service_config import load_overrides
from nicekit.tenancy.usage import record_usage

logger = logging.getLogger(__name__)

SUPPORTED_API_MODES = frozenset({"images", "chat"})
_CONFIG_KEYS = frozenset({"api_key", "base_url", "model", "api_mode", "timeout_seconds"})
_FORMAT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_N_CAP = 4
_PROMPT_MAX = 4000
_TIMEOUT_MIN = 1.0
_TIMEOUT_MAX = 900.0
_MAX_IMAGE_PIXELS = 50_000_000
_SAFE_UNAVAILABLE = "图片服务暂时不可用，请稍后重试"
_SAFE_INVALID_MEDIA = "图片服务返回了无效的图片结果"

ProgressFn = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ImageProviderConfig:
    api_key: str
    base_url: str
    model: str
    api_mode: str
    timeout_seconds: float


@dataclass(frozen=True)
class ImageServiceReadiness:
    ready: bool
    provider: str
    model: str | None
    api_mode: str | None
    reason: str | None = None

    def as_dict(self) -> dict:
        """Return browser-safe capability metadata only; never include key or endpoint."""
        return asdict(self)


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    content_type: str
    width: int
    height: int


def _clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _pick_text(override: object, fallback: object) -> str:
    return _clean_text(override) or _clean_text(fallback)


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_imagegen_payload(payload: dict) -> dict:
    """Normalize and validate admin writes without resolving or exposing env secrets."""
    unknown = set(payload) - _CONFIG_KEYS
    if unknown:
        raise ValueError(f"图片服务包含不支持的配置项:{', '.join(sorted(unknown))}")
    normalized = dict(payload)
    for key in ("api_key", "base_url", "model", "api_mode"):
        if key in normalized and isinstance(normalized[key], str):
            normalized[key] = normalized[key].strip()
    if normalized.get("base_url") and not _valid_http_url(str(normalized["base_url"])):
        raise ValueError("Base URL 必须是包含 http:// 或 https:// 的有效地址")
    if normalized.get("api_mode"):
        normalized["api_mode"] = str(normalized["api_mode"]).lower()
        if normalized["api_mode"] not in SUPPORTED_API_MODES:
            raise ValueError("接口模式仅支持 images 或 chat")
    if normalized.get("timeout_seconds") not in (None, ""):
        try:
            timeout = float(normalized["timeout_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("请求超时必须是数字") from exc
        if not _TIMEOUT_MIN <= timeout <= _TIMEOUT_MAX:
            raise ValueError("请求超时必须在 1 到 900 秒之间")
        normalized["timeout_seconds"] = timeout
    return normalized


def _resolve_config(overrides: dict | None = None) -> tuple[ImageProviderConfig | None, str | None]:
    settings = get_settings()
    values = overrides or {}
    api_key = _pick_text(values.get("api_key"), settings.imagegen_api_key)
    base_url = _pick_text(values.get("base_url"), settings.imagegen_base_url)
    model = _pick_text(values.get("model"), settings.imagegen_model)
    api_mode = _pick_text(values.get("api_mode"), settings.imagegen_api_mode).lower()
    raw_timeout = values.get("timeout_seconds")
    if raw_timeout in (None, ""):
        raw_timeout = settings.imagegen_timeout_seconds

    if not api_key:
        return None, "unconfigured_credential"
    if not base_url:
        return None, "unconfigured_endpoint"
    if not _valid_http_url(base_url):
        return None, "invalid_endpoint"
    if not model:
        return None, "unconfigured_model"
    if api_mode not in SUPPORTED_API_MODES:
        return None, "invalid_mode"
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return None, "invalid_timeout"
    if not _TIMEOUT_MIN <= timeout <= _TIMEOUT_MAX:
        return None, "invalid_timeout"
    return (
        ImageProviderConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            api_mode=api_mode,
            timeout_seconds=timeout,
        ),
        None,
    )


def image_service_readiness(overrides: dict | None = None) -> ImageServiceReadiness:
    config, reason = _resolve_config(overrides)
    if config is None:
        values = overrides or {}
        settings = get_settings()
        model = _pick_text(values.get("model"), settings.imagegen_model) or None
        api_mode = _pick_text(values.get("api_mode"), settings.imagegen_api_mode).lower() or None
        return ImageServiceReadiness(
            ready=False,
            provider="openai_compat",
            model=model,
            api_mode=api_mode,
            reason=reason,
        )
    return ImageServiceReadiness(
        ready=True,
        provider="openai_compat",
        model=config.model,
        api_mode=config.api_mode,
    )


async def resolve_image_service_readiness(session: AsyncSession) -> ImageServiceReadiness:
    return image_service_readiness(await load_overrides(session, "imagegen"))


def get_provider(overrides: dict | None = None) -> ImageGenProvider | None:
    """Build the provider from the same merged configuration used by readiness."""
    config, reason = _resolve_config(overrides)
    if config is None:
        logger.info("image provider not ready reason=%s", reason)
        return None
    return OpenAICompatImageProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        api_mode=config.api_mode,
        timeout=config.timeout_seconds,
    )


def normalize_query(prompt: str, n: int | None, size: str | None) -> ImageGenQuery:
    return ImageGenQuery(
        prompt=prompt.strip()[:_PROMPT_MAX],
        n=min(max(n or 1, 1), _N_CAP),
        size=size if size in SIZES else None,
    )


def genimg_object_key(org_id: UUID, filename: str) -> str:
    return f"{org_id}/genimg/{filename}"


def validate_image_blob(blob: ImageBlob) -> ValidatedImage:
    """Decode a bounded PNG/JPEG/WebP and derive trusted media metadata."""
    if not blob.data or len(blob.data) > MAX_IMAGE_BYTES:
        raise ValueError("image size is outside the allowed range")
    try:
        with Image.open(io.BytesIO(blob.data)) as source:
            image_format = (source.format or "").upper()
            width, height = source.size
            if image_format not in _FORMAT_MIME:
                raise ValueError("unsupported image format")
            if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions are outside the allowed range")
            source.verify()
        # Re-open and decode pixel data; verify() alone only validates container integrity.
        with Image.open(io.BytesIO(blob.data)) as decoded:
            decoded.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("malformed image data") from exc
    return ValidatedImage(
        data=blob.data,
        content_type=_FORMAT_MIME[image_format],
        width=width,
        height=height,
    )


async def _emit_progress(callback: ProgressFn | None, text: str) -> None:
    if callback is None:
        return
    try:
        await callback(text)
    except Exception:
        logger.exception("image progress emission failed")


async def generate_images(
    session: AsyncSession,
    *,
    org_id: UUID,
    query: ImageGenQuery,
    provider: ImageGenProvider | None = None,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Generate, validate, and store images; all Agent-facing failures are sanitized."""
    if provider is None:
        provider = get_provider(await load_overrides(session, "imagegen"))
    if provider is None:
        return {
            "status": "unavailable",
            "provider": "none",
            "model": "",
            "images": [],
            "error": "图片生成功能尚未配置",
        }

    await _emit_progress(on_progress, "已向图片服务提交生成请求")
    try:
        outcome: ImageGenOutcome = await provider.generate(query)
    except Exception as exc:
        logger.exception("image provider escaped contract type=%s", type(exc).__name__)
        outcome = ImageGenOutcome(
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", ""),
            status="unavailable",
            error=str(exc),
        )

    images: list[dict] = []
    total_bytes = 0
    if outcome.status == "ok":
        try:
            validated = [validate_image_blob(blob) for blob in outcome.blobs[: query.n]]
            if not validated:
                raise ValueError("provider returned no images")
        except ValueError as exc:
            logger.warning(
                "image validation failed provider=%s model=%s reason=%s",
                outcome.provider,
                outcome.model,
                exc,
            )
            outcome = ImageGenOutcome(
                provider=outcome.provider,
                model=outcome.model,
                status="unavailable",
                error=_SAFE_INVALID_MEDIA,
            )
        else:
            await _emit_progress(on_progress, "正在保存生成结果")
            try:
                for image in validated:
                    filename = f"{uuid4()}.{_MIME_EXT[image.content_type]}"
                    await put_object(
                        genimg_object_key(org_id, filename),
                        image.data,
                        image.content_type,
                    )
                    total_bytes += len(image.data)
                    images.append(
                        {
                            "filename": filename,
                            "url": f"/genimg/{filename}",
                            "content_type": image.content_type,
                            "size_bytes": len(image.data),
                            "width": image.width,
                            "height": image.height,
                        }
                    )
            except Exception as exc:
                logger.exception(
                    "generated image storage failed type=%s provider=%s model=%s",
                    type(exc).__name__,
                    outcome.provider,
                    outcome.model,
                )
                images = []
                total_bytes = 0
                outcome = ImageGenOutcome(
                    provider=outcome.provider,
                    model=outcome.model,
                    status="unavailable",
                    error="图片结果保存失败，请稍后重试",
                )

    await record_usage(
        session,
        org_id=org_id,
        task="image.gen",
        provider=outcome.provider,
        model=outcome.model,
        quantity=total_bytes,
    )
    if outcome.status != "ok" or not images:
        if outcome.error:
            logger.warning(
                "image generation unavailable provider=%s model=%s detail=%s",
                outcome.provider,
                outcome.model,
                outcome.error,
            )
        safe_error = (
            outcome.error
            if outcome.error in {_SAFE_INVALID_MEDIA, "图片结果保存失败，请稍后重试"}
            else _SAFE_UNAVAILABLE
        )
        return {
            "status": "unavailable",
            "provider": outcome.provider,
            "model": outcome.model,
            "images": [],
            "error": safe_error,
        }
    return {
        "status": "ok",
        "provider": outcome.provider,
        "model": outcome.model,
        "images": images,
        "error": None,
    }
