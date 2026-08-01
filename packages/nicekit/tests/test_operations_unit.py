"""operations 子包单测(§5.9 A1):心跳诊断 / 事件登记 / 周期任务目录。

不碰真实数据库:heartbeat 诊断用假会话喂行,IncidentRecorder 用假会话验 SQL 形状
(真库行为由迁移往返 + demo 冒烟覆盖)。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.core.config import Settings
from nicekit.core.metrics import SERVICE_HEARTBEAT_AGE, render_metrics
from nicekit.operations import runtime as ops_runtime
from nicekit.operations import schedules as ops_schedules
from nicekit.operations.incidents import SqlIncidentRecorder, count_open_kb_incidents
from nicekit.operations.schedules import (
    SystemScheduleSpec,
    build_celery_beat_schedule,
    collect_system_schedule_diagnostics,
    register_system_schedule,
    reset_system_schedules,
    system_schedules,
)

ORG_ID = uuid4()
KB_ID = uuid4()


# ---------------------------------------------------------------------------
# 心跳诊断
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def one(self):
        return self._rows[0]

    @property
    def rowcount(self) -> int:
        return len(self._rows)


class _Session:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    async def commit(self) -> None:
        return None


def _heartbeat(role: str, *, age_seconds: float) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        instance=f"host:{role}",
        version="9.9.9",
        recorded_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


async def test_heartbeat_diagnostics_classifies_and_refreshes_gauge() -> None:
    session = _Session([_heartbeat("api", age_seconds=5)])
    diagnostics = await ops_runtime.collect_heartbeat_diagnostics(session)

    by_role = {d.role: d for d in diagnostics}
    assert by_role["api"].status == "healthy"
    assert by_role["api"].required is True
    assert by_role["worker"].status == "missing"
    # A1 遗留修复:该 Gauge 此前无人刷新
    assert SERVICE_HEARTBEAT_AGE.labels(role="api")._value.get() == pytest.approx(5, abs=1)
    assert b"service_heartbeat_age_seconds" in render_metrics()


async def test_expired_heartbeat_is_reported() -> None:
    session = _Session([_heartbeat("api", age_seconds=3600)])
    diagnostics = await ops_runtime.collect_heartbeat_diagnostics(session)
    assert {d.role: d.status for d in diagnostics}["api"] == "expired"


def test_runtime_instance_id_is_bounded() -> None:
    assert ops_runtime.runtime_instance_id("api", "x" * 500) == "x" * 200
    assert ":" in ops_runtime.runtime_instance_id("api", "")


async def test_record_service_heartbeat_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        await ops_runtime.record_service_heartbeat(lambda: None, role="ghost")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 事件登记(kb/ports.py::IncidentRecorder 的默认 SQL 实现)
# ---------------------------------------------------------------------------


async def test_incident_recorder_satisfies_port_protocol() -> None:
    from nicekit.kb.ports import IncidentRecorder

    assert isinstance(SqlIncidentRecorder(), IncidentRecorder)


async def test_record_incident_builds_upsert() -> None:
    session = _Session()
    await SqlIncidentRecorder().record(
        session,
        org_id=ORG_ID,
        kb_id=KB_ID,
        category="media_projection_failure",
        code="projection_failed",
    )
    sql = str(session.statements[0]).lower()
    assert "insert into kb_operational_incidents" in sql
    assert "on conflict on constraint uq_kb_operational_incident_dedupe_key" in sql


async def test_count_open_returns_zero_age_when_empty() -> None:
    session = _Session([(0, None)])
    assert await count_open_kb_incidents(session, org_id=ORG_ID, category="x") == (0, 0.0)


async def test_count_open_computes_oldest_age() -> None:
    first_seen = datetime.now(UTC) - timedelta(seconds=120)
    session = _Session([(3, first_seen)])
    count, age = await count_open_kb_incidents(session, org_id=ORG_ID, category="x")
    assert count == 3
    assert age == pytest.approx(120, abs=5)


async def test_purge_is_noop_without_ids() -> None:
    session = _Session()
    assert await SqlIncidentRecorder().purge(
        session, org_id=ORG_ID, kb_id=KB_ID, image_asset_ids=[]
    ) == 0
    assert session.statements == []


# ---------------------------------------------------------------------------
# 周期任务目录
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_schedules():
    yield
    reset_system_schedules()


def test_builtin_schedules_drop_business_tasks() -> None:
    tasks = {spec.task for spec in system_schedules()}
    assert "fulfillment.daily_reminders" not in tasks  # 业务任务不进 SDK
    assert {"kb.consume_outbox", "icron.tick", "reliability.sweep_stale_tasks"} <= tasks


def test_beat_schedule_is_empty_in_inline_mode() -> None:
    settings = Settings(task_dispatch_mode="inline")
    assert build_celery_beat_schedule(settings, beat_instance="b") == {}


def test_beat_schedule_built_from_registry() -> None:
    settings = Settings(task_dispatch_mode="celery", icron_tick_interval_seconds=0)
    register_system_schedule(
        SystemScheduleSpec(
            name="host-nightly",
            label="宿主夜间任务",
            task="host.nightly",
            interval_setting="kb_expiry_sweep_interval_seconds",
            inline_runner=False,
        )
    )
    schedule = build_celery_beat_schedule(settings, beat_instance="beat-1")

    assert schedule["operations-beat-heartbeat"]["kwargs"]["instance"] == "beat-1"
    assert schedule["host-nightly"]["task"] == "host.nightly"
    assert "icron-tick" not in schedule  # interval 0 = 关闭


def test_schedule_diagnostics_runner_by_mode() -> None:
    inline = collect_system_schedule_diagnostics(
        Settings(task_dispatch_mode="inline"), beat_status=None
    )
    by_name = {row["name"]: row for row in inline}
    assert by_name["kb-consume-outbox"]["runner"] == "api"
    # inline_runner=False 的任务在 inline 模式下没有自动执行者
    assert by_name["operations-provider-probes"]["status"] == "manual_only"

    celery_down = collect_system_schedule_diagnostics(
        Settings(task_dispatch_mode="celery"), beat_status=None
    )
    assert {row["runner"] for row in celery_down} == {"beat"}
    assert {row["status"] for row in celery_down} == {"unavailable"}


def test_legacy_system_schedules_view_follows_registry() -> None:
    before = len(ops_schedules.SYSTEM_SCHEDULES)
    register_system_schedule(
        SystemScheduleSpec("extra", "额外", "host.extra", "stale_sweep_interval_seconds", True)
    )
    assert len(ops_schedules.SYSTEM_SCHEDULES) == before + 1
