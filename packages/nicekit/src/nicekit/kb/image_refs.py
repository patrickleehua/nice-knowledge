"""派生 Markdown 中的图片引用:解析期稳定锚点 → 摄入期可寻址资产引用。

Docling 把每张插图导出成 `<!-- image -->`,占位符本身不带任何身份信息,按出现
次序回填资产是不可靠的:取不到像素的插图不会进候选却照样产出占位符,装饰性图标
又会在入库时被过滤掉,两侧序号一旦错位就会把 A 图的说明贴到 B 图上。所以解析
阶段按 parser 的稳定锚点(self_ref)写下带 `source_occurrence` 的占位,摄入阶段
再拿同一把钥匙换成 `kb-image://{asset_id}`,全程不依赖顺序。

引用刻意不写成 `/api/v1/...` 绝对路径:派生 markdown 是不可变产物,把鉴权端点
形状烧进去会让路由变更污染历史产物。`kb-image://` 只承载资产身份,前端
(markdown-preview)据此解析出 asset_id 走既有鉴权图片端点;纯文本消费方(LLM
抽取、检索片段)读到的是 alt 里的中文说明而不是一串 URL。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from urllib.parse import quote, unquote
from uuid import UUID

#: 派生 markdown 里图片引用的 URI scheme(前端按此解析 asset_id)
IMAGE_REF_SCHEME = "kb-image"

_PLACEHOLDER_PREFIX = "<!--kb-image:"
_PLACEHOLDER_SUFFIX = "-->"
# quote(safe="") 后只剩 unreserved 字符与 %XX,故占位符内不可能出现 "-->"
_PLACEHOLDER_PATTERN = re.compile(
    rf"{re.escape(_PLACEHOLDER_PREFIX)}([A-Za-z0-9\-._~%]+){re.escape(_PLACEHOLDER_SUFFIX)}"
)


@dataclass(frozen=True, slots=True)
class ImageMarkdownRef:
    """一处图片出现在派生 Markdown 中所需的最小可寻址信息。"""

    asset_id: UUID
    page: int | None = None


def parsed_image_placeholder(source_occurrence: str) -> str:
    """解析阶段写入的图片锚点;摄入阶段按 source_occurrence 换成资产引用。

    形如 HTML 注释:万一没被解析(例如换了摄入路径),渲染出来是不可见的注释而
    不是一个断掉的图片链接。
    """
    if not source_occurrence.strip():
        raise ValueError("source_occurrence must not be blank")
    return (
        f"{_PLACEHOLDER_PREFIX}"
        f"{quote(source_occurrence, safe='')}"
        f"{_PLACEHOLDER_SUFFIX}"
    )


def parsed_image_occurrences(markdown: str) -> tuple[str, ...]:
    """按出现顺序返回 markdown 里的解析期锚点(测试与诊断用)。"""
    return tuple(
        unquote(match.group(1)) for match in _PLACEHOLDER_PATTERN.finditer(markdown)
    )


def _alt_text(index: int, page: int | None) -> str:
    """无障碍文本:解析阶段拿不到 caption/OCR(富化在解析之后才跑),
    只能给出确定性的定位说明。前端渲染时若资产已有 caption 会优先用 caption。"""
    return f"插图 {index}（第 {page} 页）" if page else f"插图 {index}"


def resolve_parsed_image_placeholders(
    markdown: str,
    refs: Mapping[str, ImageMarkdownRef],
) -> str:
    """把解析期锚点换成 `![说明](kb-image://{asset_id})`。

    没有对应资产的锚点(装饰性图标被过滤、图像校验失败)整段删除而不是留下一个
    必然 404 的引用;占位符本来就独占一行,删掉只留空行,派生 markdown 的行号
    不变,已落库的证据行锚点与切片行区间因此保持有效。
    """
    if not markdown:
        return markdown
    ordinal = count(1)

    def replace(match: re.Match[str]) -> str:
        ref = refs.get(unquote(match.group(1)))
        if ref is None:
            return ""
        return (
            f"![{_alt_text(next(ordinal), ref.page)}]"
            f"({IMAGE_REF_SCHEME}://{ref.asset_id})"
        )

    return _PLACEHOLDER_PATTERN.sub(replace, markdown)


__all__ = [
    "IMAGE_REF_SCHEME",
    "ImageMarkdownRef",
    "parsed_image_occurrences",
    "parsed_image_placeholder",
    "resolve_parsed_image_placeholders",
]
