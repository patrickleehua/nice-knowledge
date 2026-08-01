"""天气多源单测(无外部依赖,httpx.MockTransport 注入):

三 provider 正常解析 / 错误降级 unavailable / weatherapi 免费档 3 天截断 /
service 区域路由(中文→和风优先)/ 全失败 unavailable / 健康度降级排序 / 摘要压缩。
"""

from datetime import date

import httpx

from nicekit.capabilities.weather.base import WeatherDay, WeatherProvider, WeatherResult
from nicekit.capabilities.weather.open_meteo import OpenMeteoProvider, wmo_to_text
from nicekit.capabilities.weather.qweather import QWeatherProvider
from nicekit.capabilities.weather.service import WeatherService, summarize_weather
from nicekit.capabilities.weather.weatherapi import WeatherApiProvider

# ---- canned payloads

_WEATHERAPI_PAYLOAD = {
    "location": {"name": "墨尔本", "country": "澳大利亚"},
    "forecast": {
        "forecastday": [  # 免费档:请求 7 天也只回 3 天
            {"date": "2026-07-20", "day": {"condition": {"text": "多云"}, "mintemp_c": 8.2,
                                           "maxtemp_c": 15.1, "totalprecip_mm": 0.0,
                                           "maxwind_kph": 24.5}},
            {"date": "2026-07-21", "day": {"condition": {"text": "小雨"}, "mintemp_c": 9.0,
                                           "maxtemp_c": 14.0, "totalprecip_mm": 6.4,
                                           "maxwind_kph": 30.0}},
            {"date": "2026-07-22", "day": {"condition": {"text": "晴"}, "mintemp_c": 7.5,
                                           "maxtemp_c": 16.0, "totalprecip_mm": 0.0,
                                           "maxwind_kph": 18.0}},
        ]
    },
}

_QWEATHER_GEO = {
    "code": "200",
    "location": [{"id": "101010100", "name": "北京", "country": "中国"}],
}
_QWEATHER_7D = {
    "code": "200",
    "daily": [
        {"fxDate": "2026-07-20", "textDay": "多云", "tempMin": "22", "tempMax": "31",
         "precip": "0.0", "windDirDay": "东南风", "windScaleDay": "3-4"},
        {"fxDate": "2026-07-21", "textDay": "雷阵雨", "tempMin": "23", "tempMax": "29",
         "precip": "12.3", "windDirDay": "南风", "windScaleDay": "4-5"},
    ],
}

_OPEN_METEO_GEO = {
    "results": [{"latitude": 48.85, "longitude": 2.35, "name": "巴黎", "country": "法国"}]
}
_OPEN_METEO_FORECAST = {
    "daily": {
        "time": ["2026-07-20", "2026-07-21"],
        "weather_code": [61, 424242],  # 61=小雨;424242 未知码
        "temperature_2m_max": [25.0, 27.1],
        "temperature_2m_min": [15.2, 16.0],
        "precipitation_sum": [4.2, 0.0],
    }
}


def _json_transport(payload: dict, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


# ---- weatherapi


async def test_weatherapi_parses_and_truncates_to_returned_days():
    provider = WeatherApiProvider("key", transport=_json_transport(_WEATHERAPI_PAYLOAD))
    result = await provider.forecast("墨尔本", days=7)  # 请求 7 天,免费档只回 3 天
    assert not result.unavailable
    assert result.provider == "weatherapi"
    assert result.location_name == "墨尔本" and result.country == "澳大利亚"
    assert len(result.days) == 3  # 按返回的实际天数如实给
    first = result.days[0]
    assert first.date == "2026-07-20" and first.condition == "多云"
    assert first.temp_min_c == 8.2 and first.temp_max_c == 15.1
    assert first.precip_mm == 0.0 and first.wind == "最大风速 24.5 km/h"
    assert first.source == "weatherapi"


async def test_weatherapi_http_error_degrades_to_unavailable():
    provider = WeatherApiProvider("key", transport=_json_transport({"error": "x"}, status=401))
    result = await provider.forecast("Paris")
    assert result.unavailable and result.reason
    assert not result.days


async def test_weatherapi_start_filters_and_empty_coverage_is_unavailable():
    provider = WeatherApiProvider("key", transport=_json_transport(_WEATHERAPI_PAYLOAD))
    filtered = await provider.forecast("墨尔本", start=date(2026, 7, 22), days=7)
    assert [d.date for d in filtered.days] == ["2026-07-22"]
    beyond = await provider.forecast("墨尔本", start=date(2026, 8, 1), days=7)
    assert beyond.unavailable  # 免费档覆盖不到请求日期 → 如实降级


async def test_weatherapi_without_key_is_not_available():
    assert not WeatherApiProvider("").available()
    assert WeatherApiProvider("key").available()


# ---- qweather


def _qweather_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-QW-Api-Key") == "qw-key"  # 认证走请求头
        if request.url.path == "/geo/v2/city/lookup":
            return httpx.Response(200, json=_QWEATHER_GEO)
        assert request.url.path == "/v7/weather/7d"
        assert request.url.params["location"] == "101010100"  # 用 GeoAPI 的 id 查预报
        return httpx.Response(200, json=_QWEATHER_7D)

    return httpx.MockTransport(handler)


async def test_qweather_two_step_lookup_and_parse():
    provider = QWeatherProvider("qw-key", "abc.qweatherapi.com", transport=_qweather_transport())
    result = await provider.forecast("北京")
    assert not result.unavailable
    assert result.provider == "qweather"
    assert result.location_name == "北京" and result.country == "中国"
    assert len(result.days) == 2
    assert result.days[0].condition == "多云" and result.days[0].wind == "东南风3-4级"
    assert result.days[1].temp_min_c == 23.0 and result.days[1].precip_mm == 12.3


async def test_qweather_error_code_degrades_to_unavailable():
    provider = QWeatherProvider(
        "qw-key", "abc.qweatherapi.com",
        transport=_json_transport({"code": "402", "message": "配额用尽"}),
    )
    result = await provider.forecast("北京")
    assert result.unavailable and "402" in (result.reason or "")


async def test_qweather_requires_host_and_key():
    assert not QWeatherProvider("", "abc.qweatherapi.com").available()
    assert not QWeatherProvider("qw-key", "").available()  # 未配置专属 host 不可用
    assert QWeatherProvider("qw-key", "abc.qweatherapi.com").available()


# ---- open-meteo


def _open_meteo_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=_OPEN_METEO_GEO)
        assert request.url.host == "api.open-meteo.com"
        return httpx.Response(200, json=_OPEN_METEO_FORECAST)

    return httpx.MockTransport(handler)


async def test_open_meteo_geocode_forecast_and_wmo_mapping():
    provider = OpenMeteoProvider(transport=_open_meteo_transport())
    result = await provider.forecast("巴黎", days=7)
    assert not result.unavailable
    assert result.provider == "open_meteo" and result.location_name == "巴黎"
    assert [d.condition for d in result.days] == ["小雨", "未知"]  # WMO 码映射,未知码兜底
    assert result.days[0].temp_max_c == 25.0 and result.days[0].precip_mm == 4.2
    assert OpenMeteoProvider().available()  # 免 key 恒可用


async def test_open_meteo_geocode_miss_degrades_to_unavailable():
    provider = OpenMeteoProvider(transport=_json_transport({"results": []}))
    result = await provider.forecast("不存在的城市xyz")
    assert result.unavailable and "地理编码" in (result.reason or "")


def test_wmo_to_text_unknown_code():
    assert wmo_to_text(0) == "晴" and wmo_to_text(95) == "雷阵雨"
    assert wmo_to_text(None) == "未知" and wmo_to_text("abc") == "未知"


# ---- service 路由 / 降级 / 健康度


class _FakeProvider(WeatherProvider):
    def __init__(self, name: str, *, ok: bool = True, enabled: bool = True) -> None:
        self.name = name
        self._ok = ok
        self._enabled = enabled
        self.calls = 0

    def available(self) -> bool:
        return self._enabled

    async def forecast(
        self, location: str, *, start: date | None = None, days: int = 7
    ) -> WeatherResult:
        self.calls += 1
        if not self._ok:
            return WeatherResult(
                location_name=location, country=None,
                provider=self.name, unavailable=True, reason="boom",
            )
        return WeatherResult(
            location_name=location,
            country=None,
            days=[WeatherDay(date="2026-07-20", condition="晴", temp_min_c=10.0,
                             temp_max_c=20.0, source=self.name)],
            provider=self.name,
        )


def _fakes(**flags) -> dict[str, _FakeProvider]:
    return {
        name: _FakeProvider(name, **flags.get(name, {}))
        for name in ("weatherapi", "qweather", "open_meteo")
    }


async def test_service_routes_cjk_query_to_qweather_first():
    fakes = _fakes()
    service = WeatherService(list(fakes.values()))
    assert (await service.forecast("北京")).provider == "qweather"  # 含中文 → 和风优先
    assert (await service.forecast("Melbourne")).provider == "weatherapi"  # 否则 weatherapi


async def test_service_skips_unavailable_credentials():
    fakes = _fakes(qweather={"enabled": False})
    service = WeatherService(list(fakes.values()))
    assert (await service.forecast("北京")).provider == "weatherapi"
    assert fakes["qweather"].calls == 0  # 凭证未配置的直接跳过,不发请求


async def test_service_degrades_to_next_provider_on_failure():
    fakes = _fakes(qweather={"ok": False})
    service = WeatherService(list(fakes.values()))
    result = await service.forecast("北京")
    assert result.provider == "weatherapi"
    assert fakes["qweather"].calls == 1  # 先试和风,失败降级


async def test_service_all_failed_returns_unavailable():
    fakes = _fakes(
        weatherapi={"ok": False}, qweather={"ok": False}, open_meteo={"ok": False}
    )
    service = WeatherService(list(fakes.values()))
    result = await service.forecast("北京")
    assert result.unavailable and result.provider == "none"
    assert "boom" in (result.reason or "")


async def test_service_health_demotes_flaky_provider_to_tail():
    fakes = _fakes(qweather={"ok": False})
    service = WeatherService(list(fakes.values()))
    for _ in range(3):  # 连续失败 3 次触发 10 分钟降级
        await service.forecast("北京")
    ordered = [p.name for p in service._ordered("北京")]
    assert ordered == ["weatherapi", "open_meteo", "qweather"]  # 降到队尾
    result = await service.forecast("北京")
    assert result.provider == "weatherapi"
    assert fakes["qweather"].calls == 3  # 降级期内前面已成功,不再打故障源


async def test_service_success_resets_failure_counter():
    fakes = _fakes()
    service = WeatherService(list(fakes.values()))
    service._failures["qweather"] = 2
    await service.forecast("北京")  # 成功 → 连续失败清零,不会被降级
    assert service._failures["qweather"] == 0


# ---- 摘要压缩(行程节点注入用)


def test_summarize_weather_compact_chinese():
    result = WeatherResult(
        location_name="墨尔本",
        country="澳大利亚",
        days=[
            WeatherDay(date="2026-07-20", condition="多云", temp_min_c=8.2, temp_max_c=15.1,
                       precip_mm=0.0, source="weatherapi"),
            WeatherDay(date="2026-07-21", condition="多云", temp_min_c=9.0, temp_max_c=14.0,
                       precip_mm=0.2, source="weatherapi"),
            WeatherDay(date="2026-07-22", condition="小雨", temp_min_c=7.5, temp_max_c=13.0,
                       precip_mm=6.4, source="weatherapi"),
        ],
        provider="weatherapi",
    )
    text = summarize_weather(result)
    assert "墨尔本 07-20~07-22" in text
    assert "多云为主" in text and "8~15°C" in text
    assert "07-22 有雨" in text and "weatherapi" in text
