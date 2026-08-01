"""Celery 装配(MIGRATION-PLAN §5.7):broker/backend + worker 心跳信号 + beat 目录。

启动:
- worker:``uv run celery -A nicekit.runtime.celery_app worker --pool=solo -l info``
  (Windows 必须 ``--pool=solo``;Linux 生产默认 prefork)
- beat:``uv run celery -A nicekit.runtime.celery_app beat -l info``

beat schedule 由 ``operations.schedules`` 的**注册表**动态构建:宿主在自己的
celery 入口里 ``register_system_schedule(...)`` 后再 import 本模块即可加入自定义
周期任务;``task_dispatch_mode != "celery"`` 时 schedule 为空(inline 模式由 API
lifespan 的常驻循环兜底,两边同时跑会让同一批任务被抢两次)。
"""

from celery import Celery
from celery.signals import heartbeat_sent, worker_shutdown

from nicekit.core.config import get_settings
from nicekit.operations.runtime import runtime_instance_id
from nicekit.operations.schedules import build_celery_beat_schedule
from nicekit.operations.worker_heartbeat import (
    ensure_worker_heartbeat_writer,
    stop_worker_heartbeat_writer,
)

settings = get_settings()

celery_app = Celery(
    "nicekit",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["nicekit.runtime.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)


@heartbeat_sent.connect
def _start_worker_heartbeat_writer(**_kwargs) -> None:
    # heartbeat_sent originates from Celery's worker Heart service. The signal
    # only ensures one daemon writer exists; database I/O never blocks the
    # Celery heartbeat thread.
    ensure_worker_heartbeat_writer()


@worker_shutdown.connect
def _stop_worker_heartbeat_writer(**_kwargs) -> None:
    stop_worker_heartbeat_writer()


beat_schedule = build_celery_beat_schedule(
    settings,
    beat_instance=runtime_instance_id("beat"),
)
if beat_schedule:
    celery_app.conf.beat_schedule = beat_schedule


__all__ = ["celery_app"]
