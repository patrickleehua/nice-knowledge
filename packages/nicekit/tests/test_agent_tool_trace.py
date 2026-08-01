"""工具调用痕迹单测(纯函数,构造 ChatMessage 行,无 DB、无 LLM)。

迁移自 TF tests/test_tool_trace.py 的纯函数部分;service/prompts 接线层用例
随 service.py 一起在后续波次搬运。新增:噪音工具与优先参数名的注册入口。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from nicekit.agent.tool_trace import (
    TOOL_TRACE_BUDGET_CHARS,
    TOOL_TRACE_TRUNCATED_NOTE,
    build_tool_trace,
    noise_tools,
    preferred_param_keys,
    register_noise_tools,
    register_preferred_param_keys,
    reset_trace_registrations,
)
from nicekit.models.chat import ChatMessage

ORG_ID = uuid4()
SESSION_ID = uuid4()
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_registrations():
    reset_trace_registrations()
    yield
    reset_trace_registrations()


def tool_row(
    sequence: int,
    name: str,
    tool_input: dict | None = None,
    tool_output: dict | list | None = None,
    *,
    content: str | None = None,
    created_at: datetime | None = NOW,
) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        sequence=sequence,
        role="tool",
        content=content,
        tool_name=name,
        tool_call_id=f"call-{sequence}",
        tool_input=tool_input if tool_input is not None else {},
        tool_output=tool_output,
        created_at=created_at,
    )


def text_row(sequence: int, role: str, content: str) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        sequence=sequence,
        role=role,
        content=content,
        created_at=NOW,
    )


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("- ")]


# ---------- 段首声明与空会话 ----------


def test_empty_session_returns_empty_string() -> None:
    assert build_tool_trace([]) == ""
    assert build_tool_trace([text_row(1, "user", "你好")]) == ""


def test_header_declares_navigation_and_requery() -> None:
    text = build_tool_trace([tool_row(1, "kb_search", {"query": "指南"}, {"items": []})])
    assert text.startswith("【本会话已查过】")
    assert "已经调用过的工具" in text
    assert "重新调用对应工具" in text  # 需要完整结果必须重查,痕迹不是数据源
    assert "不要原样重试" in text


def test_interaction_only_tools_are_not_traced() -> None:
    """plan_update/ask_user 之流不查数据,列进痕迹只会白占预算。"""
    rows = [
        tool_row(1, "plan_update", {"steps": []}, {"steps": []}),
        tool_row(2, "ask_user", {"question": "几个人?"}, {"ok": True}),
        tool_row(3, "update_goal", {"status": "active"}, {"goal": {}}),
    ]
    assert build_tool_trace(rows) == ""


def test_hosts_can_register_extra_noise_tools() -> None:
    rows = [tool_row(1, "host_prompt", {"question": "确认?"}, {"ok": True})]
    assert build_tool_trace(rows, now=NOW) != ""

    register_noise_tools("host_prompt")

    assert "host_prompt" in noise_tools()
    assert build_tool_trace(rows, now=NOW) == ""
    reset_trace_registrations()
    assert "host_prompt" not in noise_tools()


def test_hosts_can_register_extra_preferred_param_keys() -> None:
    rows = [
        tool_row(
            1,
            "host_lookup",
            {"verbose": True, "reference_no": "R-1"},
            {"items": [1]},
        )
    ]
    register_preferred_param_keys("reference_no")

    assert "reference_no" in preferred_param_keys()
    params = _lines(build_tool_trace(rows, now=NOW))[0].split(":", 1)[1]
    assert params.index('reference_no="R-1"') < params.index("verbose=true")


# ---------- 聚合与计数 ----------


def test_aggregates_calls_per_tool_with_count() -> None:
    rows = [
        tool_row(1, "kb_search", {"query": "入门 指南"}, {"items": [1] * 8, "total": 8}),
        tool_row(2, "kb_search", {"query": "进阶 用法"}, {"items": [1] * 12}),
        tool_row(3, "kb_search", {"query": "常见 问题"}, {"items": [1] * 3}),
        tool_row(4, "record_get", {"record_id": "r-1"}, {"record": {"id": "r-1"}}),
    ]
    text = build_tool_trace(rows, now=NOW)
    lines = _lines(text)

    assert len(lines) == 2  # 每个工具一行
    kb_line = next(line for line in lines if "kb_search" in line)
    assert kb_line.startswith("- kb_search ×3")
    assert 'query="入门 指南" → 命中 8 条' in kb_line
    assert 'query="进阶 用法" → 命中 12 条' in kb_line
    # 结果要点只给条数,绝不复制结果正文
    assert "命中 3 条" in kb_line
    record_line = next(line for line in lines if "record_get" in line)
    assert record_line.startswith("- record_get ×1")
    assert "返回 record" in record_line  # 没有列表的对象:退回顶层键名


def test_identical_calls_are_merged_into_one_group() -> None:
    """同工具同参数重复调用只列一次并计数——这正是痕迹要劝阻的行为。"""
    rows = [
        tool_row(i, "record_get", {"record_id": "r-1"}, {"record": {"id": "r-1"}})
        for i in range(1, 4)
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]

    assert line.startswith("- record_get ×3")
    assert line.count('record_id="r-1"') == 1
    assert line.count("命中") + line.count("返回") == 1


def test_last_call_result_wins_within_a_group() -> None:
    rows = [
        tool_row(1, "kb_search", {"query": "指南"}, {"items": [1]}),
        tool_row(2, "kb_search", {"query": "指南"}, {"items": [1] * 9}),
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    assert "命中 9 条" in line and "命中 1 条" not in line


def test_relative_time_of_last_call() -> None:
    rows = [
        tool_row(1, "kb_search", {"query": "a"}, {"items": []}, created_at=NOW - timedelta(days=2)),
        tool_row(
            2, "kb_search", {"query": "b"}, {"items": []}, created_at=NOW - timedelta(minutes=7)
        ),
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    assert "(最近 7 分钟前)" in line

    old = [tool_row(1, "kb_list", {}, {"items": []}, created_at=NOW - timedelta(hours=5))]
    assert "(最近 5 小时前)" in build_tool_trace(old, now=NOW)


# ---------- 失败调用 ----------


def test_failed_call_is_marked_and_listed_first() -> None:
    rows = [
        tool_row(1, "record_get", {"record_id": "r-1"}, {"error": "记录不存在"}),
        tool_row(2, "kb_search", {"query": "指南"}, {"items": [1] * 5}),
    ]
    lines = _lines(build_tool_trace(rows, now=NOW))

    # 失败的工具排在最前:模型先看到"此路不通",不必等读完整段
    assert lines[0].startswith("- record_get")
    assert "失败:记录不存在" in lines[0]
    assert "失败" not in lines[1]


def test_retry_after_failure_keeps_both_signals() -> None:
    """同参数先失败后成功不合并:重试有效是重要导航信号,抹平就丢了。"""
    rows = [
        tool_row(1, "record_get", {"record_id": "r-1"}, {"error": "无权限"}),
        tool_row(2, "record_get", {"record_id": "r-1"}, {"record": {"id": "r-1"}}),
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    assert line.startswith("- record_get ×2")
    assert "失败:无权限" in line
    assert "返回 record" in line


def test_failure_read_from_truncated_content_when_output_missing() -> None:
    # 历史行可能只有模型可见文本(tool_output 为空),痕迹照样要能判成败
    rows = [tool_row(1, "weather_get", {"city": "东京"}, None, content='{"error": "上游超时"}')]
    assert "失败:上游超时" in build_tool_trace(rows, now=NOW)


# ---------- 关键参数提取 ----------


def test_identifier_params_rank_before_other_scalars() -> None:
    rows = [
        tool_row(
            1,
            "detail_get",
            {"verbose": True, "lang": "zh", "detail_id": "d-9", "day": 3},
            {"days": [1, 2, 3]},
        )
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    params = line.split(":", 1)[1]
    # 标识符优先,其余标量按原顺序补位,最多 3 个
    assert params.index('detail_id="d-9"') < params.index("verbose=true")
    assert "day=3" not in params
    assert "命中 3 条" in line


def test_query_and_list_params_are_extracted_generically() -> None:
    rows = [
        tool_row(
            1,
            "kb_search",
            {"kb_ids": ["kb-1", "kb-2", "kb-3", "kb-4"], "query": "流程", "top_k": 5},
            [{"id": 1}, {"id": 2}],
        )
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    assert 'kb_ids=["kb-1","kb-2","kb-3"…]' in line  # 列表只展示前几项
    assert 'query="流程"' in line
    assert "命中 2 条" in line  # 顶层数组直接给条数


def test_empty_and_structural_params_are_skipped() -> None:
    rows = [
        tool_row(1, "kb_list", {"filters": {"a": 1}, "cursor": None, "tag": ""}, {"items": []}),
    ]
    line = _lines(build_tool_trace(rows, now=NOW))[0]
    assert "无参数" in line  # 嵌套结构与空值不进痕迹(它们不是标识信息)


# ---------- 预算 ----------


def test_over_budget_keeps_failed_and_recent_then_marks_omission() -> None:
    rows = [tool_row(1, "t0", {"id": "a"}, {"error": "拒绝访问"})]
    rows += [tool_row(i, f"t{i}", {"id": "a"}, {"items": [1]}) for i in range(1, 10)]

    full = build_tool_trace(rows, 10_000, now=NOW)
    assert len(_lines(full)) == 10  # 预算充足时一个不落

    text = build_tool_trace(rows, 260, now=NOW)
    assert len(text) <= 260
    assert text.endswith(TOOL_TRACE_TRUNCATED_NOTE)
    lines = _lines(text)
    assert lines[0].startswith("- t0")  # 失败优先
    assert any(line.startswith("- t9") for line in lines)  # 其次最近优先
    assert not any(line.startswith(("- t3", "- t4", "- t5")) for line in lines)


def test_budget_smaller_than_header_drops_whole_section() -> None:
    rows = [tool_row(1, "kb_search", {"query": "指南"}, {"items": [1]})]
    assert build_tool_trace(rows, 20, now=NOW) == ""
    # 预算装得下声明却装不下任何一行时,同样不注入"只有声明"的空壳
    assert build_tool_trace(rows, 118, now=NOW) == ""


def test_pathological_input_does_not_blow_the_budget() -> None:
    rows = [
        tool_row(
            1,
            "kb_search",
            {"query": "甲" * 5000, "note": "乙" * 5000, "extra": "丙" * 5000},
            {"items": [{"text": "x" * 5000}] * 3},
        ),
        tool_row(2, "web_search", {"query": "y" * 5000}, {"results": [1] * 2}),
    ]
    text = build_tool_trace(rows, now=NOW)

    assert len(text) <= TOOL_TRACE_BUDGET_CHARS
    assert all(len(line) <= 220 for line in _lines(text))
    assert "甲" * 100 not in text  # 长值一律截断,不整段搬进上下文
    assert "x" * 100 not in text  # 结果正文从不进痕迹


def test_many_param_variants_are_capped_per_tool() -> None:
    rows = [tool_row(i, "kb_search", {"query": f"关键词{i}"}, {"items": [1]}) for i in range(8)]
    line = _lines(build_tool_trace(rows, now=NOW))[0]

    assert line.startswith("- kb_search ×8")
    assert line.count("query=") <= 3  # 扫参数空间的调用不必逐条列全
    assert line.endswith("…")
