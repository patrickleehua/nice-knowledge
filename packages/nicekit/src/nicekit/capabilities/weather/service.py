"""天气服务:区域路由 → 依序尝试 → 故障降级 + 进程内健康度。

路由规则:查询串含 CJK 字符(中文城市名)时优先和风(中国/亚洲数据更好),
否则 weatherapi 优先;open-meteo 免 key 恒排兜底。available()=False 的直接跳过。
健康度:连续失败 >= 3 次的 provider 在 10 分钟内降到队尾(进程内计数,无持久化)。
"""

import re
import time
from collections import Counter
from datetime import date

from nicekit.capabilities.weather.base import WeatherProvider, WeatherResult
from nicekit.capabilities.weather.open_meteo import OpenMeteoProvider
from nicekit.capabilities.weather.qweather import QWeatherProvider
from nicekit.capabilities.weather.weatherapi import WeatherApiProvider
from nicekit.core.config import get_settings
from nicekit.llm.runtime_config import runtime_overrides

_CJK_RE = re.compile(r"[一-鿿]")  # CJK 统一表意文字(中文城市名判定)
_FAILURE_THRESHOLD = 3  # 连续失败达到该次数触发降级
_DEMOTE_SECONDS = 600.0  # 降级(排队尾)持续时长

_ORDER_CJK = ["qweather", "weatherapi", "open_meteo"]
_ORDER_DEFAULT = ["weatherapi", "qweather", "open_meteo"]


def _default_providers() -> list[WeatherProvider]:
    """凭证优先取 service_configs 的 weather 行,缺键回落 .env(迁移期兜底)。

    读进程内缓存而非查库:本函数是同步工厂,每次请求都会走到。
    """
    settings = get_settings()
    override = runtime_overrides("weather")
    return [
        WeatherApiProvider(
            override.get("weatherapi_api_key") or settings.weatherapi_api_key
        ),
        QWeatherProvider(
            override.get("qweather_api_key") or settings.qweather_api_key,
            override.get("qweather_api_host") or settings.qweather_api_host,
        ),
        OpenMeteoProvider(),
    ]


class WeatherService:
    """多源天气查询(providers 缺省从 settings 构建;单测可注入假 provider)。"""

    def __init__(self, providers: list[WeatherProvider] | None = None) -> None:
        self._providers = providers if providers is not None else _default_providers()
        self._failures: dict[str, int] = {}  # provider → 连续失败次数
        self._demoted_until: dict[str, float] = {}  # provider → 降级截止 monotonic 时刻

    def _ordered(self, location: str) -> list[WeatherProvider]:
        """区域优先序 + 健康度调序:被降级的 provider 稳定移到队尾。"""
        preferred = _ORDER_CJK if _CJK_RE.search(location) else _ORDER_DEFAULT
        rank = {name: i for i, name in enumerate(preferred)}
        ordered = sorted(self._providers, key=lambda p: rank.get(p.name, len(preferred)))
        now = time.monotonic()
        healthy = [p for p in ordered if self._demoted_until.get(p.name, 0.0) <= now]
        demoted = [p for p in ordered if self._demoted_until.get(p.name, 0.0) > now]
        return healthy + demoted

    def _record_failure(self, name: str) -> None:
        self._failures[name] = self._failures.get(name, 0) + 1
        if self._failures[name] >= _FAILURE_THRESHOLD:
            self._demoted_until[name] = time.monotonic() + _DEMOTE_SECONDS

    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        """依优先序尝试各源,失败降级下一家;全部失败返回 unavailable=True。"""
        days = max(1, min(days, 14))
        reasons: list[str] = []
        for provider in self._ordered(location):
            if not provider.available():
                continue
            result = await provider.forecast(location, start=start, days=days)
            if result.unavailable or not result.days:
                self._record_failure(provider.name)
                reasons.append(f"{provider.name}: {result.reason or '无数据'}")
                continue
            self._failures[provider.name] = 0  # 成功即重置连续失败计数
            return result
        return WeatherResult(
            location_name=location,
            country=None,
            provider="none",
            unavailable=True,
            reason="; ".join(reasons) or "无可用天气源(凭证均未配置)",
        )


def summarize_weather(result: WeatherResult) -> str:
    """把预报压缩成一段中文摘要(注入 prompt / 对话展示用)。

    形如:"墨尔本 07-20~07-27:多云为主 8~15°C,07-22、07-24 有雨(来源 weatherapi)"。
    """
    days = result.days
    span = f"{days[0].date[5:]}~{days[-1].date[5:]}" if len(days) > 1 else days[0].date[5:]
    dominant = Counter(d.condition for d in days if d.condition).most_common(1)
    head = f"{result.location_name} {span}:{dominant[0][0] if dominant else '未知'}为主"
    t_min = [d.temp_min_c for d in days if d.temp_min_c is not None]
    t_max = [d.temp_max_c for d in days if d.temp_max_c is not None]
    if t_min and t_max:
        head += f" {round(min(t_min))}~{round(max(t_max))}°C"
    parts = [head]
    rainy = [d.date[5:] for d in days if (d.precip_mm or 0) >= 1 or "雨" in d.condition]
    if rainy:
        parts.append(f"{'、'.join(rainy[:4])} 有雨")
    return ",".join(parts) + f"(来源 {result.provider})"


_service: WeatherService | None = None


def get_weather_service() -> WeatherService:
    """进程级单例(健康度计数跨请求生效)。"""
    global _service
    if _service is None:
        _service = WeatherService()
    return _service
