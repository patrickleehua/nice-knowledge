"""Typed snapshot graph scoring and insight tests (pure in-memory).

由 TF ``tests/test_kb_graph.py`` 搬运适配:节点语料换成领域中性的
``concept/product/policy``;类型亲和度先验(TF 的十四组旅行域权重)已改为
注入参数(MIGRATION-PLAN B19),这里同时覆盖"默认全 0.5"与"注入后生效"两条。
"""

import pytest

from nicekit.kb.graph import (
    DEFAULT_TYPE_AFFINITY,
    GraphEdge,
    GraphNode,
    KbGraph,
    get_default_type_affinity,
    graph_insights,
    related_nodes,
    relevance,
    set_default_type_affinity,
)


def _node(g: KbGraph, nid: str, ntype: str, name: str) -> None:
    g.nodes[nid] = GraphNode(id=nid, type=ntype, name=name, kb_id="kb1")


def _edge(
    g: KbGraph, a: str, b: str, link_type: str = "related", w: float = 1.0
) -> None:
    g.edges.append(GraphEdge(a, b, link_type, w, "human", "confirmed"))
    g.adj[a].add(b)
    g.adj[b].add(a)


def _sample(type_affinity: dict | None = None) -> KbGraph:
    g = KbGraph(type_affinity=dict(type_affinity or {}))
    _node(g, "concept:c0", "concept", "质保政策")
    _node(g, "product:p1", "product", "A 型主机")
    _node(g, "product:p2", "product", "B 型主机")
    _node(g, "policy:h1", "policy", "退换货流程")
    _node(g, "spec:s1", "spec", "A 型主机技术参数")
    _node(g, "spec:s2", "spec", "孤立参数表")
    _edge(g, "product:p1", "concept:c0", "related_to")
    _edge(g, "product:p2", "concept:c0", "related_to")
    _edge(g, "policy:h1", "concept:c0", "related_to")
    _edge(g, "spec:s1", "product:p1", "describes")
    _edge(g, "spec:s1", "product:p1", "shared_context", 0.25)
    return g


def test_direct_link_signal_uses_neutral_default_affinity() -> None:
    g = _sample()
    _score, signals = relevance(g, "product:p1", "concept:c0")
    assert signals["direct_link"] == 1.0
    # SDK 不内置领域先验:任意类型组合都是同一常量,该信号无区分度
    assert signals["type_affinity"] == DEFAULT_TYPE_AFFINITY


def test_injected_type_affinity_overrides_the_neutral_default() -> None:
    g = _sample({frozenset({"product", "concept"}): 1.0})
    _score, signals = relevance(g, "product:p1", "concept:c0")
    assert signals["type_affinity"] == 1.0
    # 未声明的组合仍回落到中性默认
    _score2, signals2 = relevance(g, "spec:s1", "product:p1")
    assert signals2["type_affinity"] == DEFAULT_TYPE_AFFINITY


def test_shared_context_is_low_weight_typed_signal() -> None:
    g = _sample()
    _, signals = relevance(g, "product:p1", "spec:s1")
    assert signals["shared_context"] == 0.25
    _, signals2 = relevance(g, "product:p2", "spec:s1")
    assert signals2["shared_context"] == 0.0


def test_adamic_adar_common_neighbors() -> None:
    g = _sample()
    # p1 与 p2 无直接边、无共享上下文,但共享邻居 c0(度=3)
    _score, signals = relevance(g, "product:p1", "product:p2")
    assert signals["direct_link"] == 0.0
    assert signals["common_neighbors"] > 0.0
    # 孤立 s2 与任何点都没有邻居信号
    _, s2 = relevance(g, "spec:s2", "product:p1")
    assert s2["common_neighbors"] == 0.0


def test_related_nodes_reports_typed_and_topological_signals() -> None:
    g = _sample()
    related = related_nodes(g, "product:p1", top_k=3)
    assert related, "应有相关实体"
    by_id = {item["node"]["id"]: item for item in related}
    # 只有 spec:s1 有确认的直接关系与低权共现
    assert by_id["spec:s1"]["signals"]["direct_link"] == 1.0
    assert by_id["spec:s1"]["signals"]["shared_context"] == 0.25
    # 共享邻居的同类节点靠拓扑信号进榜(无领域先验时不被类型亲和度拉开)
    assert by_id["product:p2"]["signals"]["direct_link"] == 0.0
    assert by_id["product:p2"]["signals"]["common_neighbors"] > 0.0


def test_injected_affinity_lifts_the_direct_typed_relation_to_the_top() -> None:
    # 行为差异存证:TF 用旅行域先验(poi-cost 0.8 > poi-poi 0.6)把直接关系顶到
    # 第一;SDK 默认全 0.5 后需要宿主注入先验才会复现同样的排序。
    g = _sample({frozenset({"product", "spec"}): 1.0})
    related = related_nodes(g, "product:p1", top_k=3)
    assert related[0]["node"]["id"] == "spec:s1"


def test_insights_isolated_and_communities() -> None:
    g = _sample()
    result = graph_insights(g)
    assert result["node_count"] == 6
    assert result["isolated_count"] == 1
    assert result["isolated_nodes"][0]["id"] == "spec:s2"
    assert result["communities"], "应检出至少一个社区"
    sizes = sorted(c["size"] for c in result["communities"])
    assert sum(sizes) == 5  # 孤立点不进社区(无边)


def test_relevance_unknown_node_zero() -> None:
    g = _sample()
    score, signals = relevance(g, "product:p1", "product:nope")
    assert score == 0.0 and signals == {}


@pytest.fixture(autouse=True)
def _reset_default_affinity():
    yield
    set_default_type_affinity(None)


def test_set_default_type_affinity_accepts_plain_iterables() -> None:
    set_default_type_affinity({("product", "concept"): 0.9})
    assert get_default_type_affinity() == {frozenset({"product", "concept"}): 0.9}
    set_default_type_affinity(None)
    assert get_default_type_affinity() == {}
