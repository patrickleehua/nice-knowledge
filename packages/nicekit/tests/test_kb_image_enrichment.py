import asyncio
import io
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

import nicekit.kb.image_enrichment as image_enrichment_module
import nicekit.kb.image_validation as image_validation_module
from nicekit.domain.kb_media import ImageReviewStatus
from nicekit.kb.caption import CaptionModelSelection, ImageCaption
from nicekit.kb.image_enrichment import (
    CaptionProviderStatus,
    KbImageEnrichmentError,
    KbImageEnrichmentService,
)
from nicekit.llm.service import StructuredGeneration


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(output, format="PNG")
    return output.getvalue()


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "kb_caption_provider": "openai",
        "kb_caption_model": "vision-model",
        "kb_image_max_bytes": 1024 * 1024,
        "kb_image_max_dimension": 1000,
        "kb_image_max_pixels": 1_000_000,
        "kb_image_ocr_timeout_seconds": 1.0,
        "kb_image_ocr_max_chars": 4000,
        "kb_image_caption_max_chars": 2000,
        "kb_caption_timeout_seconds": 1.0,
        "openai_api_key": "test-key",
        "openai_base_url": "",
        "anthropic_api_key": "",
        "anthropic_base_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _isolate_image_validation_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        image_validation_module,
        "get_settings",
        _settings,
    )


def _provider_status(*, ready: bool = True, code: str = "ready"):
    return CaptionProviderStatus(
        ready=ready,
        code=code,
        provider="openai",
        model="vision-model",
        endpoint_fingerprint="a" * 64,
    )


class FakeOcrRunner:
    provider = "docling"
    model = "rapidocr:onnxruntime"
    fingerprint = "b" * 64

    def __init__(
        self,
        *,
        text: str = "OCR visible text",
        ready: bool = True,
        delay: float = 0,
        fail: Exception | None = None,
    ) -> None:
        self.text = text
        self.ready = ready
        self.delay = delay
        self.fail = fail
        self.calls: list[tuple[bytes, str]] = []

    def is_ready(self) -> bool:
        return self.ready

    async def run(self, data: bytes, *, content_type: str) -> str:
        self.calls.append((data, content_type))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return self.text


class FakeLLM:
    def __init__(
        self,
        *,
        description: str = "Hotel room photograph",
        provider: str = "openai",
        model: str = "vision-model",
        fail: Exception | None = None,
    ) -> None:
        self.description = description
        self.provider = provider
        self.model = model
        self.fail = fail
        self.calls: list[dict] = []

    async def generate_structured_with_metadata(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return StructuredGeneration(
            parsed=ImageCaption(
                description=self.description,
                visible_text="VLM text is not used as OCR",
            ),
            provider=self.provider,
            model=self.model,
            prompt_version=3,
        )


def _service(
    *,
    settings: SimpleNamespace | None = None,
    ocr: FakeOcrRunner | None = None,
    llm: FakeLLM | None = None,
    provider_status: CaptionProviderStatus | None = None,
) -> KbImageEnrichmentService:
    return KbImageEnrichmentService(
        settings=settings or _settings(),
        ocr_runner=ocr or FakeOcrRunner(),
        llm=llm or FakeLLM(),
        caption_provider_status=provider_status or _provider_status(),
    )


def test_readiness_exposes_stable_configuration_without_secrets() -> None:
    service = _service()

    first = service.readiness()
    second = service.readiness()

    assert first == second
    assert first.ready is True and first.code == "ready"
    assert len(first.config_fingerprint) == 64
    assert first.ocr_provider == "docling"
    assert first.ocr_model == "rapidocr:onnxruntime"
    assert first.caption_provider == "openai"
    assert first.caption_model == "vision-model"
    assert "api_key" not in asdict(first)


def test_unconfigured_caption_is_not_ready_and_never_executes() -> None:
    settings = _settings(kb_caption_provider="", kb_caption_model="")
    ocr = FakeOcrRunner()
    llm = FakeLLM()
    service = KbImageEnrichmentService(
        settings=settings,
        ocr_runner=ocr,
        llm=llm,
    )

    readiness = service.readiness()

    assert readiness.ready is False
    assert readiness.code == "caption_provider_unconfigured"
    assert len(readiness.config_fingerprint) == 64


async def test_enrich_keeps_ocr_and_caption_separate_and_fingerprinted() -> None:
    settings = _settings(
        kb_image_ocr_max_chars=8,
        kb_image_caption_max_chars=10,
    )
    ocr = FakeOcrRunner(text="OCR visible text")
    llm = FakeLLM(description="Hotel room photograph")
    service = _service(settings=settings, ocr=ocr, llm=llm)

    result = await service.enrich(
        _png_bytes(),
        org_id=uuid4(),
        filename="room.png",
    )

    assert result.ocr_text == "OCR visi"
    assert result.caption == "Hotel room"
    assert result.review_status == ImageReviewStatus.NEEDS_REVIEW
    assert result.ocr_provider == "docling"
    assert result.caption_provider == "openai"
    assert result.caption_model == "vision-model"
    assert result.prompt_version == 3
    assert all(
        len(value) == 64
        for value in (
            result.ocr_fingerprint,
            result.caption_fingerprint,
            result.config_fingerprint,
            result.request_fingerprint,
            result.result_fingerprint,
        )
    )
    assert ocr.calls[0][1] == "image/png"
    llm_call = llm.calls[0]
    assert llm_call["model_override"] == {
        "provider": "openai",
        "model": "vision-model",
    }
    persisted = asdict(result)
    assert "data" not in persisted
    assert "object_key" not in persisted


async def test_caption_disabled_runs_ocr_without_calling_visual_model() -> None:
    ocr = FakeOcrRunner(text="OCR-only evidence")
    llm = FakeLLM()
    service = _service(ocr=ocr, llm=llm)

    readiness = service.readiness(caption_enabled=False)
    result = await service.enrich(
        _png_bytes(),
        org_id=uuid4(),
        filename="receipt.png",
        caption_enabled=False,
    )

    assert readiness.ready is True
    assert readiness.caption_provider == ""
    assert readiness.caption_model == ""
    assert result.ocr_text == "OCR-only evidence"
    assert result.caption == ""
    assert result.caption_provider is None
    assert result.caption_model is None
    assert result.prompt_version is None
    assert result.caption_fingerprint is None
    assert ocr.calls
    assert llm.calls == []
    assert result.config_fingerprint == readiness.config_fingerprint


async def test_enrichment_fingerprints_are_stable_for_same_request() -> None:
    service = _service()
    data = _png_bytes()
    org_id = uuid4()

    first = await service.enrich(data, org_id=org_id, filename="same.png")
    second = await service.enrich(data, org_id=org_id, filename="same.png")

    assert first.config_fingerprint == second.config_fingerprint
    assert first.request_fingerprint == second.request_fingerprint
    assert first.result_fingerprint == second.result_fingerprint


async def test_kb_selection_changes_exact_route_and_configuration_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_model = "kb-vision-model"
    monkeypatch.setattr(
        image_enrichment_module,
        "runtime_providers",
        lambda: [
            {
                "name": "openai",
                "protocol": "openai",
                "api_key": "test-key",
                "base_url": "",
                "models": [selected_model],
                "model_metadata": {
                    selected_model: {
                        "capabilities": ["generation", "vision"],
                        "input_modalities": ["text", "image"],
                        "capability_source": "manual",
                        "registry_revision": None,
                        "provider_metadata": {},
                    }
                },
                "enabled": True,
            }
        ],
    )
    llm = FakeLLM(model=selected_model)
    service = _service(llm=llm)
    selection = CaptionModelSelection(provider="openai", model=selected_model)

    platform_readiness = service.readiness()
    selected_readiness = service.readiness(selection)
    result = await service.enrich(
        _png_bytes(),
        org_id=uuid4(),
        filename="selected.png",
        selection=selection,
    )

    assert selected_readiness.caption_model == selected_model
    assert selected_readiness.config_fingerprint != platform_readiness.config_fingerprint
    assert result.caption_model == selected_model
    assert llm.calls[0]["model_override"] == {
        "provider": "openai",
        "model": selected_model,
    }


async def test_invalid_image_fails_before_ocr_or_caption() -> None:
    ocr = FakeOcrRunner()
    llm = FakeLLM()
    service = _service(ocr=ocr, llm=llm)

    with pytest.raises(
        KbImageEnrichmentError,
        match="image_validation_failed",
    ):
        await service.enrich(b"not-an-image", org_id=uuid4(), filename="bad.png")

    assert ocr.calls == []
    assert llm.calls == []


async def test_ocr_timeout_is_sanitized_and_skips_caption() -> None:
    settings = _settings(kb_image_ocr_timeout_seconds=0.001)
    ocr = FakeOcrRunner(delay=0.05)
    llm = FakeLLM()
    service = _service(settings=settings, ocr=ocr, llm=llm)

    with pytest.raises(KbImageEnrichmentError, match="ocr_timeout") as caught:
        await service.enrich(
            _png_bytes(),
            org_id=uuid4(),
            filename="slow.png",
        )

    assert caught.value.code == "ocr_timeout"
    assert llm.calls == []


async def test_caption_failure_does_not_expose_provider_body() -> None:
    llm = FakeLLM(fail=RuntimeError("secret credential and provider body"))
    service = _service(llm=llm)

    with pytest.raises(KbImageEnrichmentError) as caught:
        await service.enrich(
            _png_bytes(),
            org_id=uuid4(),
            filename="room.png",
        )

    assert caught.value.code == "caption_failed"
    assert str(caught.value) == "caption_failed"
    assert "secret" not in str(caught.value)


async def test_caption_route_mismatch_is_rejected() -> None:
    service = _service(llm=FakeLLM(model="unexpected-model"))

    with pytest.raises(
        KbImageEnrichmentError,
        match="caption_route_mismatch",
    ):
        await service.enrich(
            _png_bytes(),
            org_id=uuid4(),
            filename="room.png",
        )
