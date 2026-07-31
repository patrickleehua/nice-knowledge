"""轻量 redis JSON 缓存(排期项 4/4):不可达静默降级,可用性优先。

- get_json / set_json / delete:redis 异常一律吞掉记 warning,调用方拿 None
  直接回源查库——缓存永远只是加速,不是正确性依赖(同 celery 派发回退精神)。
- 熔断:连续失败后 _DOWN_COOLDOWN 秒内不再尝试连接,避免 redis 宕机时
  每个请求都白等 socket 超时。
- 共享 redis = 跨进程一致:admin 改 prompt/路由后 delete 键,API 与 worker
  同时失效(进程内缓存做不到这点)。
- key namespace(SDK 化新增):settings.cache_namespace 非空时所有 key 加
  "<ns>:" 前缀,多套部署共用一个 redis 时互不串键;默认空 = 与 TF 行为一致。
"""

import asyncio
import json
import logging
import time
from typing import Any
from weakref import WeakKeyDictionary

from redis import asyncio as aioredis

from nicekit.core.config import get_settings

logger = logging.getLogger(__name__)

# 按事件循环隔离客户端:redis 连接绑定创建时的 loop,跨 loop 复用会炸
# (celery worker 每任务 asyncio.run 新 loop;测试同理)——同 LLM 信号量方案
_clients: WeakKeyDictionary = WeakKeyDictionary()
_down_until: float = 0.0
_DOWN_COOLDOWN = 30.0  # 失败熔断窗口(秒)


def _get_client() -> aioredis.Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = _clients[loop] = aioredis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            decode_responses=True,
        )
    return client


def _ns(key: str) -> str:
    """可选命名空间前缀:cache_namespace 为空时原样返回。"""
    namespace = get_settings().cache_namespace
    return f"{namespace}:{key}" if namespace else key


def _available() -> bool:
    return time.monotonic() >= _down_until


def _mark_down(exc: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + _DOWN_COOLDOWN
    logger.warning("redis 缓存不可用,%ss 内直查库", _DOWN_COOLDOWN, extra={"error": str(exc)})


async def get_json(key: str) -> Any | None:
    if not _available():
        return None
    try:
        raw = await _get_client().get(_ns(key))
    except Exception as exc:
        _mark_down(exc)
        return None
    return json.loads(raw) if raw is not None else None


async def ping() -> bool:
    """Probe Redis directly for readiness without consulting the cache breaker.

    Readiness must reflect dependency recovery immediately; the cache breaker's
    cooldown intentionally remains scoped to best-effort application caching.
    Connection and command timeouts are bounded on the shared client.
    """
    return bool(await _get_client().ping())


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    if not _available():
        return
    try:
        await _get_client().set(_ns(key), json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
    except Exception as exc:
        _mark_down(exc)


async def delete(*keys: str) -> None:
    if not keys or not _available():
        return
    try:
        await _get_client().delete(*(_ns(key) for key in keys))
    except Exception as exc:
        _mark_down(exc)


async def incr_window(key: str, ttl_seconds: int) -> int | None:
    """固定窗口计数(限流 redis 后端用):INCR + 首次设 TTL。
    失败返回 None,调用方降级进程内计数。"""
    if not _available():
        return None
    try:
        client = _get_client()
        count = await client.incr(_ns(key))
        if count == 1:
            await client.expire(_ns(key), ttl_seconds)
        return int(count)
    except Exception as exc:
        _mark_down(exc)
        return None


async def delete_by_prefix(prefix: str) -> None:
    """SCAN 前缀删除(如平台级路由变更需失效全部 org 的解析缓存)。"""
    if not _available():
        return
    try:
        client = _get_client()
        keys = [key async for key in client.scan_iter(match=f"{_ns(prefix)}*", count=200)]
        if keys:
            await client.delete(*keys)
    except Exception as exc:
        _mark_down(exc)
