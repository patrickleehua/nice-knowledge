"""结构感知切片(KB-2):统一 Markdown 中间表示 → StructuredChunk。

递归分割优先级:标题边界(#/##/…)→ 空行段落 → 句子(。!?.!?)→ 硬切。
硬约束:
- 表格(连续 `|` 行)不横切:整表超限按行组切,每片携带表头两行
  (header + 分隔行);table_mode="whole" 时整表一块(超限也不切,如实超长);
- 代码围栏(```/~~~)不切;
- 相邻 chunk 重叠 overlap 字符,重叠起点 snap 到句/行边界
  (窗口内找不到边界时放弃本次重叠,不做词中硬接);
- 每个 chunk 记录标题面包屑(heading_path,由标题层级栈构成)、
  markdown 全文 1-based 行区间(start_line/end_line)与向上最近的
  `<!--page:N-->` 页锚,作为检索溯源锚点。

chunk 长度保证 ≤ max_chars,例外(如实超长,不破坏结构):
整表 whole 模式 / 单行超长的表格行组 / 代码围栏块。
嵌入文本由调用方组装(ingestion._prepare_chunks 经 chunk_embedding_text)。
"""

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")
_PAGE_RE = re.compile(r"^<!--page:(\d+)-->\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|\-]+\|?\s*$")
# 句/行边界:CJK 句末标点独立成界;ASCII .!? 须后接空白/行尾(避免切碎 17.5 之类数字)
_SENT_BOUND_RE = re.compile(r"[。!?]|[.!?](?=\s|$)|\n")

_SEP = "\n\n"


@dataclass(frozen=True)
class StructuredChunk:
    """一个切片:content 为 markdown 原文片段,其余为溯源锚点。

    fixed 策略包装旧 chunk_text 时无锚点(start/end_line 为 None)。
    """

    content: str
    heading_path: str
    start_line: int | None
    end_line: int | None
    page: int | None


@dataclass(frozen=True)
class ExtractionSegment:
    """LLM input plus its exact evidence scope in derived Markdown."""

    heading_path: str
    content: str
    source_text: str
    line_offset: int


@dataclass
class _Block:
    kind: str  # heading / table / code / para
    lines: list[str]
    start: int  # 1-based inclusive
    end: int
    level: int = 0
    path: str = ""
    page: int | None = None


@dataclass
class _Piece:
    """块拆出的原子片:breakable=False(表格行组/代码围栏)不参与重叠截取。"""

    text: str
    start: int
    end: int
    path: str
    page: int | None
    breakable: bool


def _parse_blocks(lines: list[str]) -> list[_Block]:
    """行 → 结构块;顺带维护标题层级栈(path)与页锚(page)。"""
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []
    page: int | None = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        pm = _PAGE_RE.match(stripped)
        if pm:
            page = int(pm.group(1))
            i += 1
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            level, title = len(hm.group(1)), hm.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = " > ".join(t for _, t in stack)
            blocks.append(
                _Block("heading", [stripped], i + 1, i + 1, level=level, path=path, page=page)
            )
            i += 1
            continue
        path = " > ".join(t for _, t in stack)
        fm = _FENCE_RE.match(line)
        if fm:
            fence_char = fm.group(1)[0]
            j = i + 1
            close = re.compile(rf"^\s*{re.escape(fence_char)}{{3,}}\s*$")
            while j < n and not close.match(lines[j]):
                j += 1
            end = min(j, n - 1)
            blocks.append(_Block("code", lines[i : end + 1], i + 1, end + 1, path=path, page=page))
            i = end + 1
            continue
        if stripped.startswith("|"):
            j = i
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            blocks.append(
                _Block("table", [ln.strip() for ln in lines[i:j]], i + 1, j, path=path, page=page)
            )
            i = j
            continue
        j = i
        while j < n:
            cur = lines[j]
            if (
                not cur.strip()
                or cur.strip().startswith("|")
                or _HEADING_RE.match(cur)
                or _FENCE_RE.match(cur)
                or _PAGE_RE.match(cur.strip())
            ):
                break
            j += 1
        blocks.append(_Block("para", lines[i:j], i + 1, j, path=path, page=page))
        i = j
    return blocks


def _char_lines(text: str, first_line: int) -> list[int]:
    """text(以 \\n 连接)中每个字符所在的全文行号。"""
    out: list[int] = []
    ln = first_line
    for ch in text:
        out.append(ln)
        if ch == "\n":
            ln += 1
    return out


def _split_para(block: _Block, max_chars: int, overlap: int) -> list[_Piece]:
    """段落 → 片:超长段先句边界切,窗口内无边界再硬切。

    多片时窗口取 max_chars - overlap,给后续 chunk 级重叠留余量。
    """
    text = "\n".join(ln.strip() for ln in block.lines)
    if len(text) <= max_chars:
        return [_Piece(text, block.start, block.end, block.path, block.page, True)]
    eff = max(max_chars - overlap - len(_SEP), max_chars // 4, 1)
    line_of = _char_lines(text, block.start)
    pieces: list[_Piece] = []
    pos = 0
    while pos < len(text):
        if len(text) - pos <= eff:
            end = len(text)
        else:
            window = text[pos : pos + eff]
            cut = 0
            for m in _SENT_BOUND_RE.finditer(window):
                cut = m.end()
            end = pos + (cut if cut > 0 else eff)
        raw = text[pos:end]
        seg = raw.strip()
        if seg:
            s = pos + (len(raw) - len(raw.lstrip()))
            e = s + len(seg) - 1
            pieces.append(_Piece(seg, line_of[s], line_of[e], block.path, block.page, True))
        pos = end
    return pieces


def _split_table(block: _Block, max_chars: int, table_mode: str) -> list[_Piece]:
    """表格 → 片:不横切。整表超限按行组切,每组携带表头两行。"""
    whole = "\n".join(block.lines)
    if table_mode == "whole" or len(whole) <= max_chars:
        return [_Piece(whole, block.start, block.end, block.path, block.page, False)]
    header: list[str] = []
    body_start = 0
    if len(block.lines) >= 2 and _TABLE_SEP_RE.match(block.lines[1]):
        header = block.lines[:2]
        body_start = 2
    body = block.lines[body_start:]
    if not body:
        return [_Piece(whole, block.start, block.end, block.path, block.page, False)]
    header_len = sum(len(ln) + 1 for ln in header)
    groups: list[list[int]] = []  # 每组存 body 内行下标
    cur: list[int] = []
    cur_len = header_len
    for idx, row in enumerate(body):
        row_len = len(row) + 1
        if cur and cur_len + row_len > max_chars:
            groups.append(cur)
            cur = []
            cur_len = header_len
        cur.append(idx)
        cur_len += row_len
    if cur:
        groups.append(cur)
    pieces: list[_Piece] = []
    for gi, group in enumerate(groups):
        rows = [body[k] for k in group]
        text = "\n".join(header + rows)
        first_row_line = block.start + body_start + group[0]
        start = block.start if gi == 0 else first_row_line
        end = block.start + body_start + group[-1]
        pieces.append(_Piece(text, start, end, block.path, block.page, False))
    return pieces


def _tail_at_boundary(piece: _Piece, limit: int) -> _Piece | None:
    """从 piece 尾部截 ≤ limit 字符做重叠,起点 snap 到句/行边界后;
    piece 不可切(表格/围栏)或窗口内无边界则放弃重叠。"""
    if limit <= 0 or not piece.breakable:
        return None
    text = piece.text
    if len(text) <= limit:
        tail = text
    else:
        window = text[-limit:]
        m = _SENT_BOUND_RE.search(window)
        if m is None:
            return None
        tail = window[m.end() :].strip()
    tail = tail.strip()
    if not tail:
        return None
    start = max(piece.start, piece.end - tail.count("\n"))
    return _Piece(tail, start, piece.end, piece.path, piece.page, True)


def chunk_markdown(
    markdown: str,
    max_chars: int = 1200,
    overlap: int = 150,
    table_mode: str = "row",
) -> list[StructuredChunk]:
    """结构感知切片主入口(参数语义见模块 docstring)。"""
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap 须满足 0 <= overlap < max_chars")
    out: list[StructuredChunk] = []
    buf: list[_Piece] = []

    def cur_len() -> int:
        return sum(len(p.text) for p in buf) + len(_SEP) * max(len(buf) - 1, 0)

    def flush(carry_next: bool) -> _Piece | None:
        nonlocal buf
        if not buf:
            return None
        out.append(
            StructuredChunk(
                content=_SEP.join(p.text for p in buf),
                heading_path=buf[0].path,
                start_line=min(p.start for p in buf),
                end_line=max(p.end for p in buf),
                page=buf[0].page,
            )
        )
        carry = _tail_at_boundary(buf[-1], overlap) if carry_next else None
        buf = []
        return carry

    for block in _parse_blocks(markdown.splitlines()):
        if block.kind == "heading":
            # 标题边界:最高优先级切点,不跨节重叠
            flush(carry_next=False)
            buf.append(
                _Piece(block.lines[0], block.start, block.end, block.path, block.page, True)
            )
            continue
        if block.kind == "para":
            pieces = _split_para(block, max_chars, overlap)
        elif block.kind == "table":
            pieces = _split_table(block, max_chars, table_mode)
        else:  # code
            pieces = [
                _Piece(
                    "\n".join(block.lines), block.start, block.end,
                    block.path, block.page, False,
                )
            ]
        for piece in pieces:
            if buf and cur_len() + len(_SEP) + len(piece.text) > max_chars:
                carry = flush(carry_next=piece.breakable)
                if carry is not None:
                    budget = max_chars - len(piece.text) - len(_SEP)
                    if len(carry.text) > budget:
                        carry = _tail_at_boundary(carry, budget)
                    if carry is not None:
                        buf.append(carry)
            buf.append(piece)
            if len(piece.text) > max_chars:
                # 不可再切且独占超限(整表 whole/超长行组/围栏):独立成块
                flush(carry_next=False)
    flush(carry_next=False)
    return out


def wrap_fixed_chunks(texts: list[str]) -> list[StructuredChunk]:
    """fixed 策略:旧 chunk_text 产物包装为无锚点 StructuredChunk。"""
    return [
        StructuredChunk(content=t, heading_path="", start_line=None, end_line=None, page=None)
        for t in texts
    ]


_H12_RE = re.compile(r"^(#{1,2})\s+(\S.*?)\s*#*\s*$")


def _split_for_extraction_with_offsets(
    markdown: str, max_chars: int
) -> list[ExtractionSegment]:
    lines = markdown.splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    cur_head = ""
    last_h1 = ""
    cur: list[str] = []
    cur_start = 0
    in_fence = False
    for line_index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue
        match = None if in_fence else _H12_RE.match(line)
        if match:
            if any(value.strip() for value in cur):
                sections.append((cur_head, cur_start, cur))
            title = match.group(2).strip()
            if len(match.group(1)) == 1:
                last_h1, cur_head = title, title
            else:
                cur_head = f"{last_h1} > {title}" if last_h1 else title
            cur = [line]
            cur_start = line_index
        else:
            cur.append(line)
    if any(value.strip() for value in cur):
        sections.append((cur_head, cur_start, cur))

    # (heading, content, zero-based start line, exclusive end line, exact source slice)
    units: list[tuple[str, str, int, int, bool]] = []
    for head, section_start, section_lines in sections:
        first = next(
            (index for index, value in enumerate(section_lines) if value.strip()),
            None,
        )
        if first is None:
            continue
        last = max(index for index, value in enumerate(section_lines) if value.strip())
        text = "\n".join(section_lines[first : last + 1])
        line_offset = section_start + first
        end_line = section_start + last + 1
        if len(text) <= max_chars:
            units.append((head, text, line_offset, end_line, True))
            continue
        for chunk in chunk_markdown(
            text,
            max_chars=max_chars,
            overlap=0,
            table_mode="row",
        ):
            if chunk.start_line is None or chunk.end_line is None:
                raise RuntimeError("structured extraction chunk has no line anchor")
            units.append(
                (
                    chunk.heading_path or head,
                    chunk.content,
                    line_offset + chunk.start_line - 1,
                    line_offset + chunk.end_line,
                    False,
                )
            )

    segments: list[ExtractionSegment] = []
    pack: list[tuple[str, str, int, int, bool]] = []

    def flush_pack() -> None:
        if not pack:
            return
        start_line = pack[0][2]
        end_line = pack[-1][3]
        segments.append(
            ExtractionSegment(
                heading_path=pack[0][0],
                content="\n".join(lines[start_line:end_line]),
                source_text="\n".join(lines[start_line:end_line]),
                line_offset=start_line,
            )
        )
        pack.clear()

    for unit in units:
        if not unit[4]:
            flush_pack()
            segments.append(
                ExtractionSegment(
                    heading_path=unit[0],
                    content=unit[1],
                    source_text="\n".join(lines[unit[2] : unit[3]]),
                    line_offset=unit[2],
                )
            )
            continue
        if pack:
            candidate = "\n".join(lines[pack[0][2] : unit[3]])
            if len(candidate) > max_chars:
                flush_pack()
        pack.append(unit)
    flush_pack()
    return segments


def split_for_extraction(
    markdown: str,
    max_chars: int = 12000,
    *,
    include_offsets: bool = False,
) -> list[tuple[str, str]] | list[ExtractionSegment]:
    """结构化抽取分段(KB-2):按 md 一级/二级标题边界聚段。

    - 相邻小节贪心合并到 ≤ max_chars(控制 LLM 调用次数);
    - 单节超限再按表格行组/段落切(复用 chunk_markdown,overlap=0);
    - 默认返回 [(heading_path, 段文本)]，保持既有调用兼容；
    - include_offsets=True 时返回带全局零基 line_offset 的 ExtractionSegment。
    """
    segments = _split_for_extraction_with_offsets(markdown, max_chars)
    if include_offsets:
        return segments
    return [(segment.heading_path, segment.content) for segment in segments]


__all__ = [
    "ExtractionSegment",
    "StructuredChunk",
    "chunk_markdown",
    "split_for_extraction",
    "wrap_fixed_chunks",
]
