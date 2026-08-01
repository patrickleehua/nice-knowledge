r"""联网搜索的域名策略与来源分级(官方 > 权威媒体 > UGC)。

为什么需要:web_search 把公开网页原样喂给模型,搜某项政策法规时社区帖子与
主管部门官网同权,输出会被 UGC 口径带偏,存在合规风险。本模块负责
三件事:黑名单剔除、来源三级分级、按分级重排 —— 让官方口径天然排在前面。

降级约定(与 provider 一致):URL 与管理端配置都是外部输入,任何非法值只做
丢弃 + warning 日志,绝不抛异常。搜索是 agent loop 的旁路能力,不能因为管理
端填错一条规则、或某条结果 URL 畸形就让整轮对话失败。

规则语法沿用 uBlacklist 的 match pattern:
- ``<all_urls>``
- ``*://*.example.com/*``(``*.`` 前缀 = 该域名自身及其所有子域)
- ``https://example.com/news/*/detail``(路径支持 ``*`` 通配;scheme 为 ``*``
  时只匹配 http/https)
- ``/zhihu\.com/``(以 ``/`` 包裹 = 正则,只对 hostname 做 IGNORECASE search)

域名匹配走"倒排标签 trie":按 ``com → example → www`` 逐段下钻,复杂度是
O(域名段数) 而非 O(规则数),所以预置清单可以放心长到几百条。
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from nicekit.capabilities.websearch.base import SearchHit, SourceTier

logger = logging.getLogger(__name__)

_ALL_URLS = "<all_urls>"
# scheme 允许 "*"(= http/https)或常规 scheme 名
_SCHEME_RE = re.compile(r"^(?:\*|[a-z][a-z0-9+.\-]*)$")
# 剥掉 "*." 前缀与尾部 "*" 之后,主机名只允许域名合法字符
_HOST_RE = re.compile(r"^[a-z0-9\-._]+$")
# 分级冲突时的优先级:数值越小越可信
_TIER_PRIORITY: Mapping[SourceTier, int] = MappingProxyType({"official": 0, "media": 1, "ugc": 2})


# --------------------------------------------------------------------------
# match pattern 解析与匹配
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMatchPattern:
    """解析后的 match pattern(不可变,可安全复用)。"""

    scheme: str  # "*" 或具体 scheme
    host: str  # 已剥掉 "*." 前缀与尾部 "*";any_host 时为空
    path: str  # 以 "/" 开头,可含 "*"
    any_host: bool = False  # host 为 "*" 或 <all_urls>
    include_subdomains: bool = False  # "*." 前缀:命中该域名自身及所有子域
    suffix_wildcard: bool = False  # 尾部 "*":见 _compile_host_suffix 的说明

    def matches_scheme(self, scheme: str) -> bool:
        # "*" 只放行 http/https:file/ftp/javascript 不该进入搜索结果治理
        if self.scheme == "*":
            return scheme in ("http", "https")
        return self.scheme == scheme

    def matches_path(self, path: str) -> bool:
        return _compile_path(self.path).match(path) is not None


@lru_cache(maxsize=1024)
def _compile_path(path: str) -> re.Pattern[str]:
    """路径通配编译并缓存:同一批规则会对每条结果反复匹配,不能每次重编译。"""
    return re.compile("^" + ".*".join(re.escape(part) for part in path.split("*")) + r"\Z")


def _compile_host_suffix(parsed: ParsedMatchPattern) -> re.Pattern[str]:
    """主机名尾部 ``*`` 的匹配式。

    这是对 uBlacklist 语义的一处扩展:``*.embassy.gov*`` 要同时覆盖
    embassy.gov.au / embassy.gov.uk 这类"同名不同国家后缀"的使馆域名,标签
    trie 表达不了。只允许在"点边界"处继续延伸,避免误伤 embassy.government.com。
    """
    prefix = r"(?:.*\.)?" if parsed.include_subdomains else ""
    return re.compile(rf"^{prefix}{re.escape(parsed.host)}(?:\..+)?\Z", re.IGNORECASE)


def parse_match_pattern(pattern: str) -> ParsedMatchPattern | None:
    """解析一条 match pattern;非法一律返回 None(不抛异常,交由调用方记日志)。"""
    text = (pattern or "").strip()
    if not text:
        return None
    if text == _ALL_URLS:
        # 有意收窄成 http/https:搜索结果里不该出现 file/ftp/javascript
        return ParsedMatchPattern(scheme="*", host="", path="/*", any_host=True)

    scheme, sep, rest = text.partition("://")
    if not sep:
        return None
    scheme = scheme.lower()
    if not _SCHEME_RE.match(scheme):
        return None

    host_part, slash, path_part = rest.partition("/")
    # 管理端手填时经常漏掉尾部路径,缺省按 "/*" 处理比整条丢弃更符合预期
    path = f"/{path_part}" if slash else "/*"
    host_part = host_part.lower()
    if not host_part:
        return None
    if host_part == "*":
        return ParsedMatchPattern(scheme=scheme, host="", path=path, any_host=True)

    include_subdomains = host_part.startswith("*.")
    if include_subdomains:
        host_part = host_part[2:]
    suffix_wildcard = host_part.endswith("*")
    if suffix_wildcard:
        host_part = host_part[:-1]
    host_part = host_part.rstrip(".")
    # 到这里再出现 "*" 属于不支持的中缀通配(如 a*b.com),以及空标签(a..b)一并拒绝
    if not host_part or not _HOST_RE.match(host_part):
        return None
    if any(not label for label in host_part.split(".")):
        return None
    return ParsedMatchPattern(
        scheme=scheme,
        host=host_part,
        path=path,
        include_subdomains=include_subdomains,
        suffix_wildcard=suffix_wildcard,
    )


def _is_regex_rule(rule: str) -> bool:
    """``/.../`` 形式即正则规则;match pattern 永远不以 "/" 开头,无歧义。"""
    return len(rule) > 2 and rule.startswith("/") and rule.endswith("/")


def _compile_regex_rule(rule: str) -> re.Pattern[str] | None:
    body = rule[1:-1]
    if not body:
        return None
    try:
        return re.compile(body, re.IGNORECASE)
    except re.error as exc:  # 管理端写错正则不能炸掉整条搜索链路
        logger.warning("忽略非法正则域名规则 %s: %s", rule, exc)
        return None


def _split_url(url: str) -> tuple[str, str, str] | None:
    """URL → (scheme, hostname, path+query);任何畸形输入返回 None。"""
    try:
        parts = urlsplit((url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower().rstrip(".")
        query = parts.query
    except ValueError:  # 非法端口 / 畸形 IPv6 字面量
        return None
    if not scheme or not host:
        return None
    path = parts.path or "/"
    # 对齐 Chrome:路径模式匹配的是 path + query,便于写 ``/search?q=*`` 这类规则
    return scheme, host, f"{path}?{query}" if query else path


class _TrieNode:
    """倒排域名标签 trie 节点。"""

    __slots__ = ("children", "exact", "subdomain")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.exact: list[tuple[ParsedMatchPattern, Any]] = []  # 精确域名规则
        self.subdomain: list[tuple[ParsedMatchPattern, Any]] = []  # "*." 规则


class MatchPatternMap:
    """规则 → 值的多值映射(uBlacklist 同款倒排标签 trie)。

    同一 URL 可命中多条规则,``get`` 按"越具体越靠前"返回:精确域名 → 子域
    (深的在前) → 尾缀通配 → 任意主机 → 正则。正则走线性扫描,因此规则数应
    保持在小规模;能用 match pattern 表达的就别写正则。
    """

    def __init__(self) -> None:
        self._root = _TrieNode()
        self._any_host: list[tuple[ParsedMatchPattern, Any]] = []
        self._suffix: list[tuple[re.Pattern[str], ParsedMatchPattern, Any]] = []
        self._regex: list[tuple[re.Pattern[str], Any]] = []
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def set(self, pattern: str, value: Any) -> bool:
        """注册一条规则(match pattern 或 ``/正则/``);非法则记 warning 并返回 False。"""
        rule = (pattern or "").strip()
        if _is_regex_rule(rule):
            compiled = _compile_regex_rule(rule)
            if compiled is None:
                return False
            self._regex.append((compiled, value))
            self._size += 1
            return True

        parsed = parse_match_pattern(rule)
        if parsed is None:
            logger.warning("忽略非法域名规则: %r", pattern)
            return False
        if parsed.any_host:
            self._any_host.append((parsed, value))
        elif parsed.suffix_wildcard:
            self._suffix.append((_compile_host_suffix(parsed), parsed, value))
        else:
            node = self._root
            for label in reversed(parsed.host.split(".")):  # 倒排:com → example
                node = node.children.setdefault(label, _TrieNode())
            bucket = node.subdomain if parsed.include_subdomains else node.exact
            bucket.append((parsed, value))
        self._size += 1
        return True

    def get(self, url: str) -> list[Any]:
        """返回该 URL 命中的全部值(具体规则在前);URL 非法返回空列表。"""
        split = _split_url(url)
        if split is None:
            return []
        scheme, host, path = split

        candidates: list[tuple[ParsedMatchPattern, Any]] = []
        node = self._root
        subdomain_buckets: list[list[tuple[ParsedMatchPattern, Any]]] = []
        exact_hit = True
        for label in reversed(host.split(".")):
            child = node.children.get(label)
            if child is None:
                exact_hit = False
                break
            node = child
            if node.subdomain:
                subdomain_buckets.append(node.subdomain)
        if exact_hit:  # 完整走到底才算精确命中:example.com 的规则不吃 sub.example.com
            candidates.extend(node.exact)
        for bucket in reversed(subdomain_buckets):  # 深子域优先
            candidates.extend(bucket)
        for host_re, parsed, value in self._suffix:
            if host_re.match(host):
                candidates.append((parsed, value))
        candidates.extend(self._any_host)

        values = [
            value
            for parsed, value in candidates
            if parsed.matches_scheme(scheme) and parsed.matches_path(path)
        ]
        # 正则规则只作用于 hostname:路径里出现 zhihu.com 不该被判成知乎
        values.extend(value for regex, value in self._regex if regex.search(host))
        return values


# --------------------------------------------------------------------------
# 预置分级清单(默认样例:政务/出行/资讯域;宿主可经管理端来源策略整体覆盖)
# --------------------------------------------------------------------------

# 官方:政府 / 外交与移民机构。政策法规的唯一权威口径,必须压过一切二手转述。
# 行业专有的官方源(监管机构、厂商官网……)由宿主经 load_policy(overrides) 追加。
_OFFICIAL_RULES: tuple[str, ...] = (
    # 各国与地区政府域名后缀
    "*://*.gov.cn/*",
    "*://*.gov/*",
    "*://*.gov.uk/*",
    "*://*.go.jp/*",
    "*://*.go.kr/*",
    "*://*.gov.au/*",
    "*://*.gc.ca/*",
    "*://*.gov.sg/*",
    "*://*.govt.nz/*",
    "*://*.gov.hk/*",
    "*://*.europa.eu/*",
    "*://*.gov.my/*",
    "*://*.go.th/*",
    "*://*.gov.ae/*",
    # 外交与移民机构:跨境事务的第一手来源
    "*://*.mfa.gov.cn/*",
    "*://*.embassy.gov*/*",
    "*://*.immigration.gov*/*",
)

# 权威媒体:有编辑部与事实核查流程,可作官方口径的补充与解读,但不作最终依据。
_MEDIA_RULES: tuple[str, ...] = (
    "*://*.xinhuanet.com/*",
    "*://*.people.com.cn/*",
    "*://*.cctv.com/*",
    "*://*.chinanews.com.cn/*",
    "*://*.thepaper.cn/*",
    "*://*.caixin.com/*",
    "*://*.yicai.com/*",
    "*://*.bbc.com/*",
    "*://*.reuters.com/*",
    "*://*.apnews.com/*",
    "*://*.scmp.com/*",
    "*://*.nikkei.com/*",
)

# UGC:社区/问答/短视频。做主观体验参考有价值,但时效性差、个人经验易以偏概全,
# 默认降权,政策类问题再额外压制。行业垂直社区由宿主按需追加。
_UGC_RULES: tuple[str, ...] = (
    "*://*.xiaohongshu.com/*",
    "*://*.zhihu.com/*",
    "*://*.weibo.com/*",
    "*://*.douban.com/*",
    "*://tieba.baidu.com/*",
    "*://*.douyin.com/*",
    "*://*.bilibili.com/*",
)

_DEFAULT_TIER_RULES: Mapping[SourceTier, tuple[str, ...]] = MappingProxyType(
    {"official": _OFFICIAL_RULES, "media": _MEDIA_RULES, "ugc": _UGC_RULES}
)

# 默认不预置黑名单:误伤一个域名的代价(整站不可见)远大于降权,由管理端按需配置
_DEFAULT_DENY: tuple[str, ...] = ()

_DEFAULT_BOOST: Mapping[str, float] = MappingProxyType(
    {
        "official": 1.6,  # 官方口径直接顶到前排
        "media": 1.15,  # 权威媒体小幅加权
        "unknown": 1.0,  # 未识别域名保持 provider 原次序
        "ugc": 0.7,  # UGC 默认降权
    }
)


# --------------------------------------------------------------------------
# 政策类问题识别
# --------------------------------------------------------------------------

# 中文关键词直接子串匹配(中文无词边界)
_POLICY_KEYWORDS_CN: tuple[str, ...] = (
    "政策",
    "新规",
    "规定",
    "法规",
    "条例",
    "办法",
    "通知",
    "公告",
    "合规",
    "监管",
    "标准",
    "资质",
)

# 英文关键词走词边界:子串匹配会把 "policyholder" 误判成 policy
_POLICY_KEYWORDS_EN: tuple[str, ...] = (
    "policy",
    "policies",
    "regulation",
    "regulations",
    "compliance",
    "statute",
    "legislation",
)
def _compile_policy_keywords_en(words: tuple[str, ...]) -> re.Pattern[str] | None:
    """英文触发词编译为带词边界的正则;空词表返回 None(只走中文子串匹配)。"""
    cleaned = [word.strip() for word in words if word and word.strip()]
    if not cleaned:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(word) for word in cleaned) + r")\b", re.IGNORECASE
    )


_POLICY_EN_RE = _compile_policy_keywords_en(_POLICY_KEYWORDS_EN)


# --------------------------------------------------------------------------
# 策略对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CompiledPolicy:
    """策略的编译产物:规则只在构造策略时编译一次,分级要对每条结果跑。"""

    deny: MatchPatternMap
    tiers: MatchPatternMap  # value = (provenance, priority, tier)


def _compile_policy(
    deny: Iterable[str], tiers: Mapping[SourceTier, tuple[str, ...]]
) -> _CompiledPolicy:
    deny_map = MatchPatternMap()
    for rule in deny:
        deny_map.set(rule, True)
    tier_map = MatchPatternMap()
    for tier, priority in _TIER_PRIORITY.items():
        for rule in tiers.get(tier, ()) or ():
            # 不在预置清单里的规则视为管理端自定义:同一 URL 跨级冲突时自定义先赢,
            # 否则用户把某个域名改判到别的级别会被预置清单默默盖掉。
            provenance = 1 if rule in _DEFAULT_TIER_RULES.get(tier, ()) else 0
            tier_map.set(rule, (provenance, priority, tier))
    return _CompiledPolicy(deny=deny_map, tiers=tier_map)


@dataclass(frozen=True)
class DomainPolicy:
    """一份完整的域名治理策略:黑名单 + 分级规则 + 权重。"""

    deny: tuple[str, ...] = ()  # match pattern 或 /正则/
    tiers: Mapping[SourceTier, tuple[str, ...]] = field(default_factory=lambda: _DEFAULT_TIER_RULES)
    boost: Mapping[str, float] = field(default_factory=lambda: _DEFAULT_BOOST)
    policy_boost_official: float = 1.5  # 政策类问题对 official 的额外加权
    policy_penalty_ugc: float = 0.5  # 政策类问题对 ugc 的额外降权
    # 政策类问题的触发词。内置词表只覆盖通用政务/合规语义,行业专有词由宿主追加;
    # 关键词属于策略的一部分而非模块全局,不同租户可以有不同的领域词表。
    policy_keywords_cn: tuple[str, ...] = field(default_factory=lambda: _POLICY_KEYWORDS_CN)
    policy_keywords_en: tuple[str, ...] = field(default_factory=lambda: _POLICY_KEYWORDS_EN)
    # 编译产物随策略一起固化(frozen 下用 object.__setattr__ 写入),不参与比较与 repr
    compiled: _CompiledPolicy = field(init=False, repr=False, compare=False)
    compiled_policy_en: re.Pattern[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "compiled", _compile_policy(self.deny, self.tiers))
        object.__setattr__(
            self, "compiled_policy_en", _compile_policy_keywords_en(self.policy_keywords_en)
        )


DEFAULT_POLICY = DomainPolicy(deny=_DEFAULT_DENY)


def _as_rules(value: object) -> tuple[str, ...] | None:
    """管理端规则列表 → 规则元组;不是列表则返回 None(交由调用方回落默认)。"""
    if not isinstance(value, list | tuple):
        return None
    rules = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return tuple(rules)


def _as_weight(value: object, default: float) -> float:
    """管理端权重 → 非负有限浮点;非法回落默认(bool 是 int 子类,先挡掉)。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return default
    return number


def load_policy(overrides: dict | None = None) -> DomainPolicy:
    """从管理端 service_configs 覆盖项构造策略。

    键:deny_domains / tier_rules / tier_rules_mode / boost /
    policy_boost_official / policy_penalty_ugc。任何字段缺失或类型非法都逐项
    回落 DEFAULT_POLICY —— 管理端填错一格不该让联网搜索整体失灵。

    tier_rules 默认与预置清单**合并**(自定义在前、冲突时自定义优先);只有显式
    传 ``"tier_rules_mode": "replace"`` 才整体替换预置。
    """
    data = overrides if isinstance(overrides, dict) else {}

    deny = _as_rules(data.get("deny_domains"))
    if deny is None:
        deny = DEFAULT_POLICY.deny

    raw_tiers = data.get("tier_rules")
    custom: dict[SourceTier, tuple[str, ...]] = {}
    if isinstance(raw_tiers, dict):
        for tier in _TIER_PRIORITY:
            rules = _as_rules(raw_tiers.get(tier))
            if rules:
                custom[tier] = rules
    replace = str(data.get("tier_rules_mode") or "").strip().lower() == "replace"
    tiers: dict[SourceTier, tuple[str, ...]] = {}
    for tier in _TIER_PRIORITY:
        preset = () if replace else _DEFAULT_TIER_RULES[tier]
        # 去重并保持"自定义在前"的次序
        tiers[tier] = tuple(dict.fromkeys(custom.get(tier, ()) + preset))

    # 政策触发词:默认在内置词表上追加;policy_keywords_mode=replace 则整体替换
    raw_keywords = data.get("policy_keywords")
    extra_cn: tuple[str, ...] = ()
    extra_en: tuple[str, ...] = ()
    if isinstance(raw_keywords, dict):
        extra_cn = _as_rules(raw_keywords.get("cn")) or ()
        extra_en = _as_rules(raw_keywords.get("en")) or ()
    keywords_replace = str(data.get("policy_keywords_mode") or "").strip().lower() == "replace"
    keywords_cn = tuple(
        dict.fromkeys(extra_cn if keywords_replace else extra_cn + _POLICY_KEYWORDS_CN)
    )
    keywords_en = tuple(
        dict.fromkeys(extra_en if keywords_replace else extra_en + _POLICY_KEYWORDS_EN)
    )

    boost = dict(_DEFAULT_BOOST)
    raw_boost = data.get("boost")
    if isinstance(raw_boost, dict):
        for key, value in raw_boost.items():
            if isinstance(key, str):  # 部分覆盖:只改 ugc 不该把其余档位清空
                boost[key] = _as_weight(value, boost.get(key, 1.0))

    return DomainPolicy(
        deny=deny,
        tiers=MappingProxyType(tiers),
        boost=MappingProxyType(boost),
        policy_boost_official=_as_weight(
            data.get("policy_boost_official"), DEFAULT_POLICY.policy_boost_official
        ),
        policy_penalty_ugc=_as_weight(
            data.get("policy_penalty_ugc"), DEFAULT_POLICY.policy_penalty_ugc
        ),
        policy_keywords_cn=keywords_cn,
        policy_keywords_en=keywords_en,
    )


def is_policy_query(query: str, policy: DomainPolicy | None = None) -> bool:
    """是否政策法规类问题——这类问题要额外抬官方压 UGC。

    内置词表只覆盖通用政务/合规语义;行业专有触发词(某领域的证件名、准入名词
    等)由宿主经 ``load_policy({"policy_keywords": {"cn": [...], "en": [...]}})``
    追加,或 ``policy_keywords_mode="replace"`` 整体替换。
    """
    text = (query or "").strip()
    if not text:
        return False
    keywords_cn = policy.policy_keywords_cn if policy is not None else _POLICY_KEYWORDS_CN
    if any(keyword in text for keyword in keywords_cn):
        return True
    pattern = policy.compiled_policy_en if policy is not None else _POLICY_EN_RE
    return pattern is not None and pattern.search(text) is not None


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------


def classify_url(url: str, policy: DomainPolicy = DEFAULT_POLICY) -> SourceTier:
    """判定来源分级;多级同时命中按 official > media > ugc,无命中/URL 非法返回 unknown。"""
    matched = policy.compiled.tiers.get(url)
    if not matched:
        return "unknown"
    # 值为 (provenance, priority, tier):先比来源(自定义优先)再比可信度
    return min(matched)[2]


def is_denied(url: str, policy: DomainPolicy = DEFAULT_POLICY) -> bool:
    """是否命中黑名单(命中即整条剔除,不进模型上下文)。"""
    return bool(policy.compiled.deny.get(url))


def apply_policy(
    hits: list[SearchHit], *, policy: DomainPolicy = DEFAULT_POLICY, query: str = ""
) -> list[SearchHit]:
    """按域名策略过滤 + 分级 + 重排,返回新列表(纯函数,入参对象不被修改)。

    基础分只取原始位次倒数 1/(1+i),不用 provider 的 score:Tavily 是 0-1 的
    相关度、Bocha 根本没有,跨 provider 混排时用它会把两家的量纲搅在一起。
    """
    policy_mode = is_policy_query(query, policy)
    scored: list[tuple[float, SearchHit]] = []
    index = 0
    for hit in hits:
        if is_denied(hit.url, policy):
            continue
        tier = classify_url(hit.url, policy)
        # 位次按剔除黑名单后的序号:被删条目不该在分数上留下空洞
        score = (1.0 / (1 + index)) * _as_weight(policy.boost.get(tier), 1.0)
        if policy_mode:
            if tier == "official":
                score *= policy.policy_boost_official
            elif tier == "ugc":
                score *= policy.policy_penalty_ugc
        scored.append((score, hit.model_copy(update={"source_tier": tier, "rank_score": score})))
        index += 1
    # Python 的 sort 稳定,reverse=True 也不打乱同分条目的原次序
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in scored]
