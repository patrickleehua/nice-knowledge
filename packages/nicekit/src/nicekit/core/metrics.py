"""Prometheus 指标基建骨架(监控双轨之一:指标暴露,供未来抓取)。

原则:埋点只做可见性,绝不改变业务控制流——失败仍按既有路径静默降级,
指标只是让降级次数/积压水位变得可观测。使用独立 CollectorRegistry,
避免测试环境模块重载时向默认全局 registry 重复注册报错。

SDK 化后本模块只保留骨架:registry 与导出端点 + 服务心跳 Gauge。
各子包(kb/llm/...)的业务指标由子包自行通过 get_registry() 注册。
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from starlette.responses import Response

registry = CollectorRegistry()


def get_registry() -> CollectorRegistry:
    """供其他子包注册指标的统一入口(与模块级 registry 同一实例)。"""
    return registry


SERVICE_HEARTBEAT_AGE = Gauge(
    "service_heartbeat_age_seconds",
    "最新 API/worker/beat heartbeat 年龄秒数",
    ["role"],
    registry=registry,
)


def render_metrics() -> bytes:
    """Prometheus 文本格式导出(/metrics 端点正文)。"""
    return generate_latest(registry)


def metrics_endpoint() -> Response:
    """GET /metrics:无鉴权只读导出,不含任何租户业务数据明文。"""
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)
