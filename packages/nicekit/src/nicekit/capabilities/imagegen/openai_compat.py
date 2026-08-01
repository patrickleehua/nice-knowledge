"""OpenAI-compatible image provider with images and chat transports.

The chat transport follows the GPT-Image-2 gateway contract used in production:
SSE streaming with ``max_tokens=3800`` and image URLs in the completed markdown
content. Provider bytes are still validated by the service before storage.
"""

import base64
import binascii
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nicekit.capabilities.imagegen.base import (
    MAX_IMAGE_BYTES,
    ImageBlob,
    ImageGenOutcome,
    ImageGenProvider,
    ImageGenQuery,
)

logger = logging.getLogger(__name__)

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_RAW_IMAGE_URL = re.compile(
    r"https?://[^\s<>\")]+\.(?:png|jpe?g|webp)(?:\?[^\s<>\")]*)?", re.I
)
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_CHAT_MAX_TOKENS = 3800
_MAX_IMAGES_RESPONSE_BYTES = MAX_IMAGE_BYTES * 2


class _ProviderStageError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def extract_image_urls(text: str, limit: int) -> list[str]:
    """Extract complete HTTP(S) image URLs in provider order, without duplicates."""
    candidates = [
        *((match.start(), match.group(1)) for match in _MD_IMAGE.finditer(text)),
        *((match.start(), match.group(0)) for match in _RAW_IMAGE_URL.finditer(text)),
    ]
    candidates.sort(key=lambda item: item[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, url in candidates:
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _mime_from_url(url: str) -> str:
    tail = url.split("?")[0].rsplit(".", 1)
    return _EXT_MIME.get(tail[-1].lower(), "image/png") if len(tail) == 2 else "image/png"


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _safe_provider_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, json.JSONDecodeError, binascii.Error)):
        return "图片服务返回了无法解析的结果"
    if isinstance(exc, _ProviderStageError) and exc.stage in {"response_parse", "image_download"}:
        return "图片服务返回了无效的图片结果"
    return "图片服务暂时不可用，请稍后重试"


class OpenAICompatImageProvider(ImageGenProvider):
    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        api_mode: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base = base_url.strip().rstrip("/").removesuffix("/v1")
        self.model = model.strip()
        self._api_mode = api_mode.strip().lower()
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def generate(self, query: ImageGenQuery) -> ImageGenOutcome:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                if self._api_mode == "chat":
                    blobs = await self._via_chat(client, query)
                else:
                    blobs = await self._via_images(client, query)
        except Exception as exc:  # provider/network failures must not break the Agent loop
            stage = exc.stage if isinstance(exc, _ProviderStageError) else "provider_request"
            logger.exception(
                "image provider failed stage=%s type=%s mode=%s model=%s",
                stage,
                type(exc).__name__,
                self._api_mode,
                self.model,
            )
            return ImageGenOutcome(
                provider=self.name,
                model=self.model,
                status="unavailable",
                error=_safe_provider_error(exc),
            )
        if not blobs:
            return ImageGenOutcome(
                provider=self.name,
                model=self.model,
                status="unavailable",
                error="图片服务未返回可用图片",
            )
        return ImageGenOutcome(provider=self.name, model=self.model, status="ok", blobs=blobs)

    async def _via_images(self, client: httpx.AsyncClient, query: ImageGenQuery) -> list[ImageBlob]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": query.prompt,
            "n": query.n,
            "response_format": "url",
        }
        if query.size:
            body["size"] = query.size
        try:
            response = await client.post(
                f"{self._base}/v1/images/generations",
                json=body,
                headers=self._headers(),
            )
            response.raise_for_status()
        except Exception as exc:
            raise _ProviderStageError("provider_request", "images request failed") from exc
        if len(response.content) > _MAX_IMAGES_RESPONSE_BYTES:
            raise _ProviderStageError("response_parse", "images response too large")
        try:
            items = response.json().get("data") or []
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise _ProviderStageError("response_parse", "invalid images response") from exc

        blobs: list[ImageBlob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3 * 4) + 4:
                    raise _ProviderStageError("response_parse", "base64 image too large")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise _ProviderStageError("response_parse", "invalid base64 image") from exc
                blobs.append(ImageBlob(data=data, content_type="image/png"))
            elif item.get("url"):
                blobs.append(await self._download(client, str(item["url"])))
            if len(blobs) >= query.n:
                break
        return blobs

    async def _via_chat(self, client: httpx.AsyncClient, query: ImageGenQuery) -> list[ImageBlob]:
        content_parts: list[str] = []
        stream_complete = False
        try:
            async with client.stream(
                "POST",
                f"{self._base}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": query.prompt}],
                    "stream": True,
                    "max_tokens": _CHAT_MAX_TOKENS,
                },
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        stream_complete = True
                        break
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict):
                        content_parts.append(_content_text(delta.get("content")))
                    message = choice.get("message") or {}
                    if isinstance(message, dict):
                        content_parts.append(_content_text(message.get("content")))
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            partial = "".join(content_parts)
            if not extract_image_urls(partial, query.n):
                raise _ProviderStageError("provider_stream", "incomplete chat stream") from exc
            logger.warning(
                "image provider stream ended after complete image URL type=%s model=%s",
                type(exc).__name__,
                self.model,
            )
        except httpx.HTTPError as exc:
            raise _ProviderStageError("provider_request", "chat request failed") from exc

        content = "".join(content_parts)
        urls = extract_image_urls(content, query.n)
        if not urls:
            suffix = " before DONE" if not stream_complete else ""
            raise _ProviderStageError(
                "response_parse", f"chat response contained no image URL{suffix}"
            )
        return [await self._download(client, url) for url in urls]

    async def _download(self, client: httpx.AsyncClient, url: str) -> ImageBlob:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _ProviderStageError("image_download", "invalid image URL")
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        if int(raw_length) > MAX_IMAGE_BYTES:
                            raise _ProviderStageError("image_download", "image exceeds size limit")
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise _ProviderStageError("image_download", "image exceeds size limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = (response.headers.get("content-type") or "").split(";")[0].strip()
        except _ProviderStageError:
            raise
        except httpx.HTTPError as exc:
            raise _ProviderStageError("image_download", "image download failed") from exc
        if not data:
            raise _ProviderStageError("image_download", "empty image download")
        if not mime.startswith("image/"):
            mime = _mime_from_url(url)
        return ImageBlob(data=data, content_type=mime)
