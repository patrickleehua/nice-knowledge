"""API 限流中间件单测(迁移自 TF):固定窗口计数 / 429 / 豁免。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.core.ratelimit import RateLimitMiddleware


def _make_app(limit: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit_per_minute=limit)

    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/ping")
    def ping() -> dict:
        return {"pong": True}

    return app


def test_under_limit_passes() -> None:
    client = TestClient(_make_app(limit=3))
    for _ in range(3):
        assert client.get("/api/v1/ping").status_code == 200


def test_over_limit_returns_429_with_retry_after() -> None:
    client = TestClient(_make_app(limit=2))
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 200
    resp = client.get("/api/v1/ping")
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers}
    assert "上限" in resp.json()["detail"]


def test_health_is_exempt() -> None:
    client = TestClient(_make_app(limit=1))
    # health 在默认豁免路径里:不计数、不限流,连打多次都 200
    for _ in range(5):
        assert client.get("/api/v1/health").status_code == 200
    # 普通端点仍受限:第 2 次即 429
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 429


def test_options_is_exempt() -> None:
    client = TestClient(_make_app(limit=1))
    for _ in range(5):
        # 无匹配路由的 OPTIONS 会 405,但关键是没被限流成 429
        assert client.options("/api/v1/ping").status_code != 429


def test_limit_zero_disables() -> None:
    client = TestClient(_make_app(limit=0))
    for _ in range(10):
        assert client.get("/api/v1/ping").status_code == 200
