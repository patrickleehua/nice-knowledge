"""结构化日志(prod-readiness-1 D1):stdlib JSON 输出,零新依赖。

- setup_logging():进程级一次性配置,API(lifespan)与 celery worker(tasks.py)共用;
- request_id_var:contextvar,RequestLogMiddleware 生成并注入,贯穿请求内所有日志;
- 业务代码照常 logging.getLogger(__name__),extra 字典的键值会原样进结构化字段
  (如 logger.info("回收完成", extra={"org_id": ..., "count": 3}))。
"""

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nicekit.core.config import get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_access_logger = logging.getLogger("nicekit.access")

# LogRecord 内建属性;之外的字段视为 extra 透传进结构化输出
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


def _extras(record: logging.LogRecord) -> dict:
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _RESERVED and not k.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        req_id = request_id_var.get()
        if req_id:
            payload["request_id"] = req_id
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """开发态可读输出:HH:MM:SS 级别 logger: message [k=v ...]"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        extras = _extras(record)
        req_id = request_id_var.get()
        if req_id:
            extras["request_id"] = req_id
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else ConsoleFormatter()
    )
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.handlers = [handler]
    # 访问日志由 RequestLogMiddleware 记,关掉 uvicorn 的避免双写;
    # uvicorn/celery 自身日志剥掉私有 handler 走 root,全进程统一格式
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error", "celery"):
        upstream = logging.getLogger(name)
        upstream.handlers = []
        upstream.propagate = True


class RequestLogMiddleware:
    """纯 ASGI 实现(不用 BaseHTTPMiddleware,避免 SSE 流被缓冲):
    生成 request_id → 响应头 X-Request-ID → 请求完成后记一行访问日志。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req_id = uuid.uuid4().hex[:16]
        token = request_id_var.set(req_id)
        status_code = 500  # 未发出响应就异常时按 500 记
        started = time.monotonic()

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = req_id
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = scope.get("path", "")
            if path != "/api/v1/health":  # 健康检查轮询不刷屏
                client = scope.get("client")
                _access_logger.info(
                    "%s %s %s",
                    scope.get("method", "-"),
                    path,
                    status_code,
                    extra={
                        "status": status_code,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "client": client[0] if client else "-",
                    },
                )
            request_id_var.reset(token)
