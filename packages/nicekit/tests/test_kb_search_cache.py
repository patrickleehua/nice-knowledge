"""检索缓存的键构造与编解码单测(不碰 redis)。

缓存是最容易出安全事故的地方:键少压一个维度就会串结果,跨租户串结果等同越权。
所以这里逐条钉住"什么会改变键",而不是只测一遍往返。
"""

from uuid import uuid4

import pytest

from nicekit.domain.kb_media import ImageSourceCitation, KnowledgeMediaReference
from nicekit.kb.search import SearchHit, StructuredSearchQuery
from nicekit.kb.search_cache import (
    decode_hits,
    encode_hits,
    query_vector_key,
    search_result_key,
)

_BASE = {
    "query": "modelMemory 是干嘛的",
    "top_k": 10,
    "kb_ids": None,
    "structured": None,
    "graph_enabled": False,
    "graph_max_hops": 2,
    "rerank_enabled": True,
    "embedding_label": "siliconflow:BAAI/bge-m3",
    "snapshot_fingerprint": "fp-a",
}


def _key(org_id, **overrides) -> str:
    return search_result_key(org_id, **{**_BASE, **overrides})


def test_result_key_isolates_tenants() -> None:
    """跨租户共享缓存等同越权:同样的查询在不同 org 下必须是不同的键。"""
    assert _key(uuid4()) != _key(uuid4())


def test_result_key_changes_with_snapshot_fingerprint() -> None:
    """发布新快照后旧结果必须读不到——这是靠指纹换键,而不是等 TTL 过期。"""
    org_id = uuid4()

    assert _key(org_id, snapshot_fingerprint="fp-a") != _key(org_id, snapshot_fingerprint="fp-b")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query", "别的问题"),
        ("top_k", 20),
        ("kb_ids", [uuid4()]),
        ("graph_enabled", True),
        ("graph_max_hops", 1),
        ("rerank_enabled", False),
        ("embedding_label", "siliconflow:other-model"),
        ("structured", StructuredSearchQuery(must_include=("A 型主机",))),
    ],
)
def test_every_result_shaping_input_enters_the_key(field, value) -> None:
    """凡是会改变返回内容的入参,都必须改变缓存键,否则不同请求会读到彼此的结果。"""
    org_id = uuid4()

    assert _key(org_id) != _key(org_id, **{field: value})


def test_kb_id_order_does_not_change_the_key() -> None:
    """kb_ids 顺序不影响检索结果,也就不该让缓存平白落空。"""
    org_id, first, second = uuid4(), uuid4(), uuid4()

    assert _key(org_id, kb_ids=[first, second]) == _key(org_id, kb_ids=[second, first])


def test_same_inputs_are_stable_across_calls() -> None:
    org_id = uuid4()

    assert _key(org_id) == _key(org_id)


def test_vector_key_separates_model_and_dimension() -> None:
    """换嵌入模型或维度后绝不能复用旧向量。"""
    org_id = uuid4()
    base = {"provider": "siliconflow", "model": "BAAI/bge-m3", "dim": 1024, "query": "记忆"}

    assert query_vector_key(org_id, **base) != query_vector_key(
        org_id, **{**base, "model": "other"}
    )
    assert query_vector_key(org_id, **base) != query_vector_key(org_id, **{**base, "dim": 768})
    assert query_vector_key(org_id, **base) != query_vector_key(
        org_id, **{**base, "provider": "openai"}
    )
    assert query_vector_key(uuid4(), **base) != query_vector_key(uuid4(), **base)


def _hit_with_media() -> SearchHit:
    asset_id, revision_id, doc_id = uuid4(), uuid4(), uuid4()
    return SearchHit(
        kind="chunk",
        layer="tenant",
        kb_id=str(uuid4()),
        source="上下文指导.md#chunk0",
        confidence=0.78,
        data={"id": str(uuid4()), "content": "modelMemory 用于存储模型记忆"},
        citation={"kind": "source_span", "quote_text": "modelMemory"},
        media_refs=(
            KnowledgeMediaReference(
                asset_id=asset_id,
                snapshot_id=uuid4(),
                alt_text="示意图",
                content_type="image/png",
                width=800,
                height=600,
                page=2,
                citation=ImageSourceCitation(
                    asset_id=asset_id,
                    revision_id=revision_id,
                    source_doc_id=doc_id,
                    source_sha256="a" * 64,
                    image_sha256="c" * 64,
                    page=2,
                    quote_text="modelMemory 示意",
                ),
                thumbnail_url=f"/api/v1/kb/image-assets/{asset_id}/thumbnail",
                content_url=f"/api/v1/kb/image-assets/{asset_id}/content",
            ),
        ),
    )


def test_hits_survive_a_full_encode_decode_round_trip() -> None:
    hits = [_hit_with_media()]

    restored = decode_hits(encode_hits(hits))

    assert restored == hits


def test_decode_rejects_malformed_payload_instead_of_half_building() -> None:
    """缓存里可能躺着上个版本写入的数据:结构对不上就判未命中,回退真实检索。"""
    assert decode_hits("not-a-list") is None
    assert decode_hits([{"kind": "chunk"}]) is None
    assert decode_hits([{**encode_hits([_hit_with_media()])[0], "confidence": "高"}]) is None


def test_decode_rejects_broken_media_reference() -> None:
    """媒体 URL 必须是应用内鉴权路由,伪造的外链不能被反序列化进结果。"""
    payload = encode_hits([_hit_with_media()])
    payload[0]["media_refs"][0]["thumbnail_url"] = "https://evil.example.com/x.png"

    assert decode_hits(payload) is None


def test_empty_hits_round_trip_as_empty_list() -> None:
    """空结果也值得缓存:拒答类查询同样会被反复刷新。"""
    assert decode_hits(encode_hits([])) == []
