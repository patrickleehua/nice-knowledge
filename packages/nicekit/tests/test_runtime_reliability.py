"""runtime/reliability.py:RecoverableTask 框架化(§4 / §5.7)。

覆盖:注册表、逐租户骨架(_list_org_ids + org_session)、启动恢复与周期清扫的
两种 cutoff 口径、单个任务类型失败不拖垮其余、requeue 在事务提交之后发生。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nicekit.runtime import reliability
from nicekit.runtime.reliability import (
    ORPHAN_ERROR,
    STALE_ERROR,
    recover_orphan_tasks,
    recoverable_tasks,
    register_recoverable_task,
    reset_recoverable_tasks,
    sweep_stale_tasks,
)

ORG_A = uuid4()
ORG_B = uuid4()


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """清空 SDK 自带实现,逐个用例自己注册替身;结束后恢复默认。"""
    reliability._tasks.clear()
    sessions: dict = {}

    async def _org_ids(_factory) -> list:
        return [ORG_A, ORG_B]

    def _org_session(_factory, org_id):
        sessions[org_id] = sessions.get(org_id) or _Session()
        return sessions[org_id]

    monkeypatch.setattr(reliability, "_list_org_ids", _org_ids)
    monkeypatch.setattr(reliability, "org_session", _org_session)
    yield sessions
    reset_recoverable_tasks()


class _RecordingTask:
    def __init__(self, name: str, *, orphans_per_org: int = 1) -> None:
        self.name = name
        self.orphans_per_org = orphans_per_org
        self.listed: list[tuple] = []
        self.failed: list[tuple] = []
        self.requeued: list[tuple] = []
        self.order: list[str] = []

    async def list_orphans(self, session, *, org_id, cutoff):
        self.listed.append((org_id, cutoff))
        self.order.append("list")
        return [f"{self.name}-{org_id}-{i}" for i in range(self.orphans_per_org)]

    async def fail(self, session, orphans, *, org_id, error, now):
        self.failed.append((org_id, tuple(orphans), error))
        self.order.append("fail")
        return {self.name: len(orphans)}

    async def requeue(self, orphans, *, org_id):
        self.requeued.append((org_id, tuple(orphans)))
        self.order.append("requeue")
        return len(orphans)


async def test_recover_orphan_tasks_runs_every_org_with_null_cutoff() -> None:
    task = _RecordingTask("demo")
    register_recoverable_task(task)

    counts = await recover_orphan_tasks(session_factory=None)

    assert [org for org, _ in task.listed] == [ORG_A, ORG_B]
    assert {cutoff for _, cutoff in task.listed} == {None}  # 启动恢复口径
    assert [error for _, _, error in task.failed] == [ORPHAN_ERROR, ORPHAN_ERROR]
    assert counts == {"demo": 2, "requeued": 2}


async def test_sweep_stale_tasks_passes_cutoff_and_stale_error() -> None:
    task = _RecordingTask("demo")
    register_recoverable_task(task)

    counts = await sweep_stale_tasks(session_factory=None, timeout_seconds=60)

    cutoffs = [cutoff for _, cutoff in task.listed]
    assert all(isinstance(c, datetime) for c in cutoffs)
    assert all(c < datetime.now(UTC) for c in cutoffs)
    assert [error for _, _, error in task.failed] == [STALE_ERROR, STALE_ERROR]
    assert counts["demo"] == 2


async def test_requeue_happens_after_commit(_isolated_registry) -> None:
    task = _RecordingTask("demo")
    register_recoverable_task(task)
    sessions = _isolated_registry

    await recover_orphan_tasks(session_factory=None)

    # 每个 org 的顺序都是 list → fail → requeue,且 requeue 在 commit 之后
    assert task.order == ["list", "fail", "requeue"] * 2
    assert all(session.commits == 1 for session in sessions.values())
    assert all(session.closed for session in sessions.values())


async def test_empty_orphans_skip_fail_and_requeue() -> None:
    task = _RecordingTask("demo", orphans_per_org=0)
    register_recoverable_task(task)

    counts = await recover_orphan_tasks(session_factory=None)

    assert task.failed == []
    assert task.requeued == []
    assert counts == {}


async def test_one_failing_task_does_not_block_others() -> None:
    class _Boom:
        name = "boom"

        async def list_orphans(self, session, *, org_id, cutoff):
            raise RuntimeError("扫描炸了")

        async def fail(self, *args, **kwargs):  # pragma: no cover - 不会被调用
            raise AssertionError

        async def requeue(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError

    healthy = _RecordingTask("healthy")
    register_recoverable_task(_Boom())
    register_recoverable_task(healthy)

    counts = await recover_orphan_tasks(session_factory=None)

    assert counts["healthy"] == 2


def test_reset_restores_sdk_builtin_implementations() -> None:
    reset_recoverable_tasks()
    assert {task.name for task in recoverable_tasks()} == {"agent_run", "kb_ingest_run"}
