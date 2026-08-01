"""统一任务派发注册表(MIGRATION-PLAN §5.7):inline / celery 双模式。

搬自 TF backend/app/services/dispatch.py,SDK 化改造为**任务注册表**:

- ``register_task(name, celery_name, inline_fn)`` 登记一个可派发任务;
- ``dispatch(name, *args, background=..., session=..., record=...)`` 按
  ``settings.task_dispatch_mode`` 决策:
  - ``celery``:``send_task`` 成功后把 ``task_id`` 落回 ``record.celery_task_id``
    (reliability 启动恢复据此区分执行主体,非空不误杀);
  - broker 异常:log warning 后**回退 BackgroundTasks**——可用性优先,任务不因
    redis 抖动而丢;
  - 无 background(独立 sweep 上下文):起进程内 asyncio.Task 兜底,
    调用方传 ``background=None`` 且 ``allow_detached=False`` 时返回 False,
    让调用方保留 QUEUED 等下轮重派。

SDK 自带三个具体入口:``dispatch_kb_ingest_run`` / ``dispatch_kb_reextract_run``
/ ``dispatch_embedding_campaign``;宿主的业务任务自行 ``register_task`` 后调
``dispatch()``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory

logger = logging.getLogger(__name__)

#: 进程内后台任务的强引用集合(防止被 GC 提前回收)
_detached_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一个可派发任务:逻辑名 → celery 任务名 + inline 协程工厂。

    ``inline_fn`` 接受与 celery 相同的位置参数,额外的执行期依赖
    (session_factory / llm / embedder)由 ``inline_kwargs`` 工厂在派发时解析,
    保证 import 期不拖动 KB / LLM 依赖链。
    """

    name: str
    celery_name: str
    inline_fn: Callable[..., Awaitable[Any]]
    inline_kwargs: Callable[[], dict[str, Any]] | None = None

    def resolved_kwargs(self) -> dict[str, Any]:
        return dict(self.inline_kwargs()) if self.inline_kwargs is not None else {}


_registry: dict[str, TaskSpec] = {}


def register_task(
    name: str,
    celery_name: str,
    inline_fn: Callable[..., Awaitable[Any]],
    *,
    inline_kwargs: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """登记(或按 name 覆盖)一个可派发任务。装配期 / 宿主 import 期调用。"""
    _registry[name] = TaskSpec(
        name=name,
        celery_name=celery_name,
        inline_fn=inline_fn,
        inline_kwargs=inline_kwargs,
    )


def registered_tasks() -> dict[str, TaskSpec]:
    """当前注册表快照(只读用途)。"""
    return dict(_registry)


def get_task(name: str) -> TaskSpec:
    spec = _registry.get(name)
    if spec is None:
        raise KeyError(f"未注册的派发任务:{name!r}")
    return spec


def reset_tasks() -> None:
    """清空并恢复 SDK 内置任务(测试 / 重新装配用)。"""
    _registry.clear()
    _register_builtin_tasks()


def _try_celery(task_name: str, args: list) -> str | None:
    """send_task 成功返回 task_id;broker 不可达等异常返回 None(调用方回退 inline)。"""
    try:
        from nicekit.runtime.celery_app import celery_app

        result = celery_app.send_task(task_name, args=args)
        return result.id
    except Exception as exc:  # noqa: BLE001 - broker 抖动不该让 API 500
        logger.warning(
            "celery 派发失败,回退 BackgroundTasks",
            extra={"task": task_name, "error": str(exc)},
        )
        return None


async def dispatch(
    name: str,
    *args: Any,
    background: BackgroundTasks | None = None,
    session: AsyncSession | None = None,
    record: Any | None = None,
    allow_detached: bool = True,
    detach_on_broker_failure: bool = False,
) -> bool:
    """派发一个已注册任务。返回 True 表示"已有执行主体"。

    - ``record`` 非空且走 celery 成功时,把 ``celery_task_id`` 落库(需同时传
      ``session``,由本函数 commit);
    - inline 模式:优先 ``background``(FastAPI 请求生命周期),无则按
      ``allow_detached`` 起进程内 asyncio 任务;
    - celery 模式 broker 异常:有 ``background`` 就回退它;没有(独立 sweep
      上下文)默认返回 False —— 保留 QUEUED 等下轮重派,好过在一个随时可能
      退出的进程里起裸协程(TF services/dispatch.py 文件头裁决)。需要覆盖该
      语义时显式传 ``detach_on_broker_failure=True``。
    """
    spec = get_task(name)
    if get_settings().task_dispatch_mode == "celery":
        task_id = _try_celery(spec.celery_name, [_celery_arg(arg) for arg in args])
        if task_id:
            if record is not None and session is not None:
                record.celery_task_id = task_id
                session.add(record)
                await session.commit()
            return True
        if background is None and not detach_on_broker_failure:
            return False

    kwargs = spec.resolved_kwargs()
    if background is not None:
        background.add_task(spec.inline_fn, *args, **kwargs)
        return True
    if not allow_detached:
        return False
    task = asyncio.create_task(spec.inline_fn(*args, **kwargs))
    _detached_tasks.add(task)
    task.add_done_callback(_detached_tasks.discard)
    return True


def _celery_arg(value: Any) -> Any:
    """celery 只收 JSON:UUID / 非基础类型统一 str 化(None 保持 None)。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# SDK 内置任务:KB 摄入 / 重抽取 / 嵌入迁移
# ---------------------------------------------------------------------------

KB_INGEST_TASK = "kb.ingest_document"
KB_REEXTRACT_TASK = "kb.reextract_document"
KB_EMBEDDING_CAMPAIGN_TASK = "kb.reindex_embeddings"


def _ingest_kwargs() -> dict[str, Any]:
    from nicekit.kb.search import default_embedder
    from nicekit.llm.service import get_llm_service

    return {
        "session_factory": get_session_factory(),
        "llm": get_llm_service(),
        "embedder": default_embedder(),
    }


def _reextract_kwargs() -> dict[str, Any]:
    from nicekit.llm.service import get_llm_service

    return {
        "session_factory": get_session_factory(),
        "llm": get_llm_service(),
    }


def _embedding_campaign_kwargs() -> dict[str, Any]:
    settings = get_settings()
    return {
        "session_factory": get_session_factory(),
        "batch_size": settings.embedding_reindex_batch_size,
        "lease_seconds": settings.embedding_reindex_lease_seconds,
    }


async def _ingest_document(run_id: UUID, org_id: UUID, **kwargs: Any) -> Any:
    from nicekit.kb.ingestion import ingest_document

    return await ingest_document(run_id, org_id, **kwargs)


async def _reextract_document(run_id: UUID, org_id: UUID, **kwargs: Any) -> Any:
    from nicekit.kb.ingestion import reextract_document

    return await reextract_document(run_id, org_id, **kwargs)


async def _execute_embedding_campaign(campaign_id: UUID, **kwargs: Any) -> Any:
    from nicekit.kb.embedding_reindex import execute_embedding_campaign

    return await execute_embedding_campaign(campaign_id, **kwargs)


def _register_builtin_tasks() -> None:
    register_task(
        KB_INGEST_TASK,
        KB_INGEST_TASK,
        _ingest_document,
        inline_kwargs=_ingest_kwargs,
    )
    register_task(
        KB_REEXTRACT_TASK,
        KB_REEXTRACT_TASK,
        _reextract_document,
        inline_kwargs=_reextract_kwargs,
    )
    register_task(
        KB_EMBEDDING_CAMPAIGN_TASK,
        KB_EMBEDDING_CAMPAIGN_TASK,
        _execute_embedding_campaign,
        inline_kwargs=_embedding_campaign_kwargs,
    )


_register_builtin_tasks()


async def dispatch_kb_ingest_run(
    run_id: UUID, org_id: UUID, background: BackgroundTasks | None = None
) -> bool:
    """Dispatch a persisted root run; sweep recovery uses the same entry point."""
    return await dispatch(KB_INGEST_TASK, run_id, org_id, background=background)


async def dispatch_kb_reextract_run(
    run_id: UUID, org_id: UUID, background: BackgroundTasks | None = None
) -> bool:
    """Dispatch a durable typed re-extraction root run."""
    return await dispatch(KB_REEXTRACT_TASK, run_id, org_id, background=background)


async def dispatch_embedding_campaign(
    campaign_id: UUID,
    background: BackgroundTasks | None = None,
) -> bool:
    """Dispatch a durable global embedding migration campaign."""
    return await dispatch(KB_EMBEDDING_CAMPAIGN_TASK, campaign_id, background=background)


__all__ = [
    "KB_EMBEDDING_CAMPAIGN_TASK",
    "KB_INGEST_TASK",
    "KB_REEXTRACT_TASK",
    "TaskSpec",
    "dispatch",
    "dispatch_embedding_campaign",
    "dispatch_kb_ingest_run",
    "dispatch_kb_reextract_run",
    "get_task",
    "register_task",
    "registered_tasks",
    "reset_tasks",
]
