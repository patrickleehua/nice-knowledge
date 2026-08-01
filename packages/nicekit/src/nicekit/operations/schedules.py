"""周期任务目录:Celery Beat 与运维诊断共用同一份清单。

SDK 化改造(MIGRATION-PLAN §5.7):TF 的 ``SYSTEM_SCHEDULES`` 元组改为**注册表**,
beat schedule 由注册项动态构建,宿主可用 :func:`register_system_schedule` 追加
自己的周期任务(业务任务如履约提醒不在 SDK 内)。

``interval_setting`` 指向 ``Settings`` 上的秒数字段:0 = 该周期任务关闭。
``inline_runner=True`` 表示 inline 派发模式下由 API 进程的 lifespan 常驻循环兜底。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SystemScheduleSpec:
    name: str
    label: str
    task: str
    interval_setting: str
    inline_runner: bool


_schedules: dict[str, SystemScheduleSpec] = {}


def register_system_schedule(spec: SystemScheduleSpec) -> None:
    """登记(或按 name 覆盖)一个周期任务。装配期 / 宿主 import 期调用。"""
    _schedules[spec.name] = spec


def system_schedules() -> tuple[SystemScheduleSpec, ...]:
    """当前注册表快照(注册顺序)。"""
    return tuple(_schedules.values())


def reset_system_schedules() -> None:
    """清空并恢复 SDK 内置项(测试 / 重新装配用)。"""
    _schedules.clear()
    for spec in _BUILTIN_SCHEDULES:
        _schedules[spec.name] = spec


_BUILTIN_SCHEDULES: tuple[SystemScheduleSpec, ...] = (
    SystemScheduleSpec(
        name="kb-consume-outbox",
        label="知识库出箱消费",
        task="kb.consume_outbox",
        interval_setting="kb_outbox_poll_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="reliability-sweep-stale-tasks",
        label="卡死任务回收",
        task="reliability.sweep_stale_tasks",
        interval_setting="stale_sweep_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="kb-reconcile-embedding-campaigns",
        label="嵌入迁移协调",
        task="kb.reconcile_embedding_campaigns",
        interval_setting="embedding_migration_poll_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="kb-ai-review-sweep",
        label="知识事实审核",
        task="kb.ai_review_sweep",
        interval_setting="kb_ai_review_sweep_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="kb-expiry-sweep",
        label="知识过期扫描",
        task="kb.expiry_sweep",
        interval_setting="kb_expiry_sweep_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="kb-health-sweep",
        label="知识健康巡检",
        task="kb.health_sweep",
        interval_setting="kb_health_sweep_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="icron-tick",
        label="Agent 定时任务调度",
        task="icron.tick",
        interval_setting="icron_tick_interval_seconds",
        inline_runner=True,
    ),
    SystemScheduleSpec(
        name="operations-provider-probes",
        label="模型能力周期探测",
        task="operations.provider_probes",
        interval_setting="operations_provider_probe_interval_seconds",
        inline_runner=False,
    ),
)

reset_system_schedules()


def build_celery_beat_schedule(settings, *, beat_instance: str) -> dict[str, dict]:
    """Build the executable Beat schedule from the same catalog the UI reads."""
    if settings.task_dispatch_mode != "celery":
        return {}

    schedule: dict[str, dict] = {
        "operations-beat-heartbeat": {
            "task": "operations.record_heartbeat",
            "schedule": float(settings.operations_heartbeat_interval_seconds),
            "kwargs": {
                "role": "beat",
                "instance": beat_instance,
            },
        }
    }
    for spec in system_schedules():
        interval = getattr(settings, spec.interval_setting, 0)
        if interval and interval > 0:
            schedule[spec.name] = {
                "task": spec.task,
                "schedule": float(interval),
            }
    return schedule


def collect_system_schedule_diagnostics(settings, *, beat_status: str | None) -> list[dict]:
    """Describe whether each configured schedule has an actual execution owner."""
    payload: list[dict] = []
    for spec in system_schedules():
        interval = int(getattr(settings, spec.interval_setting, 0) or 0)
        enabled = interval > 0
        if not enabled:
            runner = None
            status = "disabled"
        elif settings.task_dispatch_mode == "celery":
            runner = "beat"
            status = "healthy" if beat_status == "healthy" else "unavailable"
        elif spec.inline_runner:
            runner = "api"
            status = "healthy"
        else:
            runner = "manual"
            status = "manual_only"
        payload.append(
            {
                "name": spec.name,
                "label": spec.label,
                "task": spec.task,
                "interval_seconds": interval,
                "enabled": enabled,
                "runner": runner,
                "status": status,
            }
        )
    return payload


def __getattr__(name: str):
    # TF 兼容视图:旧代码读 SYSTEM_SCHEDULES 常量,这里从注册表动态派生。
    if name == "SYSTEM_SCHEDULES":
        return system_schedules()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SYSTEM_SCHEDULES",  # noqa: F822 - 经模块级 __getattr__ 从注册表派生
    "SystemScheduleSpec",
    "build_celery_beat_schedule",
    "collect_system_schedule_diagnostics",
    "register_system_schedule",
    "reset_system_schedules",
    "system_schedules",
]
