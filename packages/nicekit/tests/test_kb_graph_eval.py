"""图谱增益评测指标的单测。

这把尺子本身错了,比没有尺子更危险——它会给"开不开图谱"这个决定提供假依据。
所以指标全部对着手算值断言,而不是自我一致性检查。
"""

import math

import pytest

from nicekit.kb.graph_eval import (
    DEFAULT_GAIN_GATE_POINTS,
    MULTI_HOP,
    SINGLE_HOP,
    EvalCase,
    LayerComparison,
    Metrics,
    aggregate,
    compare_layers,
    format_report,
    load_cases,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)


def test_recall_counts_missed_ground_truth_in_the_denominator() -> None:
    """分母是 ground truth 全集:正确文档没进 top-k 的损失必须计入。

    若分母取 min(k, |relevant|),就会得出"benchmark 很高、线上很差"的假象。
    """
    assert recall_at_k(["a", "x"], frozenset({"a", "b", "c"}), 2) == pytest.approx(1 / 3)
    assert recall_at_k(["a", "b", "c"], frozenset({"a", "b", "c"}), 3) == 1.0
    assert recall_at_k(["x", "y"], frozenset({"a"}), 2) == 0.0


def test_recall_truncates_at_k() -> None:
    """第 3 位的相关项在 k=2 下不算命中。"""
    assert recall_at_k(["x", "y", "a"], frozenset({"a"}), 2) == 0.0
    assert recall_at_k(["x", "y", "a"], frozenset({"a"}), 3) == 1.0


def test_ndcg_rewards_earlier_positions() -> None:
    """同样召回,排得靠前得分更高——这正是 recall 看不出来的差异。"""
    relevant = frozenset({"a"})
    first = ndcg_at_k(["a", "x", "y"], relevant, 3)
    third = ndcg_at_k(["x", "y", "a"], relevant, 3)

    assert first == 1.0
    assert third == pytest.approx(1 / math.log2(4))
    assert first > third


def test_ndcg_matches_hand_computed_value() -> None:
    """两个相关项落在第 1、3 位:DCG = 1/log2(2) + 1/log2(4),IDCG = 1/log2(2) + 1/log2(3)。"""
    dcg = 1 / math.log2(2) + 1 / math.log2(4)
    idcg = 1 / math.log2(2) + 1 / math.log2(3)

    assert ndcg_at_k(["a", "x", "b"], frozenset({"a", "b"}), 3) == pytest.approx(dcg / idcg)


def test_ndcg_ideal_is_capped_by_k() -> None:
    """ground truth 比 k 多时,理想排序也只能填满 k 个位置,否则 nDCG 永远到不了 1。"""
    assert ndcg_at_k(["a", "b"], frozenset({"a", "b", "c", "d"}), 2) == 1.0


def test_reciprocal_rank_uses_first_hit() -> None:
    assert reciprocal_rank(["x", "a", "a"], frozenset({"a"})) == pytest.approx(0.5)
    assert reciprocal_rank(["a"], frozenset({"a"})) == 1.0
    assert reciprocal_rank(["x", "y"], frozenset({"a"})) == 0.0


def test_empty_ground_truth_is_rejected_not_scored_as_zero() -> None:
    """无 ground truth 的样例记 0 会静默拉低均值,必须显式报错。"""
    with pytest.raises(ValueError):
        recall_at_k(["a"], frozenset(), 1)
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], frozenset(), 1)


def test_aggregate_skips_unsupported_cases_and_reports_count() -> None:
    metrics = aggregate(
        [
            (["a"], frozenset({"a"})),
            (["x"], frozenset({"b"})),
            (["y"], frozenset()),  # 无 ground truth:跳过而非记 0
        ],
        k=5,
    )

    assert metrics.supported == 2
    assert metrics.recall == pytest.approx(0.5)
    assert metrics.mrr == pytest.approx(0.5)


def test_aggregate_without_any_supported_case_is_zeroed() -> None:
    metrics = aggregate([(["a"], frozenset())], k=5)

    assert metrics == Metrics(recall=0.0, ndcg=0.0, mrr=0.0, supported=0)


def test_gain_is_reported_in_percentage_points() -> None:
    """0.62 → 0.67 记作 +5.0pt(绝对百分点),不是相对提升 8%。"""
    comparison = LayerComparison(
        query_type=MULTI_HOP,
        off=Metrics(recall=0.62, ndcg=0.50, mrr=0.5, supported=10),
        on=Metrics(recall=0.67, ndcg=0.50, mrr=0.5, supported=10),
    )

    assert comparison.recall_gain_points == pytest.approx(5.0)
    assert comparison.meets_gate(DEFAULT_GAIN_GATE_POINTS)


def test_negative_gain_does_not_meet_gate() -> None:
    """图谱在单跳事实检索上常引入噪声而掉点,这种分层必须判为不达标。"""
    comparison = LayerComparison(
        query_type=SINGLE_HOP,
        off=Metrics(recall=0.61, ndcg=0.55, mrr=0.6, supported=10),
        on=Metrics(recall=0.37, ndcg=0.30, mrr=0.4, supported=10),
    )

    assert comparison.recall_gain_points == pytest.approx(-24.0)
    assert not comparison.meets_gate()


def test_layers_are_compared_independently_without_a_grand_mean() -> None:
    """分层是核心:多跳 +10pt 与单跳 -10pt 不得相抵成"无差别"。"""
    off = {
        SINGLE_HOP: [(["a"], frozenset({"a"}))],
        MULTI_HOP: [(["x"], frozenset({"m"}))],
    }
    on = {
        SINGLE_HOP: [(["x"], frozenset({"a"}))],
        MULTI_HOP: [(["m"], frozenset({"m"}))],
    }

    comparisons = compare_layers(off, on, k=5)
    by_type = {item.query_type: item for item in comparisons}

    # 分层顺序按名称固定(字母序),保证多次运行的报告可逐行比对
    assert [item.query_type for item in comparisons] == [MULTI_HOP, SINGLE_HOP]
    assert by_type[SINGLE_HOP].recall_gain_points == pytest.approx(-100.0)
    assert by_type[MULTI_HOP].recall_gain_points == pytest.approx(100.0)
    # 报告里不出现总均值,避免这两个分层相抵后被读成"没有影响"
    assert "总均值" not in format_report(comparisons)


def test_load_cases_parses_and_defaults_query_type() -> None:
    [case] = load_cases([{"query": " 什么是 A ", "relevant": ["a.md#chunk0"]}])

    assert case == EvalCase(
        query="什么是 A", relevant=frozenset({"a.md#chunk0"}), query_type=SINGLE_HOP
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "q", "relevant": ["a"]},  # 不是数组
        [{"query": "", "relevant": ["a"]}],
        [{"query": "q", "relevant": []}],
        [{"query": "q"}],
        ["not-an-object"],
    ],
)
def test_load_cases_rejects_malformed_input(payload) -> None:
    """评测集写错了比没有评测集更危险,不做静默兜底。"""
    with pytest.raises(ValueError):
        load_cases(payload)


def test_report_marks_each_layer_against_the_gate() -> None:
    report = format_report(
        [
            LayerComparison(
                query_type=MULTI_HOP,
                off=Metrics(recall=0.50, ndcg=0.50, mrr=0.5, supported=8),
                on=Metrics(recall=0.62, ndcg=0.60, mrr=0.6, supported=8),
            )
        ]
    )

    assert MULTI_HOP in report
    assert "+12.0pt" in report
    assert "是" in report
