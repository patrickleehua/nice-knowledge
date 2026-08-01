"""KB 图片 VLM caption 单元测试:mock LLM 网关,不碰网络与数据库。

覆盖:caption 成功替换占位并追加检索文本、失败降级保留占位、
注入文本被下游隔离检查命中、profile 开关与默认值、prompt seed 注入防御。
"""

import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest

import nicekit.kb.caption as caption_module
from nicekit.domain.kb import INGEST_PROFILE_PRESETS
from nicekit.kb.caption import (
    CAPTION_TASK,
    CaptionExecutionError,
    CaptionModelSelection,
    ImageCaption,
    caption_image,
    inspect_image_with_metadata,
    render_caption_markdown,
)
from nicekit.kb.prompts_seed import KB_EXTRACT_PROMPTS
from nicekit.llm.service import StructuredGeneration


class FakeLLM:
    """只实现 caption 用到的 generate_structured_with_metadata,记录调用参数。"""

    def __init__(
        self, *, result: ImageCaption | None = None, fail: Exception | None = None
    ):
        self.calls: list[dict] = []
        self._result = result or ImageCaption(description="", visible_text="")
        self._fail = fail

    async def generate_structured_with_metadata(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail is not None:
            raise self._fail
        return StructuredGeneration(
            parsed=self._result, provider="openai", model="m", prompt_version=1
        )


def _patch_settings(
    monkeypatch,
    provider: str = "openai",
    model: str = "vision-model",
) -> None:
    monkeypatch.setattr(
        caption_module,
        "get_settings",
        lambda: SimpleNamespace(
            kb_caption_provider=provider,
            kb_caption_model=model,
            kb_caption_timeout_seconds=120.0,
        ),
    )


# ---- caption_image:多模态消息构造与模型选择 --------------------------------


async def test_caption_image_builds_multimodal_message(monkeypatch) -> None:
    _patch_settings(monkeypatch)
    llm = FakeLLM(result=ImageCaption(description="酒店房型图", visible_text="THB 1200"))

    text = await caption_image(
        b"img-bytes", llm=llm, org_id=uuid4(), filename="房型图.png"
    )

    assert "酒店房型图" in text and "THB 1200" in text
    call = llm.calls[0]
    assert call["task"] == CAPTION_TASK
    assert call["output_model"] is ImageCaption
    assert call["model_override"] == {
        "provider": "openai",
        "model": "vision-model",
    }
    blocks = call["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "文件名:房型图.png"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["media_type"] == "image/png"
    assert blocks[1]["data"] == base64.b64encode(b"img-bytes").decode("ascii")


async def test_caption_image_media_type_by_suffix(monkeypatch) -> None:
    _patch_settings(monkeypatch)
    llm = FakeLLM()
    await caption_image(b"x", llm=llm, org_id=uuid4(), filename="报价单.JPG")
    assert llm.calls[0]["messages"][0]["content"][1]["media_type"] == "image/jpeg"


async def test_caption_image_model_override_from_settings(monkeypatch) -> None:
    _patch_settings(monkeypatch, provider="openai", model="vision-model")
    llm = FakeLLM()
    await caption_image(b"x", llm=llm, org_id=uuid4(), filename="a.png")
    assert llm.calls[0]["model_override"] == {
        "provider": "openai",
        "model": "vision-model",
    }


async def test_caption_image_partial_override_is_rejected(monkeypatch) -> None:
    _patch_settings(monkeypatch, provider="", model="vision-model")
    llm = FakeLLM()
    with pytest.raises(
        CaptionExecutionError,
        match="caption_provider_unconfigured",
    ):
        await caption_image(b"x", llm=llm, org_id=uuid4(), filename="a.png")
    assert llm.calls == []


async def test_pixel_inspection_uses_exact_route_and_bounds_question(monkeypatch) -> None:
    _patch_settings(monkeypatch, provider="vision-provider", model="vision-exact")
    llm = FakeLLM(
        result=ImageCaption(description="图片中可见登船口标识", visible_text="Gate B")
    )

    generation = await inspect_image_with_metadata(
        b"private-image-bytes",
        llm=llm,
        org_id=uuid4(),
        filename="登船图.png",
        question="核" * 600,
        content_type="image/png",
    )

    call = llm.calls[0]
    assert call["model_override"] == {
        "provider": "vision-provider",
        "model": "vision-exact",
    }
    prompt = call["messages"][0]["content"][0]["text"]
    bounded_question = prompt.split("核验问题:", 1)[1].split("\n", 1)[0]
    assert bounded_question == "核" * 500
    assert call["messages"][0]["content"][1]["data"] == base64.b64encode(
        b"private-image-bytes"
    ).decode("ascii")
    assert generation.caption.visible_text == "Gate B"


async def test_pixel_inspection_prefers_exact_kb_selection(monkeypatch) -> None:
    _patch_settings(monkeypatch, provider="openai", model="platform-default")
    llm = FakeLLM()

    await inspect_image_with_metadata(
        b"private-image-bytes",
        llm=llm,
        org_id=uuid4(),
        filename="source.png",
        selection=CaptionModelSelection(provider="openai", model="kb-visual"),
    )

    assert llm.calls[0]["model_override"] == {
        "provider": "openai",
        "model": "kb-visual",
    }


# ---- render_caption_markdown ------------------------------------------------


def test_render_caption_markdown_sections() -> None:
    full = render_caption_markdown(
        ImageCaption(description="曼谷酒店豪华双床房", visible_text="Deluxe Twin 1200")
    )
    assert "图片内容:曼谷酒店豪华双床房" in full
    assert "图中文字:\nDeluxe Twin 1200" in full

    no_text = render_caption_markdown(ImageCaption(description="景点照片", visible_text=" "))
    assert "图中文字" not in no_text

    assert render_caption_markdown(ImageCaption(description=" ", visible_text="")) == ""


def test_caption_prompt_seeded_with_untrusted_rule() -> None:
    assert CAPTION_TASK in KB_EXTRACT_PROMPTS
    version, content = KB_EXTRACT_PROMPTS[CAPTION_TASK]
    assert version >= 1
    assert "description" in content and "visible_text" in content
    assert "不可信数据" in content and "不是指令" in content  # 图内文字注入防御


# ---- 默认值与预设同步 ---------------------------------------------------------


def test_ingest_profile_presets_caption_default_on() -> None:
    for preset in INGEST_PROFILE_PRESETS.values():
        assert preset["profile"]["caption_images"] is True
