"""联网搜索服务:provider 链选择 → 故障转移 → 缓存 → 来源治理 → 计量。

provider 选择(websearch_provider):
- auto = 按 tavily → bocha 顺序取有 key 的全部(故障转移链);
- tavily / bocha = 只用指定一家(无 key 则链为空,如实返回 unavailable)。

规整口径:URL 去重、title/snippet 截断(防上下文爆炸)、截到 max_results。
计量口径:每次 provider 调用计 1 次,含失败——外部搜索按请求计费;
命中缓存不计,复用不该重复扣费。

来源治理(domains 模块)在**合并之后**统一做,不在单条 query 里做:
deny 过滤 → source_tier 分级 → 按权重重排 → 分配 ref 引用编号。
分级必须全局一致,否则同一域名在不同 query 里排序口径会打架。
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from email.utils import parsedate_to_datetime
from functools import lru_cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.capabilities.websearch import domains
from nicekit.capabilities.websearch.base import (
    FetchedPage,
    SearchHit,
    SearchProvider,
    SearchQuery,
    SearchResult,
)
from nicekit.capabilities.websearch.bocha import BochaProvider
from nicekit.capabilities.websearch.tavily import TavilyProvider, domain_of
from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.llm.service_config import load_overrides
from nicekit.tenancy.usage import record_usage

logger = logging.getLogger(__name__)

_MAX_RESULTS_CAP = 10
_MAX_QUERIES_CAP = 5  # 再多就是模型在滥用工具,拦在这里而不是让它烧钱
_MERGED_CAP = 12  # 多 query 合并后的总条数上限(防上下文爆炸)
_TITLE_MAX = 200
_SNIPPET_MAX = 600
_QUERY_MAX = 512
_FRESHNESS_VALUES = {"day", "week", "month", "year"}
_CACHE_PREFIX = "websearch:v1:"
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# 会话级选项:off=本轮不联网;builtin=用模型自带搜索;其余为自建 provider id
WEBSEARCH_OFF = "off"
WEBSEARCH_BUILTIN = "builtin"
_PROVIDER_LABELS = {"tavily": "Tavily", "bocha": "博查"}


@dataclass(frozen=True)
class WebSearchOption:
    value: str
    label: str
    ready: bool
    hint: str


@dataclass(frozen=True)
class WebSearchReadiness:
    """本 org 当前能用哪些联网方式,供输入框面板与工具门控共用一套判定。"""

    ready: bool  # 至少一个自建 provider 可用
    default_provider: str | None  # 管理端配置解析出的首选(auto 时取链首)
    options: tuple[WebSearchOption, ...]
    # 内置搜索的下发配置:随就绪判定一起算好,免得工具快照那边为了拿黑名单
    # 再查一次 service_configs
    builtin: dict = dc_field(default_factory=dict)

    def allows(self, choice: str | None) -> bool:
        """会话选择是否可用;None=跟随管理端配置。"""
        if choice is None:
            return self.ready
        return any(o.value == choice and o.ready for o in self.options)


_DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def domains_for_builtin(overrides: dict | None = None) -> list[str]:
    """把来源黑名单降级成纯域名列表,供模型内置搜索使用。

    内置搜索只认域名,不认我们的 match pattern 与正则,所以从规则里把域名抠
    出来。抠不出的规则(如纯路径通配)直接丢弃——宁可少挡一条,也不能构造出
    上游不认识的参数把整个请求打挂。
    """
    rules = _split_rules((overrides or {}).get("deny_domains")) or []
    found: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        # 正则规则里的点是转义过的(/zhihu\.com/),不还原就匹配不出域名
        normalized = rule.replace("\\.", ".").replace("\\/", "/")
        for match in _DOMAIN_RE.findall(normalized):
            domain = match.lower().lstrip(".")
            if domain not in seen:
                seen.add(domain)
                found.append(domain)
    return found


def builtin_search_config(overrides: dict | None = None) -> dict:
    """模型内置搜索的下发配置(provider 无关,由各 provider 自行翻译)。"""
    settings = get_settings()
    return {
        "max_uses": max(int(settings.builtin_web_search_max_uses), 1),
        "blocked_domains": domains_for_builtin(overrides),
        "tool_version": settings.anthropic_web_search_tool,
    }


def websearch_readiness(overrides: dict | None = None) -> WebSearchReadiness:
    """按管理端配置 + .env 解析可用的联网方式(纯计算,便于单测)。"""
    chain = get_providers(overrides)
    available = {p.name for p in chain}
    options = [
        WebSearchOption(value=WEBSEARCH_OFF, label="关闭", ready=True, hint="本轮不联网"),
        # 内置搜索由模型自带,不需要我们配 API 密钥,因此恒可选;但能不能真用起来
        # 取决于当前模型是否支持,选了不支持的模型会由 provider 报错并走降级链
        WebSearchOption(
            value=WEBSEARCH_BUILTIN, label="模型内置", ready=True, hint="由模型自带,需模型支持"
        ),
    ]
    for pid, label in _PROVIDER_LABELS.items():
        ready = pid in available
        options.append(
            WebSearchOption(
                value=pid, label=label, ready=ready,
                hint="可用" if ready else "未配置 API 密钥",
            )
        )
    return WebSearchReadiness(
        ready=bool(chain),
        default_provider=chain[0].name if chain else None,
        options=tuple(options),
        builtin=builtin_search_config(overrides),
    )


async def resolve_websearch_readiness(session: AsyncSession) -> WebSearchReadiness:
    return websearch_readiness(await load_overrides(session, "websearch"))


def get_providers(overrides: dict | None = None) -> list[SearchProvider]:
    """构建 provider 链;overrides 来自 service_configs(管理端),缺省回落 .env。"""
    s = get_settings()
    o = overrides or {}
    want = o.get("provider") or s.websearch_provider
    tavily_key = o.get("tavily_api_key") or s.tavily_api_key
    bocha_key = o.get("bocha_api_key") or s.bocha_api_key
    timeout = float(o.get("timeout_seconds") or s.websearch_timeout_seconds)
    chain: list[SearchProvider] = []
    if want in ("auto", "tavily") and tavily_key:
        chain.append(TavilyProvider(api_key=tavily_key, timeout=timeout))
    if want in ("auto", "bocha") and bocha_key:
        chain.append(BochaProvider(api_key=bocha_key, timeout=timeout))
    return chain


def normalize_query(query: str, max_results: int | None, freshness: str | None) -> SearchQuery:
    settings = get_settings()
    return SearchQuery(
        query=query.strip()[:_QUERY_MAX],
        max_results=min(max(max_results or settings.websearch_max_results, 1), _MAX_RESULTS_CAP),
        freshness=freshness if freshness in _FRESHNESS_VALUES else None,
    )


def normalize_queries(
    raw: str | list[str] | None,
    max_results: int | None,
    freshness: str | None,
) -> list[SearchQuery]:
    """把工具入参(单串或串数组)展开成去重后的检索式列表。

    模型偶尔会传重复或空白的 query,这里统一清洗;超出上限直接截断而不是报错——
    多搜几条不是错误,只是浪费,拦住即可。
    """
    settings = get_settings()
    limit = min(max(settings.websearch_max_queries, 1), _MAX_QUERIES_CAP)
    items = [raw] if isinstance(raw, str) else list(raw or [])
    seen: set[str] = set()
    queries: list[SearchQuery] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()[:_QUERY_MAX]
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(normalize_query(text, max_results, freshness))
        if len(queries) >= limit:
            break
    return queries


def _normalize_published(value: object) -> str | None:
    """把各家五花八门的发布时间归一成 YYYY-MM-DD。

    博查给 ISO 8601、Tavily 偶尔给 RFC 1123。前端按这个字段算"N 天前",拿到
    没见过的格式会渲染成 NaN,所以解析不了宁可返回 None(不显示时效)也不透传原串。
    """
    if not value:
        return None
    text = str(value).strip()
    matched = _DATE_PREFIX_RE.match(text)
    if matched:
        return matched.group(1)
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def _normalize(result: SearchResult, max_results: int) -> SearchResult:
    seen: set[str] = set()
    hits = []
    for hit in result.hits:
        key = hit.url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        hit.title = hit.title[:_TITLE_MAX]
        hit.snippet = hit.snippet[:_SNIPPET_MAX]
        hit.published_at = _normalize_published(hit.published_at)
        hits.append(hit)
        if len(hits) >= max_results:
            break
    result.hits = hits
    if result.status == "ok" and not hits:
        result.status = "empty"
    return result


def _split_rules(raw: object) -> list[str] | None:
    """管理端表单只能存字符串,这里把换行/逗号分隔的域名串还原成列表。

    domains.load_policy 期望列表;不在那边兼容字符串是因为那是纯策略模块,
    不该知道"配置来自一个 textarea"这种上游形态。
    """
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", "\n").replace("，", "\n").split("\n")]
        return [p for p in parts if p]
    return None


_POLICY_KEYS = (
    "deny_domains",
    "tier_rules",
    "tier_rules_mode",
    "boost",
    "policy_boost_official",
    "policy_penalty_ugc",
)


def _policy_overrides(overrides: dict) -> dict:
    """抽出策略相关键并归一化成 domains.load_policy 认识的结构。

    只取策略键有两个用处:一是 API Key 不会进下面的缓存键,二是改密钥不会
    白白让编译好的策略失效。
    """
    normalized = {k: overrides[k] for k in _POLICY_KEYS if k in overrides}
    deny = _split_rules(overrides.get("deny_domains"))
    if deny is not None:
        normalized["deny_domains"] = deny
    return normalized


@lru_cache(maxsize=32)
def _policy_from_key(key: str) -> domains.DomainPolicy:
    return domains.load_policy(json.loads(key))


def _resolve_policy(overrides: dict) -> domains.DomainPolicy:
    """按配置内容缓存已编译的策略。

    DomainPolicy 构造时会把全部 match pattern 编译成 trie 与正则,每次搜索都
    重建纯属浪费;配置变了 key 自然就变,不需要手动失效。
    """
    key = json.dumps(_policy_overrides(overrides), ensure_ascii=False, sort_keys=True)
    return _policy_from_key(key)


def _cache_key(chain: list[SearchProvider], query: SearchQuery) -> str:
    """缓存键含 provider 链:换搜索源后旧结果自然失效,不用手动清。"""
    raw = json.dumps(
        {
            "chain": [p.name for p in chain],
            "q": query.query,
            "n": query.max_results,
            "f": query.freshness or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _CACHE_PREFIX + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_ttl(query: SearchQuery) -> int:
    """限时效的查询缓存要短——问"今天"的事却拿半小时前的结果是错的。"""
    s = get_settings()
    if query.freshness in ("day", "week"):
        return max(int(s.websearch_cache_ttl_fresh_seconds), 0)
    return max(int(s.websearch_cache_ttl_seconds), 0)


async def run_search(
    session: AsyncSession,
    *,
    org_id: UUID,
    query: SearchQuery,
    providers: list[SearchProvider] | None = None,
    use_cache: bool = True,
) -> SearchResult:
    """单条检索式:缓存 → 按链故障转移 → ok/empty 即收 → 全败返回最后一个 unavailable。"""
    if providers is None:
        chain = get_providers(await load_overrides(session, "websearch"))
    else:
        chain = providers
    if not chain:
        return SearchResult(
            provider="none", status="unavailable",
            error="未配置联网搜索(在管理端「外部服务」或后端 .env 配置搜索 API Key)",
            queries=[query.query],
        )

    settings = get_settings()
    cache_on = use_cache and settings.websearch_cache_enabled
    key = _cache_key(chain, query) if cache_on else ""
    if cache_on:
        cached = await cache.get_json(key)
        if isinstance(cached, dict):
            try:
                # 命中缓存不计 usage:外部搜索按请求计费,复用不该重复扣
                return SearchResult.model_validate({**cached, "cached": True})
            except Exception:  # 缓存里是旧版结构,当未命中处理即可
                logger.warning("联网搜索缓存反序列化失败,回源重搜", extra={"key": key})

    last: SearchResult | None = None
    for provider in chain:
        result = await provider.search(query)
        # 计量:每次外部调用计 1 次,含失败(外部搜索按请求计费)
        await record_usage(
            session, org_id=org_id, task="web.search",
            provider=provider.name, model="",
        )
        if result.status in ("ok", "empty"):
            normalized = _normalize(result, query.max_results)
            normalized.queries = [query.query]
            if cache_on and normalized.status == "ok":
                await cache.set_json(key, normalized.model_dump(), _cache_ttl(query))
            return normalized
        last = result
    assert last is not None
    last.queries = [query.query]
    return last


def _merge_hits(results: list[SearchResult], limit: int) -> list[SearchHit]:
    """跨 query 合并并按 URL 去重(保留首次出现),末尾归一化 title/snippet 长度。"""
    seen: set[str] = set()
    merged: list[SearchHit] = []
    for result in results:
        for hit in result.hits:
            key = hit.url.rstrip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(
                hit.model_copy(
                    update={
                        "title": hit.title[:_TITLE_MAX],
                        "snippet": hit.snippet[:_SNIPPET_MAX],
                        "published_at": _normalize_published(hit.published_at),
                    }
                )
            )
            if len(merged) >= limit:
                return merged
    return merged


async def run_multi_search(
    session: AsyncSession,
    *,
    org_id: UUID,
    queries: list[SearchQuery],
    providers: list[SearchProvider] | None = None,
    policy: domains.DomainPolicy | None = None,
    overrides: dict | None = None,
    use_cache: bool = True,
    provider_override: str | None = None,
) -> SearchResult:
    """多检索式并发 → 合并去重 → 来源治理 → 分配 ref。

    部分成功即算成功:比较类问题拆成多条 query 时,一条挂掉不该让整轮白跑
    (这点与"单条 query 内 provider 链全败才失败"是两个层次)。
    """
    if not queries:
        return SearchResult(provider="none", status="empty", queries=[])

    if overrides is None:
        overrides = await load_overrides(session, "websearch")
    if provider_override:
        # 会话级选择压过管理端的 auto:用户明确选了哪家就只用哪家
        overrides = {**overrides, "provider": provider_override}
    chain = providers if providers is not None else get_providers(overrides)
    if not chain:
        return SearchResult(
            provider="none", status="unavailable",
            error="未配置联网搜索(在管理端「外部服务」或后端 .env 配置搜索 API Key)",
            queries=[q.query for q in queries],
        )

    # 并发跑各条 query;单条内部已做故障转移与降级,这里只兜住意外异常
    settled = await asyncio.gather(
        *(
            run_search(session, org_id=org_id, query=q, providers=chain, use_cache=use_cache)
            for q in queries
        ),
        return_exceptions=True,
    )
    results: list[SearchResult] = []
    errors: list[str] = []
    for query, item in zip(queries, settled, strict=True):
        if isinstance(item, BaseException):
            logger.warning("联网搜索单条失败:%s", item, extra={"query": query.query})
            errors.append(str(item))
            continue
        if item.status == "unavailable":
            errors.append(item.error or "unknown")
            continue
        results.append(item)

    if not results:
        return SearchResult(
            provider=chain[0].name, status="unavailable",
            error="; ".join(errors) or "搜索失败",
            queries=[q.query for q in queries],
        )

    merged = _merge_hits(results, _MERGED_CAP)
    resolved_policy = policy or _resolve_policy(overrides)
    ranked = domains.apply_policy(
        merged, policy=resolved_policy, query=" ".join(q.query for q in queries)
    )
    # ref 在治理排序**之后**分配:模型看到的 [1] 必须是排最前的那条
    ranked = [hit.model_copy(update={"ref": i}) for i, hit in enumerate(ranked, start=1)]

    return SearchResult(
        provider=results[0].provider,
        status="ok" if ranked else "empty",
        hits=ranked,
        error="; ".join(errors) or None,
        queries=[q.query for q in queries],
        cached=all(r.cached for r in results),
    )


async def _build_score_fn(org_id: UUID, query: str):
    """把 KB 的 reranker 适配成 compress.ScoreFn;不可用返回 None(压缩自动回落 cutoff)。

    两处适配是必须的,不是防御性冗余:
    1. reranker 单次候选数有上限(RERANK_TOP_N=50),5 页正文切块轻松破百,
       因此分批打分再拼接——批间分数可比,因为 query 相同、模型相同。
    2. query 为空时 reranker 直接抛 RerankError,与其让 compress 白跑一趟再
       回落,不如在这里就返回 None。

    延迟 import `nicekit.kb.rerank`:capabilities 不该在导入期就把 KB 检索链
    拖进来(rerank 反过来依赖 llm 路由与嵌入配置)。KB 子包未装配时
    ImportError 直接降级为"不排序",压缩侧自动回落 cutoff,不阻塞联网检索。
    """
    if not query.strip():
        return None

    try:
        from nicekit.kb.rerank import RERANK_TOP_N, get_rerank_service
    except ImportError:  # KB 检索链未装配 → 不排序
        logger.info("websearch_rerank_unavailable reason=kb_rerank_not_installed")
        return None

    reranker = get_rerank_service()
    if reranker is None:
        return None

    batch = max(int(RERANK_TOP_N) - 10, 10)  # 留余量,避免贴着上限触发上游拒绝

    async def score_fn(q: str, docs: list[str]) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(docs), batch):
            scores.extend(
                await reranker.rerank(org_id=org_id, query=q, documents=docs[start : start + batch])
            )
        return scores

    return score_fn


async def run_fetch(
    session: AsyncSession,
    *,
    org_id: UUID,
    urls: list[str],
    query: str = "",
    max_chars: int | None = None,
    overrides: dict | None = None,
    provider_override: str | None = None,
) -> list[FetchedPage]:
    """抓取网页正文:域名策略前置拦截 → 并发抓取 → 分级标注 → 压缩 → 计量。

    deny 判定放在抓取**之前**:被策略拉黑的站点连请求都不该发出去,
    既省流量也避免给对方留访问日志。
    """
    from nicekit.capabilities.websearch import compress, fetch

    settings = get_settings()
    if overrides is None:
        overrides = await load_overrides(session, "websearch")
    if provider_override:
        overrides = {**overrides, "provider": provider_override}
    policy = _resolve_policy(overrides)

    limit = max(int(settings.websearch_fetch_max_pages), 1)
    seen: set[str] = set()
    wanted: list[str] = []
    overflow: list[str] = []
    blocked: list[FetchedPage] = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if domains.is_denied(url, policy):
            blocked.append(
                FetchedPage(
                    url=url, status="blocked",
                    error="该域名已被管理端来源策略屏蔽",
                    domain=domain_of(url),
                    source_tier=domains.classify_url(url, policy),
                )
            )
            continue
        if len(wanted) >= limit:
            overflow.append(url)
            continue
        wanted.append(url)

    if overflow:
        # 超限的链接如实回一条汇总,而不是静默丢弃——否则模型以为全部读过了,
        # 会把"只读了前 N 页"的结论当完整答案。合并成一条避免撑爆输出。
        blocked.append(
            FetchedPage(
                url=overflow[0], status="blocked",
                error=(
                    f"超出单次抓取上限({limit} 页),含本条在内共 {len(overflow)} 个链接"
                    "未读取,如仍需要可另发一次请求"
                ),
                domain=domain_of(overflow[0]),
                source_tier=domains.classify_url(overflow[0], policy),
            )
        )

    pages: list[FetchedPage] = []
    if wanted:
        pages = await fetch.fetch_pages(
            wanted,
            max_chars=max_chars or settings.websearch_fetch_max_chars,
            timeout=settings.websearch_fetch_timeout_seconds,
            concurrency=max(int(settings.websearch_fetch_concurrency), 1),
        )
        # 每页一次外部请求,按页计量(同搜索口径:含失败)
        await record_usage(
            session, org_id=org_id, task="web.fetch",
            provider="http", model="", calls=len(wanted),
        )

    # 分级标注用最终 URL(重定向后可能换域名,按落地域名判定才准)
    pages = [
        page.model_copy(
            update={"source_tier": domains.classify_url(page.final_url or page.url, policy)}
        )
        for page in pages
    ]

    config = compress.load_config(
        {
            "method": overrides.get("compression_method")
            or settings.websearch_compression_method,
            "total_chars": overrides.get("compression_total_chars")
            or settings.websearch_compression_total_chars,
        }
    )
    # 注:rerank 走 KB 的 reranker,其内部按 kb.search.rerank 口径计量,开启后
    # KB 检索的 rerank 指标会混入 web 压缩的量。默认 cutoff 即为回避这一点;
    # 要长期开 rerank 应先给它拆独立 task 名。
    score_fn = await _build_score_fn(org_id, query) if config.method == "rerank" else None
    pages = await compress.compress_pages(
        pages, query=query, config=config, score_fn=score_fn
    )

    # 成功的页排前面再编号:ref 1 是模型最可能引用的那条,不该是一条被拦截的记录
    ordered = pages + blocked
    return [page.model_copy(update={"ref": i}) for i, page in enumerate(ordered, start=1)]
