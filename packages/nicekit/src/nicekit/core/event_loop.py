import asyncio
import sys


def use_selector_event_loop_on_windows() -> None:
    """psycopg async 不支持 Windows 默认的 ProactorEventLoop。
    仅影响 Windows 本地开发;Linux(生产/CI)是 no-op。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
