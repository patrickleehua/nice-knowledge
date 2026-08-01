import pytest

from nicekit.kb.evidence_locator import (
    AmbiguousEvidenceError,
    EvidenceLocationError,
    EvidenceNotFoundError,
    locate_evidence,
)


def _table_document(
    *,
    text: str = "THB 3,500",
    page: int = 3,
    start_row: int = 1,
    end_row: int = 2,
    start_col: int = 1,
    end_col: int = 2,
) -> dict:
    return {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "tables": [
            {
                "self_ref": "#/tables/0",
                "prov": [
                    {
                        "page_no": page,
                        "bbox": {"l": 10, "t": 20, "r": 100, "b": 80},
                        "charspan": [0, len(text)],
                    }
                ],
                "data": {
                    "table_cells": [
                        {
                            "text": text,
                            "start_row_offset_idx": start_row,
                            "end_row_offset_idx": end_row,
                            "start_col_offset_idx": start_col,
                            "end_col_offset_idx": end_col,
                        }
                    ],
                    # Real Docling exports also contain a grid. The locator must
                    # not scan it because it duplicates table_cells.
                    "grid": [[{"text": text}]],
                },
            }
        ],
    }


def _row_document(*rows: tuple[str, str, str], page: int = 3) -> dict:
    cells = []
    for row_index, row in enumerate(rows, start=1):
        for col_index, text in enumerate(row):
            cells.append(
                {
                    "text": text,
                    "start_row_offset_idx": row_index,
                    "end_row_offset_idx": row_index + 1,
                    "start_col_offset_idx": col_index,
                    "end_col_offset_idx": col_index + 1,
                }
            )
    return {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "tables": [
            {
                "self_ref": "#/tables/0",
                "prov": [{"page_no": page, "bbox": {}, "charspan": [0, 100]}],
                "data": {
                    "table_cells": cells,
                    "grid": [[{"text": text} for text in row] for row in rows],
                },
            }
        ],
    }


def test_exact_markdown_quote_returns_one_based_line_anchor_and_page() -> None:
    markdown = "# Rates\n<!--page:2-->\nHotel Alpha\nNightly rate: THB 3,500\n"

    location = locate_evidence("Nightly rate: THB 3,500", markdown)

    assert location.page == 2
    assert location.start_line == 4
    assert location.end_line == 4
    assert location.cell_ref is None
    assert location.quote_text == "Nightly rate: THB 3,500"


def test_whitespace_normalization_preserves_the_source_quote() -> None:
    markdown = "# Hotel\nGrand   Hotel\nnightly rate"

    location = locate_evidence("Grand Hotel nightly rate", markdown)

    assert location.start_line == 2
    assert location.end_line == 3
    assert location.quote_text == "Grand   Hotel\nnightly rate"


def test_exact_match_outranks_a_whitespace_normalized_match() -> None:
    markdown = "Grand Hotel\nGrand\nHotel"

    location = locate_evidence("Grand Hotel", markdown)

    assert location.start_line == 1
    assert location.end_line == 1
    assert location.quote_text == "Grand Hotel"


def test_repeated_equally_strong_markdown_quote_is_rejected() -> None:
    with pytest.raises(AmbiguousEvidenceError, match="derived Markdown"):
        locate_evidence("THB 100", "THB 100\nTHB 100")


def test_missing_quote_fails_explicitly() -> None:
    with pytest.raises(EvidenceNotFoundError, match="not found"):
        locate_evidence("missing", "available evidence")


def test_blank_quote_is_rejected() -> None:
    with pytest.raises(EvidenceLocationError, match="non-empty"):
        locate_evidence(" \n ", "content")


def test_docling_table_cell_adds_page_and_a1_reference() -> None:
    markdown = "| Hotel | Rate |\n| --- | --- |\n| Alpha | THB 3,500 |"

    location = locate_evidence("THB 3,500", markdown, _table_document())

    assert location.page == 3
    assert location.start_line == 3
    assert location.end_line == 3
    assert location.cell_ref == "#/tables/0!B2"
    assert location.quote_text == "THB 3,500"


def test_docling_table_row_adds_page_and_covered_a1_range() -> None:
    quote = "| Hotel Alpha | Bangkok | THB 3,500 |"
    document = _row_document(("Hotel Alpha", "Bangkok", "THB 3,500"), page=4)

    location = locate_evidence(quote, f"# Rates\n{quote}", document)

    assert location.page == 4
    assert location.start_line == 2
    assert location.end_line == 2
    assert location.cell_ref == "#/tables/0!A2:C2"
    assert location.quote_text == quote


def test_docling_table_row_allows_markdown_pipe_whitespace_normalization() -> None:
    document = _row_document(("Hotel   Alpha", "Bangkok", "THB 3,500"))
    quote = "| Hotel Alpha | Bangkok | THB 3,500 |"

    location = locate_evidence(quote, quote, document)

    assert location.page == 3
    assert location.cell_ref == "#/tables/0!A2:C2"
    assert location.quote_text == quote


def test_repeated_docling_table_row_is_rejected() -> None:
    quote = "| Hotel Alpha | Bangkok | THB 3,500 |"
    document = _row_document(
        ("Hotel Alpha", "Bangkok", "THB 3,500"),
        ("Hotel Alpha", "Bangkok", "THB 3,500"),
    )

    with pytest.raises(AmbiguousEvidenceError, match="Docling JSON"):
        locate_evidence(quote, quote, document)


def test_merged_table_cell_uses_an_a1_range_beyond_column_z() -> None:
    document = _table_document(
        text="Group rate",
        start_row=2,
        end_row=4,
        start_col=26,
        end_col=28,
    )

    location = locate_evidence("Group rate", "Group rate", document)

    assert location.cell_ref == "#/tables/0!AA3:AB4"
    assert location.page == 3


def test_table_cell_allows_only_whitespace_normalization() -> None:
    document = _table_document(text="THB   3,500\nper night")

    location = locate_evidence("THB 3,500 per night", "THB 3,500 per night", document)

    assert location.cell_ref == "#/tables/0!B2"
    assert location.quote_text == "THB 3,500 per night"


def test_text_item_provenance_adds_page_to_markdown_line_anchor() -> None:
    document = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "text": "Breakfast is included",
                "prov": [{"page_no": 4, "bbox": {}, "charspan": [0, 21]}],
            }
        ]
    }

    location = locate_evidence("Breakfast is included", "Breakfast is included", document)

    assert location.page == 4
    assert location.start_line == 1
    assert location.end_line == 1
    assert location.cell_ref is None


def test_structured_json_without_quote_falls_back_to_markdown_anchor() -> None:
    location = locate_evidence(
        "Markdown-only evidence",
        "# Heading\nMarkdown-only evidence",
        {"schema_name": "DoclingDocument", "tables": [], "texts": []},
    )

    assert location.page is None
    assert location.start_line == 2
    assert location.end_line == 2


def test_line_offset_translates_segment_lines_to_document_lines() -> None:
    location = locate_evidence(
        "Evidence on two\nsegment lines",
        "Segment heading\nEvidence on two\nsegment lines",
        line_offset=40,
    )

    assert location.start_line == 42
    assert location.end_line == 43


@pytest.mark.parametrize("line_offset", [-1, True, 1.5])
def test_invalid_line_offset_is_rejected(line_offset) -> None:
    with pytest.raises(EvidenceLocationError, match="line_offset"):
        locate_evidence("evidence", "evidence", line_offset=line_offset)


def test_ambiguous_docling_cells_are_rejected_instead_of_guessing() -> None:
    document = _table_document(text="THB 100")
    duplicate = {
        **document["tables"][0],
        "self_ref": "#/tables/1",
        "prov": [{"page_no": 4, "bbox": {}, "charspan": [0, 7]}],
    }
    document["tables"].append(duplicate)

    with pytest.raises(AmbiguousEvidenceError, match="Docling JSON"):
        locate_evidence("THB 100", "THB 100", document)


def test_table_cell_without_one_page_of_provenance_degrades_to_line_anchor() -> None:
    """页码锚缺失但行锚/单元格锚仍可核验时降级返回,不再整条拒绝。"""
    document = _table_document(text="THB 100")
    document["tables"][0]["prov"] = [
        {"page_no": 1, "bbox": {}, "charspan": [0, 3]},
        {"page_no": 2, "bbox": {}, "charspan": [4, 7]},
    ]

    location = locate_evidence("THB 100", "THB 100", document)

    assert location.page is None
    assert (location.start_line, location.end_line) == (1, 1)
    assert location.cell_ref == "#/tables/0!B2"


def test_table_match_without_self_ref_degrades_to_line_anchor() -> None:
    document = _table_document(text="THB 100")
    document["tables"][0].pop("self_ref")

    location = locate_evidence("THB 100", "THB 100", document)

    assert location.cell_ref is None
    assert (location.start_line, location.end_line) == (1, 1)


def test_table_match_with_oversized_qualified_reference_degrades_to_line_anchor() -> None:
    document = _table_document(text="THB 100")
    document["tables"][0]["self_ref"] = "#/tables/" + "x" * 90

    location = locate_evidence("THB 100", "THB 100", document)

    assert location.cell_ref is None
    assert (location.start_line, location.end_line) == (1, 1)


def test_anchorless_match_is_rejected() -> None:
    """页/行/单元格锚全部缺失时仍必须拒绝(不制造不可核验的证据)。"""
    document = {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "texts": [{"text": "THB 100", "prov": None}],
    }

    with pytest.raises(EvidenceLocationError, match="page provenance"):
        locate_evidence("THB 100", "unrelated markdown", document)


def test_conflicting_markdown_page_marker_is_rejected() -> None:
    with pytest.raises(EvidenceLocationError, match="conflicts"):
        locate_evidence(
            "THB 3,500",
            "<!--page:2-->\nTHB 3,500",
            _table_document(page=3),
        )
