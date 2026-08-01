"""Tavily 搜索实现(https://docs.tavily.com,POST /search + Bearer)。

响应结构(2026-07 口径,参考 TripBoss 同款接入):
{"results": [{"title","url","content","score","published_date"}], ...}
解析全部防御式:缺 url/title 跳过该条;整体失败返回 unavailable,不抛异常。
"""

from typing import Any
from urllib.parse import urlparse

import httpx

from nicekit.capabilities.websearch.base import SearchHit, SearchProvider, SearchQuery, SearchResult

_ENDPOINT = "https://api.tavily.com/search"

_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def parse_tavily_payload(payload: dict) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for entry in payload.get("results") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("title")
        if not url or not title:
            continue
        score: Any = entry.get("score")
        hits.append(
            SearchHit(
                title=str(title),
                url=str(url),
                domain=domain_of(str(url)),
                snippet=str(entry.get("content") or ""),
                published_at=entry.get("published_date") or None,
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    return hits


class TavilyProvider(SearchProvider):
    name = "tavily"

    def __init__(self, *, api_key: str, timeout: float) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: SearchQuery) -> SearchResult:
        body: dict[str, Any] = {
            "query": query.query,
            "max_results": query.max_results,
            "search_depth": "basic",
        }
        if query.freshness in _FRESHNESS_DAYS:
            body["days"] = _FRESHNESS_DAYS[query.freshness]
            body["topic"] = "news"  # Tavily 的 days 参数仅 news topic 生效
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _ENDPOINT,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                hits = parse_tavily_payload(resp.json())
        except Exception as exc:  # 网络/鉴权/解析,统一降级
            return SearchResult(provider=self.name, status="unavailable", error=str(exc))
        return SearchResult(
            provider=self.name, status="ok" if hits else "empty", hits=hits
        )
