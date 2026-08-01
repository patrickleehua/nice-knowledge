"""图片生成适配器契约(与 provider 无关)。

上层(agent 工具/service)只依赖本模块类型:换生图网关只替换 provider 实现。
provider 返回原始图片字节(ImageBlob),落盘/出 URL 由 service 统一做——
provider 不感知存储。所有实现必须可降级:失败返回 status=unavailable,
不抛异常阻塞 agent loop(同 OtaProvider/SearchProvider 思路)。
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

SIZES = {"1024x1024", "1024x1536", "1536x1024"}
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class ImageGenQuery(BaseModel):
    prompt: str
    n: int = 1  # service 已 clamp 到 1-4
    size: str | None = None  # SIZES 之一;None = 网关默认


class ImageBlob(BaseModel):
    data: bytes
    content_type: str  # image/png 等


class ImageGenOutcome(BaseModel):
    provider: str
    model: str
    status: str  # ok / unavailable
    blobs: list[ImageBlob] = Field(default_factory=list)
    error: str | None = None


class ImageGenProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def generate(self, query: ImageGenQuery) -> ImageGenOutcome: ...
