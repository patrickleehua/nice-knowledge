"""检索缓存:query 向量与整条检索结果两层。

两层分开是因为**失效条件根本不同**:

- query 向量是 ``(文本, 模型, 维度)`` 的确定映射,知识库怎么变都不影响它,
  可以长期缓存;
- 检索结果绑定当前可见的 active 快照集合,一旦发布新快照、切换快照指针,或者
  可见知识库范围变化,就必须立刻失效——所以缓存键里压了一枚快照指纹,而不是
  单纯靠 TTL 熬过去。

安全边界(这两条是缓存最容易出事的地方,改动前先读):

1. **键一律含 org_id**。检索结果由 RLS 按会话的 org 上下文过滤,跨租户共享缓存
   等同越权。query 向量本身虽然只是查询文本的表示、不含任何租户数据,但也照样
   带上 org_id:一来杜绝"用命中延迟探测别的租户搜过什么"这类侧信道,二来嵌入
   调用是按 org 计量的,跨租户复用会让计量失真。
2. **快照指纹由 RLS 下的查询算出**。指纹查询走同一个会话,只看得到本 org 可见
   的知识库,因此"可见范围变了"会自然反映成"指纹变了",不需要额外的失效钩子。

Redis 不可用时 :mod:`nicekit.core.cache` 会静默降级(读返回 None、写丢弃),
检索照常直查库,所以这里不做任何异常处理。
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core import cache
from nicekit.models.kb import KnowledgeBase

if TYPE_CHECKING:  # pragma: no cover - 运行时延迟导入,避免与 search 循环依赖
    from nicekit.kb.search import SearchHit

_VECTOR_PREFIX = "kb:qvec"
_RESULT_PREFIX = "kb:search"
#: 缓存值的结构版本。改了编解码格式就加一,旧键自然失效而不必手动清理。
_PAYLOAD_VERSION = 2


def _digest(payload: Any) -> str:
    """对规范化后的 JSON 取摘要:键长度可控,且与字典顺序无关。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def query_vector_key(org_id: UUID, *, provider: str, model: str, dim: int, query: str) -> str:
    """query 向量键。模型与维度入键:换嵌入模型后旧向量绝不能被复用。"""
    return f"{_VECTOR_PREFIX}:{org_id}:{_digest([provider, model, dim, query])}"


async def active_snapshot_fingerprint(
    session: AsyncSession, kb_ids: list[UUID] | None
) -> str:
    """当前可见的 (kb, active_snapshot) 集合指纹。

    这是结果缓存的失效凭据:发布新快照、切换快照指针、知识库上下线或可见范围
    变化,都会改变这枚指纹,对应的旧缓存自然不再被读到(而不是等 TTL 过期)。
    查询走调用方的会话,因此受 RLS 约束、只覆盖本 org 可见的知识库。
    """
    statement = select(KnowledgeBase.id, KnowledgeBase.active_snapshot_id).where(
        KnowledgeBase.active_snapshot_id.is_not(None),
        KnowledgeBase.lifecycle_status == "active",
    )
    if kb_ids is not None:
        statement = statement.where(KnowledgeBase.id.in_(kb_ids))
    rows = sorted(
        (str(kb_id), str(snapshot_id))
        for kb_id, snapshot_id in (await session.execute(statement)).all()
    )
    return _digest(rows)


def search_result_key(
    org_id: UUID,
    *,
    query: str,
    top_k: int,
    kb_ids: list[UUID] | None,
    structured: Any,
    graph_enabled: bool,
    graph_max_hops: int,
    rerank_enabled: bool,
    embedding_label: str,
    snapshot_fingerprint: str,
) -> str:
    """结果缓存键。

    任何会改变返回内容的入参都必须进键,否则会串结果。``kb_ids`` 排序后入键
    (顺序不影响结果);``structured`` 直接交给 ``default=str`` 序列化,
    dataclass 与 None 都能稳定落成字面量。
    """
    return f"{_RESULT_PREFIX}:{org_id}:" + _digest(
        [
            _PAYLOAD_VERSION,
            query,
            top_k,
            sorted(str(kb_id) for kb_id in kb_ids) if kb_ids is not None else None,
            structured,
            graph_enabled,
            graph_max_hops,
            rerank_enabled,
            embedding_label,
            snapshot_fingerprint,
        ]
    )


def encode_hits(hits: list[SearchHit]) -> list[dict]:
    """SearchHit → 可 JSON 化的字典。媒体引用用 pydantic 的 json 模式导出。"""
    return [
        {
            "kind": hit.kind,
            "layer": hit.layer,
            "kb_id": hit.kb_id,
            "source": hit.source,
            "confidence": hit.confidence,
            "data": hit.data,
            "citation": hit.citation,
            "media_refs": [ref.model_dump(mode="json") for ref in hit.media_refs],
        }
        for hit in hits
    ]


def decode_hits(payload: Any) -> list[SearchHit] | None:
    """字典 → SearchHit;结构对不上一律判为未命中。

    宁可回退成一次真实检索,也不要把半个反序列化出来的对象喂给下游——缓存里
    可能躺着上一个版本写入的数据。
    """
    from nicekit.domain.kb_media import KnowledgeMediaReference
    from nicekit.kb.search import SearchHit

    if not isinstance(payload, list):
        return None
    hits: list[SearchHit] = []
    try:
        for item in payload:
            hits.append(
                SearchHit(
                    kind=item["kind"],
                    layer=item["layer"],
                    kb_id=item["kb_id"],
                    source=item["source"],
                    confidence=float(item["confidence"]),
                    data=item["data"],
                    citation=item["citation"],
                    media_refs=tuple(
                        KnowledgeMediaReference.model_validate(ref)
                        for ref in item.get("media_refs") or ()
                    ),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return hits


async def load_vector(key: str, *, dim: int) -> list[float] | None:
    """读缓存向量;维度对不上说明模型或配置换过,判为未命中。"""
    payload = await cache.get_json(key)
    if not isinstance(payload, list) or len(payload) != dim:
        return None
    if not all(isinstance(value, (int, float)) for value in payload):
        return None
    return [float(value) for value in payload]


async def store_vector(key: str, vector: list[float], *, ttl_seconds: int) -> None:
    if ttl_seconds > 0:
        await cache.set_json(key, vector, ttl_seconds)


async def load_hits(key: str) -> list[SearchHit] | None:
    payload = await cache.get_json(key)
    return decode_hits(payload) if payload is not None else None


async def store_hits(key: str, hits: list[SearchHit], *, ttl_seconds: int) -> None:
    if ttl_seconds > 0:
        await cache.set_json(key, encode_hits(hits), ttl_seconds)


__all__ = [
    "active_snapshot_fingerprint",
    "decode_hits",
    "encode_hits",
    "load_hits",
    "load_vector",
    "query_vector_key",
    "search_result_key",
    "store_hits",
    "store_vector",
]
