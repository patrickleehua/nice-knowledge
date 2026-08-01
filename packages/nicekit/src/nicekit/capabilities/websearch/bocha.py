"""博查搜索实现(https://open.bochaai.com,POST /v1/web-search + Bearer)。

国内可直连的中文搜索源(参考 TripBoss 同款接入)。响应结构:
{"data": {"webPages": {"value": [{"name","url","snippet","summary",
"datePublished","siteName"}]}}}
解析全部防御式:缺 url/name 跳过该条;整体失败返回 unavailable,不抛异常。
"""

from typing import Any

import httpx

from nicekit.capabilities.websearch.base import SearchHit, SearchProvider, SearchQuery, SearchResult
from nicekit.capabilities.websearch.tavily import domain_of

_ENDPOINT = "https://api.bochaai.com/v1/web-search"

_FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}


def parse_bocha_payload(payload: dict) -> list[SearchHit]:
    pages = ((payload.get("data") or {}).get("webPages") or {}).get("value") or []
    hits: list[SearchHit] = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("name")
        if not url or not title:
            continue
        hits.append(
            SearchHit(
                title=str(title),
                url=str(url),
                domain=domain_of(str(url)) or str(entry.get("siteName") or ""),
                snippet=str(entry.get("summary") or entry.get("snippet") or ""),
                published_at=entry.get("datePublished") or None,
            )
        )
    return hits


class BochaProvider(SearchProvider):
    name = "bocha"

    def __init__(self, *, api_key: str, timeout: float) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: SearchQuery) -> SearchResult:
        body: dict[str, Any] = {
            "query": query.query,
            "count": query.max_results,
            "summary": True,
            "freshness": _FRESHNESS.get(query.freshness or "", "noLimit"),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _ENDPOINT,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                hits = parse_bocha_payload(resp.json())
        except Exception as exc:
            return SearchResult(provider=self.name, status="unavailable", error=str(exc))
        return SearchResult(
            provider=self.name, status="ok" if hits else "empty", hits=hits
        )
