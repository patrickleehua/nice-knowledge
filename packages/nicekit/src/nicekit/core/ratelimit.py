"""API 限流(prod-readiness-1 D5):60s 固定窗口。

key = Authorization 头 sha1(已认证按令牌区分)否则 client IP;
豁免路径(settings.rate_limit_exempt_paths,默认 /api/v1/health)与
OPTIONS(CORS 预检)豁免;超限 429 + Retry-After。
后端两种(排期项 4/4):memory(默认,单副本)/ redis(多副本共享计数,
INCR+EXPIRE;redis 不可达自动降级 memory,可用性优先)。
挂载在 CORSMiddleware 内侧,429 响应会带 CORS 头,浏览器可读。
"""

import hashlib
import json
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from nicekit.core import cache
from nicekit.core.config import get_settings


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        limit_per_minute: int | None = None,
        backend: str | None = None,
    ):
        self.app = app
        self._limit_override = limit_per_minute  # 测试注入用;None = 读 settings
        self._backend_override = backend
        self._counters: dict[str, tuple[int, int]] = {}  # key -> (窗口起点分钟, 计数)

    @property
    def limit(self) -> int:
        if self._limit_override is not None:
            return self._limit_override
        return get_settings().rate_limit_per_minute

    @property
    def backend(self) -> str:
        return self._backend_override or get_settings().rate_limit_backend

    def _client_key(self, scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                return hashlib.sha1(value).hexdigest()
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit
        if (
            limit <= 0
            or scope.get("method") == "OPTIONS"
            or scope.get("path") in get_settings().rate_limit_exempt_paths
        ):
            await self.app(scope, receive, send)
            return

        now = time.time()
        window = int(now // 60)
        key = self._client_key(scope)

        count: int | None = None
        if self.backend == "redis":
            # 窗口键 TTL 90s(> 窗口 60s,容许时钟毛边);失败降级进程内计数
            count = await cache.incr_window(f"rl:{key}:{window}", 90)
        if count is None:  # memory 后端,或 redis 不可达降级
            start, mem_count = self._counters.get(key, (window, 0))
            if start != window:
                start, mem_count = window, 0
            mem_count += 1
            self._counters[key] = (start, mem_count)
            if len(self._counters) > 10_000:  # 防止历史客户端 key 无限累积
                self._counters = {k: v for k, v in self._counters.items() if v[0] == window}
            count = mem_count

        if count > limit:
            retry_after = 60 - int(now % 60)
            body = json.dumps(
                {"detail": f"请求过于频繁(每分钟上限 {limit}),请 {retry_after}s 后再试"},
                ensure_ascii=False,
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(retry_after).encode()),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
