"""模型内置联网搜索:工具下发构造 + 两家响应解析(不打真实 API)。

内置搜索由 provider 服务端执行,不经过本地工具循环,因此正确性全压在
"请求参数构造"与"响应块解析"这两处纯函数上,单测覆盖它们即可。
"""

from types import SimpleNamespace

from nicekit.llm.providers import (
    anthropic_web_search_tool,
    openai_web_search_tool,
    parse_anthropic_web_searches,
    parse_openai_web_searches,
)


def test_anthropic_tool_carries_version_and_limits() -> None:
    tool = anthropic_web_search_tool(
        {"max_uses": 3, "blocked_domains": ["a.com", "b.com"]}, "web_search_20250305"
    )
    assert tool["type"] == "web_search_20250305"
    assert tool["name"] == "web_search"
    assert tool["max_uses"] == 3
    assert tool["blocked_domains"] == ["a.com", "b.com"]


def test_anthropic_tool_prefers_allowlist_over_blocklist() -> None:
    """两者互斥,同时下发会被上游拒绝,因此 allowed 优先、blocked 让位。"""
    tool = anthropic_web_search_tool(
        {"allowed_domains": ["gov.cn"], "blocked_domains": ["x.com"]},
        "web_search_20250305",
    )
    assert tool["allowed_domains"] == ["gov.cn"]
    assert "blocked_domains" not in tool


def test_anthropic_tool_omits_empty_and_invalid_limits() -> None:
    tool = anthropic_web_search_tool({"max_uses": 0, "blocked_domains": []}, "v")
    assert tool == {"type": "v", "name": "web_search"}


def test_openai_tool_only_supports_allowlist() -> None:
    """Responses 的 filters 没有黑名单——deny 名单在这条路径上落不了地。"""
    assert openai_web_search_tool({"blocked_domains": ["x.com"]}) == {
        "type": "web_search"
    }
    assert openai_web_search_tool({"allowed_domains": ["gov.cn"]}) == {
        "type": "web_search",
        "filters": {"allowed_domains": ["gov.cn"]},
    }


def _anthropic_blocks() -> list:
    return [
        SimpleNamespace(type="text", text="据外交部说明…"),
        SimpleNamespace(
            type="server_tool_use", id="srv_1", name="web_search",
            input={"query": "泰国落地签 材料"},
        ),
        SimpleNamespace(
            type="web_search_tool_result", tool_use_id="srv_1",
            content=[
                SimpleNamespace(
                    type="web_search_result", url="https://www.mfa.gov.cn/a",
                    title="签证须知", page_age="3 days ago",
                ),
                SimpleNamespace(
                    type="web_search_result", url="https://x.example.com/b",
                    title=None, page_age=None,
                ),
            ],
        ),
    ]


def test_parse_anthropic_pairs_query_with_results() -> None:
    searches = parse_anthropic_web_searches(_anthropic_blocks())
    assert len(searches) == 1
    assert searches[0]["query"] == "泰国落地签 材料"
    assert [r["url"] for r in searches[0]["results"]] == [
        "https://www.mfa.gov.cn/a", "https://x.example.com/b",
    ]
    assert searches[0]["results"][0]["page_age"] == "3 days ago"
    # 缺标题时回落 URL,避免前端渲染出空链接
    assert searches[0]["results"][1]["title"] == "https://x.example.com/b"


def test_parse_anthropic_survives_error_result() -> None:
    """搜索失败时 content 是错误对象而非列表,解析不能把整轮回答带崩。"""
    blocks = [
        SimpleNamespace(
            type="server_tool_use", id="srv_1", name="web_search", input={"query": "q"}
        ),
        SimpleNamespace(
            type="web_search_tool_result", tool_use_id="srv_1",
            content=SimpleNamespace(type="web_search_tool_result_error",
                                    error_code="max_uses_exceeded"),
        ),
    ]
    searches = parse_anthropic_web_searches(blocks)
    assert searches == [{"query": "q", "results": []}]


def test_parse_anthropic_ignores_normal_tool_use() -> None:
    """本地工具调用不能被当成内置搜索(否则会重复展示一次来源)。"""
    blocks = [SimpleNamespace(type="tool_use", id="t1", name="kb_search", input={})]
    assert parse_anthropic_web_searches(blocks) == []


def test_parse_openai_merges_annotations() -> None:
    output = [
        SimpleNamespace(type="web_search_call", action=SimpleNamespace(query="泰国签证")),
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    annotations=[
                        SimpleNamespace(
                            type="url_citation", url="https://a.gov/1", title="官方"
                        ),
                        # 同一来源被多处标注,合并时要去重
                        SimpleNamespace(
                            type="url_citation", url="https://a.gov/1", title="官方"
                        ),
                        SimpleNamespace(type="file_citation", url=None, title=None),
                    ]
                )
            ],
        ),
    ]
    searches = parse_openai_web_searches(output)
    assert len(searches) == 1
    assert searches[0]["query"] == "泰国签证"
    assert [r["url"] for r in searches[0]["results"]] == ["https://a.gov/1"]


def test_parse_openai_without_search_returns_empty() -> None:
    output = [SimpleNamespace(type="message", content=[SimpleNamespace(annotations=[])])]
    assert parse_openai_web_searches(output) == []


def test_parse_openai_records_search_without_citations() -> None:
    """搜了但没引用也要留痕,否则用量对不上账。"""
    output = [SimpleNamespace(type="web_search_call", action=None)]
    assert parse_openai_web_searches(output) == [{"query": "", "results": []}]


def test_parse_openai_recovers_sources_from_streamed_annotations() -> None:
    """网关裁掉终值 annotations 时,靠流式注解还原来源。

    现场即为此:面板显示"搜了两次、0 条来源",但模型正文里带着具体出处——
    说明来源在传输中被裁掉了,不是真的没搜到。
    """
    output = [
        SimpleNamespace(type="web_search_call", action=SimpleNamespace(query="南海 最新")),
        # 终值 message 的 annotations 被网关裁空
        SimpleNamespace(type="message", content=[SimpleNamespace(annotations=[])]),
    ]
    streamed = [
        SimpleNamespace(type="url_citation", url="https://apnews.com/a", title="AP"),
        SimpleNamespace(type="url_citation", url="https://apnews.com/a", title="AP"),
        SimpleNamespace(type="url_citation", url="https://reuters.com/b", title="路透"),
    ]
    searches = parse_openai_web_searches(output, streamed)
    assert len(searches) == 1
    assert searches[0]["query"] == "南海 最新"
    # 跨两个来源去重后应有两条
    assert [r["url"] for r in searches[0]["results"]] == [
        "https://apnews.com/a", "https://reuters.com/b",
    ]


def test_parse_openai_merges_final_and_streamed_without_duplicates() -> None:
    output = [
        SimpleNamespace(type="web_search_call", action=SimpleNamespace(query="q")),
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    annotations=[
                        SimpleNamespace(
                            type="url_citation", url="https://a.gov/1", title="官方"
                        )
                    ]
                )
            ],
        ),
    ]
    streamed = [
        SimpleNamespace(type="url_citation", url="https://a.gov/1", title="官方"),
        SimpleNamespace(type="url_citation", url="https://b.gov/2", title="另一处"),
    ]
    searches = parse_openai_web_searches(output, streamed)
    assert [r["url"] for r in searches[0]["results"]] == [
        "https://a.gov/1", "https://b.gov/2",
    ]


def test_parse_openai_streamed_annotations_optional() -> None:
    """不传流式注解时行为不变(非流式路径)。"""
    output = [SimpleNamespace(type="web_search_call", action=None)]
    assert parse_openai_web_searches(output) == [{"query": "", "results": []}]


def test_parse_openai_recovers_sources_from_streamed_items() -> None:
    """终值 output 非空但 annotations 被裁空时,靠流式 item 里的那份还原。

    这是与 cherry-studio 的差别所在:它读 AI SDK 的 source part(等价于把多条
    通道都收下),而我们此前只认终值 output —— 同一个网关同一个模型,它有引用、
    我们 0 条。取并集而不是二选一才对。
    """
    call = SimpleNamespace(
        type="web_search_call", id="ws_1", action=SimpleNamespace(query="南海 最新")
    )
    output = [call, SimpleNamespace(type="message", id="m1", content=[
        SimpleNamespace(annotations=[])  # 终值被裁空
    ])]
    streamed = [call, SimpleNamespace(type="message", id="m1", content=[
        SimpleNamespace(annotations=[
            SimpleNamespace(type="url_citation", url="https://apnews.com/a", title="AP")
        ])
    ])]
    searches = parse_openai_web_searches(output, None, streamed)
    assert len(searches) == 1
    # 同一次调用出现在两个列表里,query 不能记两遍
    assert searches[0]["query"] == "南海 最新"
    assert [r["url"] for r in searches[0]["results"]] == ["https://apnews.com/a"]


def test_parse_openai_dedups_calls_without_id() -> None:
    """网关不给 item id 时也不能把同一次搜索算成两次。"""
    output = [SimpleNamespace(type="web_search_call", action=SimpleNamespace(query="q"))]
    searches = parse_openai_web_searches(output, None, [])
    assert searches[0]["query"] == "q"
