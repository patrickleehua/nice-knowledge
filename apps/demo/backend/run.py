"""开发启动入口:`uv run python run.py`

**Windows 下必须经此启动,不要直接 `uvicorn demo_backend.main:app`。**

原因(TF 现场踩过,这里原样复刻它的解法):uvicorn 0.50 在 win32 上**硬编码**
`ProactorEventLoop` 作为 loop factory(`uvicorn/loops/asyncio.py:asyncio_loop_factory`),
它会盖掉进程里已经设好的 `WindowsSelectorEventLoopPolicy`。而 psycopg 的 async
实现不支持 Proactor,后果是:HTTP 照常响应(/health 200),但 lifespan 里所有
数据库后台任务(心跳 / KB 出箱 / 各类扫描)与所有走 DB 的请求全部失败 ——
只有翻日志才看得见,现象极具迷惑性。

所以这里显式用 `asyncio.run(server.serve(), loop_factory=SelectorEventLoop)`。
Linux 生产直接 `uvicorn demo_backend.main:app` 即可。
"""

import asyncio
import os
import sys

import uvicorn
from nicekit.core.logging import setup_logging

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8020


def main() -> None:
    setup_logging()  # lifespan 之前先配好,启动早期日志也走统一格式
    config = uvicorn.Config(
        "demo_backend.main:app",
        host=os.getenv("DEMO_HOST", DEFAULT_HOST),
        # 端口被占时 uvicorn 直接退出,别以为"起来了";同机有别的服务就换 DEMO_PORT
        port=int(os.getenv("DEMO_PORT", DEFAULT_PORT)),
        log_config=None,  # 日志由 nicekit.core.logging 统一接管
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
