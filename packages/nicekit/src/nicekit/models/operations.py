"""运维可观测性表(MIGRATION-PLAN §5.9 A1):心跳 / 能力探测 / KB 运维事件。

搬自 TF backend/app/models/operations.py。三张表都是**通用运维**而非业务:

- ``service_heartbeats``:api / worker / beat 三类进程的最新心跳(无 org_id,
  平台级,不开 RLS);
- ``provider_probe_statuses``:四个 AI 能力槽位的周期探测结果(无 org_id);
- ``kb_operational_incidents``:脱敏去重后的 KB 一致性 / 投影失败事件
  (带 org_id → baseline 之后的迁移里开 RLS)。

事件表刻意不放进 ``models/kb.py``:KB 子包只经 ``kb/ports.py::IncidentRecorder``
协议登记事件,不认识这张表(装配期由 ``nicekit.operations.incidents`` 注册默认
SQL 实现)。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _utc_timestamp() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ServiceHeartbeat(SQLModel, table=True):
    """Latest heartbeat for one concrete API, worker, or beat process."""

    __tablename__ = "service_heartbeats"
    __table_args__ = (
        UniqueConstraint(
            "role",
            "instance",
            name="uq_service_heartbeat_role_instance",
        ),
        CheckConstraint(
            "role IN ('api', 'worker', 'beat')",
            name="ck_service_heartbeat_role",
        ),
        CheckConstraint(
            "length(btrim(instance)) > 0",
            name="ck_service_heartbeat_instance_nonempty",
        ),
        CheckConstraint(
            "length(btrim(version)) > 0",
            name="ck_service_heartbeat_version_nonempty",
        ),
        Index("ix_service_heartbeats_role_recorded", "role", "recorded_at"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    role: str = Field(max_length=20)
    instance: str = Field(max_length=200)
    version: str = Field(max_length=100)
    recorded_at: datetime | None = Field(default=None, sa_column=_utc_timestamp())
    started_at: datetime | None = Field(default=None, sa_column=_utc_timestamp())


class ProviderProbeStatus(SQLModel, table=True):
    """Latest scheduled bounded probe result for one selected AI capability."""

    __tablename__ = "provider_probe_statuses"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('embedding', 'rerank', 'ocr', 'caption')",
            name="ck_provider_probe_capability",
        ),
        CheckConstraint(
            "status IN ('healthy', 'degraded', 'unavailable', 'disabled')",
            name="ck_provider_probe_status",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_provider_probe_config_fingerprint",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_provider_probe_latency",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_provider_probe_consecutive_failures",
        ),
        Index("ix_provider_probe_status", "status", "last_probe_at"),
    )

    capability: str = Field(primary_key=True, max_length=30)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    config_fingerprint: str = Field(max_length=64)
    configuration_ready: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    status: str = Field(max_length=20)
    last_probe_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_success_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_failure_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    degraded_since_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    latency_ms: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    error_code: str | None = Field(default=None, max_length=100)
    consecutive_failures: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    updated_at: datetime | None = Field(default=None, sa_column=_utc_timestamp())


class KbOperationalIncident(SQLModel, table=True):
    """Sanitized, deduplicated KB consistency/projection failure observation."""

    __tablename__ = "kb_operational_incidents"
    __table_args__ = (
        UniqueConstraint(
            "dedupe_key",
            name="uq_kb_operational_incident_dedupe_key",
        ),
        ForeignKeyConstraint(
            ["image_asset_id", "org_id", "kb_id"],
            ["kb_image_assets.id", "kb_image_assets.org_id", "kb_image_assets.kb_id"],
            name="fk_kb_operational_incident_image_asset_tenant",
        ),
        CheckConstraint(
            "category IN ('object_metadata_inconsistency', 'media_projection_failure')",
            name="ck_kb_operational_incident_category",
        ),
        CheckConstraint(
            "occurrence_count > 0",
            name="ck_kb_operational_incident_occurrence_count",
        ),
        CheckConstraint(
            "length(btrim(code)) > 0",
            name="ck_kb_operational_incident_code_nonempty",
        ),
        CheckConstraint(
            "length(btrim(dedupe_key)) > 0",
            name="ck_kb_operational_incident_dedupe_key_nonempty",
        ),
        Index(
            "ix_kb_operational_incidents_open",
            "org_id",
            "category",
            "first_seen_at",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    kb_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("knowledge_bases.id"),
            nullable=False,
            index=True,
        )
    )
    image_asset_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    category: str = Field(max_length=50)
    code: str = Field(max_length=100)
    dedupe_key: str = Field(max_length=200)
    occurrence_count: int = Field(
        default=1,
        sa_column=Column(BigInteger, nullable=False, server_default=text("1")),
    )
    first_seen_at: datetime | None = Field(default=None, sa_column=_utc_timestamp())
    last_seen_at: datetime | None = Field(default=None, sa_column=_utc_timestamp())
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


__all__ = ["KbOperationalIncident", "ProviderProbeStatus", "ServiceHeartbeat"]
