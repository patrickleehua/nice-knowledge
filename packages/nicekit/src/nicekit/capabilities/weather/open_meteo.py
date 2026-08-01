"""Open-Meteo 实现——免 key 兜底源(最多 16 天预报)。

两步:geocoding-api 地理编码(language=zh,支持中文城市名)→ forecast 拿 daily。
weather_code 为 WMO 码,用小映射表转中文描述,未知码显示"未知"。
解析防御式:数组错位的天跳过;整体失败返回 unavailable,不抛异常。
"""

from datetime import date

import httpx

from nicekit.capabilities.weather.base import (
    REQUEST_TIMEOUT,
    WeatherDay,
    WeatherProvider,
    WeatherResult,
    unavailable_result,
)

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_MAX_FORECAST_DAYS = 16

# WMO weather code → 中文描述(常见码;完整表见 open-meteo 文档)
WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "雷阵雨伴冰雹",
}


def wmo_to_text(code: object) -> str:
    try:
        return WMO_CODES.get(int(code), "未知")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"


class OpenMeteoProvider(WeatherProvider):
    name = "open_meteo"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport  # 单测注入 MockTransport 用

    def available(self) -> bool:
        return True  # 免 key,恒可用

    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        # start 在未来时,forecast_days 要覆盖到 start+days 再按 start 过滤
        horizon = days
        if start is not None:
            horizon = (start - date.today()).days + days
        horizon = max(1, min(horizon, _MAX_FORECAST_DAYS))
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=self._transport
            ) as client:
                geo_resp = await client.get(
                    _GEO_URL, params={"name": location, "count": 1, "language": "zh"}
                )
                geo_resp.raise_for_status()
                results = (geo_resp.json() or {}).get("results") or []
                place = results[0] if results and isinstance(results[0], dict) else None
                if place is None or place.get("latitude") is None:
                    return unavailable_result(self.name, location, f"地理编码失败:{location}")
                resp = await client.get(
                    _FORECAST_URL,
                    params={
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_sum",
                        "timezone": "auto",
                        "forecast_days": horizon,
                    },
                )
                resp.raise_for_status()
                daily = (resp.json() or {}).get("daily") or {}
        except Exception as exc:  # 契约:天气不可用不阻塞主链
            return unavailable_result(self.name, location, f"{type(exc).__name__}: {exc}")

        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        t_max = daily.get("temperature_2m_max") or []
        t_min = daily.get("temperature_2m_min") or []
        precip = daily.get("precipitation_sum") or []
        parsed: list[WeatherDay] = []
        for i, day in enumerate(dates):
            if start is not None and str(day) < start.isoformat():
                continue
            parsed.append(
                WeatherDay(
                    date=str(day),
                    condition=wmo_to_text(codes[i] if i < len(codes) else None),
                    temp_min_c=_num(t_min[i]) if i < len(t_min) else None,
                    temp_max_c=_num(t_max[i]) if i < len(t_max) else None,
                    precip_mm=_num(precip[i]) if i < len(precip) else None,
                    wind=None,  # 未请求风力字段,保持 None
                    source=self.name,
                )
            )
            if len(parsed) >= days:
                break
        if not parsed:
            return unavailable_result(self.name, location, "预报未覆盖请求日期(最多 16 天)")
        return WeatherResult(
            location_name=str(place.get("name") or location),
            country=str(place["country"]) if place.get("country") else None,
            days=parsed,
            provider=self.name,
        )


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
