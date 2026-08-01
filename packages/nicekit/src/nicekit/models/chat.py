"""AgentRuntime 表:agent 卡片 + chat 会话/消息(迁移自 TF backend/app/models/chat.py)。

- agent_cards:每个 agent 一张卡(system_prompt/model_task/max_turns/timeout/工具白名单)。
  org_id 可空 = 平台默认卡;seed 的平台卡挂 platform org,
  RLS 读策略同时放行 平台 org 与 NULL 两种平台表示。
- chat_sessions / chat_messages:工作台会话与消息,严格本 org 隔离(RLS)。

**scope 泛化**(MIGRATION-PLAN §5.4"4 条硬 FK 泛化"):TF 里会话直接挂
`project_id`/`customer_id` 两个业务外键;SDK 不认识宿主的业务表,统一改为
`scope_type`(宿主自定的作用域种类名)+ `scope_id`(该作用域根对象的 UUID),
**不建外键**——外键会把 SDK 的 schema 钉死在某个宿主的表名上。作用域的解析
与可见性由宿主注册的 ScopeResolver 负责。
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from nicekit.domain.agent_permission import PermissionProfile, PermissionScope

# scope_type 的落库长度上限;宿主注册的作用域种类名(如 "ticket"/"case")受此约束
SCOPE_TYPE_MAX_LENGTH = 40


class ChatSessionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatSessionOriginMode(StrEnum):
    """会话来源模式。

    - GENERAL:通用入口创建,创建时不绑定任何宿主业务作用域;
    - SCOPED:创建时即绑定宿主业务作用域(scope_type + scope_id)。

    绑定关系后续可变,但 origin_mode 是创建事实,永不回写——权限策略据它区分
    "通用会话越权改动已有作用域"与"作用域内的正常操作"。
    """

    GENERAL = "general"
    SCOPED = "scoped"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated_at() -> Column:
    # onupdate 必须是客户端可知值:服务端表达式会让属性在 UPDATE 后过期,
    # commit 后序列化访问触发同步惰性刷新 → async 下 MissingGreenlet
    return Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentCard(SQLModel, table=True):
    """agent 配置卡:model_task 接 model_routes 的 task 名(如 agent.assistant)。"""

    __tablename__ = "agent_cards"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    name: str = Field(max_length=100)
    description: str | None = Field(default=None, max_length=500)
    icon: str | None = Field(default=None, max_length=100)
    sort_order: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    # 以下能力字段仅用于旧库兼容；运行时配置来自 active AgentCardVersion。
    system_prompt: str = Field(sa_column=Column(Text, nullable=False))
    model_task: str = Field(max_length=100)
    max_turns: int = Field(default=8, sa_column=Column(Integer, nullable=False))
    timeout_seconds: float = Field(default=300.0, sa_column=Column(Float, nullable=False))
    # 工具白名单:["kb_search", "memory_write", ...];不在名单内的调用被 loop 拒绝
    tools: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    is_active: bool = Field(default=True)
    # 可被主 agent 通过 Agent 工具委派的专员卡。默认 false:开放委派是显式动作,
    # 存量卡(尤其主卡)不能因为加了这一列就突然出现在委派名单里。
    delegatable: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class AgentCardVersion(SQLModel, table=True):
    """不可变 agent 能力版本；修改配置必须创建新版本并激活。"""

    __tablename__ = "agent_card_versions"
    __table_args__ = (
        UniqueConstraint("card_id", "version", name="uq_agent_card_versions_card_version"),
        Index(
            "uq_agent_card_versions_active",
            "card_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    card_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_cards.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    version: int = Field(sa_column=Column(Integer, nullable=False))
    system_prompt: str = Field(sa_column=Column(Text, nullable=False))
    model_task: str = Field(max_length=100)
    max_turns: int = Field(default=8, sa_column=Column(Integer, nullable=False))
    timeout_seconds: int = Field(default=300, sa_column=Column(Integer, nullable=False))
    tools: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    mcp_bindings: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    skills: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    is_active: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    status: str = Field(
        default="draft",
        sa_column=Column(String(20), nullable=False, server_default=text("'draft'")),
    )
    created_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class ChatSession(SQLModel, table=True):
    """聊天会话:来源模式固定,绑定的宿主作用域可变且供工具缺省参数回退。

    scope_type + scope_id 是会话与宿主业务对象的唯一联系(无外键):
    scope_type 是宿主注册的作用域种类名,scope_id 是该作用域根对象主键。
    两者要么都为空(纯通用会话),要么都有值——半绑定状态没有意义,
    由服务层保证,不落 CHECK(避免宿主迁移期数据被硬拒)。
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint(
            "origin_mode IN ('general', 'scoped')",
            name="ck_chat_sessions_origin_mode",
        ),
        Index("ix_chat_sessions_scope", "org_id", "scope_type", "scope_id"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    user_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    agent_card_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("agent_cards.id"), nullable=False
        )
    )
    # 宿主业务作用域种类名(如 "ticket"/"case"/"project");None = 未绑定
    scope_type: str | None = Field(
        default=None, sa_column=Column(String(SCOPE_TYPE_MAX_LENGTH), nullable=True)
    )
    # 作用域根对象主键。刻意不建外键:SDK 不认识宿主的表。
    scope_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    # 会话是从通用入口还是带作用域的入口创建。绑定 scope 后也不改变此值。
    origin_mode: ChatSessionOriginMode = Field(
        default=ChatSessionOriginMode.GENERAL,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'general'"),
        ),
    )
    title: str = Field(default="新对话", max_length=200)
    status: str = Field(
        default=ChatSessionStatus.ACTIVE.value, sa_column=Column(String(20), nullable=False)
    )
    active_run_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    cancel_requested: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    # 待批的高危工具调用 {run_id, tool_call_id, name, input, summary, requested_at};
    # loop stop=confirm 时写入,用户批准/拒绝或发新消息后清除
    pending_confirmation: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # 待答结构化澄清请求；与高危操作审批分开持久化和恢复。
    pending_user_input: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    permission_profile: PermissionProfile = Field(
        default=PermissionProfile.REQUEST_APPROVAL,
        sa_column=Column(
            String(32), nullable=False, server_default=text("'request_approval'")
        ),
    )
    permission_scope: PermissionScope = Field(
        default=PermissionScope.SESSION,
        sa_column=Column(String(32), nullable=False, server_default=text("'session'")),
    )
    permission_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    permission_policy_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_approval_policies.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    permission_policy_version: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    permission_custom_rules: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    permission_policy_snapshot: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    permission_revision: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default=text("1"))
    )
    reviewer_override_candidates: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    # agent 执行计划 [{id, title, status, note}]:plan_update 工具全量覆盖,
    # 前端计划面板据此渲染;仅是可视化意图声明,不参与调度
    plan: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class ChatMessage(SQLModel, table=True):
    """会话消息:role=user/assistant/tool;tool 行带 tool_name/tool_call_id/
    tool_input(参数)/tool_output(完整结果);content 存对模型可见文本(tool 行为截断串)。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False, index=True
        )
    )
    sequence: int = Field(sa_column=Column(Integer, nullable=False))
    role: str = Field(sa_column=Column(String(20), nullable=False))
    content: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tool_name: str | None = Field(default=None, max_length=100)
    tool_call_id: str | None = Field(default=None, max_length=100)
    run_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    tool_input: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    tool_output: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    trace_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class ChatSessionSummary(SQLModel, table=True):
    """会话早前对话摘要(上下文压缩):status=success 的最新一条生效,
    covered_until_sequence 之前的消息在模型历史中由摘要代替。

    摘要只是会话上下文的替身、不是业务真相:消息行与工具完整输出
    (chat_messages.tool_output)一律不删,审计与前端展示不受影响。
    status=failed 行保留失败痕迹(content 存失败原因),连续失败用于熔断。
    """

    __tablename__ = "chat_session_summaries"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    content: str = Field(sa_column=Column(Text, nullable=False))
    # 摘要覆盖到的消息 sequence(含);之后的消息原样进模型历史
    covered_until_sequence: int = Field(sa_column=Column(Integer, nullable=False))
    # 摘要自身的粗估 token(字符数/4),用于观测压缩收益
    token_estimate: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    status: str = Field(
        default="success", sa_column=Column(String(20), nullable=False)
    )  # success | failed
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class ChatRunInputStatus(StrEnum):
    QUEUED = "queued"
    CONSUMED = "consumed"
    SKIPPED = "skipped"


class ChatRunInput(SQLModel, table=True):
    """运行中输入队列。

    run 进行中用户继续发消息时不再 409,而是入队;loop 只在安全检查点
    (run_loop 以 end_turn 收尾后)消费,消费时幂等转为正式 user message
    (consumed_message_id 记录对应 chat_messages 行,防重复写);
    run 以 cancelled/error 终止时剩余项标 skipped,绝不静默丢弃。
    queued 状态下可编辑/删除/重排,consumed/skipped 为终态只读。
    """

    __tablename__ = "chat_run_inputs"
    __table_args__ = (
        Index(
            "ix_chat_run_inputs_session_run_status",
            "session_id",
            "run_id",
            "status",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # 入队时归属的 run;该 run 停在 confirm/ask_user 后由下一个 run 收编(改写 run_id)
    run_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    user_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    content: str = Field(sa_column=Column(Text, nullable=False))
    position: int = Field(sa_column=Column(Integer, nullable=False))
    # 该条输入自带的"本轮临时工具"(见 runtime_tools.py):排队消息也可能是
    # "帮我上网查一下…",消费它时按这份名单临时放开只读工具,只影响这一轮,
    # 不写 agent 卡白名单;判定结果同样进事件流与运行时快照。
    runtime_tools: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    status: str = Field(
        default=ChatRunInputStatus.QUEUED.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'queued'")),
    )
    consumed_message_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class SessionGoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    BUDGET_LIMITED = "budget_limited"
    CANCELLED = "cancelled"


class SessionGoal(SQLModel, table=True):
    """会话目标与自动续跑。

    用户给会话设一个目标,agent 空闲时后台自动起新 run 继续推进,直到目标完成
    或预算耗尽。objective 只是"用户提供的任务数据",注入历史时以 user 角色包在
    <goal_context> 里,不是更高优先级指令。

    每个会话至多一条目标(session_id 唯一):重设目标时原地覆盖同一行,旧目标的
    终结通过 goal.updated(status=cancelled)事件留痕——目标是会话的当前意图而
    非需要版本化的业务对象,为它多存一份历史行不值当。

    预算双闸:token_budget 兜住"跑太久烧钱",max_auto_runs 兜住"来回空转";
    任一触顶都不再续跑(token 触顶还会转 budget_limited,让模型收尾而不是硬停)。
    """

    __tablename__ = "session_goals"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        )
    )
    objective: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(
        default=SessionGoalStatus.ACTIVE.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'active'")),
    )
    token_budget: int = Field(
        default=200000, sa_column=Column(Integer, nullable=False, server_default=text("200000"))
    )
    tokens_used: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    auto_runs: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    max_auto_runs: int = Field(
        default=10, sa_column=Column(Integer, nullable=False, server_default=text("10"))
    )
    # 目标完成时模型给出的中文总结(做了什么/结论是什么),供前端横幅与审计回看
    summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class ChatEvent(SQLModel, table=True):
    """Durable AgentEvent envelope used by SSE replay."""

    __tablename__ = "chat_events"
    __table_args__ = (
        UniqueConstraint("session_id", "run_id", "seq", name="uq_chat_events_run_seq"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    run_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    seq: int = Field(sa_column=Column(Integer, nullable=False))
    payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
