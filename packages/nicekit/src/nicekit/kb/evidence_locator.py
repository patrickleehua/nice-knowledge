"""Deterministic evidence anchoring against Docling JSON and derived Markdown."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PAGE_MARKER_RE = re.compile(r"^\s*<!--page:(\d+)-->\s*$")
_MAX_CELL_REF_LENGTH = 100


class EvidenceLocationError(ValueError):
    """Base error for evidence that cannot be anchored safely."""


class EvidenceNotFoundError(EvidenceLocationError):
    """Raised when the quote does not occur in any supplied artifact."""


class AmbiguousEvidenceError(EvidenceLocationError):
    """Raised when the artifacts contain more than one equally strong anchor."""


@dataclass(frozen=True, slots=True)
class EvidenceLocation:
    """Values required to create an ``EvidenceSpan`` record."""

    page: int | None
    start_line: int | None
    end_line: int | None
    cell_ref: str | None
    quote_text: str


@dataclass(frozen=True, slots=True)
class _TextMatch:
    quality: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ArtifactMatch:
    quality: int
    quote_text: str
    page: int | None
    cell_ref: str | None
    identity: tuple[str, int, int]
    error: str | None = None


def _normalized_with_offsets(value: str) -> tuple[str, list[int], list[int]]:
    """Collapse whitespace while retaining offsets into the original string."""

    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < len(value):
        if value[index].isspace():
            run_start = index
            while index < len(value) and value[index].isspace():
                index += 1
            if chars and index < len(value):
                chars.append(" ")
                starts.append(run_start)
                ends.append(index)
            continue
        chars.append(value[index])
        starts.append(index)
        index += 1
        ends.append(index)
    return "".join(chars), starts, ends


def _find_all(haystack: str, needle: str) -> list[int]:
    positions: list[int] = []
    offset = 0
    while True:
        position = haystack.find(needle, offset)
        if position < 0:
            return positions
        positions.append(position)
        offset = position + 1


def _matches(value: str, quote: str) -> list[_TextMatch]:
    exact_positions = _find_all(value, quote)
    if exact_positions:
        quality = 0 if value == quote else 1
        return [
            _TextMatch(quality=quality, start=start, end=start + len(quote))
            for start in exact_positions
        ]

    normalized_value, starts, ends = _normalized_with_offsets(value)
    normalized_quote, _, _ = _normalized_with_offsets(quote)
    if not normalized_quote:
        return []
    normalized_positions = _find_all(normalized_value, normalized_quote)
    quality = 2 if normalized_value == normalized_quote else 3
    return [
        _TextMatch(
            quality=quality,
            start=starts[start],
            end=ends[start + len(normalized_quote) - 1],
        )
        for start in normalized_positions
    ]


def _line_range(value: str, match: _TextMatch) -> tuple[int, int]:
    start_line = value.count("\n", 0, match.start) + 1
    end_offset = max(match.start, match.end - 1)
    end_line = value.count("\n", 0, end_offset) + 1
    return start_line, end_line


def _page_before_line(markdown: str, start_line: int) -> int | None:
    page: int | None = None
    for line in markdown.splitlines()[:start_line]:
        marker = _PAGE_MARKER_RE.match(line)
        if marker:
            page = int(marker.group(1))
    return page


def _select_markdown_match(markdown: str, quote: str) -> tuple[_TextMatch, int | None] | None:
    matches = _matches(markdown, quote)
    if not matches:
        return None
    best_quality = min(match.quality for match in matches)
    best = [match for match in matches if match.quality == best_quality]
    if len(best) != 1:
        raise AmbiguousEvidenceError(
            f"quote has {len(best)} equally strong matches in derived Markdown"
        )
    start_line, _ = _line_range(markdown, best[0])
    return best[0], _page_before_line(markdown, start_line)


def _positive_page(provenance: Any) -> tuple[int | None, str | None]:
    if not isinstance(provenance, Sequence) or isinstance(provenance, (str, bytes)):
        return None, "matched item has no Docling page provenance"
    pages = {
        page
        for item in provenance
        if isinstance(item, Mapping)
        and isinstance((page := item.get("page_no")), int)
        and not isinstance(page, bool)
        and page >= 1
    }
    if len(pages) == 1:
        return pages.pop(), None
    if not pages:
        return None, "matched item has no Docling page provenance"
    return None, "matched item spans multiple Docling pages and cannot be anchored safely"


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _column_name(index: int) -> str:
    name = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _cell_reference(cell: Mapping[str, Any]) -> str | None:
    bounds = _cell_bounds(cell)
    if bounds is None:
        return None
    start_row, end_row, start_col, end_col = bounds
    return _range_reference(start_row, end_row, start_col, end_col)


def _cell_bounds(cell: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    start_row = _non_negative_int(cell.get("start_row_offset_idx"))
    end_row = _non_negative_int(cell.get("end_row_offset_idx"))
    start_col = _non_negative_int(cell.get("start_col_offset_idx"))
    end_col = _non_negative_int(cell.get("end_col_offset_idx"))
    if (
        start_row is None
        or end_row is None
        or start_col is None
        or end_col is None
        or end_row <= start_row
        or end_col <= start_col
    ):
        return None
    return start_row, end_row, start_col, end_col


def _range_reference(start_row: int, end_row: int, start_col: int, end_col: int) -> str:
    start_ref = f"{_column_name(start_col)}{start_row + 1}"
    end_ref = f"{_column_name(end_col - 1)}{end_row}"
    return start_ref if start_ref == end_ref else f"{start_ref}:{end_ref}"


def _qualified_cell_reference(
    table: Mapping[str, Any], reference: str
) -> tuple[str | None, str | None]:
    self_ref = table.get("self_ref")
    if not isinstance(self_ref, str) or not self_ref or self_ref.strip() != self_ref:
        return None, "matched Docling table has no valid self_ref"
    qualified = f"{self_ref}!{reference}"
    if len(qualified) > _MAX_CELL_REF_LENGTH:
        return None, "matched Docling table cell reference exceeds 100 characters"
    return qualified, None


def _best_item_match(value: str, quote: str) -> _TextMatch | None:
    matches = _matches(value, quote)
    if not matches:
        return None
    return min(matches, key=lambda match: (match.quality, match.start, match.end))


def _table_matches(document: Mapping[str, Any], quote: str) -> list[_ArtifactMatch]:
    tables = document.get("tables")
    if not isinstance(tables, Sequence) or isinstance(tables, (str, bytes)):
        return []
    matches: list[_ArtifactMatch] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, Mapping):
            continue
        data = table.get("data")
        cells = data.get("table_cells") if isinstance(data, Mapping) else None
        if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes)):
            continue
        page, page_error = _positive_page(table.get("prov"))
        for cell_index, cell in enumerate(cells):
            if not isinstance(cell, Mapping) or not isinstance(cell.get("text"), str):
                continue
            cell_text = cell["text"]
            text_match = _best_item_match(cell_text, quote)
            if text_match is None:
                continue
            cell_ref = _cell_reference(cell)
            error = page_error
            if cell_ref is None:
                error = "matched Docling table cell has invalid row or column offsets"
            else:
                cell_ref, reference_error = _qualified_cell_reference(table, cell_ref)
                error = error or reference_error
            matches.append(
                _ArtifactMatch(
                    quality=text_match.quality,
                    quote_text=cell_text[text_match.start : text_match.end],
                    page=page,
                    cell_ref=cell_ref,
                    identity=("table", table_index, cell_index),
                    error=error,
                )
            )
    return matches


def _table_row_matches(document: Mapping[str, Any], quote: str) -> list[_ArtifactMatch]:
    tables = document.get("tables")
    if not isinstance(tables, Sequence) or isinstance(tables, (str, bytes)):
        return []
    matches: list[_ArtifactMatch] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, Mapping):
            continue
        data = table.get("data")
        raw_cells = data.get("table_cells") if isinstance(data, Mapping) else None
        if not isinstance(raw_cells, Sequence) or isinstance(raw_cells, (str, bytes)):
            continue
        rows: dict[int, list[tuple[int, Mapping[str, Any], tuple[int, int, int, int]]]] = {}
        for cell_index, cell in enumerate(raw_cells):
            if not isinstance(cell, Mapping) or not isinstance(cell.get("text"), str):
                continue
            bounds = _cell_bounds(cell)
            if bounds is None:
                continue
            start_row, _, start_col, _ = bounds
            rows.setdefault(start_row, []).append((cell_index, cell, bounds))

        page, page_error = _positive_page(table.get("prov"))
        for row_index, row_cells in rows.items():
            # A one-cell row adds no precision over the ordinary cell matcher.
            if len(row_cells) < 2:
                continue
            ordered = sorted(row_cells, key=lambda entry: (entry[2][2], entry[0]))
            row_text = "| " + " | ".join(str(entry[1]["text"]) for entry in ordered) + " |"
            text_match = _best_item_match(row_text, quote)
            if text_match is None:
                continue
            min_row = min(entry[2][0] for entry in ordered)
            max_row = max(entry[2][1] for entry in ordered)
            min_col = min(entry[2][2] for entry in ordered)
            max_col = max(entry[2][3] for entry in ordered)
            cell_ref, reference_error = _qualified_cell_reference(
                table,
                _range_reference(min_row, max_row, min_col, max_col),
            )
            matches.append(
                _ArtifactMatch(
                    quality=text_match.quality,
                    quote_text=row_text[text_match.start : text_match.end],
                    page=page,
                    cell_ref=cell_ref,
                    identity=("table_row", table_index, row_index),
                    error=page_error or reference_error,
                )
            )
    return matches


def _text_item_matches(document: Mapping[str, Any], quote: str) -> list[_ArtifactMatch]:
    items = document.get("texts")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    matches: list[_ArtifactMatch] = []
    for item_index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        value = item.get("text")
        if not isinstance(value, str):
            continue
        text_match = _best_item_match(value, quote)
        if text_match is None:
            continue
        page, page_error = _positive_page(item.get("prov"))
        matches.append(
            _ArtifactMatch(
                quality=text_match.quality,
                quote_text=value[text_match.start : text_match.end],
                page=page,
                cell_ref=None,
                identity=("text", item_index, 0),
                error=page_error,
            )
        )
    return matches


def _select_artifact_match(
    document: Mapping[str, Any], quote: str
) -> _ArtifactMatch | None:
    matches = [
        *_table_matches(document, quote),
        *_table_row_matches(document, quote),
        *_text_item_matches(document, quote),
    ]
    if not matches:
        return None
    best_quality = min(match.quality for match in matches)
    best = [match for match in matches if match.quality == best_quality]
    if len(best) != 1:
        raise AmbiguousEvidenceError(
            f"quote has {len(best)} equally strong matches in Docling JSON"
        )
    # 锚定信息缺失(如 DOCX 无页码 provenance)不在此处拒绝:
    # locate_evidence 会在所有可核验锚(页/行/单元格)全部缺失时才失败。
    return best[0]


def locate_evidence(
    quote: str,
    markdown: str,
    structured_document: Mapping[str, Any] | None = None,
    *,
    line_offset: int = 0,
) -> EvidenceLocation:
    """Locate one quote without approximate or semantic guessing.

    Exact, case-sensitive substring matches outrank whitespace-normalized
    matches. Equally strong candidates are rejected instead of selecting an
    arbitrary occurrence.
    """

    if not isinstance(quote, str) or not quote.strip():
        raise EvidenceLocationError("evidence quote must be non-empty")
    if not isinstance(markdown, str):
        raise EvidenceLocationError("derived Markdown must be a string")
    if not isinstance(line_offset, int) or isinstance(line_offset, bool) or line_offset < 0:
        raise EvidenceLocationError("line_offset must be a non-negative integer")

    artifact_match = (
        _select_artifact_match(structured_document, quote)
        if structured_document is not None
        else None
    )
    markdown_result = _select_markdown_match(markdown, quote)
    if artifact_match is None and markdown_result is None:
        raise EvidenceNotFoundError("evidence quote was not found in supplied artifacts")

    start_line: int | None = None
    end_line: int | None = None
    markdown_page: int | None = None
    markdown_match: _TextMatch | None = None
    if markdown_result is not None:
        markdown_match, markdown_page = markdown_result
        start_line, end_line = _line_range(markdown, markdown_match)
        start_line += line_offset
        end_line += line_offset

    artifact_page = artifact_match.page if artifact_match is not None else None
    if artifact_page is not None and markdown_page is not None and artifact_page != markdown_page:
        raise EvidenceLocationError(
            "Docling page provenance conflicts with the derived Markdown page marker"
        )
    page = artifact_page if artifact_page is not None else markdown_page

    cell_ref = artifact_match.cell_ref if artifact_match is not None else None
    if page is None and start_line is None and cell_ref is None:
        # DOCX 等流式格式可能拿不到页码;只要行锚或单元格锚存在即可核验,
        # 全部缺失才拒绝——与评测口径(_citation_is_field_verifiable)一致。
        reason = artifact_match.error if artifact_match is not None else None
        raise EvidenceLocationError(reason or "evidence has no verifiable anchor")

    if artifact_match is not None and (
        markdown_match is None or artifact_match.quality <= markdown_match.quality
    ):
        quote_text = artifact_match.quote_text
    else:
        assert markdown_match is not None
        quote_text = markdown[markdown_match.start : markdown_match.end]

    return EvidenceLocation(
        page=page,
        start_line=start_line,
        end_line=end_line,
        cell_ref=cell_ref,
        quote_text=quote_text,
    )


__all__ = [
    "AmbiguousEvidenceError",
    "EvidenceLocation",
    "EvidenceLocationError",
    "EvidenceNotFoundError",
    "locate_evidence",
]
