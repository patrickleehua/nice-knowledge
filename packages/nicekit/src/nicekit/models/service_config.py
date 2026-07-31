"""平台级外部服务配置(联网搜索/图片生成等):DB 覆盖 .env 兜底。

payload 为自由 JSON,键与 Settings 字段对应(如 websearch 的 provider /
tavily_api_key / bocha_api_key / timeout_seconds);空串/缺失的键回落 env。
仅平台管理员可写(admin router 整路由守门),服务读取方按需合并。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


class ServiceConfig(SQLModel, table=True):
    __tablename__ = "service_configs"

    id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    )
    name: str = Field(sa_column=Column(String(40), nullable=False, unique=True))
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
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
