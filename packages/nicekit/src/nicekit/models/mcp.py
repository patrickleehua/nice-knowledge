"""MCP server configuration visible to an org or shared by the platform."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


class McpServer(SQLModel, table=True):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        Index(
            "uq_mcp_servers_platform_name",
            "name",
            unique=True,
            postgresql_where=text("org_id IS NULL"),
        ),
        Index(
            "uq_mcp_servers_org_name",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
        ),
    )

    id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    )
    org_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    name: str = Field(max_length=80)
    # streamable_http / sse / stdio(stdio 在宿主机拉起子进程,仅平台管理员可配)
    transport: str = Field(sa_column=Column(String(30), nullable=False))
    # http/sse 用;stdio 时为空串
    endpoint_url: str = Field(default="", max_length=2000)
    headers: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    # ---- stdio 传输:command + args 拉起子进程,env 附加到子进程环境
    command: str | None = Field(
        default=None, sa_column=Column(String(500), nullable=True)
    )
    args: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    env: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    # ---- 超时(秒):null = 用管理器默认(连接 10s / 调用 60s)
    connect_timeout_seconds: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    call_timeout_seconds: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    enabled_tools: list[str] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    enabled: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default=text("true"))
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
            onupdate=lambda: datetime.now(UTC),
        ),
    )
