"""Contextual Retrieval(分块上下文增强):为每个 chunk 生成"该块在全文档
中的定位"上下文(Anthropic Contextual Retrieval 范式)。

- feature flag `kb_contextual_chunking_enabled` 默认关闭:nDCG 检索线已封版、
  评测链路重建中,实现完整但不启用,待评测恢复后 A/B 验证增益再开;
- prompt 走 Registry(task=kb.chunk.context,prompts_seed 带版本 seed),
  文档全貌与 chunk 批次均按不可信数据 fence(fence_untrusted_document);
- 批量结构化输出(每批 kb_contextual_chunk_batch_size 个 chunk),
  返回 items 带 index,与输入逐一对齐校验,缺失/重复/越界/空文本一律判失败;
- 超长文档自实现头尾截断(参考 wiki_gen 的 WIKI_DOC_BUDGET 思路但不耦合),
  不做滚动摘要——上下文定位只需文档骨架,截断稿已够;
- 任何失败(限流/降级链耗尽/对齐失败)原样抛出,由 ingestion._prepare_chunks
  降级为无 context 摄入(warning,不中断、不重试阻塞——与 caption 同姿态)。
"""

from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel

from nicekit.core.config import get_settings
from nicekit.kb.guardrails import fence_untrusted_document
from nicekit.llm.service import LLMService

CONTEXT_TASK = "kb.chunk.context"

# 文档全貌预算(字符):超长时保头 2/3 + 尾 1/3(标题/总述多在头部,
# 落款/附注多在尾部),中间标注截断。
DOC_CONTEXT_BUDGET = 12000
# 单个 chunk 送入 prompt 的原文上限(定位不需要完整正文)
CHUNK_EXCERPT_CHARS = 1500
# 单条 context 落库上限(kb_chunks.meta.context_text)
MAX_CONTEXT_CHARS = 500

_TRUNCATION_MARKER = "\n……(文档过长,中间部分已截断)……\n"


class ChunkContextItem(BaseModel):
    index: int
    context: str


class ChunkContextBatch(BaseModel):
    items: list[ChunkContextItem]


class ContextAlignmentError(RuntimeError):
    """批量输出与输入 chunk 的 index 不对齐(缺失/重复/越界/空文本)。"""


def _document_overview(full_text: str, budget: int = DOC_CONTEXT_BUDGET) -> str:
    if len(full_text) <= budget:
        return full_text
    head = budget * 2 // 3
    tail = budget - head
    return full_text[:head] + _TRUNCATION_MARKER + full_text[-tail:]


def _batch_message(
    overview: str, batch: Sequence[tuple[int, str]], total: int
) -> str:
    excerpts = "\n\n".join(
        f"[chunk index={index}]\n{text[:CHUNK_EXCERPT_CHARS]}" for index, text in batch
    )
    return (
        fence_untrusted_document(overview, label="document overview")
        + f"\n\n以下是同一文档切出的切片批次(全文共 {total} 个切片,"
        f"本批 {len(batch)} 个),请为每个 index 输出定位上下文:\n"
        + fence_untrusted_document(excerpts, label="chunk batch")
    )


def _validated_contexts(
    result: ChunkContextBatch, expected: Sequence[int]
) -> dict[int, str]:
    """index 对齐校验:输出必须与本批输入一一对应,context 非空。"""
    contexts: dict[int, str] = {}
    for item in result.items:
        if item.index in contexts:
            raise ContextAlignmentError(f"chunk context index 重复: {item.index}")
        cleaned = " ".join(item.context.split()).strip()
        if not cleaned:
            raise ContextAlignmentError(f"chunk context 为空: index={item.index}")
        contexts[item.index] = cleaned[:MAX_CONTEXT_CHARS]
    if set(contexts) != set(expected):
        raise ContextAlignmentError(
            f"chunk context index 不对齐: 期望 {sorted(expected)},"
            f"实际 {sorted(contexts)}"
        )
    return contexts


async def generate_chunk_contexts(
    texts: Sequence[str],
    *,
    full_text: str,
    llm: LLMService,
    org_id: UUID,
) -> list[str]:
    """为 texts(按输入顺序)生成定位上下文;任何一批失败即整体失败。"""
    if not texts:
        return []
    batch_size = get_settings().kb_contextual_chunk_batch_size
    overview = _document_overview(full_text)
    indexed = list(enumerate(texts))
    contexts: list[str | None] = [None] * len(texts)
    for start in range(0, len(indexed), batch_size):
        batch = indexed[start : start + batch_size]
        result = await llm.generate_structured(
            task=CONTEXT_TASK,
            messages=[
                {
                    "role": "user",
                    "content": _batch_message(overview, batch, len(texts)),
                }
            ],
            output_model=ChunkContextBatch,
            org_id=org_id,
        )
        for index, context in _validated_contexts(
            result, [index for index, _text in batch]
        ).items():
            contexts[index] = context
    if any(context is None for context in contexts):  # pragma: no cover - 批次已互斥覆盖
        raise ContextAlignmentError("chunk context 覆盖不完整")
    return [context for context in contexts if context is not None]
