"""iCron 自主定时任务(迁移自 TF backend/app/models/icron.py)。

用户或 agent 登记一条"到点自己干活"的任务:调度器到点起一个真实的 agent run,
把 instruction 当成一条 user 消息喂进会话,跑完把结果转成通知。

作用域绑定与 chat_sessions 同一口径:scope_type + scope_id,无外键
(MIGRATION-PLAN §5.4"4 条硬 FK 泛化")。

两条硬纪律写在数据结构里:

1. **instruction 必须自包含**——无人值守时没有人回答追问,任务描述里缺的信息
   到点也补不上。因此它是 Text 而不是短标题,鼓励写全目标、约束与产出形式。
2. **每次 run 必须留痕**——IcronTaskRun 要么带 agent_run_id(真的起了 run),
   要么带 skip_reason / error(为什么没起来)。不允许"到点了但什么都查不到"。

时间列一律 UTC(timezone=True);timezone 字段只用于解释 cron 表达式与前端展示,
不改变存储口径——否则夏令时切换会让"每天 9 点"悄悄漂一小时。
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from nicekit.models.chat import SCOPE_TYPE_MAX_LENGTH

DEFAULT_ICRON_TIMEZONE = "Asia/Shanghai"


class IcronScheduleKind(StrEnum):
    """四种调度口径。

    MANUAL 是刻意保留的一档:任务已经登记好(instruction 审过、目标明确),
    但只在人点"立即运行"时才跑。没有它,一次性的复杂任务只能靠伪造一个
    永远不到的 run_at 来表达。
    """

    MANUAL = "manual"
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class IcronTaskStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class IcronTriggerSource(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    TEST = "test"
    AGENT = "agent"


class IcronRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated_at() -> Column:
    # onupdate 必须是客户端可知值(同 chat.py):服务端表达式会让属性在 UPDATE 后
    # 过期,commit 后序列化访问触发同步惰性刷新 → async 下 MissingGreenlet
    return Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
    )


class IcronTask(SQLModel, table=True):
    """一条定时任务的定义。

    target_session_id 可空:留空表示"每次运行新开一个会话",适合互不相干的
    周期作业(每天早上出一份汇总);指定已有会话则是"在这条对话里继续干",
    历史与目标都还在,适合长期盯着同一单业务。

    next_run_at 是调度器唯一的扫描入口(索引在此):任何改调度的操作都必须
    同步重算它,否则任务要么不跑、要么按旧节奏跑。manual 档与暂停/归档的任务
    next_run_at 置空,天然被扫描条件排除。
    """

    __tablename__ = "icron_tasks"
    __table_args__ = (
        Index("ix_icron_tasks_status_next_run_at", "status", "next_run_at"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    # 任务的归属人:通知发给他,起 run 时也以他的身份进入会话
    created_by: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)
    schedule_kind: str = Field(sa_column=Column(String(20), nullable=False))
    # cron 五段表达式(分 时 日 月 周),仅 schedule_kind=cron 时有意义
    cron_expr: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    timezone: str = Field(
        default=DEFAULT_ICRON_TIMEZONE,
        sa_column=Column(String(64), nullable=False, server_default=text("'Asia/Shanghai'")),
    )
    target_session_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # 任务归属的宿主业务作用域(可空);无外键,含义见 models/chat.py
    scope_type: str | None = Field(
        default=None, sa_column=Column(String(SCOPE_TYPE_MAX_LENGTH), nullable=True)
    )
    scope_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    # 自包含任务描述:无人值守跑的就是这段文字,缺的上下文到点没人补
    instruction: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(
        default=IcronTaskStatus.ACTIVE.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'active'")),
    )
    next_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    last_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class IcronTaskRun(SQLModel, table=True):
    """一次触发的执行记录(成功、失败与"没跑"都记一行)。

    agent_run_id 指向 chat_events.run_id / chat_sessions.active_run_id 用的那个
    run 标识,不加外键——run 不是一张表,它是会话事件流上的一个标识符。
    有它就能从这条记录直接跳到对话回放;没有它就必须有 skip_reason 或 error。
    """

    __tablename__ = "icron_task_runs"
    __table_args__ = (
        Index("ix_icron_task_runs_task_started", "task_id", "started_at"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    task_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("icron_tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    trigger_source: str = Field(
        default=IcronTriggerSource.SCHEDULE.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'schedule'")),
    )
    status: str = Field(
        default=IcronRunStatus.RUNNING.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'running'")),
    )
    agent_run_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    session_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    skip_reason: str | None = Field(default=None, max_length=100)
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    started_at: datetime | None = Field(default=None, sa_column=_created_at())
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
