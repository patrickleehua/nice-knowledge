"""结构感知切片(KB-2)单元测试:不碰外部服务。

覆盖:标题边界与面包屑 / 表格不横切(行组携带表头、whole 整块)/
句边界 overlap / 行号与页锚溯源 / 代码围栏不切 / fixed 包装 /
IngestProfile 校验 / h1-h2 聚段抽取分割。
"""

import pytest
from pydantic import ValidationError

from nicekit.domain.kb import (
    INGEST_PROFILE_PRESETS,
    GenericEntityExtraction,
    GenericExtractedItem,
    IngestProfile,
)
from nicekit.kb.chunker import (
    ExtractionSegment,
    chunk_markdown,
    split_for_extraction,
    wrap_fixed_chunks,
)

COST_MD = """# 年度参考成本

<!--page:1-->

## 设备

以下为 2026 参考价。以合同为准。

| 厂区 | 设备 | 价格 |
| --- | --- | --- |
| 华东 | A-100 | 95 |
| 华南 | B-200 | 88 |

## 备件

一号机备件17元。二号机组备件20元。
"""


def test_heading_boundary_and_breadcrumb() -> None:
    chunks = chunk_markdown(COST_MD, max_chars=200, overlap=50)
    paths = [c.heading_path for c in chunks]
    assert "年度参考成本 > 设备" in paths
    assert "年度参考成本 > 备件" in paths
    # 标题边界是最高优先级切点:设备节与备件节不会混在同一 chunk
    device = next(c for c in chunks if c.heading_path.endswith("设备"))
    assert "备件" not in device.content.replace("## 备件", "")
    part = next(c for c in chunks if c.heading_path.endswith("备件"))
    assert "一号机" in part.content


def test_line_and_page_anchors() -> None:
    chunks = chunk_markdown(COST_MD, max_chars=200, overlap=50)
    lines = COST_MD.splitlines()
    for c in chunks:
        assert c.start_line is not None and c.end_line is not None
        assert 1 <= c.start_line <= c.end_line <= len(lines)
        # 首行锚点可回指原文(chunk 内容以该行文本开头)
        assert c.content.splitlines()[0] == lines[c.start_line - 1].strip()
    part = next(c for c in chunks if c.heading_path.endswith("备件"))
    assert lines[part.end_line - 1].startswith("一号机")
    # 页锚:page:1 之后的块都记 page=1,之前的为 None
    assert chunks[0].page is None
    assert part.page == 1


def _table_md(rows: int) -> str:
    body = "\n".join(f"| 厂区{i} | 设备{i} | {i}00 |" for i in range(rows))
    return f"## 设备表\n\n| 厂区 | 设备 | 价格 |\n| --- | --- | --- |\n{body}"


def test_table_never_cut_mid_row_and_header_copied() -> None:
    chunks = chunk_markdown(_table_md(30), max_chars=300, overlap=50)
    table_chunks = [c for c in chunks if "| 厂区" in c.content]
    assert len(table_chunks) >= 2, "整表超限应按行组切成多片"
    seen_rows: list[str] = []
    for c in table_chunks:
        rows = [ln for ln in c.content.splitlines() if ln.startswith("|")]
        # 每片携带表头两行(header + 分隔行)
        assert rows[0] == "| 厂区 | 设备 | 价格 |"
        assert set(rows[1]) <= set("|- ")
        # 行组完整:每个数据行都是完整的 GFM 行(不被横切)
        for row in rows[2:]:
            assert row.startswith("| 厂区") and row.endswith("00 |")
        seen_rows += rows[2:]
    assert len(seen_rows) == 30, "所有数据行都应被覆盖且不重复"
    # 行号锚点:第二片起 start_line 指向自己的首个数据行
    md_lines = _table_md(30).splitlines()
    second = table_chunks[1]
    assert md_lines[second.start_line - 1] == second.content.splitlines()[2]


def test_table_mode_whole_keeps_table_intact() -> None:
    md = _table_md(30)
    chunks = chunk_markdown(md, max_chars=300, overlap=50, table_mode="whole")
    table_chunks = [c for c in chunks if "| 厂区0 |" in c.content]
    assert len(table_chunks) == 1, "whole 模式整表一块,超限也不切"
    whole = table_chunks[0]
    assert len(whole.content) > 300  # 如实超长
    assert whole.content.count("| 厂区 | 设备 | 价格 |") == 1
    assert "| 厂区29 |" in whole.content


def test_overlap_snaps_to_sentence_boundary() -> None:
    para_a = "甲段第一句。甲段第二句。甲段结尾句是这一句。"
    para_b = "乙" * 180
    md = f"{para_a * 8}\n\n{para_b}"
    chunks = chunk_markdown(md, max_chars=200, overlap=60)
    assert len(chunks) >= 2
    for cur in chunks[1:]:
        # 重叠起点 snap 到句边界:每个后续 chunk 都以完整句子开头(不从句中开始)
        head = cur.content.splitlines()[0]
        assert head.startswith(("甲段", "乙")), head
    # 相邻 chunk 确有重叠:chunk1 的某个后缀 == chunk2 的前缀,且不超 overlap
    a, b = chunks[0].content, chunks[1].content
    ov = max(
        (k for k in range(1, min(len(a), len(b)) + 1) if a.endswith(b[:k])), default=0
    )
    assert 0 < ov <= 60, "相邻 chunk 应有 ≤overlap 的重叠"
    # 重叠起点 snap 到句边界:重叠段在 chunk1 里紧跟句末标点
    assert a[-ov - 1] in "。!?.!?\n"


def test_chunks_respect_max_chars_except_unbreakable() -> None:
    md = COST_MD + "\n\n" + ("长句子内容测试。" * 400)
    for c in chunk_markdown(md, max_chars=300, overlap=50):
        assert len(c.content) <= 300


def test_code_fence_not_split() -> None:
    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(60)) + "\n```"
    md = f"## 代码\n\n{code}"
    chunks = chunk_markdown(md, max_chars=200, overlap=50)
    fence = [c for c in chunks if "```python" in c.content]
    assert len(fence) == 1
    assert fence[0].content.count("```") == 2, "围栏完整,不被切开"
    assert "line_59" in fence[0].content


def test_empty_and_blank_markdown() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("  \n\n   \n") == []


def test_wrap_fixed_chunks_no_anchor() -> None:
    wrapped = wrap_fixed_chunks(["片段一", "片段二"])
    assert [c.content for c in wrapped] == ["片段一", "片段二"]
    assert all(
        c.heading_path == "" and c.start_line is None and c.end_line is None and c.page is None
        for c in wrapped
    )


# ---- IngestProfile 校验 ----------------------------------------------------


def test_ingest_profile_defaults() -> None:
    p = IngestProfile()
    assert p.parser == "docling" and p.chunk_strategy == "structure"
    assert p.chunk_max_chars == 1200 and p.chunk_overlap_chars == 150
    assert p.table_mode == "row"
    # caption_images 默认开启(自动化优先:VLM caption 失败时摄入侧自动降级)
    assert p.parent_child is False and p.caption_images is True
    assert p.caption_provider is None and p.caption_model is None


@pytest.mark.parametrize(
    "bad",
    [
        {"chunk_max_chars": 100},  # < 200
        {"chunk_max_chars": 9000},  # > 8000
        {"chunk_overlap_chars": 600},  # > 500
        {"chunk_max_chars": 300, "chunk_overlap_chars": 300},  # overlap >= max
        {"parser": "no-such"},
        {"table_mode": "column"},
        {"caption_provider": "openai"},
        {"caption_model": "vision-model"},
        {"unknown_field": 1},  # extra=forbid
    ],
)
def test_ingest_profile_rejects_invalid(bad: dict) -> None:
    with pytest.raises(ValidationError):
        IngestProfile.model_validate(bad)


def test_ingest_profile_presets_all_valid() -> None:
    for key, preset in INGEST_PROFILE_PRESETS.items():
        assert preset["label"]
        p = IngestProfile.model_validate(preset["profile"])
        assert p.chunk_overlap_chars < p.chunk_max_chars, key
    assert INGEST_PROFILE_PRESETS["tabular_xlsx"]["profile"]["table_mode"] == "row"
    assert INGEST_PROFILE_PRESETS["illustrated_pdf"]["profile"]["table_mode"] == "whole"


# ---- 抽取条目置信度契约(mock 层校验 schema)--------------------------------
# SDK 化后行业专用契约(ExtractedHotel/HotelExtraction 等)已删除,
# 契约锁定改在唯一保留的通用条目 GenericExtractedItem 上做。

_ITEM = {
    "attributes_json": '{"name": "A-100", "amount": 95.0}',
    "evidence_quote": "A-100 | 95.0",
}


def test_extraction_item_confidence_contract() -> None:
    # 模型给了置信度 → 原样入库;漏给 → validator 兜底 0.9(schema 仍必填)
    assert GenericExtractedItem.model_validate({**_ITEM, "confidence": 0.4}).confidence == 0.4
    assert GenericExtractedItem.model_validate(_ITEM).confidence == 0.9
    with pytest.raises(ValidationError):
        GenericExtractedItem.model_validate({**_ITEM, "confidence": 1.5})
    # 双协议 strict 交集:confidence 必填且不携带 default/minimum/maximum 关键字
    item_schema = GenericEntityExtraction.model_json_schema()["$defs"]["GenericExtractedItem"]
    assert "confidence" in item_schema["required"]
    assert not {"default", "minimum", "maximum"} & set(
        item_schema["properties"]["confidence"]
    )


def test_extraction_item_evidence_quote_contract() -> None:
    item = GenericExtractedItem.model_validate({**_ITEM, "confidence": 0.8})
    assert item.evidence_quote == _ITEM["evidence_quote"]

    for evidence_quote in ("", "   ", "x" * 2001):
        with pytest.raises(ValidationError):
            GenericExtractedItem.model_validate(
                {**_ITEM, "confidence": 0.8, "evidence_quote": evidence_quote}
            )

    with pytest.raises(ValidationError):
        GenericExtractedItem.model_validate(
            {
                key: value
                for key, value in {**_ITEM, "confidence": 0.8}.items()
                if key != "evidence_quote"
            }
        )


def test_generic_item_requires_json_object_attributes() -> None:
    # attributes_json 必须是合法 JSON 对象字符串(落库前还会过类型 field_schema)
    assert GenericExtractedItem.model_validate(
        {**_ITEM, "confidence": 0.8}
    ).parsed_attributes() == {"name": "A-100", "amount": 95.0}
    for bad in ("not json", "[1, 2]", '"text"'):
        with pytest.raises(ValidationError):
            GenericExtractedItem.model_validate(
                {**_ITEM, "confidence": 0.8, "attributes_json": bad}
            )


# ---- 结构化抽取分段(h1/h2 聚段)-------------------------------------------


def test_split_for_extraction_h1_h2_sections() -> None:
    md = (
        "# 区域一\n\n" + "华东概况。" * 10
        + "\n\n## 设备\n\n" + "设备明细。" * 10
        + "\n\n### 高配\n\n" + "高配补充。" * 5
        + "\n\n# 区域二\n\n" + "华南概况。" * 10
    )
    segments = split_for_extraction(md, max_chars=120)
    heads = [h for h, _ in segments]
    # h1/h2 是聚段边界;h3 不单独成段(跟随所属 h2)
    assert "区域一" in heads and "区域一 > 设备" in heads and "区域二" in heads
    device_seg = next(t for h, t in segments if h == "区域一 > 设备")
    assert "### 高配" in device_seg
    # 全文无遗漏
    assert "华南概况" in "".join(t for _, t in segments)


def test_split_for_extraction_packs_small_sections() -> None:
    md = "# A\n\n短内容甲。\n\n## B\n\n短内容乙。\n\n## C\n\n短内容丙。"
    segments = split_for_extraction(md, max_chars=12000)
    assert len(segments) == 1, "小节应贪心合并,控制 LLM 调用次数"
    head, text = segments[0]
    assert head == "A"
    assert "短内容丙" in text


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\n\n\n\n"])
def test_split_for_extraction_preserves_source_lines_and_offsets(separator: str) -> None:
    markdown = f"# A\n首段证据{separator}## B\n第二段证据"

    segments = split_for_extraction(markdown, 12000, include_offsets=True)

    assert len(segments) == 1
    assert isinstance(segments[0], ExtractionSegment)
    assert segments[0].content == markdown
    assert segments[0].source_text == markdown
    assert segments[0].line_offset == 0
    evidence_line = segments[0].content.splitlines().index("第二段证据") + 1
    assert evidence_line + segments[0].line_offset == len(markdown.splitlines())


def test_split_for_extraction_oversized_section_sub_split() -> None:
    body = "\n".join(f"| 项目{i} | {i}0元 |" for i in range(80))
    md = f"## 成本表\n\n| 项目 | 价格 |\n| --- | --- |\n{body}"
    segments = split_for_extraction(md, max_chars=400)
    assert len(segments) > 1, "单节超限应再切"
    for _, text in segments[1:]:
        if text.startswith("|"):
            assert text.splitlines()[0] == "| 项目 | 价格 |", "子段表格片应携带表头"


def test_split_for_extraction_table_header_copy_does_not_shift_evidence_lines() -> None:
    body = "\n".join(f"| 项目{i} | {i}0元 |" for i in range(80))
    markdown = f"## 成本表\n\n| 项目 | 价格 |\n| --- | --- |\n{body}"
    source_lines = markdown.splitlines()

    segments = split_for_extraction(markdown, 400, include_offsets=True)

    assert all(isinstance(segment, ExtractionSegment) for segment in segments)
    copied_header_segment = next(
        segment
        for segment in segments
        if segment.content.startswith("| 项目 | 价格 |")
        and not segment.source_text.startswith("| 项目 | 价格 |")
    )
    for local_index, line in enumerate(copied_header_segment.source_text.splitlines()):
        assert source_lines[copied_header_segment.line_offset + local_index] == line


def test_split_for_extraction_no_heading_fallback() -> None:
    md = "没有任何标题的纯文本。" * 3
    segments = split_for_extraction(md, max_chars=12000)
    assert len(segments) == 1 and segments[0][0] == ""
