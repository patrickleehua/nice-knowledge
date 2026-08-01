"""Governed OCR and visual-caption enrichment for validated KB images."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from threading import Lock
from typing import Protocol
from uuid import UUID

import anyio

from nicekit.core.config import Settings, get_settings
from nicekit.domain.kb_media import ImageReviewStatus
from nicekit.kb.caption import (
    CAPTION_TASK,
    CaptionExecutionError,
    CaptionModelSelection,
    caption_image_with_metadata,
    platform_caption_selection,
)
from nicekit.kb.image_validation import (
    KbImageValidationError,
    validate_kb_image,
)
from nicekit.kb.prompts_seed import KB_EXTRACT_PROMPTS
from nicekit.llm.model_catalog import effective_model_metadata
from nicekit.llm.runtime_config import runtime_providers
from nicekit.llm.service import LLMService, get_llm_service

OCR_PROVIDER = "docling"
OCR_MODEL = "rapidocr:onnxruntime"
PREPROCESSING_VERSION = "kb-image-enrichment:v1"
_OCR_LANGUAGES = ("chinese", "english")


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


@dataclass(frozen=True, slots=True)
class CaptionProviderStatus:
    ready: bool
    code: str
    provider: str
    model: str
    endpoint_fingerprint: str


@dataclass(frozen=True, slots=True)
class ImageEnrichmentReadiness:
    ready: bool
    code: str
    config_fingerprint: str
    ocr_provider: str
    ocr_model: str
    ocr_fingerprint: str
    caption_provider: str
    caption_model: str
    endpoint_fingerprint: str


@dataclass(frozen=True, slots=True)
class ImageEnrichmentResult:
    ocr_text: str
    caption: str
    ocr_provider: str
    ocr_model: str
    caption_provider: str | None
    caption_model: str | None
    prompt_version: int | None
    ocr_fingerprint: str
    caption_fingerprint: str | None
    config_fingerprint: str
    request_fingerprint: str
    result_fingerprint: str
    review_status: ImageReviewStatus = ImageReviewStatus.NEEDS_REVIEW


class KbImageEnrichmentError(RuntimeError):
    """Stable sanitized failure returned to persistence/orchestration layers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OcrRunner(Protocol):
    provider: str
    model: str
    fingerprint: str

    def is_ready(self) -> bool: ...

    async def run(self, data: bytes, *, content_type: str) -> str: ...


class DoclingRapidOcrRunner:
    """Docling public Image pipeline configured with RapidOCR."""

    provider = OCR_PROVIDER
    model = OCR_MODEL

    def __init__(self) -> None:
        self._converter = None
        self._converter_lock = Lock()
        self.fingerprint = _fingerprint(
            {
                "provider": self.provider,
                "model": self.model,
                "docling_version": _package_version("docling"),
                "docling_core_version": _package_version("docling-core"),
                "rapidocr_version": _package_version("rapidocr"),
                "backend": "onnxruntime",
                "force_full_page_ocr": True,
                "languages": list(_OCR_LANGUAGES),
                "preprocessing_version": PREPROCESSING_VERSION,
            }
        )

    def is_ready(self) -> bool:
        packages_ready = all(
            _package_version(package) != "unavailable"
            for package in ("docling", "rapidocr")
        )
        if not packages_ready:
            return False
        try:
            with self._converter_lock:
                self._get_converter()
        except Exception:
            return False
        return True

    def _get_converter(self):
        if self._converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                RapidOcrOptions,
            )
            from docling.document_converter import DocumentConverter, ImageFormatOption

            options = PdfPipelineOptions(
                do_ocr=True,
                do_table_structure=False,
                ocr_options=RapidOcrOptions(
                    lang=list(_OCR_LANGUAGES),
                    force_full_page_ocr=True,
                    backend="onnxruntime",
                ),
            )
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)
                }
            )
        return self._converter

    def _run_sync(self, data: bytes, content_type: str) -> str:
        from docling.datamodel.base_models import DocumentStream

        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }[content_type]
        with self._converter_lock:
            result = self._get_converter().convert(
                DocumentStream(
                    name=f"ocr-input.{extension}",
                    stream=io.BytesIO(data),
                )
            )
        texts = [
            value.strip()
            for item, _level in result.document.iterate_items()
            if isinstance((value := getattr(item, "text", None)), str)
            and value.strip()
        ]
        return "\n".join(texts)

    async def run(self, data: bytes, *, content_type: str) -> str:
        return await anyio.to_thread.run_sync(self._run_sync, data, content_type)


def _default_caption_provider_status(
    settings: Settings,
    selection: CaptionModelSelection | None = None,
) -> CaptionProviderStatus:
    provider, model = (
        (selection.provider, selection.model)
        if selection is not None
        else platform_caption_selection(settings)
    )
    if not provider or not model:
        return CaptionProviderStatus(
            ready=False,
            code="caption_provider_unconfigured",
            provider=provider,
            model=model,
            endpoint_fingerprint="",
        )

    built_in_credentials = {
        "openai": (
            "openai",
            settings.openai_api_key,
            settings.openai_base_url,
        ),
        "anthropic": (
            "anthropic",
            settings.anthropic_api_key,
            settings.anthropic_base_url,
        ),
    }
    runtime = runtime_providers()
    protocol = ""
    api_key = ""
    base_url = ""
    catalog_code = ""
    if runtime is None:
        catalog_code = "caption_provider_unavailable"
    else:
        row = next(
            (
                item
                for item in runtime
                if item["name"] == provider and item["enabled"]
            ),
            None,
        )
        if row is not None:
            env = built_in_credentials.get(provider)
            protocol = row["protocol"]
            api_key = row["api_key"] or (env[1] if env else "")
            base_url = row["base_url"] or (env[2] if env else "")
            if model not in set(row.get("models") or []):
                catalog_code = "caption_model_unavailable"
            else:
                stored = row.get("model_metadata") or {}
                metadata = effective_model_metadata(model, stored.get(model))
                if not {"generation", "vision"}.issubset(metadata.capabilities):
                    catalog_code = "caption_model_ineligible"

    endpoint_fingerprint = (
        _fingerprint(
            {
                "protocol": protocol,
                "endpoint": base_url or f"{protocol}:default",
            }
        )
        if protocol
        else ""
    )
    if catalog_code:
        code = catalog_code
    elif not protocol:
        code = "caption_provider_unavailable"
    elif not api_key:
        code = "caption_provider_credentials_missing"
    else:
        code = "ready"
    return CaptionProviderStatus(
        ready=code == "ready",
        code=code,
        provider=provider,
        model=model,
        endpoint_fingerprint=endpoint_fingerprint,
    )


@lru_cache
def _default_ocr_runner() -> DoclingRapidOcrRunner:
    return DoclingRapidOcrRunner()


class KbImageEnrichmentService:
    """Validate, OCR, and caption one image without persistence side effects."""

    def __init__(
        self,
        *,
        llm: LLMService,
        settings: Settings | None = None,
        ocr_runner: OcrRunner | None = None,
        caption_provider_status: CaptionProviderStatus | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm
        self._ocr = ocr_runner or _default_ocr_runner()
        self._caption_status = caption_provider_status or (
            _default_caption_provider_status(self._settings)
        )

    def _caption_status_for(
        self,
        selection: CaptionModelSelection | None,
    ) -> CaptionProviderStatus:
        if selection is None:
            return self._caption_status
        return _default_caption_provider_status(self._settings, selection)

    def readiness(
        self,
        selection: CaptionModelSelection | None = None,
        *,
        caption_enabled: bool = True,
    ) -> ImageEnrichmentReadiness:
        caption = (
            self._caption_status_for(selection)
            if caption_enabled
            else CaptionProviderStatus(
                ready=True,
                code="disabled",
                provider="",
                model="",
                endpoint_fingerprint="",
            )
        )
        seed_prompt_version = KB_EXTRACT_PROMPTS[CAPTION_TASK][0]
        config_fingerprint = _fingerprint(
            {
                "caption": {
                    "enabled": caption_enabled,
                    "endpoint_fingerprint": caption.endpoint_fingerprint,
                    "max_chars": self._settings.kb_image_caption_max_chars,
                    "model": caption.model if caption_enabled else None,
                    "prompt_task": CAPTION_TASK if caption_enabled else None,
                    "prompt_version": seed_prompt_version if caption_enabled else None,
                    "provider": caption.provider,
                    "timeout_seconds": self._settings.kb_caption_timeout_seconds,
                },
                "ocr": {
                    "fingerprint": self._ocr.fingerprint,
                    "max_chars": self._settings.kb_image_ocr_max_chars,
                    "model": self._ocr.model,
                    "provider": self._ocr.provider,
                    "timeout_seconds": self._settings.kb_image_ocr_timeout_seconds,
                },
                "preprocessing_version": PREPROCESSING_VERSION,
                "validation": {
                    "max_bytes": self._settings.kb_image_max_bytes,
                    "max_dimension": self._settings.kb_image_max_dimension,
                    "max_pixels": self._settings.kb_image_max_pixels,
                },
            }
        )
        if not self._ocr.is_ready():
            ready, code = False, "ocr_provider_unavailable"
        elif caption_enabled and not caption.ready:
            ready, code = False, caption.code
        else:
            ready, code = True, "ready"
        return ImageEnrichmentReadiness(
            ready=ready,
            code=code,
            config_fingerprint=config_fingerprint,
            ocr_provider=self._ocr.provider,
            ocr_model=self._ocr.model,
            ocr_fingerprint=self._ocr.fingerprint,
            caption_provider=caption.provider,
            caption_model=caption.model,
            endpoint_fingerprint=caption.endpoint_fingerprint,
        )

    def ocr_configuration_ready(self) -> bool:
        return self._ocr.is_ready()

    def caption_configuration_status(
        self,
        selection: CaptionModelSelection | None = None,
    ) -> CaptionProviderStatus:
        return self._caption_status_for(selection)

    async def probe_ocr(self, data: bytes) -> None:
        """Run one bounded OCR execution through the selected production runner."""
        try:
            image = validate_kb_image(
                data,
                max_bytes=self._settings.kb_image_max_bytes,
                max_dimension=self._settings.kb_image_max_dimension,
                max_pixels=self._settings.kb_image_max_pixels,
            )
            await asyncio.wait_for(
                self._ocr.run(image.data, content_type=image.content_type),
                timeout=self._settings.kb_image_ocr_timeout_seconds,
            )
        except TimeoutError:
            raise KbImageEnrichmentError("ocr_timeout") from None
        except KbImageEnrichmentError:
            raise
        except Exception:
            raise KbImageEnrichmentError("ocr_failed") from None

    async def probe_caption(
        self,
        data: bytes,
        *,
        org_id: UUID,
        selection: CaptionModelSelection | None = None,
    ) -> None:
        """Run one bounded VLM execution through the exact configured route."""
        status = self._caption_status_for(selection)
        if not status.ready:
            raise KbImageEnrichmentError(status.code)
        try:
            image = validate_kb_image(
                data,
                max_bytes=self._settings.kb_image_max_bytes,
                max_dimension=self._settings.kb_image_max_dimension,
                max_pixels=self._settings.kb_image_max_pixels,
            )
            result = await caption_image_with_metadata(
                image.data,
                llm=self._llm,
                org_id=org_id,
                filename="operations-provider-probe.png",
                content_type=image.content_type,
                timeout_seconds=self._settings.kb_caption_timeout_seconds,
                settings=self._settings,
                selection=selection,
            )
        except CaptionExecutionError as exc:
            raise KbImageEnrichmentError(exc.code) from None
        except KbImageEnrichmentError:
            raise
        except Exception:
            raise KbImageEnrichmentError("caption_failed") from None
        if result.provider != status.provider or result.model != status.model:
            raise KbImageEnrichmentError("caption_route_mismatch")

    async def enrich(
        self,
        data: bytes,
        *,
        org_id: UUID,
        filename: str,
        selection: CaptionModelSelection | None = None,
        caption_enabled: bool = True,
    ) -> ImageEnrichmentResult:
        readiness = self.readiness(
            selection,
            caption_enabled=caption_enabled,
        )
        if not readiness.ready:
            raise KbImageEnrichmentError(readiness.code)
        try:
            image = validate_kb_image(
                data,
                max_bytes=self._settings.kb_image_max_bytes,
                max_dimension=self._settings.kb_image_max_dimension,
                max_pixels=self._settings.kb_image_max_pixels,
            )
        except KbImageValidationError:
            raise KbImageEnrichmentError("image_validation_failed") from None

        request_fingerprint = _fingerprint(
            {
                "config_fingerprint": readiness.config_fingerprint,
                "filename_sha256": hashlib.sha256(
                    filename.encode("utf-8")
                ).hexdigest(),
                "image_sha256": image.sha256,
            }
        )
        try:
            ocr_text = await asyncio.wait_for(
                self._ocr.run(image.data, content_type=image.content_type),
                timeout=self._settings.kb_image_ocr_timeout_seconds,
            )
        except TimeoutError:
            raise KbImageEnrichmentError("ocr_timeout") from None
        except Exception:
            raise KbImageEnrichmentError("ocr_failed") from None
        ocr_text = ocr_text.strip()[: self._settings.kb_image_ocr_max_chars]

        caption = ""
        caption_provider: str | None = None
        caption_model: str | None = None
        prompt_version: int | None = None
        caption_fingerprint: str | None = None
        if caption_enabled:
            try:
                caption_generation = await caption_image_with_metadata(
                    image.data,
                    llm=self._llm,
                    org_id=org_id,
                    filename=filename,
                    content_type=image.content_type,
                    timeout_seconds=self._settings.kb_caption_timeout_seconds,
                    settings=self._settings,
                    selection=selection,
                )
            except CaptionExecutionError as exc:
                raise KbImageEnrichmentError(exc.code) from None
            if (
                caption_generation.provider != readiness.caption_provider
                or caption_generation.model != readiness.caption_model
            ):
                raise KbImageEnrichmentError("caption_route_mismatch")
            caption = caption_generation.caption.description.strip()[
                : self._settings.kb_image_caption_max_chars
            ]
            caption_provider = caption_generation.provider
            caption_model = caption_generation.model
            prompt_version = caption_generation.prompt_version
            caption_fingerprint = _fingerprint(
                {
                    "endpoint_fingerprint": readiness.endpoint_fingerprint,
                    "model": caption_generation.model,
                    "preprocessing_version": PREPROCESSING_VERSION,
                    "prompt_task": CAPTION_TASK,
                    "prompt_version": caption_generation.prompt_version,
                    "provider": caption_generation.provider,
                }
            )
        result_fingerprint = _fingerprint(
            {
                "caption": caption,
                "caption_fingerprint": caption_fingerprint,
                "ocr_fingerprint": readiness.ocr_fingerprint,
                "ocr_text": ocr_text,
                "request_fingerprint": request_fingerprint,
            }
        )
        return ImageEnrichmentResult(
            ocr_text=ocr_text,
            caption=caption,
            ocr_provider=readiness.ocr_provider,
            ocr_model=readiness.ocr_model,
            caption_provider=caption_provider,
            caption_model=caption_model,
            prompt_version=prompt_version,
            ocr_fingerprint=readiness.ocr_fingerprint,
            caption_fingerprint=caption_fingerprint,
            config_fingerprint=readiness.config_fingerprint,
            request_fingerprint=request_fingerprint,
            result_fingerprint=result_fingerprint,
        )


def get_kb_image_enrichment_service(
    llm: LLMService | None = None,
) -> KbImageEnrichmentService:
    return KbImageEnrichmentService(llm=llm or get_llm_service())


__all__ = [
    "CaptionProviderStatus",
    "DoclingRapidOcrRunner",
    "ImageEnrichmentReadiness",
    "ImageEnrichmentResult",
    "KbImageEnrichmentError",
    "KbImageEnrichmentService",
    "get_kb_image_enrichment_service",
]
