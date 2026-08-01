"""docling 后端:IBM Docling 版面理解 → Markdown(pdf/docx 质量优于 fast)。

依赖较重(torch CPU + 版面/表格模型,首次转换从 HuggingFace 下载模型),
因此全部延迟导入。配置为 Docling 时，依赖、模型或 OCR 失败会交由摄入
租约生命周期标记失败并重试，不得静默降级为缺少 provenance 的 fast 产物。

PDF、DOCX 与图片由 Docling 处理；xlsx/csv/txt/md 继续复用 fast 转换。

导出 Markdown 时,进入候选的插图会把 `<!-- image -->` 换成带 source_occurrence
的解析期锚点(见 image_refs),摄入侧再换成 `kb-image://{asset_id}`;替换按
self_ref 身份进行,不依赖占位符与插图的出现次序。
"""

import io
import logging
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from nicekit.kb.image_refs import parsed_image_placeholder
from nicekit.kb.parsers.base import (
    DocumentParser,
    ParsedDocument,
    ParsedImageCandidate,
)
from nicekit.kb.parsers.fast import IMAGE_SUFFIXES, FastParser

logger = logging.getLogger(__name__)

# Standard pipeline handles PDFs and images; DOCX uses Docling's default pipeline.
_DOCLING_SUFFIXES = (".pdf", ".docx", *IMAGE_SUFFIXES)
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DoclingParserError(RuntimeError):
    """Stable, sanitized parser failure suitable for ingestion diagnostics."""

    code = "docling_conversion_incomplete"

    def __init__(self, status: str) -> None:
        super().__init__(self.code)
        self.status = status


def _png_bytes(image: Any) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _provenance_pages(item: Any) -> set[int]:
    return {
        provenance.page_no
        for provenance in (getattr(item, "prov", None) or ())
    }


def _bounded_nearby_text(
    items: list[tuple[Any, int]],
    index: int,
    *,
    page: int | None,
) -> str | None:
    selected: list[tuple[int, str]] = []
    for distance in range(1, len(items)):
        for candidate_index in (index - distance, index + distance):
            if not 0 <= candidate_index < len(items):
                continue
            candidate_item = items[candidate_index][0]
            candidate_pages = _provenance_pages(candidate_item)
            if (
                (page is not None and page not in candidate_pages)
                or (page is None and candidate_pages)
            ):
                continue
            value = getattr(candidate_item, "text", None)
            if isinstance(value, str) and value.strip():
                selected.append((candidate_index, value.strip()))
                if len(selected) >= 4:
                    text = "\n".join(
                        value
                        for _, value in sorted(selected, key=lambda item: item[0])
                    )
                    return text[:4000]
    if not selected:
        return None
    return "\n".join(
        value for _, value in sorted(selected, key=lambda item: item[0])
    )[:4000]


def _normalized_bbox(doc: Any, provenance: Any) -> tuple[float, float, float, float] | None:
    page = doc.pages.get(provenance.page_no)
    if page is None or page.size is None:
        return None
    bbox = provenance.bbox.to_top_left_origin(page.size.height).normalized(page.size)
    values = (
        max(0.0, min(1.0, float(bbox.l))),
        max(0.0, min(1.0, float(bbox.t))),
        max(0.0, min(1.0, float(bbox.r))),
        max(0.0, min(1.0, float(bbox.b))),
    )
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _docling_picture_candidates(
    doc: Any,
    *,
    filename: str,
) -> tuple[ParsedImageCandidate, ...]:
    from docling_core.types.doc import PictureItem

    items = list(doc.iterate_items())
    stem = PurePosixPath(filename).stem
    candidates: list[ParsedImageCandidate] = []
    for reading_order, (item, _level) in enumerate(items):
        if not isinstance(item, PictureItem):
            continue
        image = item.get_image(doc=doc)
        if image is None:
            logger.warning("Docling picture has no extractable pixels: %s", item.self_ref)
            continue
        provenance = item.prov[0] if item.prov else None
        page = provenance.page_no if provenance is not None else None
        self_ref = str(item.self_ref)
        candidates.append(
            ParsedImageCandidate(
                content=_png_bytes(image),
                source_occurrence=f"docling:{self_ref}",
                filename=f"{stem}-picture-{len(candidates) + 1}.png",
                content_type="image/png",
                page=page,
                bbox=(
                    _normalized_bbox(doc, provenance)
                    if provenance is not None
                    else None
                ),
                reading_order=reading_order,
                parser_item_ref=self_ref,
                surrounding_text=_bounded_nearby_text(
                    items,
                    reading_order,
                    page=page,
                ),
            )
        )
    return tuple(candidates)


def _docling_scanned_page_candidates(
    doc: Any,
    *,
    data: bytes,
    filename: str,
    existing: tuple[ParsedImageCandidate, ...],
) -> tuple[ParsedImageCandidate, ...]:
    """Return one rendered candidate for raster-only PDF pages.

    A page is treated as scanned only when pypdf sees at least one embedded image
    and no source text layer. Pages already represented by a Docling PictureItem
    are excluded to avoid publishing both a detected figure and its full page.
    """

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - Docling remains authoritative
        logger.warning("PDF scan detection failed for %s: %s", filename, exc)
        return ()
    existing_pages = {candidate.page for candidate in existing if candidate.page}
    stem = PurePosixPath(filename).stem
    scanned: list[ParsedImageCandidate] = []
    for page_number, source_page in enumerate(reader.pages, start=1):
        if page_number in existing_pages:
            continue
        try:
            source_text = (source_page.extract_text() or "").strip()
            contains_raster = bool(list(source_page.images))
        except Exception as exc:  # noqa: BLE001 - malformed page is skipped
            logger.warning(
                "PDF page %s scan detection failed for %s: %s",
                page_number,
                filename,
                exc,
            )
            continue
        page_item = doc.pages.get(page_number)
        if source_text or not contains_raster or page_item is None or page_item.image is None:
            continue
        page_image = page_item.image.pil_image
        if page_image is None:
            continue
        scanned.append(
            ParsedImageCandidate(
                content=_png_bytes(page_image),
                source_occurrence=f"docling:scanned-page:{page_number}",
                filename=f"{stem}-page-{page_number}.png",
                content_type="image/png",
                page=page_number,
                bbox=(0.0, 0.0, 1.0, 1.0),
                reading_order=page_number - 1,
                parser_item_ref=f"#/pages/{page_number}",
            )
        )
    return tuple(scanned)


def _picture_anchors(
    pictures: tuple[ParsedImageCandidate, ...],
) -> Mapping[str, str]:
    """self_ref → 解析期图片锚点。只覆盖真正进了候选的插图。"""
    return {
        candidate.parser_item_ref: parsed_image_placeholder(
            candidate.source_occurrence
        )
        for candidate in pictures
        if candidate.parser_item_ref
    }


def _strip_embedded_image_payloads(structured_json: dict[str, Any]) -> None:
    pictures = structured_json.get("pictures")
    if isinstance(pictures, list):
        for picture in pictures:
            if isinstance(picture, dict):
                picture.pop("image", None)
    pages = structured_json.get("pages")
    if isinstance(pages, dict):
        for page in pages.values():
            if isinstance(page, dict):
                page.pop("image", None)


def _docx_header_footer_candidates(
    data: bytes,
    filename: str,
) -> tuple[ParsedImageCandidate, ...]:
    """Supplement OOXML header/footer images that Docling does not expose."""
    parsed = FastParser().parse(data, filename)
    return tuple(
        candidate
        for candidate in parsed.images
        if candidate.parser_item_ref is not None
        and candidate.parser_item_ref.startswith(("word/header", "word/footer"))
    )


class DoclingParser(DocumentParser):
    name = "docling"

    def __init__(self) -> None:
        # 进程内复用 converter(模型加载昂贵);构造失败在 parse 时抛出
        self._converter = None

    def _get_converter(self):
        if self._converter is None:
            # ImportError(未安装)/模型下载失败等统一上抛，由摄入生命周期重试。
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                RapidOcrOptions,
                ThreadedPdfPipelineOptions,
            )
            from docling.document_converter import (
                DocumentConverter,
                ImageFormatOption,
                PdfFormatOption,
            )

            pipeline_options = ThreadedPdfPipelineOptions(
                do_ocr=True,
                do_table_structure=True,
                generate_page_images=True,
                generate_picture_images=True,
                ocr_batch_size=1,
                layout_batch_size=1,
                table_batch_size=1,
                queue_max_size=1,
                ocr_options=RapidOcrOptions(
                    lang=["chinese", "english"],
                    force_full_page_ocr=False,
                ),
            )
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
                }
            )
        return self._converter

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        suffix = PurePosixPath(filename.lower()).suffix
        if suffix not in _DOCLING_SUFFIXES:
            return FastParser().parse(data, filename)

        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        from nicekit.kb.parsers.docling_markdown import (
            export_markdown_with_image_anchors,
        )

        converter = self._get_converter()
        stream = DocumentStream(name=filename, stream=io.BytesIO(data))
        result = converter.convert(stream)
        if result.status != ConversionStatus.SUCCESS:
            logger.error(
                "Docling conversion did not complete",
                extra={
                    "conversion_status": str(result.status),
                    "error_count": len(result.errors),
                },
            )
            raise DoclingParserError(str(result.status))
        doc = result.document
        anchors: Mapping[str, str] = {}
        if suffix in IMAGE_SUFFIXES:
            # 整份文件就是那一张图,markdown 里没有需要回指的插图占位
            images = (
                ParsedImageCandidate(
                    content=data,
                    source_occurrence="source:0",
                    filename=filename,
                    content_type=_IMAGE_CONTENT_TYPES[suffix],
                    reading_order=0,
                    parser_item_ref="source",
                ),
            )
        else:
            pictures = _docling_picture_candidates(doc, filename=filename)
            scanned_pages = (
                _docling_scanned_page_candidates(
                    doc,
                    data=data,
                    filename=filename,
                    existing=pictures,
                )
                if suffix == ".pdf"
                else ()
            )
            office_supplements = (
                _docx_header_footer_candidates(data, filename)
                if suffix == ".docx"
                else ()
            )
            images = (*pictures, *scanned_pages, *office_supplements)
            anchors = _picture_anchors(pictures)
        structured_json = doc.export_to_dict()
        _strip_embedded_image_payloads(structured_json)
        markdown = export_markdown_with_image_anchors(doc, anchors)
        page_count = len(doc.pages) if doc.pages else None
        return ParsedDocument(
            markdown=markdown,
            page_count=page_count,
            parser_name=self.name,
            structured_json=structured_json,
            images=images,
        )
