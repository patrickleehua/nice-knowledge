"""Parser backend contract for structured source and derived Markdown artifacts.

Downstream callers may continue consuming ``ParsedDocument.markdown`` while
Docling-capable paths retain the lossless structured document for provenance.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedImageCandidate:
    """Parser-neutral image occurrence extracted from one immutable source.

    ``bbox`` uses normalized top-left coordinates ``(left, top, right, bottom)``.
    A repeated payload at two source positions is represented by two candidates;
    persistence may deduplicate the bytes without losing either occurrence.
    """

    content: bytes
    source_occurrence: str
    filename: str
    content_type: str
    page: int | None = None
    slide: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    reading_order: int | None = None
    parser_item_ref: str | None = None
    surrounding_text: str | None = None

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("image candidate content must not be empty")
        if not self.source_occurrence.strip():
            raise ValueError("image candidate source_occurrence must not be empty")
        if not self.filename.strip():
            raise ValueError("image candidate filename must not be empty")
        if not self.content_type.startswith("image/"):
            raise ValueError("image candidate content_type must be an image MIME type")
        for label, value in (("page", self.page), ("slide", self.slide)):
            if value is not None and value < 1:
                raise ValueError(f"image candidate {label} must be one-based")
        if self.page is not None and self.slide is not None:
            raise ValueError("image candidate page and slide are mutually exclusive")
        if self.reading_order is not None and self.reading_order < 0:
            raise ValueError("image candidate reading_order must be non-negative")
        if self.bbox is not None:
            left, top, right, bottom = self.bbox
            if not all(0.0 <= value <= 1.0 for value in self.bbox):
                raise ValueError("image candidate bbox coordinates must be normalized")
            if right <= left or bottom <= top:
                raise ValueError("image candidate bbox must have positive area")


@dataclass(frozen=True)
class ParsedDocument:
    """Parser output with an optional lossless structured representation.

    ``structured_json`` is the primary Docling artifact. Markdown is derived
    from the same document and remains the compatibility format for existing
    chunking, extraction, and preview consumers. Backends without trustworthy
    provenance leave ``structured_json`` as ``None``.
    """

    markdown: str
    page_count: int | None = None
    parser_name: str = "fast"
    structured_json: dict[str, Any] | None = None
    images: tuple[ParsedImageCandidate, ...] = ()


class DocumentParser(ABC):
    """parser 后端接口:bytes + 文件名 → ParsedDocument。

    实现约定:
    - 不支持的扩展名 raise ValueError(消息面向用户)
    - 后端自身不可用(依赖未装/模型缺失)raise ImportError 或 RuntimeError,
      由工厂层捕获并降级 fast,不得让摄入流程整体失败
    """

    name: str = "base"

    @abstractmethod
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        raise NotImplementedError
