"""Image generation unit coverage without a database or object store."""

import base64
import io
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from nicekit.capabilities.imagegen import service as imagegen_service
from nicekit.capabilities.imagegen.base import (
    MAX_IMAGE_BYTES,
    ImageBlob,
    ImageGenOutcome,
    ImageGenProvider,
    ImageGenQuery,
)
from nicekit.capabilities.imagegen.openai_compat import (
    OpenAICompatImageProvider,
    extract_image_urls,
)
from nicekit.capabilities.imagegen.service import (
    generate_images,
    genimg_object_key,
    image_service_readiness,
    normalize_imagegen_payload,
    normalize_query,
    validate_image_blob,
)


def image_bytes(
    image_format: str = "PNG", size: tuple[int, int] = (12, 8), color: str = "#ef4444"
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
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
        self.calls = 0

    async def generate(self, query: ImageGenQuery) -> ImageGenOutcome:
        self.calls += 1
        return self._outcome


def settings(**overrides):
    values = {
        "imagegen_api_key": "env-key",
        "imagegen_base_url": "https://api.v36.cm",
        "imagegen_model": "gpt-image-2-c",
        "imagegen_api_mode": "chat",
        "imagegen_timeout_seconds": 300.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_extract_image_urls_markdown_first_dedup_capped() -> None:
    text = (
        "生成好了 ![img](https://cdn.x.com/a.png) 另存\n"
        "https://cdn.x.com/a.png https://cdn.x.com/b.jpg?sig=1 https://cdn.x.com/c.webp"
    )
    assert extract_image_urls(text, 2) == [
        "https://cdn.x.com/a.png",
        "https://cdn.x.com/b.jpg?sig=1",
    ]
    assert extract_image_urls("没有图", 4) == []


def test_extract_image_urls_preserves_mixed_provider_order() -> None:
    text = (
        "https://cdn.x.com/first.webp 已完成，"
        "随后 ![second](https://cdn.x.com/second.png)"
    )
    assert extract_image_urls(text, 2) == [
        "https://cdn.x.com/first.webp",
        "https://cdn.x.com/second.png",
    ]


def test_normalize_query_clamps() -> None:
    query = normalize_query("  海报  ", 9, "800x600")
    assert query.prompt == "海报"
    assert query.n == 4
    assert query.size is None
    assert normalize_query("x", None, "1024x1536").size == "1024x1536"


def test_admin_payload_strips_and_validates() -> None:
    assert normalize_imagegen_payload(
        {
            "api_key": "  secret  ",
            "base_url": " https://api.v36.cm ",
            "model": " gpt-image-2-c ",
            "api_mode": " CHAT ",
            "timeout_seconds": "300",
        }
    ) == {
        "api_key": "secret",
        "base_url": "https://api.v36.cm",
        "model": "gpt-image-2-c",
        "api_mode": "chat",
        "timeout_seconds": 300.0,
    }
    with pytest.raises(ValueError, match="http://"):
        normalize_imagegen_payload({"base_url": "api.v36.cm"})
    with pytest.raises(ValueError, match="images 或 chat"):
        normalize_imagegen_payload({"api_mode": "responses"})
    with pytest.raises(ValueError, match="1 到 900"):
        normalize_imagegen_payload({"timeout_seconds": 0})


def test_readiness_merges_config_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imagegen_service, "get_settings", lambda: settings())
    ready = image_service_readiness(
        {"api_key": " db-key ", "base_url": " https://api.v36.cm/v1 "}
    )
    assert ready.ready
    assert ready.model == "gpt-image-2-c"
    serialized = json.dumps(ready.as_dict())
    assert "db-key" not in serialized
    assert "api.v36.cm" not in serialized

    monkeypatch.setattr(
        imagegen_service,
        "get_settings",
        lambda: settings(imagegen_api_key=""),
    )
    unavailable = image_service_readiness()
    assert not unavailable.ready
    assert unavailable.reason == "unconfigured_credential"


def test_readiness_rejects_invalid_mode_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imagegen_service, "get_settings", lambda: settings())
    assert image_service_readiness({"api_mode": "bad"}).reason == "invalid_mode"
    assert image_service_readiness({"base_url": "api.v36.cm"}).reason == "invalid_endpoint"


def test_validate_image_derives_actual_type_and_dimensions() -> None:
    validated = validate_image_blob(
        ImageBlob(data=image_bytes("JPEG", (37, 19)), content_type="image/png")
    )
    assert validated.content_type == "image/jpeg"
    assert (validated.width, validated.height) == (37, 19)


def test_validate_image_rejects_malformed_and_unsupported() -> None:
    with pytest.raises(ValueError):
        validate_image_blob(ImageBlob(data=b"not-an-image", content_type="image/png"))
    with pytest.raises(ValueError, match="unsupported"):
        validate_image_blob(
            ImageBlob(data=image_bytes("GIF"), content_type="image/gif")
        )


def test_genimg_object_key_org_prefixed() -> None:
    org = uuid4()
    assert genimg_object_key(org, "a.png") == f"{org}/genimg/a.png"


async def test_chat_provider_uses_v36_stream_contract_and_preserves_order() -> None:
    first = image_bytes("PNG", (20, 10))
    second = image_bytes("WEBP", (9, 15))
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            seen_body.update(json.loads(request.content))
            stream = (
                'data: {"choices":[{"delta":{"content":"![one](https://cdn.test/1.png)"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"![two](https://cdn.test/2.webp)"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        if request.url.path == "/1.png":
            return httpx.Response(200, content=first, headers={"content-type": "image/png"})
        if request.url.path == "/2.webp":
            return httpx.Response(200, content=second, headers={"content-type": "image/webp"})
        return httpx.Response(404)

    provider = OpenAICompatImageProvider(
        api_key="key",
        base_url="https://api.v36.cm/v1",
        model="gpt-image-2-c",
        api_mode="chat",
        timeout=30,
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.generate(ImageGenQuery(prompt="海报", n=2))
    assert outcome.status == "ok"
    assert [blob.data for blob in outcome.blobs] == [first, second]
    assert seen_body["stream"] is True
    assert seen_body["max_tokens"] == 3800


class InterruptedSSEStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk

    async def __aiter__(self):
        yield self.chunk
        raise httpx.RemoteProtocolError("peer closed the stream")


@pytest.mark.parametrize(
    ("content", "expected_status", "download_count"),
    [
        ("![done](https://cdn.test/complete.png)", "ok", 1),
        ("![pending](https://cdn.test/incomplete", "unavailable", 0),
    ],
)
async def test_chat_stream_interruption_never_retries_provider(
    content: str,
    expected_status: str,
    download_count: int,
) -> None:
    image = image_bytes("PNG", (11, 7))
    provider_requests = 0
    downloads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_requests, downloads
        if request.url.path == "/v1/chat/completions":
            provider_requests += 1
            event = json.dumps({"choices": [{"delta": {"content": content}}]})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=InterruptedSSEStream(f"data: {event}\n\n".encode()),
            )
        if request.url.path == "/complete.png":
            downloads += 1
            return httpx.Response(200, content=image, headers={"content-type": "image/png"})
        return httpx.Response(404)

    provider = OpenAICompatImageProvider(
        api_key="key",
        base_url="https://api.v36.cm",
        model="gpt-image-2-c",
        api_mode="chat",
        timeout=30,
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.generate(ImageGenQuery(prompt="海报"))
    assert outcome.status == expected_status
    assert provider_requests == 1
    assert downloads == download_count


async def test_images_provider_accepts_base64_and_url() -> None:
    first = image_bytes("PNG", (5, 7))
    second = image_bytes("JPEG", (8, 6))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": base64.b64encode(first).decode()},
                        {"url": "https://cdn.test/two.jpg"},
                    ]
                },
            )
        if request.url.path == "/two.jpg":
            return httpx.Response(200, content=second, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    provider = OpenAICompatImageProvider(
        api_key="key",
        base_url="https://api.test",
        model="image-model",
        api_mode="images",
        timeout=30,
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.generate(ImageGenQuery(prompt="p", n=2))
    assert outcome.status == "ok"
    assert [blob.data for blob in outcome.blobs] == [first, second]


async def test_images_provider_rejects_oversize_download_before_reading() -> None:
    downloads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.test/oversize.png"}]},
            )
        if request.url.path == "/oversize.png":
            downloads += 1
            return httpx.Response(
                200,
                content=b"not-read",
                headers={
                    "content-type": "image/png",
                    "content-length": str(MAX_IMAGE_BYTES + 1),
                },
            )
        return httpx.Response(404)

    provider = OpenAICompatImageProvider(
        api_key="key",
        base_url="https://api.test",
        model="image-model",
        api_mode="images",
        timeout=30,
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.generate(ImageGenQuery(prompt="p"))
    assert outcome.status == "unavailable"
    assert outcome.error == "图片服务返回了无效的图片结果"
    assert downloads == 1


async def test_provider_failure_is_nonempty_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text="upstream leaked sk-secret https://internal.example",
        )

    provider = OpenAICompatImageProvider(
        api_key="sk-secret",
        base_url="https://internal.example",
        model="gpt-image-2-c",
        api_mode="chat",
        timeout=30,
        transport=httpx.MockTransport(handler),
    )
    outcome = await provider.generate(ImageGenQuery(prompt="p"))
    assert outcome.status == "unavailable"
    assert outcome.error
    assert "sk-secret" not in outcome.error
    assert "internal.example" not in outcome.error


async def test_generate_images_stores_validated_metadata_and_real_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[tuple[str, bytes, str]] = []
    progress: list[str] = []

    async def fake_put(key: str, data: bytes, content_type: str = "") -> None:
        stored.append((key, data, content_type))

    async def on_progress(text: str) -> None:
        progress.append(text)

    monkeypatch.setattr(imagegen_service, "put_object", fake_put)
    session = StubSession()
    org = uuid4()
    provider = FakeProvider(
        ImageGenOutcome(
            provider="openai_compat",
            model="gpt-image-2-c",
            status="ok",
            blobs=[
                ImageBlob(data=image_bytes("PNG", (40, 20)), content_type="text/plain"),
                ImageBlob(data=image_bytes("WEBP", (12, 18)), content_type="image/png"),
            ],
        )
    )
    output = await generate_images(
        session,  # type: ignore[arg-type]
        org_id=org,
        query=ImageGenQuery(prompt="p", n=2),
        provider=provider,
        on_progress=on_progress,
    )
    assert output["status"] == "ok"
    assert [(item["width"], item["height"]) for item in output["images"]] == [
        (40, 20),
        (12, 18),
    ]
    assert output["images"][0]["url"].startswith("/genimg/")
    assert output["images"][0]["url"].endswith(".png")
    assert output["images"][1]["url"].endswith(".webp")
    assert all(key.startswith(f"{org}/genimg/") for key, _, _ in stored)
    assert progress == ["已向图片服务提交生成请求", "正在保存生成结果"]
    assert session.executed == 1


async def test_generate_images_rejects_malformed_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = False

    async def fake_put(*_args, **_kwargs) -> None:
        nonlocal stored
        stored = True

    monkeypatch.setattr(imagegen_service, "put_object", fake_put)
    provider = FakeProvider(
        ImageGenOutcome(
            provider="openai_compat",
            model="gpt-image-2-c",
            status="ok",
            blobs=[ImageBlob(data=b"broken", content_type="image/png")],
        )
    )
    output = await generate_images(
        StubSession(),  # type: ignore[arg-type]
        org_id=uuid4(),
        query=ImageGenQuery(prompt="p"),
        provider=provider,
    )
    assert output["status"] == "unavailable"
    assert output["images"] == []
    assert output["error"] == "图片服务返回了无效的图片结果"
    assert not stored


async def test_generate_images_sanitizes_arbitrary_provider_error() -> None:
    provider = FakeProvider(
        ImageGenOutcome(
            provider="openai_compat",
            model="m",
            status="unavailable",
            error="sk-secret at https://internal.example",
        )
    )
    output = await generate_images(
        StubSession(),  # type: ignore[arg-type]
        org_id=uuid4(),
        query=ImageGenQuery(prompt="p"),
        provider=provider,
    )
    assert output["status"] == "unavailable"
    assert output["error"] == "图片服务暂时不可用，请稍后重试"
    assert "secret" not in json.dumps(output)


async def test_generate_images_without_provider_says_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_overrides(session, name):
        return {}

    monkeypatch.setattr(imagegen_service, "load_overrides", no_overrides)
    monkeypatch.setattr(imagegen_service, "get_provider", lambda overrides=None: None)
    output = await generate_images(
        StubSession(),  # type: ignore[arg-type]
        org_id=uuid4(),
        query=ImageGenQuery(prompt="p"),
    )
    assert output["status"] == "unavailable"
    assert "尚未配置" in output["error"]
