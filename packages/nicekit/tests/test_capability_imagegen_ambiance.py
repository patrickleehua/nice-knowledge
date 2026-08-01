"""每日行程图氛围底图入口的单元覆盖(无数据库、无对象存储)。"""

import io
from uuid import uuid4

import pytest
from PIL import Image

from nicekit.capabilities.imagegen import ambiance
from nicekit.capabilities.imagegen import service as imagegen_service
from nicekit.capabilities.imagegen.ambiance import (
    AMBIANCE_SIZE,
    build_ambiance_prompt,
    generate_ambiance_base,
)
from nicekit.capabilities.imagegen.base import (
    ImageBlob,
    ImageGenOutcome,
    ImageGenProvider,
    ImageGenQuery,
)


def image_bytes(image_format: str = "PNG", size: tuple[int, int] = (12, 8)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "#94a3b8").save(output, format=image_format)
    return output.getvalue()


class StubSession:
    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, *_args, **_kwargs) -> None:
        self.executed += 1


class FakeProvider(ImageGenProvider):
    name = "openai_compat"
    model = "gpt-image-2-c"

    def __init__(self, outcome: ImageGenOutcome) -> None:
        self._outcome = outcome
        self.queries: list[ImageGenQuery] = []

    async def generate(self, query: ImageGenQuery) -> ImageGenOutcome:
        self.queries.append(query)
        return self._outcome


class RaisingProvider(ImageGenProvider):
    name = "openai_compat"
    model = "gpt-image-2-c"

    async def generate(self, query: ImageGenQuery) -> ImageGenOutcome:
        raise RuntimeError("provider exploded")


def ok_outcome(data: bytes) -> ImageGenOutcome:
    return ImageGenOutcome(
        provider="openai_compat",
        model="gpt-image-2-c",
        status="ok",
        blobs=[ImageBlob(data=data, content_type="image/png")],
    )


def test_build_ambiance_prompt_is_deterministic_and_contains_subject() -> None:
    assert build_ambiance_prompt("大阪") == build_ambiance_prompt("大阪")
    prompt = build_ambiance_prompt("  大阪  ")
    assert "大阪" in prompt
    assert prompt == build_ambiance_prompt("大阪")


def test_build_ambiance_prompt_writes_brand_negative_constraints() -> None:
    prompt = build_ambiance_prompt("清迈")
    for keyword in ("文字", "标志", "二维码", "价格", "地标", "紫粉", "留白"):
        assert keyword in prompt
    assert "禁止" in prompt
    assert "不要" in prompt


def test_build_ambiance_prompt_empty_subject_falls_back() -> None:
    prompt = build_ambiance_prompt("   ")
    assert "自然风景" in prompt
    assert prompt == build_ambiance_prompt("")


async def test_generate_ambiance_base_returns_stored_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = image_bytes("PNG", (40, 60))
    stored: dict[str, bytes] = {}

    async def fake_put(key: str, payload: bytes, content_type: str = "") -> None:
        stored[key] = payload

    async def fake_get(key: str) -> bytes:
        return stored[key]

    monkeypatch.setattr(imagegen_service, "put_object", fake_put)
    monkeypatch.setattr(ambiance, "get_object", fake_get)
    provider = FakeProvider(ok_outcome(data))
    org = uuid4()

    result = await generate_ambiance_base(
        StubSession(),  # type: ignore[arg-type]
        org,
        subject="京都",
        provider=provider,
    )
    assert result == data
    assert len(provider.queries) == 1
    query = provider.queries[0]
    assert query.n == 1
    assert query.size == AMBIANCE_SIZE
    assert query.prompt == build_ambiance_prompt("京都")
    assert all(key.startswith(f"{org}/genimg/") for key in stored)


async def test_generate_ambiance_base_unconfigured_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_overrides(session, name):
        return {}

    monkeypatch.setattr(imagegen_service, "load_overrides", no_overrides)
    monkeypatch.setattr(imagegen_service, "get_provider", lambda overrides=None: None)
    result = await generate_ambiance_base(
        StubSession(),  # type: ignore[arg-type]
        uuid4(),
        subject="京都",
    )
    assert result is None


async def test_generate_ambiance_base_provider_error_returns_none() -> None:
    result = await generate_ambiance_base(
        StubSession(),  # type: ignore[arg-type]
        uuid4(),
        subject="京都",
        provider=RaisingProvider(),
    )
    assert result is None


async def test_generate_ambiance_base_unavailable_outcome_returns_none() -> None:
    provider = FakeProvider(
        ImageGenOutcome(
            provider="openai_compat",
            model="gpt-image-2-c",
            status="unavailable",
            error="quota exceeded",
        )
    )
    result = await generate_ambiance_base(
        StubSession(),  # type: ignore[arg-type]
        uuid4(),
        subject="京都",
        provider=provider,
    )
    assert result is None


async def test_generate_ambiance_base_storage_fetch_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_put(*_args, **_kwargs) -> None:
        return None

    async def broken_get(key: str) -> bytes:
        raise OSError("minio down")

    monkeypatch.setattr(imagegen_service, "put_object", fake_put)
    monkeypatch.setattr(ambiance, "get_object", broken_get)
    result = await generate_ambiance_base(
        StubSession(),  # type: ignore[arg-type]
        uuid4(),
        subject="京都",
        provider=FakeProvider(ok_outcome(image_bytes())),
    )
    assert result is None
