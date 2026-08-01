"""Agent 执行计划的状态词表与中断收敛。

plan 由 `plan_update` 工具全量覆盖，是模型声明的意图。模型只在自己还活着时
更新它,所以轮次异常终止(模型调用失败、连接中断、进程重启)后,步骤会永久
停在 in_progress,前端读成"正在推进"。这里提供纯函数把这些残留步骤收敛到
failed,供 agent service(轮次内中断)与 reliability(进程级回收)共用。
"""

PLAN_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "done", "skipped", "failed"}
)

# 步骤 note 前缀:标记是系统收敛而非模型自述,前端据此提示可重试
PLAN_FAILURE_PREFIX = "执行中断"


class PlanContinuationError(ValueError):
    """The requested display-plan step cannot be continued."""


def plan_failure_note(reason: str | None) -> str:
    reason = (reason or "").strip()
    return f"{PLAN_FAILURE_PREFIX}:{reason}" if reason else PLAN_FAILURE_PREFIX


def mark_plan_interrupted(plan: object, *, reason: str | None = None) -> list | None:
    """把仍处于 in_progress 的步骤落 failed;无需改动时返回 None。

    返回新列表(不原地改),便于 JSONB 字段赋值触发脏检测。
    """
    if not isinstance(plan, list):
        return None
    note = plan_failure_note(reason)
    changed = False
    out: list = []
    for step in plan:
        if isinstance(step, dict) and step.get("status") == "in_progress":
            out.append({**step, "status": "failed", "note": note})
            changed = True
        else:
            out.append(step)
    return out if changed else None


def continue_failed_plan_step(
    plan: object,
    *,
    step_id: str,
) -> tuple[list, dict]:
    """Return a copied plan with one failed step moved back to in-progress."""
    if not isinstance(plan, list):
        raise PlanContinuationError("计划步骤不存在或当前不可继续")

    for index, value in enumerate(plan):
        if not isinstance(value, dict) or str(value.get("id")) != step_id:
            continue
        if value.get("status") != "failed":
            break
        step = dict(value)
        updated = list(plan)
        updated[index] = {**step, "status": "in_progress"}
        return updated, step

    raise PlanContinuationError("计划步骤不存在或当前不可继续")
