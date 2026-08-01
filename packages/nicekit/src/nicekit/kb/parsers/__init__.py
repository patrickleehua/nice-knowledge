"""Parser backend factory for structured artifacts and derived Markdown.

Docling is the authoritative parser for formats routed to it, so dependency,
model, and OCR failures are surfaced to the ingestion retry lifecycle. Other
optional backends retain the legacy fast-parser fallback.
"""

import logging

from nicekit.core.config import get_settings
from nicekit.kb.parsers.base import (
    DocumentParser,
    ParsedDocument,
    ParsedImageCandidate,
)
from nicekit.kb.parsers.fast import FastParser

logger = logging.getLogger(__name__)

_instances: dict[str, DocumentParser] = {}


def get_parser(name: str) -> DocumentParser:
    """Return a process-cached parser instance by backend name.

    Docling keeps its converter and loaded models on the cached instance.
    """
    if name in _instances:
        return _instances[name]
    if name == "fast":
        parser: DocumentParser = FastParser()
    elif name == "docling":
        from nicekit.kb.parsers.docling import DoclingParser

        parser = DoclingParser()
    else:
        raise ValueError(f"未知 parser 后端: {name}(可选 fast/docling)")
    _instances[name] = parser
    return parser


def parse_document(
    data: bytes, filename: str, backend: str | None = None
) -> ParsedDocument:
    """Parse document bytes into structured and compatibility artifacts.

    Explicit Docling failures are retryable ingestion failures rather than
    silent fast-parser successes. Unknown configured backends (including the
    removed "mineru" stub that may linger in stored profiles) keep the
    compatibility fallback to the fast parser.
    """
    name = backend or get_settings().kb_parser_backend
    if name != "fast":
        try:
            parser = get_parser(name)
        except ValueError as exc:  # 配置了未知后端名:不因配置错杀摄入
            logger.warning("%s,降级 fast", exc)
        else:
            try:
                return parser.parse(data, filename)
            except ValueError:
                raise  # 文件类型不支持:降级也无济于事,如实上抛
            except Exception as exc:  # noqa: BLE001 - backend boundary
                if name == "docling":
                    raise
                logger.warning("parser 后端 %s 不可用,降级 fast: %s", name, exc)
    return get_parser("fast").parse(data, filename)


__all__ = [
    "DocumentParser",
    "ParsedDocument",
    "ParsedImageCandidate",
    "get_parser",
    "parse_document",
]
