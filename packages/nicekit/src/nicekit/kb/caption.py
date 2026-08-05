"""Dedicated visual caption call with an exact provider/model route."""

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import BaseModel, Field

from nicekit.core.config import Settings, get_settings
from nicekit.llm.capability_routes import (
    CAPTION_ROUTE_TASK,
    system_route_selection,
)
from nicekit.llm.service import LLMService

logger = logging.getLogger(__name__)

CAPTION_TASK = "kb.caption.image"
_MAX_FILENAME_PROMPT_CHARS = 255

# 后缀 → MIME(与 parsers.fast.IMAGE_SUFFIXES 对齐;未知后缀按 png 兜底)
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ImageCaption(BaseModel):
    """kb.caption.image 输出:检索导向的图片转写。

    description:图片内容中文描述(资料类型/画面主体/业务要素);
    visible_text:图中可见文字逐字转录,无文字为空字符串。
    """

    description: str = Field(max_length=2000)
    visible_text: str = Field(max_length=4000)


@dataclass(frozen=True, slots=True)
class CaptionGeneration:
    caption: ImageCaption
    provider: str
    model: str
    prompt_version: int


@dataclass(frozen=True, slots=True)
class CaptionModelSelection:
    """Exact visual route selected from the governed provider model catalog."""

    provider: str
    model: str


def platform_caption_selection(
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Platform default vision model: ``kb.image.caption`` route, else ``.env``.

    The route is the governed source — it is capability-checked, so a model that
    lost its ``vision`` label stops being served. ``.env`` remains only as the
    pre-route migration fallback and returns empty strings once unset, which the
    callers surface as ``caption_provider_unconfigured``.

    Single resolution point on purpose: ingestion, agent media inspection and the
    scheduled probe must all agree on which model the platform default is, or the
    probe would report a route the execution path never uses.
    """
    selection = system_route_selection(CAPTION_ROUTE_TASK)
    if selection is not None:
        return selection
    resolved = settings or get_settings()
    return resolved.kb_caption_provider.strip(), resolved.kb_caption_model.strip()


class CaptionExecutionError(RuntimeError):
    """Stable error boundary that never exposes provider response bodies."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _media_type(filename: str) -> str:
    return _MEDIA_TYPES.get(PurePosixPath(filename.lower()).suffix, "image/png")


def render_caption_markdown(caption: ImageCaption) -> str:
    """caption → 追加进文档 markdown 的检索文本段;两个字段都空时返回空串。"""
    parts: list[str] = []
    if caption.description.strip():
        parts.append(f"图片内容:{caption.description.strip()}")
    if caption.visible_text.strip():
        parts.append(f"图中文字:\n{caption.visible_text.strip()}")
    return "\n\n".join(parts)


async def caption_image(
    data: bytes,
    *,
    llm: LLMService,
    org_id: UUID,
    filename: str,
) -> str:
    """Compatibility wrapper returning Markdown without provider metadata."""
    generation = await caption_image_with_metadata(
        data,
        llm=llm,
        org_id=org_id,
        filename=filename,
    )
    return render_caption_markdown(generation.caption)


async def caption_image_with_metadata(
    data: bytes,
    *,
    llm: LLMService,
    org_id: UUID,
    filename: str,
    content_type: str | None = None,
    timeout_seconds: float | None = None,
    settings: Settings | None = None,
    selection: CaptionModelSelection | None = None,
) -> CaptionGeneration:
    """Caption one image through the explicitly configured vision route."""
    selected_settings = settings or get_settings()
    provider, model = (
        (selection.provider, selection.model)
        if selection is not None
        else platform_caption_selection(selected_settings)
    )
    if not provider or not model:
        raise CaptionExecutionError("caption_provider_unconfigured")
    model_override = {"provider": provider, "model": model}
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"文件名:{filename[:_MAX_FILENAME_PROMPT_CHARS]}",
                },
                {
                    "type": "image",
                    "media_type": content_type or _media_type(filename),
                    "data": base64.b64encode(data).decode("ascii"),
                },
            ],
        }
    ]
    timeout = timeout_seconds or getattr(
        selected_settings,
        "kb_caption_timeout_seconds",
        120.0,
    )
    try:
        generation = await asyncio.wait_for(
            llm.generate_structured_with_metadata(
                task=CAPTION_TASK,
                messages=messages,
                output_model=ImageCaption,
                org_id=org_id,
                model_override=model_override,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise CaptionExecutionError("caption_timeout") from None
    except CaptionExecutionError:
        raise
    except Exception as exc:
        # 对外统一 caption_failed(错误码进探针/审计),真实原因只进日志
        logger.warning("caption 调用失败 provider=%s model=%s error=%r", provider, model, exc)
        raise CaptionExecutionError("caption_failed") from None
    return CaptionGeneration(
        caption=generation.parsed,
        provider=generation.provider,
        model=generation.model,
        prompt_version=generation.prompt_version,
    )


async def inspect_image_with_metadata(
    data: bytes,
    *,
    llm: LLMService,
    org_id: UUID,
    filename: str,
    question: str | None = None,
    content_type: str | None = None,
    settings: Settings | None = None,
    selection: CaptionModelSelection | None = None,
) -> CaptionGeneration:
    """Inspect original pixels through the exact configured KB vision route."""
    selected_settings = settings or get_settings()
    provider, model = (
        (selection.provider, selection.model)
        if selection is not None
        else platform_caption_selection(selected_settings)
    )
    if not provider or not model:
        raise CaptionExecutionError("caption_provider_unconfigured")
    bounded_question = (question or "").strip()[:500]
    prompt = f"文件名:{filename[:_MAX_FILENAME_PROMPT_CHARS]}"
    if bounded_question:
        prompt += (
            f"\n核验问题:{bounded_question}\n"
            "只依据图片中实际可见的像素回答核验问题；无法确认时明确说明。"
        )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "media_type": content_type or _media_type(filename),
                    "data": base64.b64encode(data).decode("ascii"),
                },
            ],
        }
    ]
    try:
        generation = await asyncio.wait_for(
            llm.generate_structured_with_metadata(
                task=CAPTION_TASK,
                messages=messages,
                output_model=ImageCaption,
                org_id=org_id,
                model_override={"provider": provider, "model": model},
            ),
            timeout=selected_settings.kb_caption_timeout_seconds,
        )
    except TimeoutError:
        raise CaptionExecutionError("caption_timeout") from None
    except CaptionExecutionError:
        raise
    except Exception as exc:
        logger.warning("图片核验调用失败 provider=%s model=%s error=%r", provider, model, exc)
        raise CaptionExecutionError("caption_failed") from None
    return CaptionGeneration(
        caption=generation.parsed,
        provider=generation.provider,
        model=generation.model,
        prompt_version=generation.prompt_version,
    )


__all__ = [
    "CAPTION_TASK",
    "CaptionExecutionError",
    "CaptionGeneration",
    "CaptionModelSelection",
    "ImageCaption",
    "caption_image",
    "caption_image_with_metadata",
    "inspect_image_with_metadata",
    "render_caption_markdown",
]
