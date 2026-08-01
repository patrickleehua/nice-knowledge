"""工具结果双层预算纯函数单测(无 DB、无 LLM):
单条紧凑视图、总量预算替换旧条目保留最近 N、幂等性、不删消息不动 tool_call_id。"""

import json

from nicekit.agent.context_budget import (
    SINGLE_TOOL_OUTPUT_MAX_CHARS,
    TOOL_RESULT_KEEP_RECENT,
    TOOL_RESULT_TOTAL_BUDGET_CHARS,
    apply_tool_result_budget,
    compact_tool_output,
    summarize_output_shape,
)


def _tool_msg(cid: str, content: str, name: str = "kb_search") -> dict:
    return {
        "role": "tool",
        "tool_call_id": cid,
        "name": name,
        "content": content,
        "tool_input": {},
        "tool_output": {"raw": True},
    }


# ---------- 第一层:单条紧凑视图 ----------


def test_small_output_uses_plain_serialization() -> None:
    output = {"items": [1, 2], "总数": 2}
    assert compact_tool_output("kb_search", output, 4000) == json.dumps(
        output, ensure_ascii=False, default=str
    )


def test_oversized_output_becomes_compact_view() -> None:
    output = {
        "items": [{"name": f"条目{i}", "desc": "x" * 200} for i in range(50)],
        "total": 50,
    }
    text = compact_tool_output("web_search", output, 1000)
    payload = json.loads(text)
    assert payload["result_compacted"] is True
    assert payload["tool_name"] == "web_search"
    assert payload["original_char_count"] > 1000
    assert len(payload["content_prefix"]) == 1000
    # 结构概要:顶层键、列表长度、示例首条
    assert payload["shape"]["top_level"] == "object"
    assert "items" in payload["shape"]["keys"]
    assert payload["shape"]["list_lengths"]["items"] == 50
    assert "条目0" in payload["shape"]["first_item"]
    # 审计说明:告诉模型完整结果可查、可重查
    assert "审计" in payload["note"]
    assert "重新查询" in payload["note"]


def test_shape_summary_for_top_level_array() -> None:
    shape = summarize_output_shape([{"id": 1}, {"id": 2}])
    assert shape == {"top_level": "array", "item_count": 2, "first_item": '{"id": 1}'}


def test_default_single_threshold_matches_loop_constant() -> None:
    from nicekit.agent.loop import TOOL_OUTPUT_MAX_CHARS

    assert SINGLE_TOOL_OUTPUT_MAX_CHARS == TOOL_OUTPUT_MAX_CHARS == 4000


# ---------- 第二层:总量预算 ----------


def _conversation(tool_count: int, chunk: int) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "帮我查一下"}]
    for i in range(tool_count):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"c{i}", "name": "web_search", "arguments": {}}],
            }
        )
        messages.append(_tool_msg(f"c{i}", "x" * chunk, name="web_search"))
    messages.append({"role": "assistant", "content": "查询完成"})
    return messages


def test_under_budget_returns_messages_unchanged() -> None:
    messages = _conversation(tool_count=3, chunk=1000)
    assert apply_tool_result_budget(messages, 60000, 5) is messages


def test_over_budget_replaces_oldest_and_keeps_recent() -> None:
    messages = _conversation(tool_count=8, chunk=10000)  # 工具 content 共 80000 字符
    out = apply_tool_result_budget(messages, 60000, 5)
    tool_msgs = [m for m in out if m["role"] == "tool"]
    # 最旧 3 条被替换为占位符,最近 5 条保持完整
    for msg in tool_msgs[:3]:
        payload = json.loads(msg["content"])
        assert payload["tool_result_compacted"] is True
        assert payload["tool_name"] == "web_search"
        assert payload["original_char_count"] == 10000
        assert "shape" in payload
    for msg in tool_msgs[3:]:
        assert msg["content"] == "x" * 10000
    # 不删消息、消息顺序与 tool_call_id 原样保留
    assert len(out) == len(messages)
    assert [m.get("tool_call_id") for m in out] == [
        m.get("tool_call_id") for m in messages
    ]
    assert [m["role"] for m in out] == [m["role"] for m in messages]


def test_budget_does_not_mutate_original_messages() -> None:
    # loop 里同一 dict 同时挂在 messages 与落库路径上,原地改写会污染持久化内容
    messages = _conversation(tool_count=8, chunk=10000)
    apply_tool_result_budget(messages, 60000, 5)
    assert all(
        m["content"] == "x" * 10000 for m in messages if m["role"] == "tool"
    )


def test_budget_is_idempotent_on_placeholders() -> None:
    # 最近 5 条本身仍超预算:第二次调用不得二次包装已有占位符
    messages = _conversation(tool_count=8, chunk=20000)
    once = apply_tool_result_budget(messages, 60000, 5)
    twice = apply_tool_result_budget(once, 60000, 5)
    assert twice is once  # 无新替换发生,原列表原样返回
    payload = json.loads([m for m in twice if m["role"] == "tool"][0]["content"])
    assert payload["original_char_count"] == 20000  # 记录的是原始字符数,非占位符长度


def test_keep_recent_floor_prevents_replacing_everything() -> None:
    # 工具消息条数不超过 keep_recent 时,即使总量超预算也不动(近期结果必须完整)
    messages = _conversation(tool_count=5, chunk=50000)
    assert apply_tool_result_budget(messages, 60000, 5) is messages


def test_default_constants() -> None:
    assert TOOL_RESULT_TOTAL_BUDGET_CHARS == 60000
    assert TOOL_RESULT_KEEP_RECENT == 5
