"""联网搜索适配器契约(与 provider 无关)。

上层(agent 工具/service)只依赖本模块类型:换搜索源只替换 provider 实现。
所有实现必须可降级——任何失败返回 status=unavailable 的 SearchResult
(error 如实透传),绝不抛异常阻塞 agent loop(同 OtaProvider 思路)。

结果归一化口径:title/snippet 截断由 service 统一做,provider 只负责
把各家响应字段映射到 SearchHit,缺关键字段(url/title)的条目防御式跳过。
ref/source_tier/rank_score 三个治理字段一律由 service 层填充,provider
不许自行赋值——来源分级是全局策略,不能被单家 provider 的口径带偏。
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

# 来源可信度分级:官方(政府/使馆/航司/景区官网) > 权威媒体 > UGC(点评/社区)
SourceTier = Literal["official", "media", "ugc", "unknown"]

# 正文抓取结局:ok=拿到正文;empty=页面无正文;blocked=被安全或域名策略拒绝;
# unavailable=网络或解析失败(如实透传 error,不抛异常)
FetchStatus = Literal["ok", "empty", "blocked", "unavailable"]


class SearchQuery(BaseModel):
    """单条检索式(service 已 clamp)。多查询由 service 层展开为多个本对象。"""

    query: str
    max_results: int = 5  # service 已 clamp 到 1-10
    freshness: str | None = None  # day / week / month / year;None = 不限时效


class SearchHit(BaseModel):
    """归一化搜索结果条目(工具输出 results 的元素)。"""

    title: str
    url: str
    domain: str = ""
    snippet: str = ""
    published_at: str | None = None
    score: float | None = None  # provider 相关性得分(有则透传,口径各家不同)

    # ---- 以下由 service 层统一填充,provider 不赋值 ----
    ref: int | None = None  # 引用编号(1 起),模型用 [n] 引用、前端据此定位
    source_tier: SourceTier = "unknown"
    rank_score: float | None = None  # 域名策略加权后的排序分(排序用,不喂模型)


class SearchResult(BaseModel):
    provider: str
    status: str  # ok / empty / unavailable
    hits: list[SearchHit] = Field(default_factory=list)
    error: str | None = None

    # ---- 多查询与缓存的可观测字段 ----
    queries: list[str] = Field(default_factory=list)  # 本次实际执行的检索式
    cached: bool = False  # 命中缓存(命中不计 usage)


class FetchedPage(BaseModel):
    """单页正文抓取结果(web_fetch 工具输出的元素)。"""

    url: str  # 请求的原始 URL
    final_url: str = ""  # 重定向后的最终 URL(无重定向时等于 url)
    title: str = ""
    content: str = ""  # 正文 markdown(已按 max_chars 截断)
    status: FetchStatus
    error: str | None = None
    truncated: bool = False
    domain: str = ""
    source_tier: SourceTier = "unknown"
    published_at: str | None = None
    ref: int | None = None


class SearchProvider(ABC):
    name: str

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResult: ...
