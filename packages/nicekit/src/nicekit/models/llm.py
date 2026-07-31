"""LLM 平台表:Prompt Registry / 模型路由 / 调用 trace(FR-130~132)。

RLS 约定:llm_traces 按 org 隔离(FORCE RLS);prompts 与 model_routes 是
平台配置表,不做 RLS——访问控制在 API 层(platform_admin / org_admin)。
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Prompt(SQLModel, table=True):
    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("task", "version", name="uq_prompt_task_version"),)

    id: UUID = Field(sa_column=_uuid_pk())
    task: str = Field(max_length=100, index=True)
    version: int = Field(default=1)
    content: str = Field(sa_column=Column(Text, nullable=False))
    is_active: bool = Field(default=True, sa_column=Column(Boolean, nullable=False))
    created_by: UUID | None = Field(default=None, sa_column=Column(PGUUID(as_uuid=True)))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class PromptCatalogEntry(SQLModel, table=True):
    """自定义 Prompt 任务的在线目录登记(中文名/作用描述)。

    内置任务的元信息随源码 BUILTIN_PROMPT_CATALOG 治理、不落库;此表只收
    业务方自定义任务,让管理端不再对自定义 task 露机器名。与 prompts 表
    同口径:平台配置表,不做 RLS——访问控制在 API 层(platform_admin)。
    区别于 prompts 的"内容不可改只发新版",目录只是展示元信息,允许
    原地改写(upsert),不需要版本化。
    """

    __tablename__ = "prompt_catalog_entries"

    id: UUID = Field(sa_column=_uuid_pk())
    # 一个 task 只有一条目录登记:upsert 语义靠唯一约束兜底并发写
    task: str = Field(sa_column=Column(String(100), nullable=False, unique=True, index=True))
    name_zh: str = Field(max_length=100)
    description: str = Field(sa_column=Column(Text, nullable=False))
    created_by: UUID | None = Field(default=None, sa_column=Column(PGUUID(as_uuid=True)))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
            onupdate=text("now()"),
        ),
    )


class ModelRoute(SQLModel, table=True):
    """任务级模型路由。org_id 为空 = 平台默认;org 行覆盖平台行。"""

    __tablename__ = "model_routes"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True)
    )
    task: str = Field(max_length=100, index=True)
    primary_provider: str = Field(max_length=50)
    primary_model: str = Field(max_length=300)
    # 降级链:[{"provider": "...", "model": "..."}, ...]
    fallback_chain: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    max_tokens: int = Field(default=8192, sa_column=Column(Integer, nullable=False, default=8192))
    timeout_seconds: float = Field(default=120.0)
    is_active: bool = Field(default=True, sa_column=Column(Boolean, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class LlmTrace(SQLModel, table=True):
    """每次 provider 调用一行(降级链中每跳各一行)。RLS 按 org 隔离。"""

    __tablename__ = "llm_traces"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    task: str = Field(max_length=100)
    provider: str = Field(max_length=50)
    model: str = Field(max_length=100)
    prompt_version: int | None = Field(default=None)
    attempt: int = Field(default=1)
    fallback_from: str | None = Field(default=None, max_length=200)
    status: str = Field(max_length=20)  # success / error
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tokens_in: int = Field(default=0)
    tokens_out: int = Field(default=0)
    # 缓存计量(billing):tokens_in 口径统一为"非缓存新输入"(openai 已扣除 cached),
    # 缓存读/写单独计——计费公式对双协议一致
    cache_read_tokens: int = Field(default=0)
    cache_write_tokens: int = Field(default=0)
    latency_ms: int = Field(default=0)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class ModelPrice(SQLModel, table=True):
    """模型单价(billing):平台配置表,不做 RLS,访问控制在 API 层(platform_admin)。

    价格单位:USD / 1M tokens。计费公式(与记账口径配套):
    cost = tokens_in×input + tokens_out×output + cache_read×cache_read + cache_write×cache_write
    """

    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", name="uq_model_price_key"),)

    id: UUID = Field(sa_column=_uuid_pk())
    provider: str = Field(max_length=50)
    model: str = Field(max_length=100)
    display_name: str = Field(max_length=100)
    input_price: Decimal = Field(sa_column=Column(Numeric(12, 4), nullable=False))
    output_price: Decimal = Field(sa_column=Column(Numeric(12, 4), nullable=False))
    cache_read_price: Decimal = Field(
        default=Decimal(0), sa_column=Column(Numeric(12, 4), nullable=False, default=0)
    )
    cache_write_price: Decimal = Field(
        default=Decimal(0), sa_column=Column(Numeric(12, 4), nullable=False, default=0)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
            onupdate=text("now()"),
        ),
    )
