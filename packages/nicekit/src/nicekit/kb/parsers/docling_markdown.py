"""Docling Markdown 导出:按 self_ref 身份改写插图占位符。

docling_core 的序列化器是官方扩展点(`MarkdownDocSerializer.picture_serializer`),
用它逐个 PictureItem 改写,占位符与插图之间是身份对应而非次序对应 —— 这正是
`DoclingDocument.export_to_markdown()` 不敢直接做字符串替换的原因:取不到像素的
插图不会进候选却照样产出 `<!-- image -->`,按出现次序回填必然错位。

docling_core 依赖沉重,本模块只在真正走 Docling 后端时才被导入。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from docling_core.transforms.serializer.base import SerializationResult
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.transforms.serializer.markdown import (
    MarkdownDocSerializer,
    MarkdownParams,
    MarkdownPictureSerializer,
)


class _AnchoredPictureSerializer(MarkdownPictureSerializer):
    """在默认插图序列化结果里,把该 item 自己的占位符换成它自己的锚点。

    走 `super().serialize()` 而不是重写整段:caption、annotation 与
    `get_excluded_refs()` 排除逻辑全部沿用 docling 默认行为,本类只负责认领
    属于自己的那一个占位符。
    """

    def __init__(self, *, anchors: Mapping[str, str], placeholder: str) -> None:
        self._anchors = dict(anchors)
        self._placeholder = placeholder

    def serialize(
        self,
        *,
        item: Any,
        doc_serializer: Any,
        doc: Any,
        **kwargs: Any,
    ) -> SerializationResult:
        result = super().serialize(
            item=item, doc_serializer=doc_serializer, doc=doc, **kwargs
        )
        anchor = self._anchors.get(str(item.self_ref))
        if anchor is None or self._placeholder not in result.text:
            return result
        return create_ser_result(
            text=result.text.replace(self._placeholder, anchor, 1),
            span_source=item,
        )


def export_markdown_with_image_anchors(
    doc: Any,
    anchors: Mapping[str, str],
) -> str:
    """导出 Markdown,并把 `anchors` 里 self_ref 命中的插图占位符换成锚点。

    `MarkdownParams()` 的默认值与 `export_to_markdown()` 的默认参数逐项一致
    (labels/layers 的默认值同源于 docling_core),锚点为空时直接走官方入口。
    """
    if not anchors:
        return doc.export_to_markdown()
    params = MarkdownParams()
    serializer = MarkdownDocSerializer(
        doc=doc,
        params=params,
        picture_serializer=_AnchoredPictureSerializer(
            anchors=anchors,
            placeholder=params.image_placeholder,
        ),
    )
    return serializer.serialize().text


__all__ = ["export_markdown_with_image_anchors"]
