"""图谱通道增益评测:按 query 类型分层对比 graph on/off 的检索质量。

存在的理由:``kb_graph_search_enabled`` 的开启门槛写着"实测增益 >= 5pt",但长期
没有配套工具,开关因此被锁在默认关闭。这里补上那把尺子。

**必须分层看,不要只看总均值。** 公开评测(GraphRAG-Bench arXiv:2506.05690、
RAG vs. GraphRAG arXiv:2502.11371)一致显示图谱增益高度依赖问题类型:多跳/关系
推理上可达 +10pt 以上,而简单单跳事实检索上图谱常因引入冗余与噪声而低于纯
向量+稀疏。把两类混在一个均值里,正负相抵,足以让一个真正有用的通道被误杀,
也足以让一个有害的通道蒙混过关。

命令行::

    python -m nicekit.kb.graph_eval --cases eval.json --org-id <uuid> --kb-id <uuid>

评测集格式(JSON 数组),``relevant`` 填 ``SearchHit.source``::

    [{"query": "...", "query_type": "multi_hop", "relevant": ["a.md#chunk0"]}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注,避免 CLI 之外的导入开销
    from sqlalchemy.ext.asyncio import AsyncSession

#: 建议的分层维度。不强制,评测集里出现的任何 query_type 都会独立成组统计。
SINGLE_HOP = "single_hop"
MULTI_HOP = "multi_hop"
SUMMARY = "summary"
#: 与 search.py manifest 的 enable_gate_min_gain_points 同源:人工开启的建议门槛。
DEFAULT_GAIN_GATE_POINTS = 5.0


@dataclass(frozen=True, slots=True)
class EvalCase:
    """一条评测样例。``relevant`` 是该 query 的 ground truth 命中集合。"""

    query: str
    relevant: frozenset[str]
    query_type: str = SINGLE_HOP


@dataclass(frozen=True, slots=True)
class Metrics:
    """二元相关性下的检索质量三件套;``supported`` 是参与统计的样例数。"""

    recall: float
    ndcg: float
    mrr: float
    supported: int


@dataclass(frozen=True, slots=True)
class LayerComparison:
    """单个 query 分层上 graph off → on 的对照。"""

    query_type: str
    off: Metrics
    on: Metrics

    @property
    def recall_gain_points(self) -> float:
        """百分点差值(不是相对提升):0.62 → 0.67 记作 +5.0。"""
        return (self.on.recall - self.off.recall) * 100

    @property
    def ndcg_gain_points(self) -> float:
        return (self.on.ndcg - self.off.ndcg) * 100

    def meets_gate(self, gate_points: float = DEFAULT_GAIN_GATE_POINTS) -> bool:
        """Recall 与 nDCG 任一达标即视为该分层有增益。"""
        return max(self.recall_gain_points, self.ndcg_gain_points) >= gate_points


def recall_at_k(ranked: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """命中的相关项占全部相关项的比例。

    分母是 ground truth 全集而非 min(k, |relevant|):正确文档没进 top-k,下游
    模型就无从补救,这个损失必须计入,否则会得出"benchmark 很高、线上很差"的
    经典假象。
    """
    if not relevant:
        raise ValueError("relevant 不能为空,否则 recall 无定义")
    hit = {item for item in ranked[:k] if item in relevant}
    return len(hit) / len(relevant)


def ndcg_at_k(ranked: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """二元相关性的 nDCG:按位置对数折损,奖励相关项更早出现。

    接了 rerank 时比 recall 更能反映体感——同样召回,排得靠前才有用。
    """
    if not relevant:
        raise ValueError("relevant 不能为空,否则 nDCG 无定义")
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(ranked[:k], start=1)
        if item in relevant
    )
    ideal = sum(
        1.0 / math.log2(rank + 1)
        for rank in range(1, min(len(relevant), k) + 1)
    )
    return dcg / ideal if ideal else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: frozenset[str]) -> float:
    """第一个相关项排名的倒数;一个都没命中记 0。"""
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def aggregate(rankings: Iterable[tuple[Sequence[str], frozenset[str]]], *, k: int) -> Metrics:
    """按样例求平均(macro):每条 query 等权,不被长 ground truth 的样例带偏。"""
    recalls: list[float] = []
    ndcgs: list[float] = []
    rrs: list[float] = []
    for ranked, relevant in rankings:
        if not relevant:
            continue  # 无 ground truth 的样例不参与统计,而不是记 0 拉低均值
        recalls.append(recall_at_k(ranked, relevant, k))
        ndcgs.append(ndcg_at_k(ranked, relevant, k))
        rrs.append(reciprocal_rank(ranked, relevant))
    if not recalls:
        return Metrics(recall=0.0, ndcg=0.0, mrr=0.0, supported=0)
    count = len(recalls)
    return Metrics(
        recall=sum(recalls) / count,
        ndcg=sum(ndcgs) / count,
        mrr=sum(rrs) / count,
        supported=count,
    )


def load_cases(payload: Any) -> list[EvalCase]:
    """解析评测集;字段缺失即报错,不做静默兜底(评测集错了比没有更危险)。"""
    if not isinstance(payload, list):
        raise ValueError("评测集必须是 JSON 数组")
    cases: list[EvalCase] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条样例不是对象")
        query = item.get("query")
        relevant = item.get("relevant")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"第 {index} 条样例缺少非空 query")
        if not isinstance(relevant, list) or not relevant:
            raise ValueError(f"第 {index} 条样例缺少非空 relevant")
        cases.append(
            EvalCase(
                query=query.strip(),
                relevant=frozenset(str(value) for value in relevant),
                query_type=str(item.get("query_type") or SINGLE_HOP),
            )
        )
    return cases


def compare_layers(
    off_by_type: dict[str, list[tuple[Sequence[str], frozenset[str]]]],
    on_by_type: dict[str, list[tuple[Sequence[str], frozenset[str]]]],
    *,
    k: int,
) -> list[LayerComparison]:
    """逐分层汇总;分层顺序按 query_type 名称固定,保证多次运行可比。"""
    return [
        LayerComparison(
            query_type=query_type,
            off=aggregate(off_by_type.get(query_type, []), k=k),
            on=aggregate(on_by_type.get(query_type, []), k=k),
        )
        for query_type in sorted(set(off_by_type) | set(on_by_type))
    ]


async def run_comparison(
    session: AsyncSession,
    org_id: UUID,
    cases: Sequence[EvalCase],
    *,
    kb_ids: list[UUID] | None,
    embedder: Any,
    top_k: int = 10,
) -> list[LayerComparison]:
    """对每条样例各跑一次 graph off / on,按 query_type 分层汇总。

    显式传 ``graph_enabled`` 而不依赖全局配置:评测的意义就是在开关翻转之前
    先量出差值,读全局配置会让结果取决于跑评测时的环境。
    """
    from nicekit.kb.search import search_kb

    off_by_type: dict[str, list[tuple[Sequence[str], frozenset[str]]]] = defaultdict(list)
    on_by_type: dict[str, list[tuple[Sequence[str], frozenset[str]]]] = defaultdict(list)
    for case in cases:
        for graph_enabled, bucket in ((False, off_by_type), (True, on_by_type)):
            hits = await search_kb(
                session,
                org_id,
                case.query,
                top_k=top_k,
                kb_ids=kb_ids,
                embedder=embedder,
                graph_enabled=graph_enabled,
            )
            bucket[case.query_type].append(([hit.source for hit in hits], case.relevant))
    return compare_layers(off_by_type, on_by_type, k=top_k)


def format_report(
    comparisons: Sequence[LayerComparison],
    *,
    gate_points: float = DEFAULT_GAIN_GATE_POINTS,
) -> str:
    """人可读的分层报告;刻意不输出总均值,避免正负相抵掩盖分层结论。"""
    lines = [
        f"图谱通道增益评测(门槛 {gate_points:+.1f}pt,按 query 类型分层)",
        "",
        f"{'分层':<12}{'样例':>5}{'Recall off→on':>22}{'nDCG off→on':>22}{'达标':>6}",
    ]
    for item in comparisons:
        recall = f"{item.off.recall:.3f}→{item.on.recall:.3f} ({item.recall_gain_points:+.1f}pt)"
        ndcg = f"{item.off.ndcg:.3f}→{item.on.ndcg:.3f} ({item.ndcg_gain_points:+.1f}pt)"
        gate = "是" if item.meets_gate(gate_points) else "否"
        lines.append(f"{item.query_type:<12}{item.on.supported:>5}{recall:>22}{ndcg:>22}{gate:>6}")
    lines += [
        "",
        "判读:多跳/关系类分层达标即说明图谱有效;单跳事实类若为负增益,说明全局",
        "开启会伤害简单查询——此时应按 query 意图路由,而不是把开关一把打开。",
    ]
    return "\n".join(lines)


async def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nicekit.kb.graph_eval",
        description="对比 graph on/off 的检索质量,为 kb_graph_search_enabled 提供依据",
    )
    parser.add_argument("--cases", required=True, type=Path, help="评测集 JSON 路径")
    parser.add_argument("--org-id", required=True, type=UUID)
    parser.add_argument("--kb-id", action="append", type=UUID, dest="kb_ids")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--gate", type=float, default=DEFAULT_GAIN_GATE_POINTS)
    args = parser.parse_args(argv)

    from nicekit.core.db import get_session_factory, org_session
    from nicekit.kb.search import default_embedder
    from nicekit.llm.runtime_config import load_runtime_overrides

    cases = load_cases(json.loads(args.cases.read_text(encoding="utf-8")))
    factory = get_session_factory()
    # 与线上同一条取数路径:不加载运行时配置就拿不到 embedding 端点,dense 一路
    # 会静默缺席,评测出来的"增益"实际是在跟一个残缺基线比。
    await load_runtime_overrides(factory)
    session = org_session(factory, args.org_id)
    try:
        comparisons = await run_comparison(
            session,
            args.org_id,
            cases,
            kb_ids=args.kb_ids,
            embedder=default_embedder(),
            top_k=args.top_k,
        )
    finally:
        await session.close()
    print(format_report(comparisons, gate_points=args.gate))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """同步入口。事件循环策略必须在 ``asyncio.run`` **之前**设置,否则 Windows 上
    psycopg 会拿到 ProactorEventLoop 直接拒绝连接。"""
    from nicekit.core.event_loop import use_selector_event_loop_on_windows

    use_selector_event_loop_on_windows()
    return asyncio.run(_main(argv))


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
