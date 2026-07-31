"""指标基建骨架单测:registry 单例、心跳 Gauge、Prometheus 文本导出与端点。

KB_* 业务指标不在 core 骨架内(由 kb 子包自行注册),故只测骨架。
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from nicekit.core import metrics


def _sample(name: str, labels: dict | None = None) -> float:
    return metrics.registry.get_sample_value(name, labels or {}) or 0.0


def test_get_registry_returns_module_registry() -> None:
    assert metrics.get_registry() is metrics.registry


def test_heartbeat_gauge_set() -> None:
    metrics.SERVICE_HEARTBEAT_AGE.labels(role="api").set(7)
    assert _sample("service_heartbeat_age_seconds", {"role": "api"}) == 7


def test_render_metrics_contains_heartbeat() -> None:
    metrics.SERVICE_HEARTBEAT_AGE.labels(role="worker").set(1)
    output = metrics.render_metrics().decode("utf-8")
    assert "service_heartbeat_age_seconds" in output


def test_subpackage_can_register_into_shared_registry() -> None:
    """其他子包经 get_registry() 注册的指标要出现在同一份导出里。"""
    from prometheus_client import Counter

    counter = Counter(
        "nicekit_test_registered_total",
        "测试注册用计数器",
        registry=metrics.get_registry(),
    )
    counter.inc()
    assert "nicekit_test_registered_total" in metrics.render_metrics().decode("utf-8")


def test_metrics_endpoint_returns_200_prometheus_text() -> None:
    app = FastAPI()
    app.add_api_route("/metrics", metrics.metrics_endpoint, methods=["GET"])
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "service_heartbeat_age_seconds" in response.text
