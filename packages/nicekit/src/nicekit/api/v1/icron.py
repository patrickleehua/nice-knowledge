"""iCron 自主定时任务 API。

RLS 只到 org 级,归属过滤在本层做:所有端点都锚定 ctx.user_id,别人的任务
一律 404(不暴露存在性),与 chat 会话同口径。

保存前的 /schedule/preview 是刻意独立的端点:定时任务最贵的错误是"建好了但
节奏和我想的不一样",而这种错误只有到点(或没到点)才暴露。让用户在保存前
看到未来 5 次的具体时刻,是把这个错误挪到最便宜的时候。

搬自 TF backend/app/api/v1/icron.py。SDK 化改造:任务绑定的业务对象
`project_id` 泛化为 `scope_type` + `scope_id`(与 chat_sessions 同一口径,无外键)。
"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent import icron
from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.core.db import get_session_factory
from nicekit.models.chat import ChatSession
from nicekit.models.icron import (
    DEFAULT_ICRON_TIMEZONE,
    IcronTask,
    IcronTaskRun,
    IcronTaskStatus,
    IcronTriggerSource,
)

router = APIRouter(prefix="/icron")

Session = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]

ScheduleKind = Literal["manual", "once", "interval", "cron"]

# scope_type 的落库长度上限(见 models/icron.py)
_SCOPE_TYPE_MAX = 50


class ScheduleIn(BaseModel):
    """调度参数(新建 / 编辑 / 预览共用一套,三处口径不会漂)。"""

    model_config = ConfigDict(extra="forbid")

    schedule_kind: ScheduleKind
    cron_expr: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    timezone: str = Field(default=DEFAULT_ICRON_TIMEZONE, max_length=64)


class TaskIn(ScheduleIn):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    instruction: str = Field(min_length=1, max_length=20000)
    target_session_id: UUID | None = None
    # 绑定宿主业务作用域:两者要么同时给,要么都不给
    scope_type: str | None = Field(default=None, max_length=_SCOPE_TYPE_MAX)
    scope_id: UUID | None = None

    @field_validator("name", "instruction")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (self.scope_type is None) != (self.scope_id is None):
            raise ValueError("scope_type 与 scope_id 必须同时提供")
        return self


class TaskPatch(BaseModel):
    """编辑:未出现的字段保持不变(与 create 分开,避免 PATCH 被当成整体覆盖)。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    instruction: str | None = Field(default=None, min_length=1, max_length=20000)
    schedule_kind: ScheduleKind | None = None
    cron_expr: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    target_session_id: UUID | None = None
    scope_type: str | None = Field(default=None, max_length=_SCOPE_TYPE_MAX)
    scope_id: UUID | None = None


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "paused", "archived"]


class TaskRunOut(BaseModel):
    id: UUID
    task_id: UUID
    trigger_source: str
    status: str
    agent_run_id: UUID | None
    session_id: UUID | None
    skip_reason: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TaskOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    instruction: str
    schedule_kind: str
    schedule_summary: str
    cron_expr: str | None
    interval_seconds: int | None
    run_at: datetime | None
    timezone: str
    target_session_id: UUID | None
    scope_type: str | None
    scope_id: UUID | None
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    last_run: TaskRunOut | None = None


class PreviewOut(BaseModel):
    """upcoming 用任务时区表示;manual 恒为空数组。"""

    schedule_summary: str
    timezone: str
    upcoming: list[datetime]


def _run_out(row: IcronTaskRun) -> TaskRunOut:
    return TaskRunOut.model_validate(row, from_attributes=True)


def _task_out(task: IcronTask, *, last_run: IcronTaskRun | None = None) -> TaskOut:
    return TaskOut.model_validate(
        {
            **task.model_dump(),
            "schedule_summary": icron.schedule_summary(task),
            "last_run": _run_out(last_run) if last_run is not None else None,
        }
    )


async def _own_task_or_404(
    session: AsyncSession, task_id: UUID, user_id: UUID
) -> IcronTask:
    task = await icron.get_task(session, task_id)
    if task is None or task.created_by != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
    return task


async def _own_session_or_404(
    session: AsyncSession, session_id: UUID, user_id: UUID
) -> ChatSession:
    chat = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if chat is None or chat.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return chat


def _draft_task(body: ScheduleIn, *, org_id: UUID, user_id: UUID) -> IcronTask:
    """未落库的临时任务对象,只为喂给纯函数算预览。"""
    return IcronTask(
        org_id=org_id,
        created_by=user_id,
        name="preview",
        instruction="preview",
        schedule_kind=body.schedule_kind,
        cron_expr=body.cron_expr,
        interval_seconds=body.interval_seconds,
        run_at=body.run_at,
        timezone=body.timezone or DEFAULT_ICRON_TIMEZONE,
    )


@router.post("/schedule/preview", response_model=PreviewOut)
async def preview_schedule(body: ScheduleIn, ctx: Ctx) -> PreviewOut:
    """保存前预览未来 5 次运行时间(带任务时区);参数非法直接 422 报原因。"""
    draft = _draft_task(body, org_id=ctx.org_id, user_id=ctx.user_id)
    try:
        icron.validate_schedule(
            body.schedule_kind,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            run_at=body.run_at,
            timezone=body.timezone,
        )
    except icron.IcronScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return PreviewOut(
        schedule_summary=icron.schedule_summary(draft),
        timezone=draft.timezone,
        upcoming=icron.preview_schedule(draft),
    )


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    session: Session, ctx: Ctx, status_filter: str | None = None
) -> list[TaskOut]:
    """本人的定时任务列表(带最近一次运行结果,列表页直接显示"上次结果")。"""
    tasks = await icron.list_tasks(
        session, created_by=ctx.user_id, status=status_filter
    )
    return [
        _task_out(task, last_run=await icron.latest_run(session, task.id))
        for task in tasks
    ]


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskIn, session: Session, ctx: Ctx) -> TaskOut:
    if body.target_session_id is not None:
        await _own_session_or_404(session, body.target_session_id, ctx.user_id)
    try:
        task = await icron.create_task(
            session,
            org_id=ctx.org_id,
            created_by=ctx.user_id,
            name=body.name,
            instruction=body.instruction,
            schedule_kind=body.schedule_kind,
            description=body.description,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            run_at=body.run_at,
            timezone=body.timezone,
            target_session_id=body.target_session_id,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
        )
    except icron.IcronScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _task_out(task)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: UUID, session: Session, ctx: Ctx) -> TaskOut:
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    return _task_out(task, last_run=await icron.latest_run(session, task.id))


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: UUID, body: TaskPatch, session: Session, ctx: Ctx
) -> TaskOut:
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    if body.target_session_id is not None:
        await _own_session_or_404(session, body.target_session_id, ctx.user_id)
    provided = body.model_dump(exclude_unset=True)
    try:
        task = await icron.update_task(session, task, **provided)
    except icron.IcronScheduleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _task_out(task)


@router.post("/tasks/{task_id}/status", response_model=TaskOut)
async def set_status(
    task_id: UUID, body: StatusIn, session: Session, ctx: Ctx
) -> TaskOut:
    """暂停 / 恢复 / 归档。恢复按当前时刻重算下一跳(不会立刻补跑)。"""
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    if (
        task.status == IcronTaskStatus.ARCHIVED.value
        and body.status == IcronTaskStatus.ACTIVE.value
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "已归档的任务不能恢复,请新建一条"
        )
    task = await icron.set_task_status(session, task, body.status)
    return _task_out(task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: UUID, session: Session, ctx: Ctx) -> None:
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    await icron.delete_task(session, task)


@router.post("/tasks/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_now(task_id: UUID, session: Session, ctx: Ctx) -> dict:
    """立即运行一次(后台执行,不改调度)。

    202 而不是同步等结果:一次 run 可能跑好几分钟。手动触发**刻意不推进**
    next_run_at —— 试跑一次不该把原本的节奏往后顺延,也不该消耗掉 once 的
    那一次机会。
    """
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    if task.status == IcronTaskStatus.ARCHIVED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "已归档的任务不能运行")
    icron.schedule_launch(
        get_session_factory(),
        org_id=ctx.org_id,
        task_id=task.id,
        trigger_source=IcronTriggerSource.MANUAL.value,
    )
    return {"accepted": True, "task_id": str(task.id)}


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRunOut])
async def list_runs(
    task_id: UUID, session: Session, ctx: Ctx, limit: int = 50
) -> list[TaskRunOut]:
    task = await _own_task_or_404(session, task_id, ctx.user_id)
    runs = await icron.list_task_runs(session, task.id, limit=max(1, min(limit, 200)))
    return [_run_out(row) for row in runs]
