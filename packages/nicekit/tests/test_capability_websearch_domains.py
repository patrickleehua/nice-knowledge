"""域名策略与来源分级单测(无库无网):match pattern 匹配 + 分级 + 重排 + 配置回落。

重点验证两件事:一是任何非法输入(URL / 规则 / 管理端配置)都只降级不抛异常;
二是政策类问题下官方来源确实能压过 UGC(小红书 vs 大使馆官网的合规诉求)。
"""

import pytest

from nicekit.capabilities.websearch.base import SearchHit
from nicekit.capabilities.websearch.domains import (
    DEFAULT_POLICY,
    DomainPolicy,
    MatchPatternMap,
    apply_policy,
    classify_url,
    is_denied,
    is_policy_query,
    load_policy,
    parse_match_pattern,
)


def _hit(url: str, title: str = "t") -> SearchHit:
    return SearchHit(title=title, url=url)


def _map(*rules: str) -> MatchPatternMap:
    m = MatchPatternMap()
    for index, rule in enumerate(rules):
        m.set(rule, index)
    return m


# --------------------------------------------------------------------------
# match pattern 解析与匹配
# --------------------------------------------------------------------------


def test_all_urls_matches_any_http_page() -> None:
    m = _map("<all_urls>")
    assert m.get("https://whatever.example/a/b?c=1") == [0]
    assert m.get("http://whatever.example/") == [0]
    # 有意收窄:非 http(s) 的 URL 不参与域名治理
    assert m.get("ftp://whatever.example/") == []


def test_subdomain_pattern_covers_self_and_children() -> None:
    m = _map("*://*.example.com/*")
    assert m.get("https://example.com/") == [0]
    assert m.get("https://www.example.com/x") == [0]
    assert m.get("https://a.b.example.com/x") == [0]
    # 相邻但不同的域名不能被误吃
    assert m.get("https://notexample.com/x") == []
    assert m.get("https://example.com.cn/x") == []


def test_exact_host_pattern_does_not_match_subdomain() -> None:
    m = _map("*://example.com/*")
    assert m.get("https://example.com/x") == [0]
    assert m.get("https://www.example.com/x") == []


def test_path_wildcard() -> None:
    m = _map("https://news.example/section/*/detail")
    assert m.get("https://news.example/section/travel/detail") == [0]
    assert m.get("https://news.example/section/a/b/detail") == [0]
    assert m.get("https://news.example/section/travel/other") == []
    # 缺省路径按 /* 处理:管理端手填常漏尾部
    assert _map("https://news.example").get("https://news.example/any/where") == [0]


def test_scheme_is_respected() -> None:
    https_only = _map("https://secure.example/*")
    assert https_only.get("https://secure.example/x") == [0]
    assert https_only.get("http://secure.example/x") == []
    any_scheme = _map("*://secure.example/*")
    assert any_scheme.get("http://secure.example/x") == [0]


def test_any_host_pattern() -> None:
    m = _map("*://*/promo/*")
    assert m.get("https://a.example/promo/1") == [0]
    assert m.get("https://a.example/news/1") == []


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "   ",
        "example.com/*",  # 缺 scheme 分隔符
        "://example.com/*",  # 空 scheme
        "*://",  # 空主机
        "*://*.exa*mple.com/*",  # 不支持中缀通配
        "*://.example.com/*",  # 空标签
        "*://a..b.com/*",
        "<all_url>",  # 拼错的关键字
    ],
)
def test_invalid_patterns_return_none_without_raising(pattern: str) -> None:
    assert parse_match_pattern(pattern) is None
    assert MatchPatternMap().set(pattern, "v") is False


def test_regex_rule_matches_hostname_only() -> None:
    m = MatchPatternMap()
    assert m.set(r"/zhihu\.com/", "ugc") is True
    assert m.get("https://www.zhihu.com/question/1") == ["ugc"]
    assert m.get("https://zhuanlan.zhihu.com/p/1") == ["ugc"]
    # 只对 hostname 生效:路径里出现同名字符串不算
    assert m.get("https://example.org/go?to=zhihu.com") == []


def test_broken_regex_rule_is_swallowed() -> None:
    m = MatchPatternMap()
    assert m.set("/([unclosed/", "x") is False
    assert m.set("//", "x") is False
    assert len(m) == 0
    assert m.get("https://a.example/") == []


def test_malformed_url_never_raises() -> None:
    m = _map("<all_urls>")
    for url in ["", "not a url", "javascript:alert(1)", "http://[::1", "http://"]:
        assert m.get(url) == []
    # 端口写坏了仍按主机名处理:能降级就别整条丢弃
    assert m.get("https://a.example:port/x") == [0]


# --------------------------------------------------------------------------
# 分级
# --------------------------------------------------------------------------


def test_classify_url_covers_three_tiers_and_unknown() -> None:
    assert classify_url("https://www.mfa.gov.cn/web/index.html") == "official"
    assert classify_url("https://www.bbc.com/news/world-1") == "media"
    assert classify_url("https://www.xiaohongshu.com/explore/abc") == "ugc"
    assert classify_url("https://some-random-blog.org/post/1") == "unknown"


def test_classify_url_handles_deep_subdomain() -> None:
    # 驻外使领馆都是 *.mfa.gov.cn 的多级子域,必须归官方
    assert classify_url("https://bjhtb.mfa.gov.cn/zygy/detail.html") == "official"
    assert classify_url("https://zhuanlan.zhihu.com/p/123456") == "ugc"


def test_classify_url_exact_rule_does_not_leak_to_parent_domain() -> None:
    assert classify_url("https://tieba.baidu.com/p/1") == "ugc"
    assert classify_url("https://www.baidu.com/s?wd=x") == "unknown"


def test_classify_url_host_suffix_wildcard() -> None:
    # *.immigration.gov* 覆盖各国后缀,但不能吃掉 immigration.government.org
    assert classify_url("https://immigration.gov.tw/news") == "official"
    assert classify_url("https://immigration.government.org/news") == "unknown"


def test_classify_url_invalid_input_returns_unknown() -> None:
    assert classify_url("not a url") == "unknown"
    assert classify_url("") == "unknown"


def test_is_denied_default_policy_blocks_nothing() -> None:
    assert is_denied("https://www.xiaohongshu.com/explore/abc") is False


# --------------------------------------------------------------------------
# 政策类问题识别
# --------------------------------------------------------------------------


def test_is_policy_query_positive_cn_and_en() -> None:
    # 内置词表只覆盖通用政务/合规语义,不含任何行业专有词
    assert is_policy_query("数据出境的最新监管规定") is True
    assert is_policy_query("这个行业有什么资质要求") is True
    assert is_policy_query("GDPR compliance checklist") is True
    assert is_policy_query("new data protection regulation") is True


def test_is_policy_query_negative() -> None:
    assert is_policy_query("附近好吃的拉面") is False
    assert is_policy_query("") is False
    # 词边界:policyholder 里含 policy 子串,不能误判成政策问题
    assert is_policy_query("policyholder contact information") is False


def test_policy_keywords_are_host_configurable() -> None:
    """行业专有触发词由宿主经 load_policy 追加,SDK 不内置任何领域词表。"""
    assert is_policy_query("签证怎么办") is False  # 内置词表命中不了

    appended = load_policy({"policy_keywords": {"cn": ["签证"], "en": ["visa"]}})
    assert is_policy_query("签证怎么办", appended) is True
    assert is_policy_query("visa requirements", appended) is True
    assert is_policy_query("最新监管规定", appended) is True  # 追加模式保留内置词

    replaced = load_policy(
        {"policy_keywords": {"cn": ["配方"]}, "policy_keywords_mode": "replace"}
    )
    assert is_policy_query("配方标准变更", replaced) is True
    assert is_policy_query("最新监管规定", replaced) is False  # 替换模式丢弃内置词


# --------------------------------------------------------------------------
# apply_policy
# --------------------------------------------------------------------------


def _mixed_hits() -> list[SearchHit]:
    """真实场景:UGC 霸占前排,官方口径被 provider 排到第 5。"""
    return [
        _hit("https://www.xiaohongshu.com/explore/1"),
        _hit("https://www.zhihu.com/question/2"),
        _hit("https://www.mafengwo.cn/i/3"),
        _hit("https://weibo.com/4"),
        _hit("https://www.mfa.gov.cn/visa/5"),
    ]


def test_apply_policy_filters_denied_hits() -> None:
    policy = DomainPolicy(deny=("*://*.spam.example/*", r"/malware\.test/"))
    hits = [
        _hit("https://a.spam.example/1"),
        _hit("https://ok.example/2"),
        _hit("https://www.malware.test/3"),
    ]
    out = apply_policy(hits, policy=policy)
    assert [h.url for h in out] == ["https://ok.example/2"]


def test_apply_policy_lifts_official_above_ugc() -> None:
    out = apply_policy(_mixed_hits(), query="泰国签证政策")
    assert out[0].url == "https://www.mfa.gov.cn/visa/5"
    assert out[0].source_tier == "official"
    assert out[1].source_tier == "ugc"
    assert len(out) == 5  # 只重排不丢条目


def test_apply_policy_policy_query_widens_official_advantage() -> None:
    hits = _mixed_hits()
    plain = {h.url: h.rank_score for h in apply_policy(hits, query="泰国 美食 推荐")}
    policy_mode = {h.url: h.rank_score for h in apply_policy(hits, query="泰国签证政策")}
    official = "https://www.mfa.gov.cn/visa/5"
    ugc = "https://www.xiaohongshu.com/explore/1"
    assert plain[official] / plain[ugc] < policy_mode[official] / policy_mode[ugc]


def test_apply_policy_without_policy_query_applies_base_weights_only() -> None:
    out = apply_policy(_mixed_hits(), query="泰国 美食 推荐")
    scores = {h.url: h.rank_score for h in out}
    # 位次倒数 * tier 权重,不含政策加权
    assert scores["https://www.xiaohongshu.com/explore/1"] == pytest.approx(1.0 * 0.7)
    assert scores["https://www.mfa.gov.cn/visa/5"] == pytest.approx(0.2 * 1.6)
    assert out[0].url == "https://www.xiaohongshu.com/explore/1"


def test_apply_policy_uses_position_not_provider_score() -> None:
    # provider 给的 score 完全反向,也不该影响结果次序
    hits = [
        SearchHit(title="a", url="https://a.example/1", score=0.01),
        SearchHit(title="b", url="https://b.example/2", score=0.99),
    ]
    out = apply_policy(hits, query="行程建议")
    assert [h.url for h in out] == ["https://a.example/1", "https://b.example/2"]
    assert out[0].rank_score == pytest.approx(1.0)
    assert out[1].rank_score == pytest.approx(0.5)


def test_apply_policy_is_stable_for_equal_scores() -> None:
    policy = load_policy({"boost": {"unknown": 0}})
    hits = [_hit(f"https://x{i}.example/{i}") for i in range(4)]
    out = apply_policy(hits, policy=policy)
    assert [h.url for h in out] == [h.url for h in hits]
    assert all(h.rank_score == 0 for h in out)


def test_apply_policy_reindexes_after_deny() -> None:
    policy = DomainPolicy(deny=("*://blocked.example/*",))
    hits = [_hit("https://blocked.example/1"), _hit("https://kept.example/2")]
    out = apply_policy(hits, policy=policy)
    assert out[0].rank_score == pytest.approx(1.0)  # 被剔除的不留分数空洞


def test_apply_policy_does_not_mutate_input() -> None:
    hits = _mixed_hits()
    out = apply_policy(hits, query="泰国签证政策")
    assert all(h.source_tier == "unknown" for h in hits)
    assert all(h.rank_score is None for h in hits)
    assert out[0] is not hits[4]


def test_apply_policy_empty_input() -> None:
    assert apply_policy([], query="签证") == []


# --------------------------------------------------------------------------
# load_policy(管理端配置)
# --------------------------------------------------------------------------


def test_load_policy_none_equals_default() -> None:
    policy = load_policy(None)
    assert policy.deny == DEFAULT_POLICY.deny
    assert policy.policy_boost_official == DEFAULT_POLICY.policy_boost_official
    assert classify_url("https://www.bbc.com/news/1", policy) == "media"


def test_load_policy_falls_back_on_bad_types() -> None:
    policy = load_policy(
        {
            "deny_domains": "*://a.example/*",  # 该是列表
            "tier_rules": 123,
            "boost": "nope",
            "policy_boost_official": "1.5",
            "policy_penalty_ugc": float("nan"),
        }
    )
    assert policy.deny == ()
    assert policy.boost["official"] == 1.6
    assert policy.policy_boost_official == 1.5
    assert policy.policy_penalty_ugc == 0.5
    assert classify_url("https://www.xiaohongshu.com/explore/1", policy) == "ugc"


def test_load_policy_ignores_bad_rules_inside_lists() -> None:
    policy = load_policy({"deny_domains": ["*://bad.example/*", "", 42, "**not a pattern**"]})
    assert is_denied("https://bad.example/1", policy) is True
    assert is_denied("https://good.example/1", policy) is False


def test_load_policy_merges_custom_tier_rules_with_presets() -> None:
    policy = load_policy({"tier_rules": {"official": ["*://*.myembassy.example/*"]}})
    assert classify_url("https://www.myembassy.example/visa", policy) == "official"
    # 预置清单不能被冲掉
    assert classify_url("https://www.zhihu.com/question/1", policy) == "ugc"
    assert classify_url("https://www.mfa.gov.cn/x", policy) == "official"


def test_load_policy_custom_rule_wins_conflict_with_preset() -> None:
    # 管理端显式把某官方域名降为 UGC,自定义优先级更高
    policy = load_policy({"tier_rules": {"ugc": ["*://*.gov.cn/*"]}})
    assert classify_url("https://www.mfa.gov.cn/x", policy) == "ugc"


def test_load_policy_replace_mode_drops_presets() -> None:
    policy = load_policy(
        {"tier_rules": {"ugc": ["*://*.myforum.example/*"]}, "tier_rules_mode": "replace"}
    )
    assert classify_url("https://www.myforum.example/t/1", policy) == "ugc"
    assert classify_url("https://www.zhihu.com/question/1", policy) == "unknown"


def test_load_policy_partial_boost_override() -> None:
    policy = load_policy({"boost": {"ugc": 0.2, "media": "bad"}})
    assert policy.boost["ugc"] == 0.2
    assert policy.boost["media"] == 1.15  # 非法项保留默认
    assert policy.boost["official"] == 1.6


def test_default_policy_is_not_polluted_by_overrides() -> None:
    load_policy({"tier_rules": {"ugc": ["*://*.gov.cn/*"]}, "deny_domains": ["<all_urls>"]})
    assert classify_url("https://www.mfa.gov.cn/x") == "official"
    assert is_denied("https://www.mfa.gov.cn/x") is False
