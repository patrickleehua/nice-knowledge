"""operations:运维可观测性三表(心跳 / 能力探测 / KB 运维事件)

Revision ID: cb3e134da690
Revises: 4a6a2d9d8aa4
Create Date: 2026-08-01 14:13:50.648738

autogenerate 产物基础上做了两处人工修正:
- 删掉 ``fk_knowledge_bases_active_snapshot`` 的重复创建:该 FK 由 baseline 用
  ``use_alter=True`` 建过,autogenerate 每次都会重报(它比对不到 use_alter FK);
- 删掉 ``ix_notifications_user_read`` 的 drop:该索引是 baseline 手工加的
  (models 里没声明),不是本次变更要删的东西。

``kb_operational_incidents`` 带 org_id → 按 migrations/rls.py 的 helper 开
FORCE RLS + org_isolation;另两张表是平台级(无 org_id),不开 RLS。
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

from nicekit.migrations.rls import op_enable_org_rls

revision: str = 'cb3e134da690'
down_revision: str | None = '4a6a2d9d8aa4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'provider_probe_statuses',
        sa.Column('capability', sqlmodel.sql.sqltypes.AutoString(length=30), nullable=False),
        sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column('model', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column('config_fingerprint', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('configuration_ready', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('last_probe_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('degraded_since_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("capability IN ('embedding', 'rerank', 'ocr', 'caption')", name='ck_provider_probe_capability'),
        sa.CheckConstraint("config_fingerprint ~ '^[0-9a-f]{64}$'", name='ck_provider_probe_config_fingerprint'),
        sa.CheckConstraint("status IN ('healthy', 'degraded', 'unavailable', 'disabled')", name='ck_provider_probe_status'),
        sa.CheckConstraint('consecutive_failures >= 0', name='ck_provider_probe_consecutive_failures'),
        sa.CheckConstraint('latency_ms IS NULL OR latency_ms >= 0', name='ck_provider_probe_latency'),
        sa.PrimaryKeyConstraint('capability'),
    )
    op.create_index('ix_provider_probe_status', 'provider_probe_statuses', ['status', 'last_probe_at'], unique=False)

    op.create_table(
        'service_heartbeats',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('role', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('instance', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column('version', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("role IN ('api', 'worker', 'beat')", name='ck_service_heartbeat_role'),
        sa.CheckConstraint('length(btrim(instance)) > 0', name='ck_service_heartbeat_instance_nonempty'),
        sa.CheckConstraint('length(btrim(version)) > 0', name='ck_service_heartbeat_version_nonempty'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('role', 'instance', name='uq_service_heartbeat_role_instance'),
    )
    op.create_index('ix_service_heartbeats_role_recorded', 'service_heartbeats', ['role', 'recorded_at'], unique=False)

    op.create_table(
        'kb_operational_incidents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('kb_id', sa.UUID(), nullable=False),
        sa.Column('image_asset_id', sa.UUID(), nullable=True),
        sa.Column('category', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
        sa.Column('code', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column('dedupe_key', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column('occurrence_count', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("category IN ('object_metadata_inconsistency', 'media_projection_failure')", name='ck_kb_operational_incident_category'),
        sa.CheckConstraint('length(btrim(code)) > 0', name='ck_kb_operational_incident_code_nonempty'),
        sa.CheckConstraint('length(btrim(dedupe_key)) > 0', name='ck_kb_operational_incident_dedupe_key_nonempty'),
        sa.CheckConstraint('occurrence_count > 0', name='ck_kb_operational_incident_occurrence_count'),
        sa.ForeignKeyConstraint(
            ['image_asset_id', 'org_id', 'kb_id'],
            ['kb_image_assets.id', 'kb_image_assets.org_id', 'kb_image_assets.kb_id'],
            name='fk_kb_operational_incident_image_asset_tenant',
        ),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedupe_key', name='uq_kb_operational_incident_dedupe_key'),
    )
    op.create_index(op.f('ix_kb_operational_incidents_image_asset_id'), 'kb_operational_incidents', ['image_asset_id'], unique=False)
    op.create_index(op.f('ix_kb_operational_incidents_kb_id'), 'kb_operational_incidents', ['kb_id'], unique=False)
    op.create_index('ix_kb_operational_incidents_open', 'kb_operational_incidents', ['org_id', 'category', 'first_seen_at'], unique=False, postgresql_where=sa.text('resolved_at IS NULL'))
    op.create_index(op.f('ix_kb_operational_incidents_org_id'), 'kb_operational_incidents', ['org_id'], unique=False)

    # 带 org_id 的表一律开 RLS(约束见 migrations/rls.py 文件头)
    op_enable_org_rls(op, 'kb_operational_incidents')


def downgrade() -> None:
    op.drop_index(op.f('ix_kb_operational_incidents_org_id'), table_name='kb_operational_incidents')
    op.drop_index('ix_kb_operational_incidents_open', table_name='kb_operational_incidents', postgresql_where=sa.text('resolved_at IS NULL'))
    op.drop_index(op.f('ix_kb_operational_incidents_kb_id'), table_name='kb_operational_incidents')
    op.drop_index(op.f('ix_kb_operational_incidents_image_asset_id'), table_name='kb_operational_incidents')
    op.drop_table('kb_operational_incidents')  # 策略随表删除
    op.drop_index('ix_service_heartbeats_role_recorded', table_name='service_heartbeats')
    op.drop_table('service_heartbeats')
    op.drop_index('ix_provider_probe_status', table_name='provider_probe_statuses')
    op.drop_table('provider_probe_statuses')
