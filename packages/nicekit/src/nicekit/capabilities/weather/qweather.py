"""和风天气(QWeather)实现——中国/亚洲城市增强源。

每个账号有专属 API Host(settings.qweather_api_host,形如 xxx.qweatherapi.com),
未配置 host 或 key 时 available()=False,service 直接跳过。
认证:请求头 X-QW-Api-Key;响应恒为 gzip(httpx 自动解压),code=="200" 为成功。
两步:GeoAPI 城市定位取 id → /v7/weather/7d 拿 7 天预报。
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


class QWeatherProvider(WeatherProvider):
    name = "qweather"

    def __init__(
        self,
        api_key: str,
        api_host: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_host = api_host.strip().rstrip("/")
        self._transport = transport  # 单测注入 MockTransport 用

    def available(self) -> bool:
        return bool(self._api_key and self._api_host)

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        resp = await client.get(f"https://{self._api_host}{path}", params=params)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict) or str(body.get("code")) != "200":
            raise ValueError(f"和风响应异常:{str(body)[:200]}")
        return body

    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                headers={"X-QW-Api-Key": self._api_key},
                transport=self._transport,
            ) as client:
                geo = await self._get(client, "/geo/v2/city/lookup", {"location": location})
                city = next(
                    (c for c in geo.get("location") or [] if isinstance(c, dict) and c.get("id")),
                    None,
                )
                if city is None:
                    return unavailable_result(self.name, location, f"城市无法定位:{location}")
                payload = await self._get(client, "/v7/weather/7d", {"location": city["id"]})
        except Exception as exc:  # 契约:天气不可用不阻塞主链
            return unavailable_result(self.name, location, f"{type(exc).__name__}: {exc}")

        parsed: list[WeatherDay] = []
        for entry in payload.get("daily") or []:
            if not isinstance(entry, dict) or not entry.get("fxDate"):
                continue
            if start is not None and str(entry["fxDate"]) < start.isoformat():
                continue
            wind_dir = entry.get("windDirDay") or ""
            wind_scale = entry.get("windScaleDay") or ""
            parsed.append(
                WeatherDay(
                    date=str(entry["fxDate"]),
                    condition=str(entry.get("textDay") or "未知"),
                    temp_min_c=_num(entry.get("tempMin")),
                    temp_max_c=_num(entry.get("tempMax")),
                    precip_mm=_num(entry.get("precip")),
                    wind=f"{wind_dir}{wind_scale}级" if wind_dir or wind_scale else None,
                    source=self.name,
                )
            )
            if len(parsed) >= days:
                break
        if not parsed:
            return unavailable_result(self.name, location, "预报未覆盖请求日期(最多 7 天)")
        return WeatherResult(
            location_name=str(city.get("name") or location),
            country=str(city["country"]) if city.get("country") else None,
            days=parsed,
            provider=self.name,
        )


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
