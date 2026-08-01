"""执行计划的中断收敛与续跑(迁移自 TF domain/agent_plan 相关用例)。

TF 里 validate_plan_steps 住在 tools.py 的 plan_update 工具里(下一波搬运),
本文件只覆盖 domain 层纯函数。
"""

import pytest

from nicekit.domain.agent_plan import (
    PLAN_FAILURE_PREFIX,
    PLAN_STEP_STATUSES,
    PlanContinuationError,
    continue_failed_plan_step,
    mark_plan_interrupted,
    plan_failure_note,
)


def _step(sid="s1", title="第一步", status="pending", note=None) -> dict:
    return {"id": sid, "title": title, "status": status, "note": note}


def test_status_vocabulary_is_stable() -> None:
    assert set(PLAN_STEP_STATUSES) == {
        "pending",
        "in_progress",
        "done",
        "skipped",
        "failed",
    }


def test_failure_note_marks_system_convergence() -> None:
    assert plan_failure_note("模型调用失败") == f"{PLAN_FAILURE_PREFIX}:模型调用失败"
    assert plan_failure_note("  ") == PLAN_FAILURE_PREFIX
    assert plan_failure_note(None) == PLAN_FAILURE_PREFIX


def test_interrupted_in_progress_steps_converge_to_failed() -> None:
    plan = [
        _step("s1", status="done"),
        _step("s2", status="in_progress"),
        _step("s3", status="pending"),
    ]

    converged = mark_plan_interrupted(plan, reason="进程重启")

    assert converged is not None
    assert [item["status"] for item in converged] == ["done", "failed", "pending"]
    assert converged[1]["note"] == f"{PLAN_FAILURE_PREFIX}:进程重启"
    # 返回新列表,原 plan 不被原地改(JSONB 字段靠对象替换触发脏检测)
    assert plan[1]["status"] == "in_progress"


def test_no_in_progress_step_means_no_write() -> None:
    assert mark_plan_interrupted([_step(status="done")]) is None
    assert mark_plan_interrupted(None) is None
    assert mark_plan_interrupted("not-a-plan") is None


def test_failed_step_can_be_continued_once() -> None:
    plan = [_step("s1", status="done"), _step("s2", status="failed", note="执行中断")]

    updated, step = continue_failed_plan_step(plan, step_id="s2")

    assert updated[1]["status"] == "in_progress"
    assert step["status"] == "failed"  # 返回的是变更前的步骤快照
    assert plan[1]["status"] == "failed"


@pytest.mark.parametrize(
    "plan",
    [
        [_step("s1", status="done")],  # 非 failed 状态不可续跑
        [_step("s1", status="in_progress")],
        [],
        None,
    ],
)
def test_continuation_rejects_missing_or_non_failed_steps(plan) -> None:
    with pytest.raises(PlanContinuationError, match="不存在或当前不可继续"):
        continue_failed_plan_step(plan, step_id="s1")
