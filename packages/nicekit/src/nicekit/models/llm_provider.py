"""模型提供商实例(Cherry Studio 式):任意多个,路由降级链按名称引用混用。

- name:实例名(小写标识),model_routes 的 primary_provider / fallback_chain
  引用的就是它;内置两行 "openai" / "anthropic"(is_builtin,不可删)。
- protocol:接入协议(openai = Responses API / anthropic = Messages API),
  决定用哪个 SDK 客户端;同协议可有多个实例(不同网关/账号)。
- api_key / base_url 为空时:内置实例回落 .env,自定义实例视为未配置(跳过)。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


class LlmProvider(SQLModel, table=True):
    __tablename__ = "llm_providers"

    id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    )
    name: str = Field(sa_column=Column(String(50), nullable=False, unique=True))
    protocol: str = Field(sa_column=Column(String(20), nullable=False))
    api_key: str = Field(default="", max_length=500)
    base_url: str = Field(default="", max_length=2000)
    # 该实例的模型列表(路由表单候选):从网关导入(/v1/models)或手动维护,
    # 网关不支持导入时纯手动
    models: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    # key=model id; value is the resolved capability record. ``models`` remains
    # the compatibility inventory used by existing route/admin clients.
    model_metadata: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    is_builtin: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
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
