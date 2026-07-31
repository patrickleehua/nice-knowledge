"""redis 缓存封装单测(迁移自 TF):降级熔断全 mock,不碰真 redis。"""

import pytest

import nicekit.core.cache as cache


class BoomClient:
    """任何操作都抛异常的 redis 替身。"""

    def __init__(self):
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        raise OSError("redis down")

    async def set(self, *a, **k):
        self.calls += 1
        raise OSError("redis down")

    async def incr(self, key):
        self.calls += 1
        raise OSError("redis down")


class MemoryClient:
    """内存字典 redis 替身(get/set/delete/incr/expire)。"""

    def __init__(self):
        self.data: dict = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key, ttl):
        pass


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch):
    monkeypatch.setattr(cache, "_down_until", 0.0)


async def test_roundtrip_with_memory_client(monkeypatch) -> None:
    client = MemoryClient()
    monkeypatch.setattr(cache, "_get_client", lambda: client)
    await cache.set_json("k", {"a": 1, "中文": "值"}, ttl_seconds=60)
    assert await cache.get_json("k") == {"a": 1, "中文": "值"}
    await cache.delete("k")
    assert await cache.get_json("k") is None


async def test_incr_window(monkeypatch) -> None:
    client = MemoryClient()
    monkeypatch.setattr(cache, "_get_client", lambda: client)
    assert await cache.incr_window("rl:x:1", 90) == 1
    assert await cache.incr_window("rl:x:1", 90) == 2


async def test_failure_trips_breaker_and_skips_redis(monkeypatch) -> None:
    client = BoomClient()
    monkeypatch.setattr(cache, "_get_client", lambda: client)
    # 第一次失败:返回 None 并熔断
    assert await cache.get_json("k") is None
    assert client.calls == 1
    # 熔断窗口内:不再碰 redis,直接返回 None / 静默
    assert await cache.get_json("k") is None
    await cache.set_json("k", 1, 60)
    assert await cache.incr_window("rl:x:1", 90) is None
    assert client.calls == 1  # 后续调用全部短路


async def test_breaker_recovers_after_cooldown(monkeypatch) -> None:
    good = MemoryClient()
    monkeypatch.setattr(cache, "_get_client", lambda: good)
    monkeypatch.setattr(cache, "_down_until", 0.0)  # 冷却已过
    await cache.set_json("k2", "v", 60)
    assert await cache.get_json("k2") == "v"


async def test_namespace_prefix_applied(monkeypatch) -> None:
    """SDK 化新增:cache_namespace 非空时 key 加前缀,空时保持 TF 原行为。"""
    client = MemoryClient()
    monkeypatch.setattr(cache, "_get_client", lambda: client)

    class _NsSettings:
        cache_namespace = "tenant-a"

    monkeypatch.setattr(cache, "get_settings", lambda: _NsSettings())
    await cache.set_json("k", "v", 60)
    assert "tenant-a:k" in client.data
    assert await cache.get_json("k") == "v"
    await cache.delete("k")
    assert "tenant-a:k" not in client.data
