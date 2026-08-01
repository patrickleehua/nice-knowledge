"""天气预报适配器契约(与 provider 无关,与本包其余能力同一套 base/provider/service 分层)。

上层(agent 工具 / 宿主业务节点)只依赖本模块类型:换/加天气源只替换 provider 实现。
所有实现必须可降级——任何失败返回 unavailable=True 的 WeatherResult
(reason 如实透传),绝不抛异常阻塞调用方主链(可降级能力的统一姿态)。

口径:
- location 为城市名(中文优先,英文亦可),由各 provider 自行地理编码;
- days 为期望预报天数,provider 覆盖不足时如实返回实际天数(如 weatherapi
  免费档最多 3 天),完全未覆盖请求日期时返回 unavailable。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

#: 所有 provider 的 httpx 超时(秒)
REQUEST_TIMEOUT = 10.0


@dataclass
class WeatherDay:
    """单日预报(温度摄氏度;precip_mm/wind 源不提供时为 None)。"""

    date: str  # YYYY-MM-DD
    condition: str  # 天气文字描述(中文优先)
    temp_min_c: float | None
    temp_max_c: float | None
    precip_mm: float | None = None
    wind: str | None = None  # 风力文字描述,如 "东南风3级" / "最大 24 km/h"
    source: str = ""  # provider 名


@dataclass
class WeatherResult:
    """一次预报查询的归一化结果;unavailable=True 表示该源当前拿不到数据。"""

    location_name: str
    country: str | None
    days: list[WeatherDay] = field(default_factory=list)
    provider: str = ""
    unavailable: bool = False
    reason: str | None = None


class WeatherProvider(ABC):
    name: str

    @abstractmethod
    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        """查询 location 未来 days 天预报;start 给定时只保留 >= start 的日期。"""

    @abstractmethod
    def available(self) -> bool:
        """凭证是否配置齐全(未配置的 provider 由 service 直接跳过)。"""


def unavailable_result(provider: str, location: str, reason: str) -> WeatherResult:
    """统一的失败降级结果(失败不抛给上层,如实带原因)。"""
    return WeatherResult(
        location_name=location,
        country=None,
        provider=provider,
        unavailable=True,
        reason=reason[:500],
    )
