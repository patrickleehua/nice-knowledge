"""租户底座表(迁移自 TF backend/app/models/tenancy.py,MIGRATION-PLAN §5.2)。

改造点:Role 收敛为 platform_admin / org_admin / member 三个内置角色;
业务角色由宿主经 nicekit.tenancy.roles.register_roles() 注册(存 varchar(32),
加角色不动 DDL),因此 membership/invitation 的 role 列注解为 str。
表结构与 TF 完全一致。
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


class Role(StrEnum):
    """内置角色。业务角色不进本枚举:走 nicekit.tenancy.roles 注册表。"""

    PLATFORM_ADMIN = "platform_admin"
    ORG_ADMIN = "org_admin"
    MEMBER = "member"


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Organization(SQLModel, table=True):
    __tablename__ = "organizations"

    id: UUID = Field(sa_column=_uuid_pk())
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, unique=True, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(sa_column=_uuid_pk())
    email: str = Field(max_length=320, unique=True, index=True)
    password_hash: str = Field(sa_column=Column(Text, nullable=False))
    full_name: str = Field(max_length=200)
    is_active: bool = Field(default=True)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class Membership(SQLModel, table=True):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
        )
    )
    user_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    )
    # varchar 存角色值(而非 PG 原生 enum):加角色不需要 ALTER TYPE。
    # 取值 = 内置 Role 或宿主注册的角色名(tenancy/roles.py)。
    role: str = Field(sa_column=Column(String(32), nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class Invitation(SQLModel, table=True):
    __tablename__ = "invitations"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
        )
    )
    email: str = Field(max_length=320)
    role: str = Field(sa_column=Column(String(32), nullable=False))
    token_hash: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    accepted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: UUID = Field(sa_column=_uuid_pk())
    user_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    )
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    )
    token_hash: str = Field(sa_column=Column(Text, nullable=False, unique=True, index=True))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class AuditLog(SQLModel, table=True):
    """RLS 隔离(org 级)。写入必须在 org 上下文事务内。"""

    __tablename__ = "audit_logs"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    user_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    action: str = Field(max_length=100)
    entity_type: str = Field(max_length=100)
    entity_id: str | None = Field(default=None, max_length=100)
    detail: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class Notification(SQLModel, table=True):
    """站内信:RLS org 隔离,用户级过滤在 API 查询层。

    kind = 事件键(如 task.recovered / kb.document.expiring,宿主可自定);
    link = 前端路由(如 /app/notifications),前端点击跳转。
    """

    __tablename__ = "notifications"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    user_id: UUID = Field(  # 收件人;(user_id, read_at) 复合索引在迁移里建
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    kind: str = Field(max_length=50)
    title: str = Field(max_length=200)
    body: str = Field(sa_column=Column(Text, nullable=False))
    link: str | None = Field(default=None, max_length=500)
    read_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class UsageDaily(SQLModel, table=True):
    """RLS 隔离(org 级)。用量按天聚合(LLM token 与通用计量共用一张表)。"""

    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "usage_date", "task", "provider", "model", name="uq_usage_daily_key"
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    usage_date: date = Field(sa_column=Column(Date, nullable=False))
    task: str = Field(max_length=100)
    provider: str = Field(max_length=50)
    model: str = Field(max_length=100)
    tokens_in: int = Field(default=0)
    tokens_out: int = Field(default=0)
    # 缓存计量(billing):LLM 行才有值;tokens_in 口径为"非缓存新输入"
    cache_read_tokens: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, server_default=text("0"))
    )
    cache_write_tokens: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, server_default=text("0"))
    )
    calls: int = Field(default=0)
    # 通用计量数:字节类计量(存储/文件)存字节数,纯次数类只计 calls,LLM 行恒 0
    quantity: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, server_default=text("0"))
    )
