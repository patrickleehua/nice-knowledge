"""WeatherAPI.com 实现(主全球源)。

GET https://api.weatherapi.com/v1/forecast.json?key=..&q={city}&days={n}&lang=zh
注意:免费档 forecast 最多返回 3 天——请求天数超出时按返回的实际天数如实给,
不报错也不编造(截断由上层感知,可换 open-meteo 兜底拿更长预报)。
解析防御式:缺字段的天跳过;整体失败返回 unavailable,不抛异常。
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

_BASE_URL = "https://api.weatherapi.com/v1"


class WeatherApiProvider(WeatherProvider):
    name = "weatherapi"

    def __init__(
        self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport  # 单测注入 MockTransport 用

    def available(self) -> bool:
        return bool(self._api_key)

    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{_BASE_URL}/forecast.json",
                    params={
                        "key": self._api_key,
                        "q": location,
                        "days": days,
                        "aqi": "no",
                        "alerts": "no",
                        "lang": "zh",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # 契约:天气不可用不阻塞主链
            return unavailable_result(self.name, location, f"{type(exc).__name__}: {exc}")

        loc = payload.get("location") or {}
        parsed: list[WeatherDay] = []
        for entry in (payload.get("forecast") or {}).get("forecastday") or []:
            if not isinstance(entry, dict) or not entry.get("date"):
                continue
            if start is not None and str(entry["date"]) < start.isoformat():
                continue
            day = entry.get("day") or {}
            condition = str((day.get("condition") or {}).get("text") or "未知")
            wind_kph = day.get("maxwind_kph")
            parsed.append(
                WeatherDay(
                    date=str(entry["date"]),
                    condition=condition,
                    temp_min_c=_num(day.get("mintemp_c")),
                    temp_max_c=_num(day.get("maxtemp_c")),
                    precip_mm=_num(day.get("totalprecip_mm")),
                    wind=f"最大风速 {wind_kph} km/h" if wind_kph is not None else None,
                    source=self.name,
                )
            )
        if not parsed:
            return unavailable_result(self.name, location, "预报未覆盖请求日期(免费档最多 3 天)")
        return WeatherResult(
            location_name=str(loc.get("name") or location),
            country=str(loc["country"]) if loc.get("country") else None,
            days=parsed,
            provider=self.name,
        )


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
