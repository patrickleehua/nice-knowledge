"""联网搜索单测(无库):payload 解析 + 故障转移 + 结果规整 + 多查询治理。

record_usage 走 stub session(execute 记录不落库),外部 provider 用假实现——
不打真实搜索接口。缓存默认关掉(use_cache=False),避免用例结果取决于本机
有没有起 redis;缓存行为由专门的用例 monkeypatch 掉 cache 模块来验证。
"""

from uuid import uuid4

import pytest

import nicekit.agent.builtin_tools  # noqa: F401  (触发内置工具注册)
from nicekit.agent.tools import CitationCounter, default_registry
from nicekit.capabilities.websearch import fetch as websearch_fetch
from nicekit.capabilities.websearch import service as websearch_service
from nicekit.capabilities.websearch.base import (
    FetchedPage,
    SearchHit,
    SearchProvider,
    SearchQuery,
    SearchResult,
)
from nicekit.capabilities.websearch.bocha import parse_bocha_payload
from nicekit.capabilities.websearch.service import (
    _normalize,
    _normalize_published,
    _split_rules,
    builtin_search_config,
    domains_for_builtin,
    get_providers,
    normalize_queries,
    normalize_query,
    run_fetch,
    run_multi_search,
    run_search,
    websearch_readiness,
)
from nicekit.capabilities.websearch.tavily import domain_of, parse_tavily_payload


class StubSession:
    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, *_args, **_kwargs) -> None:
        self.executed += 1


class FakeProvider(SearchProvider):
    def __init__(self, name: str, result: SearchResult) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    async def search(self, query: SearchQuery) -> SearchResult:
        self.calls += 1
        return self._result


def _hit(url: str, title: str = "t") -> SearchHit:
    return SearchHit(title=title, url=url)


def test_parse_tavily_payload_skips_broken_entries() -> None:
    payload = {
        "results": [
            {"title": "东京塔开放时间", "url": "https://example.jp/a",
             "content": "9:00-23:00", "score": 0.91, "published_date": "2026-07-01"},
            {"title": "缺 url 跳过"},
            "not-a-dict",
        ]
    }
    hits = parse_tavily_payload(payload)
    assert len(hits) == 1
    assert hits[0].domain == "example.jp"
    assert hits[0].score == 0.91


def test_parse_bocha_payload_prefers_summary() -> None:
    payload = {
        "data": {"webPages": {"value": [
            {"name": "签证新政", "url": "https://news.cn/v",
             "snippet": "短摘要", "summary": "长摘要", "datePublished": "2026-07-10"},
            {"name": "缺 url 跳过"},
        ]}}
    }
    hits = parse_bocha_payload(payload)
    assert len(hits) == 1
    assert hits[0].snippet == "长摘要"
    assert hits[0].published_at == "2026-07-10"


def test_domain_of_strips_www() -> None:
    assert domain_of("https://www.example.com/path?q=1") == "example.com"
    assert domain_of("not a url") == ""


def test_normalize_query_clamps_and_filters() -> None:
    q = normalize_query("  东京 天气  ", 99, "hour")
    assert q.query == "东京 天气"
    assert q.max_results == 10  # cap
    assert q.freshness is None  # 非法值丢弃
    assert normalize_query("x", None, "week").freshness == "week"


def test_normalize_dedups_and_truncates() -> None:
    result = SearchResult(
        provider="tavily", status="ok",
        hits=[
            SearchHit(title="a" * 500, url="https://a.com/", snippet="b" * 2000),
            _hit("https://a.com"),  # 尾斜杠归一后重复
            _hit("https://b.com"),
            _hit("https://c.com"),
        ],
    )
    out = _normalize(result, 2)
    assert [h.url.rstrip("/") for h in out.hits] == ["https://a.com", "https://b.com"]
    assert len(out.hits[0].title) == 200
    assert len(out.hits[0].snippet) == 600


async def test_run_search_failover_and_metering() -> None:
    session = StubSession()
    bad = FakeProvider("tavily", SearchResult(provider="tavily", status="unavailable", error="429"))
    good = FakeProvider("bocha", SearchResult(provider="bocha", status="ok", hits=[_hit("https://x.com")]))
    result = await run_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        query=SearchQuery(query="q", max_results=5), providers=[bad, good],
        use_cache=False,
    )
    assert result.provider == "bocha"
    assert result.status == "ok"
    assert bad.calls == 1 and good.calls == 1
    assert session.executed == 2  # 含失败调用也计量


async def test_run_search_all_failed_returns_unavailable() -> None:
    session = StubSession()
    bad = FakeProvider("tavily", SearchResult(provider="tavily", status="unavailable", error="x"))
    result = await run_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        query=SearchQuery(query="q"), providers=[bad], use_cache=False,
    )
    assert result.status == "unavailable"


async def test_run_search_without_providers_says_unconfigured() -> None:
    result = await run_search(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        query=SearchQuery(query="q"), providers=[],
    )
    assert result.status == "unavailable"
    assert "未配置" in (result.error or "")


# ---- 多检索式并发与来源治理 ----


def test_normalize_queries_dedups_and_caps() -> None:
    queries = normalize_queries(
        ["泰国签证", "  泰国签证  ", "TAIGUO", "", None, "越南签证", "日本签证", "韩国签证"],
        None, None,
    )
    # 大小写/空白归一后去重,并截到 websearch_max_queries(默认 3)
    assert [q.query for q in queries] == ["泰国签证", "TAIGUO", "越南签证"]


def test_normalize_queries_accepts_single_string() -> None:
    assert [q.query for q in normalize_queries("东京 天气", None, None)] == ["东京 天气"]


def test_normalize_queries_empty_input() -> None:
    assert normalize_queries(None, None, None) == []
    assert normalize_queries(["  ", ""], None, None) == []


async def test_run_multi_search_merges_dedups_and_assigns_refs() -> None:
    session = StubSession()
    provider = FakeProvider(
        "tavily",
        SearchResult(
            provider="tavily", status="ok",
            hits=[_hit("https://www.gov.cn/policy"), _hit("https://www.xiaohongshu.com/n")],
        ),
    )
    result = await run_multi_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["泰国签证", "泰国入境"], None, None),
        providers=[provider], overrides={}, use_cache=False,
    )
    assert result.status == "ok"
    assert provider.calls == 2  # 两条检索式各打一次
    # 两条 query 返回同样的 URL,合并后按 URL 去重
    assert len(result.hits) == 2
    assert [h.ref for h in result.hits] == [1, 2]
    assert result.queries == ["泰国签证", "泰国入境"]


async def test_run_multi_search_ranks_official_above_ugc() -> None:
    """政策类问题必须官方优先——这是本次改造的核心业务诉求,退化即回归。"""
    session = StubSession()
    provider = FakeProvider(
        "tavily",
        SearchResult(
            provider="tavily", status="ok",
            hits=[
                _hit("https://www.xiaohongshu.com/note/1", "小红书攻略"),
                _hit("https://zhuanlan.zhihu.com/p/2", "知乎回答"),
                _hit("https://www.mfa.gov.cn/visa", "外交部签证须知"),
            ],
        ),
    )
    result = await run_multi_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["泰国签证政策"], None, None),
        providers=[provider], overrides={}, use_cache=False,
    )
    assert result.hits[0].url == "https://www.mfa.gov.cn/visa"
    assert result.hits[0].ref == 1
    assert result.hits[0].source_tier == "official"
    assert {h.source_tier for h in result.hits[1:]} == {"ugc"}


async def test_run_multi_search_deny_list_filters_hits() -> None:
    session = StubSession()
    provider = FakeProvider(
        "tavily",
        SearchResult(
            provider="tavily", status="ok",
            hits=[_hit("https://www.xiaohongshu.com/note/1"), _hit("https://www.gov.cn/a")],
        ),
    )
    result = await run_multi_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["签证"], None, None),
        providers=[provider],
        # 管理端表单存的是换行分隔的字符串,service 负责还原成列表
        overrides={"deny_domains": "*://*.xiaohongshu.com/*"}, use_cache=False,
    )
    assert [h.url for h in result.hits] == ["https://www.gov.cn/a"]


async def test_run_multi_search_partial_failure_still_succeeds() -> None:
    """一条 query 挂掉不该让整轮白跑——与"单条内 provider 链全败才失败"是两个层次。"""
    session = StubSession()
    calls = {"n": 0}

    class FlakyProvider(SearchProvider):
        name = "tavily"

        async def search(self, query: SearchQuery) -> SearchResult:
            calls["n"] += 1
            if query.query == "坏":
                return SearchResult(provider="tavily", status="unavailable", error="429")
            return SearchResult(
                provider="tavily", status="ok", hits=[_hit("https://ok.example.com/a")]
            )

    result = await run_multi_search(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["好", "坏"], None, None),
        providers=[FlakyProvider()], overrides={}, use_cache=False,
    )
    assert result.status == "ok"
    assert len(result.hits) == 1
    assert "429" in (result.error or "")  # 失败原因如实透传,不静默吞掉


async def test_run_multi_search_all_failed_returns_unavailable() -> None:
    bad = FakeProvider("tavily", SearchResult(provider="tavily", status="unavailable", error="x"))
    result = await run_multi_search(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["a", "b"], None, None),
        providers=[bad], overrides={}, use_cache=False,
    )
    assert result.status == "unavailable"


# ---- 缓存 ----


async def test_run_search_cache_hit_skips_provider_and_metering(monkeypatch) -> None:
    """命中缓存不计 usage:外部搜索按请求计费,复用不该重复扣。"""
    store: dict[str, dict] = {}

    async def fake_get(key: str):
        return store.get(key)

    async def fake_set(key: str, value, ttl: int) -> None:
        store[key] = value

    monkeypatch.setattr(websearch_service.cache, "get_json", fake_get)
    monkeypatch.setattr(websearch_service.cache, "set_json", fake_set)

    provider = FakeProvider(
        "tavily", SearchResult(provider="tavily", status="ok", hits=[_hit("https://x.com")])
    )
    session = StubSession()
    org = uuid4()
    query = SearchQuery(query="缓存测试", max_results=5)

    first = await run_search(session, org_id=org, query=query, providers=[provider])  # type: ignore[arg-type]
    assert first.cached is False
    assert provider.calls == 1 and session.executed == 1

    second = await run_search(session, org_id=org, query=query, providers=[provider])  # type: ignore[arg-type]
    assert second.cached is True
    assert provider.calls == 1  # 没有再打外部接口
    assert session.executed == 1  # 也没有再计量


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a.com\nb.com", ["a.com", "b.com"]),
        ("a.com, b.com，c.com", ["a.com", "b.com", "c.com"]),
        (["a.com", " ", "b.com"], ["a.com", "b.com"]),
        (None, None),
        (123, None),
    ],
)
def test_split_rules_handles_form_input(raw, expected) -> None:
    assert _split_rules(raw) == expected


# ---- 正文抓取编排(run_fetch) ----


def _page(url: str, content: str = "正文", status: str = "ok") -> FetchedPage:
    return FetchedPage(url=url, final_url=url, title="t", content=content, status=status)


async def test_run_fetch_blocks_denied_domain_before_requesting(monkeypatch) -> None:
    """被拉黑的站点连请求都不该发出去:既省流量,也不给对方留访问日志。"""
    requested: list[list[str]] = []

    async def fake_fetch_pages(urls, **_kwargs):
        requested.append(list(urls))
        return [_page(u) for u in urls]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    pages = await run_fetch(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        urls=["https://www.xiaohongshu.com/note/1", "https://www.gov.cn/a"],
        overrides={"deny_domains": "*://*.xiaohongshu.com/*"},
    )
    assert requested == [["https://www.gov.cn/a"]]  # 黑名单 URL 未进入抓取
    blocked = [p for p in pages if p.status == "blocked"]
    assert len(blocked) == 1
    assert "屏蔽" in (blocked[0].error or "")


async def test_run_fetch_tags_tier_by_final_url(monkeypatch) -> None:
    """分级按重定向后的落地域名判定,否则短链会被当成 unknown。"""

    async def fake_fetch_pages(urls, **_kwargs):
        return [
            FetchedPage(
                url="https://t.cn/abc", final_url="https://www.mfa.gov.cn/visa",
                title="签证须知", content="正文", status="ok",
            )
        ]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    pages = await run_fetch(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        urls=["https://t.cn/abc"], overrides={},
    )
    assert pages[0].source_tier == "official"
    assert pages[0].ref == 1


async def test_run_fetch_meters_and_assigns_refs(monkeypatch) -> None:
    async def fake_fetch_pages(urls, **_kwargs):
        return [_page(u) for u in urls]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    session = StubSession()
    pages = await run_fetch(
        session, org_id=uuid4(),  # type: ignore[arg-type]
        urls=["https://a.example.com/1", "https://b.example.com/2"], overrides={},
    )
    assert [p.ref for p in pages] == [1, 2]
    assert session.executed == 1  # 一次批量抓取记一条 usage(calls=页数)


async def test_run_fetch_respects_page_limit(monkeypatch) -> None:
    captured: list[list[str]] = []

    async def fake_fetch_pages(urls, **_kwargs):
        captured.append(list(urls))
        return [_page(u) for u in urls]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    many = [f"https://e{i}.example.com/x" for i in range(20)]
    pages = await run_fetch(
        StubSession(), org_id=uuid4(), urls=many, overrides={}  # type: ignore[arg-type]
    )
    # 上限来自 websearch_fetch_max_pages(默认 5),防模型一次拽几十页把上下文撑爆
    assert len(captured[0]) == 5
    # 超限部分必须如实回报,否则模型会把"只读了前 5 页"的结论当完整答案
    overflow = [p for p in pages if p.status == "blocked" and "超出单次抓取上限" in (p.error or "")]
    assert len(overflow) == 1
    assert "15 个链接" in (overflow[0].error or "")


async def test_run_fetch_compresses_long_content(monkeypatch) -> None:
    async def fake_fetch_pages(urls, **_kwargs):
        return [_page(u, content="长" * 9000) for u in urls]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    pages = await run_fetch(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        urls=["https://a.example.com/1", "https://b.example.com/2"],
        overrides={"compression_method": "cutoff", "compression_total_chars": 1000},
    )
    total = sum(len(p.content) for p in pages)
    assert total < 2000  # 预算内(含截断提示的少量富余)
    assert all(p.truncated for p in pages)


# ---- 引用编号在一次 agent run 内累加 ----


def test_citation_counter_take_returns_offsets() -> None:
    counter = CitationCounter()
    assert counter.take(3) == 0  # 第一批:ref 1..3
    assert counter.take(2) == 3  # 第二批:ref 4..5
    assert counter.take(0) == 5  # 空批不推进
    assert counter.value == 5


class _CommitSession(StubSession):
    async def commit(self) -> None:  # 工具内会 commit 计量
        return None


def _tool_ctx(counter: CitationCounter):
    from nicekit.agent.tools import ToolContext

    return ToolContext(
        session=_CommitSession(),  # type: ignore[arg-type]
        org_id=uuid4(),
        user_id=uuid4(),
        role=None,  # type: ignore[arg-type]
        chat_session=None,  # type: ignore[arg-type]
        citations=counter,
    )


async def test_web_search_ref_continues_across_calls_in_one_run(monkeypatch) -> None:
    """同一轮里搜两次,第二次的 ref 必须接着往后编。

    否则模型两次都看到 [1],引用指代不明;前端按 ref 生成的锚点也会撞车。
    """
    async def fake_multi(_session, **_kwargs):
        return SearchResult(
            provider="tavily", status="ok",
            hits=[_hit("https://a.example.com/1"), _hit("https://b.example.com/2")],
            queries=["q"],
        )

    monkeypatch.setattr(websearch_service, "run_multi_search", fake_multi)

    counter = CitationCounter()
    ctx = _tool_ctx(counter)
    args = {"query": "q", "queries": None, "max_results": None, "freshness": None}

    first = await default_registry.require("web_search").executor(ctx, args)
    second = await default_registry.require("web_search").executor(ctx, args)

    assert [r["ref"] for r in first["results"]] == [1, 2]
    assert [r["ref"] for r in second["results"]] == [3, 4]


async def test_web_fetch_ref_continues_after_search(monkeypatch) -> None:
    async def fake_multi(_session, **_kwargs):
        return SearchResult(
            provider="tavily", status="ok", hits=[_hit("https://a.example.com/1")], queries=["q"]
        )

    async def fake_fetch(_session, **_kwargs):
        return [_page("https://a.example.com/1"), _page("https://b.example.com/2")]

    monkeypatch.setattr(websearch_service, "run_multi_search", fake_multi)
    monkeypatch.setattr(websearch_service, "run_fetch", fake_fetch)

    ctx = _tool_ctx(CitationCounter())
    await default_registry.require("web_search").executor(
        ctx, {"query": "q", "queries": None, "max_results": None, "freshness": None}
    )
    fetched = await default_registry.require("web_fetch").executor(
        ctx, {"urls": ["https://a.example.com/1", "https://b.example.com/2"], "query": None}
    )
    # 搜索占了 ref 1,读取从 2 续编——两个工具共用同一个游标
    assert [p["ref"] for p in fetched["pages"]] == [2, 3]


# ---- 发布时间归一化 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-10", "2026-07-10"),
        ("2026-07-10T08:30:00Z", "2026-07-10"),  # 博查 ISO 8601
        ("Mon, 01 Jul 2026 00:00:00 GMT", "2026-07-01"),  # Tavily 偶发 RFC 1123
        ("三天前", None),  # 解析不了宁可不显示,也不能让前端渲染出 NaN
        ("", None),
        (None, None),
    ],
)
def test_normalize_published_handles_provider_formats(raw, expected) -> None:
    assert _normalize_published(raw) == expected


async def test_run_multi_search_normalizes_published_at() -> None:
    provider = FakeProvider(
        "bocha",
        SearchResult(
            provider="bocha", status="ok",
            hits=[
                SearchHit(
                    title="签证新政", url="https://www.gov.cn/a",
                    published_at="2026-07-10T08:30:00Z",
                )
            ],
        ),
    )
    result = await run_multi_search(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["签证"], None, None),
        providers=[provider], overrides={}, use_cache=False,
    )
    assert result.hits[0].published_at == "2026-07-10"


async def test_run_fetch_numbers_successful_pages_first(monkeypatch) -> None:
    """ref 1 应该是可引用的来源,而不是一条被域名策略拦下的记录。"""

    async def fake_fetch_pages(urls, **_kwargs):
        return [_page(u) for u in urls]

    monkeypatch.setattr(websearch_fetch, "fetch_pages", fake_fetch_pages)

    pages = await run_fetch(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        urls=["https://www.xiaohongshu.com/note/1", "https://www.gov.cn/a"],
        overrides={"deny_domains": "*://*.xiaohongshu.com/*"},
    )
    assert pages[0].status == "ok" and pages[0].ref == 1
    assert pages[1].status == "blocked" and pages[1].ref == 2


# ---- 会话级联网开关与就绪判定 ----


def test_websearch_readiness_marks_unconfigured_providers() -> None:
    readiness = websearch_readiness({"provider": "auto", "tavily_api_key": "k"})
    assert readiness.ready is True
    assert readiness.default_provider == "tavily"
    by_value = {o.value: o for o in readiness.options}
    assert by_value["off"].ready is True  # 关闭永远可选
    assert by_value["tavily"].ready is True
    assert by_value["bocha"].ready is False  # 没配 key 的置灰而不是消失
    assert "未配置" in by_value["bocha"].hint


def test_websearch_readiness_without_any_key() -> None:
    readiness = websearch_readiness({"provider": "auto"})
    assert readiness.ready is False
    assert readiness.default_provider is None
    # None = 跟随管理端配置,此时无源可用
    assert readiness.allows(None) is False
    assert readiness.allows("off") is True
    assert readiness.allows("tavily") is False


def test_websearch_readiness_respects_pinned_provider() -> None:
    # 管理端指定了 bocha,即使 tavily 也有 key,可用集合里也只有 bocha
    readiness = websearch_readiness(
        {"provider": "bocha", "tavily_api_key": "k1", "bocha_api_key": "k2"}
    )
    by_value = {o.value: o for o in readiness.options}
    assert by_value["bocha"].ready is True
    assert by_value["tavily"].ready is False


def test_get_providers_pins_single_source() -> None:
    both = {"tavily_api_key": "k1", "bocha_api_key": "k2"}
    assert [p.name for p in get_providers({**both, "provider": "auto"})] == [
        "tavily", "bocha",
    ]
    assert [p.name for p in get_providers({**both, "provider": "bocha"})] == ["bocha"]


async def test_run_multi_search_provider_override_pins_chain(monkeypatch) -> None:
    """会话选定某家时压过管理端 auto——否则用户的选择等于没选。

    这里拦住 get_providers 断言它收到的配置,而不是真去构造 provider——
    否则用例会打到真实搜索接口上。
    """
    seen: dict = {}

    def fake_get_providers(overrides=None):
        seen.update(overrides or {})
        return [FakeProvider("bocha", SearchResult(provider="bocha", status="empty"))]

    monkeypatch.setattr(websearch_service, "get_providers", fake_get_providers)

    await run_multi_search(
        StubSession(), org_id=uuid4(),  # type: ignore[arg-type]
        queries=normalize_queries(["签证"], None, None),
        overrides={"tavily_api_key": "k1", "bocha_api_key": "k2", "provider": "auto"},
        provider_override="bocha",
        use_cache=False,
    )
    assert seen["provider"] == "bocha"  # 会话选择压过管理端的 auto


# ---- 工具门控:关闭/未配置时不向模型暴露联网工具 ----
#
# 该段(TF tests/test_websearch_unit.py 的 _snapshot 系列)依赖
# agent/service.py 的 _build_tool_snapshot,service 层随 P2c 交付,
# 届时把这 5 个用例一并搬回来。


def test_domains_for_builtin_extracts_plain_domains() -> None:
    """内置搜索只认域名:match pattern 与正则要降级成纯域名,抠不出的丢弃。"""
    got = domains_for_builtin(
        {"deny_domains": "*://*.xiaohongshu.com/*\n" + r"/zhihu\.com/" + "\n/*"}
    )
    assert got == ["xiaohongshu.com", "zhihu.com"]


def test_builtin_search_config_carries_blocklist() -> None:
    config = builtin_search_config({"deny_domains": "*://*.example.com/*"})
    assert config["blocked_domains"] == ["example.com"]
    assert config["max_uses"] >= 1
    assert config["tool_version"].startswith("web_search_")
