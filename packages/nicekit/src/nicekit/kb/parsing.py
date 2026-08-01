"""切片算法 + 兼容薄委托。

解析逻辑已迁至 parsers 包(统一 Markdown 中间表示,KB-1);
extract_text 保留为 fast 后端的薄委托,供同步请求路径使用
(宿主的即时文件解析场景)——那里要低延迟,不走重后端。
"""


def extract_text(filename: str, data: bytes) -> str:
    """bytes → Markdown 文本(fast 后端)。新代码请直接用 parsers.parse_document。"""
    from nicekit.kb.parsers import parse_document

    return parse_document(data, filename, backend="fast").markdown


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """按段落聚合切片,超长段落硬切,相邻片重叠 overlap 字符。

    KB-1 起输入为 Markdown 全文(逐行切,对标题/表格行同样适用);
    结构感知切片(表格不横切、标题携带上下文)留待 KB-2。
    """
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= max_chars:
            buf = f"{buf}\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        while len(para) > max_chars:
            chunks.append(para[:max_chars])
            para = para[max_chars - overlap :]
        buf = f"{buf}\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks
