"""runtime/dispatch.py:任务注册表与 inline/celery 双模式派发语义(§5.7)。

覆盖 TF services/dispatch.py 的三条核心裁决:
1. celery 派发成功 → task_id 落回 record.celery_task_id(reliability 据此不误杀);
2. broker 异常 → 有 background 就回退 BackgroundTasks(可用性优先);
3. broker 异常 + 无 background(独立 sweep)→ 返回 False,保留 QUEUED 等下轮。
"""

from uuid import uuid4

import pytest
from fastapi import BackgroundTasks

from nicekit.core.config import get_settings
from nicekit.runtime import dispatch as dispatch_module
from nicekit.runtime.dispatch import (
    KB_INGEST_TASK,
    dispatch,
    dispatch_kb_ingest_run,
    get_task,
    register_task,
    registered_tasks,
    reset_tasks,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    yield
    reset_tasks()


@pytest.fixture
def dispatch_mode(monkeypatch):
    """按需切换 task_dispatch_mode(get_settings 带 lru_cache,改实例字段即可)。"""

    def _set(mode: str) -> None:
        monkeypatch.setattr(get_settings(), "task_dispatch_mode", mode, raising=False)

    return _set


class _Record:
    def __init__(self) -> None:
        self.celery_task_id: str | None = None


class _Session:
    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def test_builtin_tasks_registered() -> None:
    names = set(registered_tasks())
    assert {"kb.ingest_document", "kb.reextract_document", "kb.reindex_embeddings"} <= names
    assert get_task(KB_INGEST_TASK).celery_name == KB_INGEST_TASK


def test_register_task_overrides_by_name() -> None:
    async def _noop(*_args, **_kwargs) -> None:
        return None

    register_task("demo.task", "demo.celery", _noop)
    assert get_task("demo.task").celery_name == "demo.celery"
    register_task("demo.task", "demo.celery.v2", _noop)
    assert get_task("demo.task").celery_name == "demo.celery.v2"


def test_unknown_task_raises() -> None:
    with pytest.raises(KeyError):
        get_task("nope")


async def test_inline_mode_uses_background_tasks(dispatch_mode) -> None:
    dispatch_mode("inline")
    seen: list = []

    async def _fn(run_id, org_id, **kwargs) -> None:
        seen.append((run_id, org_id, kwargs))

    register_task("demo.inline", "demo.inline", _fn, inline_kwargs=lambda: {"flag": True})
    background = BackgroundTasks()
    run_id, org_id = uuid4(), uuid4()

    assert await dispatch("demo.inline", run_id, org_id, background=background) is True
    await background()
    assert seen == [(run_id, org_id, {"flag": True})]


async def test_inline_mode_without_background_runs_detached(dispatch_mode) -> None:
    dispatch_mode("inline")
    done: list = []

    async def _fn(value) -> None:
        done.append(value)

    register_task("demo.detached", "demo.detached", _fn)
    assert await dispatch("demo.detached", "x") is True
    # 让出一次事件循环,detached task 得以执行
    import asyncio

    await asyncio.sleep(0)
    assert done == ["x"]


async def test_celery_success_records_task_id(monkeypatch, dispatch_mode) -> None:
    dispatch_mode("celery")
    monkeypatch.setattr(dispatch_module, "_try_celery", lambda name, args: "celery-42")

    async def _fn(*_args, **_kwargs) -> None:  # 不该被调用
        raise AssertionError("celery 成功时不应走 inline")

    register_task("demo.celery", "demo.celery", _fn)
    record, session = _Record(), _Session()
    run_id = uuid4()

    assert (
        await dispatch("demo.celery", run_id, record=record, session=session) is True
    )
    assert record.celery_task_id == "celery-42"
    assert session.commits == 1


async def test_celery_serializes_uuid_args(monkeypatch, dispatch_mode) -> None:
    dispatch_mode("celery")
    captured: list = []

    def _fake(name, args):
        captured.append((name, args))
        return "id"

    monkeypatch.setattr(dispatch_module, "_try_celery", _fake)

    async def _fn(*_args, **_kwargs) -> None:
        return None

    register_task("demo.args", "demo.args", _fn)
    run_id, org_id = uuid4(), uuid4()
    await dispatch("demo.args", run_id, org_id, None)
    assert captured == [("demo.args", [str(run_id), str(org_id), None])]


async def test_broker_failure_falls_back_to_background(monkeypatch, dispatch_mode) -> None:
    dispatch_mode("celery")
    monkeypatch.setattr(dispatch_module, "_try_celery", lambda name, args: None)
    seen: list = []

    async def _fn(value) -> None:
        seen.append(value)

    register_task("demo.fallback", "demo.fallback", _fn)
    background = BackgroundTasks()
    assert await dispatch("demo.fallback", "v", background=background) is True
    await background()
    assert seen == ["v"]


async def test_broker_failure_without_background_returns_false(
    monkeypatch, dispatch_mode
) -> None:
    """独立 sweep 上下文:保留 QUEUED 等下轮重派,好过在随时会退出的进程里起裸协程。"""
    dispatch_mode("celery")
    monkeypatch.setattr(dispatch_module, "_try_celery", lambda name, args: None)

    async def _fn(*_args, **_kwargs) -> None:
        raise AssertionError("不应执行")

    register_task("demo.noexec", "demo.noexec", _fn)
    assert await dispatch("demo.noexec", "v") is False


async def test_kb_ingest_entrypoint_matches_tf_semantics(
    monkeypatch, dispatch_mode
) -> None:
    dispatch_mode("celery")
    monkeypatch.setattr(dispatch_module, "_try_celery", lambda name, args: None)
    # celery 失败 + 无 background → False(与 TF dispatch_kb_ingest_run 一致)
    assert await dispatch_kb_ingest_run(uuid4(), uuid4()) is False
