"""知识库表(FR-110~115)。

知识库(KnowledgeBase)是一等公民对象:每个 org 可建多个不同类型的库,
所有知识实体挂在某个库下(kb_id NOT NULL)。可见性三层:
1. 本 org 自有库(读写);
2. 平台 org(固定 UUID)的库 = 平台层,全租户可读;
3. 其他 org 通过 kb_shares 显式分享给我的库(只读)。
RLS 读策略 = 本 org OR 平台 org OR 已分享;写策略 = 仅本 org(见迁移文件)。

"知识图谱" = 通用实体(kb_entities,类型由 kb_entity_types 注册)+ 图边(kb_graph_edges);
经审核的 FactClaim 在快照 SQL 投影物化时完成实体关联。SDK 不内置任何行业专表,
行业实体一律以 KbEntityType + KbEntity 的注册式结构存放。
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from nicekit.core.config import get_settings
from nicekit.domain.kb_media import ImageEnrichmentStatus, ImageReviewStatus

_settings = get_settings()

# 向量维度参数化(MIGRATION-PLAN §5.5 B32):与迁移中 vector(D) 必须一致,
# 换 embedding 模型走 embedding_migration_campaigns 全量 reindex,不在此处随意改。
# 部署期一次性确定;KbSettings 未提供 kb_embedding_dim 时回落 1024(bge-m3 维度)。
EMBEDDING_DIM: int = int(getattr(_settings, "kb_embedding_dim", 1024))

# 中文分词全文检索配置名(MIGRATION-PLAN §5.5 B15):由 deploy/ 的 PG 初始化 SQL
# 创建的 TEXT SEARCH CONFIGURATION,Computed tsvector 列与检索层必须用同一个值。
# KbSettings 未提供 kb_fts_regconfig 时回落 public.nicekit_zhparser。
KB_FTS_REGCONFIG: str = str(getattr(_settings, "kb_fts_regconfig", "public.nicekit_zhparser"))


class DocType(StrEnum):
    """文档类型的**内置两档**。

    SDK 不做行业分类:`source_documents.doc_type` 是开放字符串列,除这两个内置值
    外还可以存任意已注册的实体类型 key(kb_entity_types.type_key),用于给
    typed 抽取阶段(`extract_typed:{doc_type}`)选契约。摄入链的分支语义只认
    GENERAL(仅切 chunk 不做结构化抽取)与其他(走通用实体抽取)。
    """

    UNCLASSIFIED = "unclassified"
    GENERAL = "general"  # 仅切 chunk 检索,不做结构化抽取


class DocStatus(StrEnum):
    # 原始文件与 immutable revision 已保存，但用户尚未显式排入摄入队列。
    # 该状态不能被 worker、重试或可靠性清扫当作可执行候选。
    STAGED = "staged"
    UPLOADED = "uploaded"
    PARSING = "parsing"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"  # 人工取消(排队中直接生效;解析中在段间协作式停止)
    # 人工暂停:与取消同为协作式停止,区别在于已完成阶段的产物保留且可继续,
    # 续跑靠 ingest_run 的阶段幂等键跳过已 staged 的段,不重复花 LLM/OCR 的钱
    PAUSED = "paused"


class RevisionStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    STAGED = "staged"
    ACTIVE = "active"
    FAILED = "failed"
    TOMBSTONED = "tombstoned"


class DocumentLifecycleStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWAL_PENDING = "withdrawal_pending"
    WITHDRAWN = "withdrawn"
    REINGESTION_PENDING = "reingestion_pending"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


class DocumentOperationType(StrEnum):
    WITHDRAWAL = "withdrawal"
    REINGESTION = "reingestion"
    PURGE = "purge"


class DocumentOperationStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class KnowledgeBaseLifecycleStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


class KnowledgeBaseLifecycleOperationType(StrEnum):
    PURGE = "purge"


class KnowledgeBaseLifecycleOperationStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class KnowledgeBaseLifecyclePhase(StrEnum):
    REVALIDATE_AND_QUIESCE = "revalidate_and_quiesce"
    DELETE_OBJECTS = "delete_objects"
    CLEANUP_METADATA = "cleanup_metadata"
    FINALIZE_AUDIT_SHELL = "finalize_audit_shell"
    COMPLETED = "completed"


class KnowledgeBasePurgeObjectStatus(StrEnum):
    PENDING = "pending"
    DELETED = "deleted"
    SKIPPED_SHARED = "skipped_shared"
    FAILED = "failed"


class IngestRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STAGED = "staged"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class FactReviewStatus(StrEnum):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ORPHANED = "orphaned"


class GraphPredicate(StrEnum):
    LOCATED_IN = "located_in"
    NEAR = "near"
    INCLUDES = "includes"
    PART_OF = "part_of"
    SERVES = "serves"
    SUPPORTS = "supports"
    DERIVED_FROM = "derived_from"
    RELATED = "related"
    SHARED_CONTEXT = "shared_context"


class GraphDirection(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class GraphEdgeKind(StrEnum):
    DIRECT = "direct"
    SHARED_SOURCE = "shared_source"


class SnapshotStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class EmbeddingMigrationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DUAL_READ = "dual_read"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class EmbeddingReindexJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    CANCELED = "canceled"


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _org_id() -> Column:
    return Column(PGUUID(as_uuid=True), nullable=False, index=True)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated_at() -> Column:
    return Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


def _kb_id() -> Column:
    return Column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )


def _snapshot_id() -> Column:
    return Column(PGUUID(as_uuid=True), nullable=True)


class KnowledgeBase(SQLModel, table=True):
    """一等公民:一个 org 下可建多个库,库间隔离,可显式分享。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "org_id",
            name="uq_knowledge_base_tenant_identity",
        ),
        ForeignKeyConstraint(
            ["id", "active_snapshot_id"],
            ["knowledge_snapshots.kb_id", "knowledge_snapshots.id"],
            name="fk_knowledge_bases_active_snapshot",
            use_alter=True,
        ),
        CheckConstraint(
            "consumption_epoch >= 0",
            name="ck_knowledge_base_consumption_epoch",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'archived', 'purge_pending', 'purged')",
            name="ck_knowledge_base_lifecycle_status",
        ),
        CheckConstraint(
            "(archived_at IS NULL AND archived_by IS NULL AND archive_reason IS NULL) "
            "OR (archived_at IS NOT NULL AND archived_by IS NOT NULL "
            "AND archive_reason IS NOT NULL AND length(btrim(archive_reason)) > 0)",
            name="ck_knowledge_base_archive_audit_state",
        ),
        CheckConstraint(
            "(lifecycle_status = 'active' "
            "AND purge_requested_at IS NULL AND purged_at IS NULL) "
            "OR (lifecycle_status = 'archived' "
            "AND archived_at IS NOT NULL AND purge_requested_at IS NULL "
            "AND purged_at IS NULL) "
            "OR (lifecycle_status = 'purge_pending' "
            "AND archived_at IS NOT NULL AND purge_requested_at IS NOT NULL "
            "AND purged_at IS NULL) "
            "OR (lifecycle_status = 'purged' "
            "AND archived_at IS NOT NULL AND purge_requested_at IS NOT NULL "
            "AND purged_at IS NOT NULL)",
            name="ck_knowledge_base_lifecycle_audit_state",
        ),
        Index(
            "ix_knowledge_bases_org_lifecycle",
            "org_id",
            "lifecycle_status",
            "created_at",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    name: str = Field(max_length=200)
    # 纯展示用 tag(开放字符串,租户自定义如 mixed/document/faq/policy),
    # SDK 内**没有任何分支依赖它**——摄入/检索/权限都不读这个字段。
    kb_type: str = Field(default="mixed", sa_column=Column(String(30), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # per-KB 摄入切片配置(KB-2):经 domain.kb.IngestProfile 校验后的 dict;
    # None = 用默认值(parser 取全局 kb_parser_backend,切片用 IngestProfile 默认)
    ingest_profile: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Wiki 左栏的团队共享排序。折叠状态属于个人浏览偏好，不在服务端保存。
    wiki_navigation: dict | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    active_snapshot_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    lifecycle_status: KnowledgeBaseLifecycleStatus = Field(
        default=KnowledgeBaseLifecycleStatus.ACTIVE,
        sa_column=Column(
            String(30),
            nullable=False,
            server_default=text("'active'"),
        ),
    )
    consumption_epoch: int = Field(
        default=0,
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default=text("0"),
        ),
    )
    archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    archived_by: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    archive_reason: str | None = Field(default=None, max_length=500)
    purge_requested_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    purged_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbShare(SQLModel, table=True):
    """库级分享:owner org 把某个库只读分享给 grantee org。"""

    __tablename__ = "kb_shares"

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())  # 库 owner(冗余,便于 RLS)
    kb_id: UUID = Field(sa_column=_kb_id())
    grantee_org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    created_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KnowledgeBaseLifecycleOperation(SQLModel, table=True):
    """Durable, tenant-scoped lifecycle work for one knowledge base."""

    __tablename__ = "knowledge_base_lifecycle_operations"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            name="uq_kb_lifecycle_operation_tenant_identity",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_kb_lifecycle_operation_idempotency",
        ),
        UniqueConstraint(
            "outbox_event_id",
            name="uq_kb_lifecycle_operation_outbox_event",
        ),
        ForeignKeyConstraint(
            ["kb_id", "org_id"],
            ["knowledge_bases.id", "knowledge_bases.org_id"],
            name="fk_kb_lifecycle_operation_kb_tenant",
        ),
        CheckConstraint(
            "operation_type = 'purge'",
            name="ck_kb_lifecycle_operation_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')",
            name="ck_kb_lifecycle_operation_status",
        ),
        CheckConstraint(
            "phase IN ('revalidate_and_quiesce', 'delete_objects', "
            "'cleanup_metadata', 'finalize_audit_shell', 'completed')",
            name="ck_kb_lifecycle_operation_phase",
        ),
        CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kb_lifecycle_operation_plan_hash",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_kb_lifecycle_operation_idempotency_key_nonempty",
        ),
        CheckConstraint(
            "length(btrim(manifest_version)) > 0",
            name="ck_kb_lifecycle_operation_manifest_version_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(confirmation_summary) = 'object' "
            "AND jsonb_typeof(manifest_summary) = 'object'",
            name="ck_kb_lifecycle_operation_json_shapes",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_kb_lifecycle_operation_attempt_count",
        ),
        CheckConstraint(
            "(status = 'completed' AND phase = 'completed' "
            "AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_kb_lifecycle_operation_completed_state",
        ),
        CheckConstraint(
            "(status IN ('failed', 'dead_letter') AND failed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL "
            "AND length(btrim(last_error_code)) > 0 "
            "AND last_error_message IS NOT NULL "
            "AND length(btrim(last_error_message)) > 0) "
            "OR (status NOT IN ('failed', 'dead_letter') AND failed_at IS NULL)",
            name="ck_kb_lifecycle_operation_failed_state",
        ),
        Index(
            "ix_kb_lifecycle_operations_kb_created",
            "org_id",
            "kb_id",
            "created_at",
        ),
        Index(
            "ix_kb_lifecycle_operations_work_queue",
            "org_id",
            "status",
            "updated_at",
            postgresql_where=text(
                "status IN ('pending', 'processing', 'failed', 'dead_letter')"
            ),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    operation_type: KnowledgeBaseLifecycleOperationType = Field(
        default=KnowledgeBaseLifecycleOperationType.PURGE,
        sa_column=Column(
            String(30),
            nullable=False,
            server_default=text("'purge'"),
        ),
    )
    status: KnowledgeBaseLifecycleOperationStatus = Field(
        default=KnowledgeBaseLifecycleOperationStatus.PENDING,
        sa_column=Column(
            String(30),
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    phase: KnowledgeBaseLifecyclePhase = Field(
        default=KnowledgeBaseLifecyclePhase.REVALIDATE_AND_QUIESCE,
        sa_column=Column(
            String(50),
            nullable=False,
            server_default=text("'revalidate_and_quiesce'"),
        ),
    )
    plan_hash: str = Field(max_length=64)
    idempotency_key: str = Field(max_length=200)
    requested_by: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False)
    )
    confirmation_summary: dict = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'{}'::jsonb"),
        ),
    )
    manifest_version: str = Field(default="kb-purge-v1", max_length=50)
    manifest_summary: dict = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'{}'::jsonb"),
        ),
    )
    outbox_event_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("outbox_events.id"),
            nullable=True,
        ),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    last_error_code: str | None = Field(default=None, max_length=100)
    last_error_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    failed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class KnowledgeBasePurgeObject(SQLModel, table=True):
    """Idempotent per-object progress ledger for one KB purge operation."""

    __tablename__ = "knowledge_base_purge_objects"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "bucket",
            "object_key",
            "object_version",
            name="uq_kb_purge_object_identity",
        ),
        ForeignKeyConstraint(
            ["operation_id", "org_id", "kb_id"],
            [
                "knowledge_base_lifecycle_operations.id",
                "knowledge_base_lifecycle_operations.org_id",
                "knowledge_base_lifecycle_operations.kb_id",
            ],
            name="fk_kb_purge_object_operation_tenant",
        ),
        CheckConstraint(
            "status IN ('pending', 'deleted', 'skipped_shared', 'failed')",
            name="ck_kb_purge_object_status",
        ),
        CheckConstraint(
            "length(btrim(bucket)) > 0 "
            "AND length(btrim(object_key)) > 0 "
            "AND length(btrim(object_kind)) > 0",
            name="ck_kb_purge_object_identity_nonempty",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_kb_purge_object_attempt_count",
        ),
        CheckConstraint(
            "(status IN ('deleted', 'skipped_shared') "
            "AND completed_at IS NOT NULL) "
            "OR (status NOT IN ('deleted', 'skipped_shared') "
            "AND completed_at IS NULL)",
            name="ck_kb_purge_object_completed_state",
        ),
        CheckConstraint(
            "(status = 'failed' AND last_error_code IS NOT NULL "
            "AND length(btrim(last_error_code)) > 0 "
            "AND last_error_message IS NOT NULL "
            "AND length(btrim(last_error_message)) > 0) "
            "OR status <> 'failed'",
            name="ck_kb_purge_object_failed_state",
        ),
        Index(
            "ix_kb_purge_objects_operation_status",
            "operation_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_kb_purge_objects_kb_status",
            "org_id",
            "kb_id",
            "status",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    operation_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    bucket: str = Field(max_length=255)
    object_key: str = Field(max_length=1000)
    object_version: str = Field(
        default="",
        sa_column=Column(
            String(255),
            nullable=False,
            server_default=text("''"),
        ),
    )
    etag: str | None = Field(default=None, max_length=255)
    expected_missing: bool = Field(
        default=False,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=text("false"),
        ),
    )
    object_kind: str = Field(max_length=50)
    status: KnowledgeBasePurgeObjectStatus = Field(
        default=KnowledgeBasePurgeObjectStatus.PENDING,
        sa_column=Column(
            String(30),
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    last_error_code: str | None = Field(default=None, max_length=100)
    last_error_message: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class KbPage(SQLModel, table=True):
    """通用 wiki 页节点(borrow llm_wiki 的页面概念):
    page_type 开放字符串(concept/rule/faq/policy/…),结构化实体之外的
    任意类型知识都可以成为图节点,新增类型零迁移。content 为 Markdown,
    可被切 chunk 进向量检索。"""

    __tablename__ = "kb_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_pages_snapshot",
        ),
        Index("ix_kb_pages_kb_snapshot", "kb_id", "snapshot_id"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID | None = Field(default=None, sa_column=_snapshot_id())
    page_type: str = Field(default="concept", sa_column=Column(String(30), nullable=False))
    title: str = Field(max_length=300, index=True)
    content: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # 自动 Wiki 只能写待审核草稿；content 始终表示已发布正文。
    draft_content: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    draft_status: str | None = Field(
        default=None, sa_column=Column(String(20), nullable=True)
    )
    meta: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # 已发布页面来源:human 人工 / llm 自动生成后经人工发布 / system。
    # 自动流程不得改写已有页面的 origin。
    origin: str = Field(
        default="human",
        sa_column=Column(String(20), nullable=False, server_default=text("'human'")),
    )
    source_doc_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("source_documents.id"), nullable=True),
    )
    created_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class EntityReviewPolicy(StrEnum):
    """类型级 AI 确认档位(规划文档 5.2 第 4 点):auto=校验过即确认,
    ai=AI 复核 agent 判定(kb.review.judge 三档),human=人工必审。"""

    AUTO = "auto"
    AI = "ai"
    HUMAN = "human"


class KbEntityType(SQLModel, table=True):
    """实体类型注册表(KB 通用化):类型是可注册的 schema 配置,不是写死的表。

    - SDK 只 seed 一个兜底类型 `concept`(is_builtin,不可删);行业类型由宿主
      通过 `ensure_builtin_entity_types(specs)` 注册,平台 org 下的定义全租户可见;
    - 任意类型(内置或租户自建)的实体数据统一走 kb_entities 通用存储,无专表;
    - field_schema = JSON Schema,是 LLM 抽取输出与人工编辑共用的强校验层
      (llm_wiki 没有而我们必须有的,规划文档 5.2 第 3 点);
    - filterable_fields = 参与结构化检索过滤/排序的字段声明
      [{"field","type"(text|number|date),"label"}],检索层据此动态构建 JSONB 条件;
    - card_template = 实体卡片语句模板("{name}"占位),产出进 chunk 检索通道。
    """

    __tablename__ = "kb_entity_types"
    __table_args__ = (
        UniqueConstraint("org_id", "type_key", name="uq_kb_entity_types_org_key"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    type_key: str = Field(max_length=50, index=True)
    display_name: str = Field(max_length=100)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    field_schema: dict = Field(sa_column=Column(JSONB, nullable=False))
    filterable_fields: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    card_template: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    review_policy: str = Field(
        default=EntityReviewPolicy.AI.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'ai'")),
    )
    is_builtin: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class KbEntity(SQLModel, table=True):
    """通用实体存储:所有已注册类型的实体行(SDK 内唯一的结构化实体落表)。

    挂快照、同款 RLS+snapshot 限制策略、投影物化幂等 id;
    attributes 必须通过所属类型 field_schema 的 JSON Schema 校验后才能写入;
    name 为提升列(词面匹配/实体卡标题/归一);结构化过滤经 attributes 的
    GIN 索引 + 声明字段的 JSONB 表达式条件。
    """

    __tablename__ = "kb_entities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_entities_snapshot",
        ),
        Index("ix_kb_entities_kb_snapshot", "kb_id", "snapshot_id"),
        Index("ix_kb_entities_type", "kb_id", "entity_type_key"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID | None = Field(default=None, sa_column=_snapshot_id())
    entity_type_key: str = Field(max_length=50)
    name: str = Field(max_length=300, index=True)
    attributes: dict = Field(sa_column=Column(JSONB, nullable=False))
    source_doc_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("source_documents.id"), nullable=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class SourceDocument(SQLModel, table=True):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "sha256", name="uq_source_document_kb_content"),
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            name="uq_source_document_tenant_identity",
        ),
        CheckConstraint(
            "lifecycle_status IN "
            "('active', 'withdrawal_pending', 'withdrawn', 'reingestion_pending', "
            "'purge_pending', 'purged')",
            name="ck_source_document_lifecycle_status",
        ),
        CheckConstraint(
            "(legal_hold_at IS NULL AND legal_hold_by IS NULL "
            "AND legal_hold_reason IS NULL) OR "
            "(legal_hold_at IS NOT NULL AND legal_hold_by IS NOT NULL "
            "AND legal_hold_reason IS NOT NULL "
            "AND length(btrim(legal_hold_reason)) > 0)",
            name="ck_source_document_legal_hold_state",
        ),
        Index(
            "ix_source_documents_kb_lifecycle",
            "kb_id",
            "lifecycle_status",
        ),
        Index(
            "ix_source_documents_legal_hold",
            "org_id",
            "legal_hold_at",
            postgresql_where=text("legal_hold_at IS NOT NULL"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    filename: str = Field(max_length=500)
    object_key: str = Field(max_length=1000)
    # 统一 Markdown 中间表示(KB-1):解析产物落 MinIO 的 key;解析前为 None
    markdown_key: str | None = Field(default=None, max_length=1000)
    # 实际完成解析的后端(fast/docling;降级时记录的是兜底后端)
    parser_name: str | None = Field(default=None, max_length=50)
    sha256: str = Field(max_length=64, index=True)
    # varchar 存开放字符串(与 Role 同决策):加类型/状态不需要 ALTER TYPE。
    # 取值 = DocType 的两个内置值,或任意已注册的 kb_entity_types.type_key
    # (typed 抽取阶段 `extract_typed:{doc_type}` 据此选契约);故**不加 CHECK 约束**。
    doc_type: str = Field(sa_column=Column(String(50), nullable=False))
    status: DocStatus = Field(
        default=DocStatus.UPLOADED, sa_column=Column(String(50), nullable=False)
    )
    lifecycle_status: DocumentLifecycleStatus = Field(
        default=DocumentLifecycleStatus.ACTIVE,
        sa_column=Column(
            String(30),
            nullable=False,
            server_default=text("'active'"),
        ),
    )
    legal_hold_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    legal_hold_by: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    legal_hold_reason: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    version: int = Field(default=1)
    # 文件夹上传时的库内相对路径(如 "2026 年度报表/Q1.xlsx"),用于 UI 目录分组
    rel_path: str | None = Field(default=None, max_length=500)
    # 处理进度 0-100(全阶段加权:解析/图片理解/切片/抽取,见 ingestion._ProgressReporter)
    progress: int = Field(default=0)
    # 当前阶段与阶段内分子分母:让"解析中"能显示成"图片理解 76/244",
    # 否则图片多的文档会在 progress=0 上停留几十分钟,用户无从判断是否卡死
    progress_stage: str | None = Field(default=None, max_length=30)
    progress_done: int = Field(default=0)
    progress_total: int = Field(default=0)
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    # 过期提醒已发标记(prod-readiness-4):sweep 幂等,不重复轰炸
    expiry_notified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    parsing_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class DocumentRevision(SQLModel, table=True):
    """Immutable source version and references to its parsing artifacts."""

    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("doc_id", "revision_no", name="uq_document_revision_number"),
        Index(
            "uq_document_revision_live_content",
            "doc_id",
            "sha256",
            unique=True,
            postgresql_where=text("tombstoned_at IS NULL"),
        ),
        UniqueConstraint(
            "id", "org_id", "kb_id", name="uq_document_revision_tenant_identity"
        ),
        UniqueConstraint(
            "id",
            "doc_id",
            "org_id",
            "kb_id",
            name="uq_document_revision_support_identity",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    doc_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("source_documents.id"), nullable=False, index=True
        )
    )
    revision_no: int = Field(sa_column=Column(Integer, nullable=False))
    sha256: str = Field(max_length=64, index=True)
    original_object_key: str = Field(max_length=1000)
    structured_json_key: str | None = Field(default=None, max_length=1000)
    markdown_key: str | None = Field(default=None, max_length=1000)
    status: RevisionStatus = Field(
        default=RevisionStatus.UPLOADED,
        sa_column=Column(String(30), nullable=False, server_default=text("'uploaded'")),
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tombstoned_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    tombstoned_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    tombstone_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbDocumentOperation(SQLModel, table=True):
    """Durable user-facing withdrawal, reingestion, or purge operation."""

    __tablename__ = "kb_document_operations"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            name="uq_kb_document_operation_tenant_identity",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_kb_document_operation_idempotency_key",
        ),
        CheckConstraint(
            "operation_type IN ('withdrawal', 'reingestion', 'purge')",
            name="ck_kb_document_operation_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')",
            name="ck_kb_document_operation_status",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_kb_document_operation_idempotency_key_nonempty",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0",
            name="ck_kb_document_operation_reason_nonempty",
        ),
        CheckConstraint(
            "stage IS NULL OR length(btrim(stage)) > 0",
            name="ck_kb_document_operation_stage_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(impact_summary) = 'object'",
            name="ck_kb_document_operation_impact_summary",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_kb_document_operation_attempts",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) "
            "OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_kb_document_operation_completed_state",
        ),
        CheckConstraint(
            "(status IN ('failed', 'dead_letter') AND failed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL "
            "AND length(btrim(last_error_code)) > 0 "
            "AND last_error IS NOT NULL AND length(btrim(last_error)) > 0) "
            "OR (status NOT IN ('failed', 'dead_letter') AND failed_at IS NULL)",
            name="ck_kb_document_operation_failed_state",
        ),
        ForeignKeyConstraint(
            ["document_id", "org_id", "kb_id"],
            [
                "source_documents.id",
                "source_documents.org_id",
                "source_documents.kb_id",
            ],
            name="fk_kb_document_operation_document_tenant",
        ),
        ForeignKeyConstraint(
            ["revision_id", "document_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.doc_id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_document_operation_revision_document_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "target_snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_document_operation_target_snapshot_tenant",
        ),
        Index(
            "ix_kb_document_operations_document_created",
            "document_id",
            "created_at",
        ),
        Index(
            "ix_kb_document_operations_status",
            "org_id",
            "status",
            "created_at",
            postgresql_where=text(
                "status IN ('pending', 'processing', 'failed', 'dead_letter')"
            ),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    document_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    revision_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    operation_type: DocumentOperationType = Field(
        sa_column=Column(String(20), nullable=False)
    )
    status: DocumentOperationStatus = Field(
        default=DocumentOperationStatus.PENDING,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'pending'"),
        ),
    )
    stage: str | None = Field(default=None, max_length=50)
    idempotency_key: str = Field(max_length=200)
    requested_by: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    reason: str = Field(max_length=500)
    target_snapshot_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    impact_summary: dict = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'{}'::jsonb"),
        ),
    )
    attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    retryable: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    last_error_code: str | None = Field(default=None, max_length=100)
    last_error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    failed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class IngestRun(SQLModel, table=True):
    """Leased, retryable processing unit for one revision stage and segment."""

    __tablename__ = "ingest_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ingest_run_idempotency_key"),
        UniqueConstraint(
            "revision_id", "stage", "segment_no", name="uq_ingest_run_stage_segment"
        ),
        ForeignKeyConstraint(
            ["revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_ingest_run_revision_tenant",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    revision_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("document_revisions.id"),
            nullable=False,
            index=True,
        )
    )
    stage: str = Field(max_length=50)
    segment_no: int = Field(default=0, sa_column=Column(Integer, nullable=False))
    status: IngestRunStatus = Field(
        default=IngestRunStatus.QUEUED,
        sa_column=Column(String(30), nullable=False, server_default=text("'queued'")),
    )
    idempotency_key: str = Field(max_length=200)
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    heartbeat_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    available_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    stats: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbImageAsset(SQLModel, table=True):
    """One immutable source occurrence with governed image enrichment."""

    __tablename__ = "kb_image_assets"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            name="uq_kb_image_asset_tenant_identity",
        ),
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            "revision_id",
            name="uq_kb_image_asset_revision_identity",
        ),
        UniqueConstraint(
            "revision_id",
            "source_occurrence",
            name="uq_kb_image_asset_revision_occurrence",
        ),
        ForeignKeyConstraint(
            ["revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_image_asset_revision_tenant",
        ),
        CheckConstraint(
            "image_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_sha256",
        ),
        CheckConstraint(
            "extraction_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_extraction_fingerprint",
        ),
        CheckConstraint(
            "ocr_fingerprint IS NULL OR ocr_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_ocr_fingerprint",
        ),
        CheckConstraint(
            "caption_fingerprint IS NULL OR caption_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_caption_fingerprint",
        ),
        CheckConstraint(
            "config_fingerprint IS NULL OR config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_config_fingerprint",
        ),
        CheckConstraint(
            "thumbnail_sha256 IS NULL OR thumbnail_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_kb_image_asset_thumbnail_sha256",
        ),
        CheckConstraint(
            "size_bytes > 0",
            name="ck_kb_image_asset_size_bytes",
        ),
        CheckConstraint(
            "page IS NULL OR page >= 1",
            name="ck_kb_image_asset_page",
        ),
        CheckConstraint(
            "slide IS NULL OR slide >= 1",
            name="ck_kb_image_asset_slide",
        ),
        CheckConstraint(
            "page IS NULL OR slide IS NULL",
            name="ck_kb_image_asset_page_slide_exclusive",
        ),
        CheckConstraint(
            "reading_order IS NULL OR reading_order >= 0",
            name="ck_kb_image_asset_reading_order",
        ),
        CheckConstraint(
            """
            CASE
                WHEN bbox IS NULL THEN TRUE
                WHEN jsonb_typeof(bbox) <> 'object' THEN FALSE
                WHEN NOT (
                    bbox ? 'left' AND bbox ? 'top'
                    AND bbox ? 'right' AND bbox ? 'bottom'
                ) THEN FALSE
                WHEN jsonb_typeof(bbox -> 'left') <> 'number'
                    OR jsonb_typeof(bbox -> 'top') <> 'number'
                    OR jsonb_typeof(bbox -> 'right') <> 'number'
                    OR jsonb_typeof(bbox -> 'bottom') <> 'number'
                    THEN FALSE
                ELSE
                    (bbox ->> 'left')::numeric >= 0
                    AND (bbox ->> 'top')::numeric >= 0
                    AND (bbox ->> 'right')::numeric <= 1
                    AND (bbox ->> 'bottom')::numeric <= 1
                    AND (bbox ->> 'right')::numeric > (bbox ->> 'left')::numeric
                    AND (bbox ->> 'bottom')::numeric > (bbox ->> 'top')::numeric
            END
            """,
            name="ck_kb_image_asset_bbox",
        ),
        CheckConstraint(
            """
            (
                content_type IS NULL
                AND width IS NULL
                AND height IS NULL
                AND original_object_key IS NULL
            )
            OR (
                content_type IN ('image/png', 'image/jpeg', 'image/webp')
                AND width > 0
                AND height > 0
                AND original_object_key IS NOT NULL
                AND length(btrim(original_object_key)) > 0
            )
            """,
            name="ck_kb_image_asset_original_metadata",
        ),
        CheckConstraint(
            """
            (
                thumbnail_object_key IS NULL
                AND thumbnail_sha256 IS NULL
                AND thumbnail_content_type IS NULL
                AND thumbnail_size_bytes IS NULL
                AND thumbnail_width IS NULL
                AND thumbnail_height IS NULL
            )
            OR (
                thumbnail_object_key IS NOT NULL
                AND length(btrim(thumbnail_object_key)) > 0
                AND thumbnail_sha256 IS NOT NULL
                AND thumbnail_content_type IN ('image/png', 'image/jpeg', 'image/webp')
                AND thumbnail_size_bytes > 0
                AND thumbnail_width > 0
                AND thumbnail_height > 0
            )
            """,
            name="ck_kb_image_asset_thumbnail_metadata",
        ),
        CheckConstraint(
            "enrichment_status IN ('pending', 'processing', 'succeeded', 'failed')",
            name="ck_kb_image_asset_enrichment_status",
        ),
        CheckConstraint(
            "review_status IN ('needs_review', 'accepted', 'excluded', 'rejected')",
            name="ck_kb_image_asset_review_status",
        ),
        CheckConstraint(
            "(enrichment_status = 'failed' AND failure_code IS NOT NULL "
            "AND length(btrim(failure_code)) > 0) "
            "OR (enrichment_status <> 'failed' AND failure_code IS NULL)",
            name="ck_kb_image_asset_failure_state",
        ),
        CheckConstraint(
            """
            review_status <> 'accepted'
            OR (
                enrichment_status = 'succeeded'
                AND original_object_key IS NOT NULL
                AND thumbnail_object_key IS NOT NULL
                AND (
                    length(btrim(COALESCE(caption, ''))) > 0
                    OR length(btrim(COALESCE(ocr_text, ''))) > 0
                )
            )
            """,
            name="ck_kb_image_asset_accepted_state",
        ),
        CheckConstraint(
            "lock_version >= 1",
            name="ck_kb_image_asset_lock_version",
        ),
        Index(
            "ix_kb_image_assets_review_queue",
            "org_id",
            "kb_id",
            "review_status",
            "created_at",
            postgresql_where=text("review_status IN ('needs_review', 'rejected')"),
        ),
        Index(
            "ix_kb_image_assets_enrichment_queue",
            "org_id",
            "kb_id",
            "enrichment_status",
            "updated_at",
            postgresql_where=text(
                "enrichment_status IN ('pending', 'processing', 'failed')"
            ),
        ),
        Index(
            "ix_kb_image_assets_revision_position",
            "revision_id",
            "page",
            "slide",
            "reading_order",
        ),
        Index(
            "ix_kb_image_assets_content_hash",
            "org_id",
            "kb_id",
            "image_sha256",
        ),
        Index("ix_kb_image_assets_ingest_run_id", "ingest_run_id"),
        Index(
            "ix_kb_image_assets_failure_code",
            "failure_code",
            postgresql_where=text("failure_code IS NOT NULL"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    doc_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("source_documents.id"),
            nullable=False,
            index=True,
        )
    )
    revision_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    ingest_run_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("ingest_runs.id"),
            nullable=True,
        ),
    )
    source_occurrence: str = Field(max_length=500)
    parser_name: str = Field(max_length=50)
    parser_item_ref: str | None = Field(default=None, max_length=500)
    page: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    slide: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    reading_order: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    bbox: dict[str, float] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    surrounding_text: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    image_sha256: str = Field(max_length=64)
    size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    content_type: str | None = Field(default=None, max_length=50)
    width: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    height: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    original_object_key: str | None = Field(default=None, max_length=1000)
    thumbnail_object_key: str | None = Field(default=None, max_length=1000)
    thumbnail_sha256: str | None = Field(default=None, max_length=64)
    thumbnail_content_type: str | None = Field(default=None, max_length=50)
    thumbnail_size_bytes: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    thumbnail_width: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    thumbnail_height: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    ocr_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    caption: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    ocr_provider: str | None = Field(default=None, max_length=100)
    ocr_model: str | None = Field(default=None, max_length=200)
    caption_provider: str | None = Field(default=None, max_length=100)
    caption_model: str | None = Field(default=None, max_length=200)
    extraction_fingerprint: str = Field(max_length=64)
    ocr_fingerprint: str | None = Field(default=None, max_length=64)
    caption_fingerprint: str | None = Field(default=None, max_length=64)
    config_fingerprint: str | None = Field(default=None, max_length=64)
    enrichment_status: ImageEnrichmentStatus = Field(
        default=ImageEnrichmentStatus.PENDING,
        sa_column=Column(
            String(20), nullable=False, server_default=text("'pending'")
        ),
    )
    review_status: ImageReviewStatus = Field(
        default=ImageReviewStatus.NEEDS_REVIEW,
        sa_column=Column(
            String(20), nullable=False, server_default=text("'needs_review'")
        ),
    )
    failure_code: str | None = Field(default=None, max_length=100)
    failure_detail: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    reviewed_by: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    review_source: str | None = Field(default=None, max_length=50)
    review_note: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    lock_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class KbImageAssetEvent(SQLModel, table=True):
    """Append-only enrichment and review history for one image occurrence."""

    __tablename__ = "kb_image_asset_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["image_asset_id", "org_id", "kb_id"],
            [
                "kb_image_assets.id",
                "kb_image_assets.org_id",
                "kb_image_assets.kb_id",
            ],
            name="fk_kb_image_asset_event_asset_tenant",
        ),
        UniqueConstraint(
            "image_asset_id",
            "event_version",
            name="uq_kb_image_asset_event_version",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_kb_image_asset_event_idempotency_key",
        ),
        CheckConstraint(
            "event_version >= 1",
            name="ck_kb_image_asset_event_version",
        ),
        CheckConstraint(
            "length(btrim(action)) > 0",
            name="ck_kb_image_asset_event_action_nonempty",
        ),
        CheckConstraint(
            "actor_kind IN ('human', 'policy', 'system', 'provider')",
            name="ck_kb_image_asset_event_actor_kind",
        ),
        CheckConstraint(
            "before_state IS NULL OR jsonb_typeof(before_state) = 'object'",
            name="ck_kb_image_asset_event_before_state",
        ),
        CheckConstraint(
            "after_state IS NULL OR jsonb_typeof(after_state) = 'object'",
            name="ck_kb_image_asset_event_after_state",
        ),
        Index(
            "ix_kb_image_asset_events_asset_created",
            "image_asset_id",
            "created_at",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    image_asset_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    event_version: int = Field(sa_column=Column(Integer, nullable=False))
    action: str = Field(max_length=100)
    actor_user_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    actor_kind: str = Field(max_length=20)
    before_state: dict | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    after_state: dict | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(max_length=200)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class CanonicalEntity(SQLModel, table=True):
    """KB-local entity registry used by governed facts and graph projections."""

    __tablename__ = "canonical_entities"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "kb_id",
            "id",
            name="uq_canonical_entity_tenant_identity",
        ),
        CheckConstraint(
            "length(btrim(entity_type)) > 0", name="ck_canonical_entity_type_nonempty"
        ),
        CheckConstraint(
            "length(btrim(canonical_name)) > 0",
            name="ck_canonical_entity_name_nonempty",
        ),
        CheckConstraint(
            "support_status IN ('supported', 'unsupported')",
            name="ck_canonical_entity_support_status",
        ),
        CheckConstraint(
            "support_status <> 'unsupported' OR "
            "(unsupported_at IS NOT NULL AND support_status_reason IS NOT NULL "
            "AND length(btrim(support_status_reason)) > 0)",
            name="ck_canonical_entity_unsupported_state",
        ),
        CheckConstraint(
            "NOT is_pinned OR "
            "(pinned_at IS NOT NULL AND pinned_by IS NOT NULL "
            "AND pin_reason IS NOT NULL AND length(btrim(pin_reason)) > 0)",
            name="ck_canonical_entity_pin_state",
        ),
        CheckConstraint(
            "(merged_into_entity_id IS NULL AND merged_at IS NULL "
            "AND merged_by IS NULL AND merge_reason IS NULL) OR "
            "(merged_into_entity_id IS NOT NULL AND merged_into_entity_id <> id "
            "AND merged_at IS NOT NULL AND merged_by IS NOT NULL "
            "AND merge_reason IS NOT NULL AND length(btrim(merge_reason)) > 0)",
            name="ck_canonical_entity_merge_state",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "support_status_snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_canonical_entity_support_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "merged_into_entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_canonical_entity_merge_target_tenant",
        ),
        Index(
            "ix_canonical_entities_lookup",
            "kb_id",
            "entity_type",
            "canonical_name",
        ),
        Index(
            "ix_canonical_entities_tsv_gin",
            "tsv",
            postgresql_using="gin",
        ),
        Index(
            "ix_canonical_entities_support_status",
            "kb_id",
            "support_status",
            "updated_at",
        ),
        Index(
            "ix_canonical_entities_pinned",
            "kb_id",
            "updated_at",
            postgresql_where=text("is_pinned"),
        ),
        Index(
            "ix_canonical_entities_merged_into",
            "merged_into_entity_id",
            postgresql_where=text("merged_into_entity_id IS NOT NULL"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    entity_type: str = Field(max_length=50)
    canonical_name: str = Field(max_length=300)
    tsv: str | None = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(
                f"to_tsvector('{KB_FTS_REGCONFIG}'::regconfig, canonical_name)",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    metadata_: dict = Field(
        default_factory=dict,
        sa_column=Column(
            "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
        ),
    )
    support_status: str = Field(
        default="supported",
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=text("'supported'"),
        ),
    )
    support_status_reason: str | None = Field(default=None, max_length=500)
    support_status_changed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    support_status_snapshot_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    unsupported_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    is_pinned: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    pinned_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    pinned_by: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    pin_reason: str | None = Field(default=None, max_length=500)
    merged_into_entity_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    merged_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    merged_by: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    merge_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class EntityAlias(SQLModel, table=True):
    """Normalized alternate name resolving to one canonical entity within a KB."""

    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint(
            "kb_id",
            "normalized_alias",
            "locale",
            name="uq_entity_alias_kb_normalized_locale",
        ),
        CheckConstraint("length(btrim(alias)) > 0", name="ck_entity_alias_nonempty"),
        CheckConstraint(
            "length(btrim(normalized_alias)) > 0",
            name="ck_entity_alias_normalized_nonempty",
        ),
        CheckConstraint("length(btrim(locale)) > 0", name="ck_entity_alias_locale_nonempty"),
        CheckConstraint("length(btrim(source)) > 0", name="ck_entity_alias_source_nonempty"),
        Index("ix_entity_aliases_entity_id", "entity_id"),
        Index("ix_entity_aliases_tsv_gin", "tsv", postgresql_using="gin"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    entity_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("canonical_entities.id"), nullable=False
        )
    )
    alias: str = Field(max_length=300)
    normalized_alias: str = Field(max_length=300)
    tsv: str | None = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(
                f"to_tsvector('{KB_FTS_REGCONFIG}'::regconfig, "
                "alias || ' ' || normalized_alias)",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    locale: str = Field(
        default="und",
        sa_column=Column(String(35), nullable=False, server_default=text("'und'")),
    )
    source: str = Field(
        default="human",
        sa_column=Column(String(50), nullable=False, server_default=text("'human'")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class FactClaim(SQLModel, table=True):
    """Reviewable fact whose original extraction payload remains immutable."""

    __tablename__ = "fact_claims"
    __table_args__ = (
        UniqueConstraint(
            "id", "org_id", "kb_id", name="uq_fact_claim_tenant_identity"
        ),
        CheckConstraint(
            "review_status IN ('suggested', 'confirmed', 'rejected', 'orphaned')",
            name="ck_fact_claim_review_status",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to",
            name="ck_fact_claim_valid_range",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_claim_confidence",
        ),
        CheckConstraint(
            "length(btrim(subject_type)) > 0", name="ck_fact_claim_subject_type_nonempty"
        ),
        CheckConstraint(
            "length(btrim(predicate)) > 0", name="ck_fact_claim_predicate_nonempty"
        ),
        CheckConstraint(
            "subject_entity_id IS NULL OR object_entity_id IS NULL "
            "OR subject_entity_id <> object_entity_id",
            name="ck_fact_claim_distinct_entity_endpoints",
        ),
        CheckConstraint(
            "review_status <> 'confirmed' "
            "OR predicate NOT IN ("
            "'located_in', 'near', 'includes', 'part_of', 'serves', "
            "'supports', 'derived_from', 'related'"
            ") OR (subject_entity_id IS NOT NULL AND object_entity_id IS NOT NULL)",
            name="ck_fact_claim_confirmed_relation_endpoints",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "subject_entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_fact_claim_subject_entity_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "object_entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_fact_claim_object_entity_tenant",
        ),
        Index(
            "ix_fact_claims_review_queue",
            "org_id",
            "kb_id",
            "review_status",
            "created_at",
            postgresql_where=text(
                "review_status IN ('suggested', 'orphaned')"
            ),
        ),
        Index(
            "ix_fact_claims_subject_predicate",
            "kb_id",
            "subject_type",
            "subject_id",
            "predicate",
        ),
        Index("ix_fact_claims_ingest_run_id", "ingest_run_id"),
        Index("ix_fact_claims_subject_entity_id", "subject_entity_id"),
        Index("ix_fact_claims_object_entity_id", "object_entity_id"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    ingest_run_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("ingest_runs.id"), nullable=True),
    )
    subject_entity_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    object_entity_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    subject_type: str = Field(max_length=50)
    subject_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    predicate: str = Field(max_length=100)
    value_json: dict = Field(sa_column=Column(JSONB, nullable=False))
    raw_payload: dict = Field(sa_column=Column(JSONB, nullable=False))
    corrected_payload: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    valid_from: date | None = Field(default=None, sa_column=Column(Date, nullable=True))
    valid_to: date | None = Field(default=None, sa_column=Column(Date, nullable=True))
    confidence: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    review_status: FactReviewStatus = Field(
        default=FactReviewStatus.SUGGESTED,
        sa_column=Column(
            String(20), nullable=False, server_default=text("'suggested'")
        ),
    )
    model_name: str | None = Field(default=None, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=100)
    # 审核审计:谁定的(如 'ai:kb.review.judge' / 'human')与判定理由
    reviewed_by: str | None = Field(default=None, max_length=100)
    review_note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class EvidenceSpan(SQLModel, table=True):
    """Field-level citation anchored to an immutable document revision."""

    __tablename__ = "evidence_spans"
    __table_args__ = (
        UniqueConstraint(
            "id", "org_id", "kb_id", name="uq_evidence_span_tenant_identity"
        ),
        UniqueConstraint(
            "id",
            "fact_claim_id",
            "revision_id",
            "org_id",
            "kb_id",
            name="uq_evidence_span_support_identity",
        ),
        CheckConstraint("page IS NULL OR page >= 1", name="ck_evidence_span_page"),
        CheckConstraint(
            "(start_line IS NULL AND end_line IS NULL) OR "
            "(start_line IS NOT NULL AND end_line IS NOT NULL "
            "AND start_line >= 1 AND end_line >= start_line)",
            name="ck_evidence_span_line_range",
        ),
        CheckConstraint(
            "page IS NOT NULL OR start_line IS NOT NULL OR cell_ref IS NOT NULL "
            "OR image_asset_id IS NOT NULL",
            name="ck_evidence_span_anchor",
        ),
        CheckConstraint(
            "cell_ref IS NULL OR length(btrim(cell_ref)) > 0",
            name="ck_evidence_span_cell_ref_nonempty",
        ),
        CheckConstraint(
            "length(btrim(quote_text)) > 0", name="ck_evidence_span_quote_nonempty"
        ),
        Index("ix_evidence_spans_fact_claim_id", "fact_claim_id"),
        Index("ix_evidence_spans_revision_page", "revision_id", "page"),
        Index("ix_evidence_spans_chunk_id", "chunk_id"),
        Index("ix_evidence_spans_image_asset_id", "image_asset_id"),
        ForeignKeyConstraint(
            ["image_asset_id", "org_id", "kb_id", "revision_id"],
            [
                "kb_image_assets.id",
                "kb_image_assets.org_id",
                "kb_image_assets.kb_id",
                "kb_image_assets.revision_id",
            ],
            name="fk_evidence_span_image_asset_revision",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    fact_claim_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("fact_claims.id"), nullable=False)
    )
    revision_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("document_revisions.id"), nullable=False
        )
    )
    chunk_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("kb_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    image_asset_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    page: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    start_line: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    end_line: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    cell_ref: str | None = Field(default=None, max_length=100)
    quote_text: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KnowledgeSnapshot(SQLModel, table=True):
    """Immutable, fingerprinted release unit for one knowledge base."""

    __tablename__ = "knowledge_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "kb_id",
            "revision_set_hash",
            "embedding_fingerprint",
            "config_fingerprint",
            name="uq_knowledge_snapshot_fingerprints",
        ),
        UniqueConstraint("kb_id", "id", name="uq_knowledge_snapshot_kb_id_id"),
        UniqueConstraint(
            "org_id",
            "kb_id",
            "id",
            name="uq_knowledge_snapshot_tenant_identity",
        ),
        CheckConstraint(
            "revision_set_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_snapshot_revision_set_hash",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_snapshot_config_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(embedding_fingerprint) = 'object' "
            "AND jsonb_typeof(embedding_fingerprint -> 'provider') = 'string' "
            "AND length(btrim(embedding_fingerprint ->> 'provider')) > 0 "
            "AND jsonb_typeof(embedding_fingerprint -> 'model') = 'string' "
            "AND length(btrim(embedding_fingerprint ->> 'model')) > 0 "
            "AND jsonb_typeof(embedding_fingerprint -> 'dim') = 'number' "
            "AND (embedding_fingerprint ->> 'dim')::numeric > 0 "
            "AND mod((embedding_fingerprint ->> 'dim')::numeric, 1) = 0",
            name="ck_knowledge_snapshot_embedding_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(revision_manifest) = 'array'",
            name="ck_knowledge_snapshot_revision_manifest",
        ),
        CheckConstraint(
            "jsonb_typeof(config_manifest) = 'object'",
            name="ck_knowledge_snapshot_config_manifest",
        ),
        CheckConstraint(
            "jsonb_typeof(build_stats) = 'object'",
            name="ck_knowledge_snapshot_build_stats",
        ),
        CheckConstraint(
            "status IN ('building', 'ready', 'active', 'retired', 'failed')",
            name="ck_knowledge_snapshot_status",
        ),
        CheckConstraint(
            "status <> 'ready' OR ready_at IS NOT NULL",
            name="ck_knowledge_snapshot_ready_at",
        ),
        CheckConstraint(
            "status <> 'active' OR (ready_at IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_knowledge_snapshot_activated_at",
        ),
        CheckConstraint(
            "status <> 'retired' OR (activated_at IS NOT NULL AND retired_at IS NOT NULL)",
            name="ck_knowledge_snapshot_retired_at",
        ),
        CheckConstraint(
            "status <> 'failed' OR (failed_at IS NOT NULL "
            "AND error IS NOT NULL AND length(btrim(error)) > 0)",
            name="ck_knowledge_snapshot_failed_state",
        ),
        Index(
            "uq_knowledge_snapshots_active_kb",
            "kb_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_knowledge_snapshots_build_queue",
            "org_id",
            "status",
            "created_at",
            postgresql_where=text("status IN ('building', 'ready')"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    revision_set_hash: str = Field(max_length=64)
    embedding_fingerprint: dict = Field(sa_column=Column(JSONB, nullable=False))
    config_fingerprint: str = Field(max_length=64)
    revision_manifest: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    config_manifest: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    build_stats: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    status: SnapshotStatus = Field(
        default=SnapshotStatus.BUILDING,
        sa_column=Column(
            String(20), nullable=False, server_default=text("'building'")
        ),
    )
    ready_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    activated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    retired_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    failed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class SnapshotFactSupport(SQLModel, table=True):
    """Immutable fact/evidence membership in one candidate snapshot manifest."""

    __tablename__ = "snapshot_fact_supports"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "fact_claim_id",
            "evidence_span_id",
            name="uq_snapshot_fact_support_membership",
        ),
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            "snapshot_id",
            name="uq_snapshot_fact_support_tenant_identity",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_snapshot_fact_support_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            [
                "evidence_span_id",
                "fact_claim_id",
                "revision_id",
                "org_id",
                "kb_id",
            ],
            [
                "evidence_spans.id",
                "evidence_spans.fact_claim_id",
                "evidence_spans.revision_id",
                "evidence_spans.org_id",
                "evidence_spans.kb_id",
            ],
            name="fk_snapshot_fact_support_evidence_tenant",
        ),
        ForeignKeyConstraint(
            ["revision_id", "doc_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.doc_id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_snapshot_fact_support_revision_document_tenant",
        ),
        ForeignKeyConstraint(
            ["doc_id", "org_id", "kb_id"],
            [
                "source_documents.id",
                "source_documents.org_id",
                "source_documents.kb_id",
            ],
            name="fk_snapshot_fact_support_document_tenant",
        ),
        Index(
            "ix_snapshot_fact_supports_snapshot_fact",
            "snapshot_id",
            "fact_claim_id",
        ),
        Index(
            "ix_snapshot_fact_supports_snapshot_revision",
            "snapshot_id",
            "revision_id",
        ),
        Index(
            "ix_snapshot_fact_supports_snapshot_document",
            "snapshot_id",
            "doc_id",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    fact_claim_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    evidence_span_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    revision_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    doc_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class SnapshotProjectionSupport(SQLModel, table=True):
    """Evidence-backed provenance for one fact-derived snapshot projection row."""

    __tablename__ = "snapshot_projection_supports"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "projection_type",
            "projection_row_id",
            "fact_support_id",
            name="uq_snapshot_projection_support_membership",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_snapshot_projection_support_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            ["fact_support_id", "org_id", "kb_id", "snapshot_id"],
            [
                "snapshot_fact_supports.id",
                "snapshot_fact_supports.org_id",
                "snapshot_fact_supports.kb_id",
                "snapshot_fact_supports.snapshot_id",
            ],
            name="fk_snapshot_projection_support_fact",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "length(btrim(projection_type)) > 0",
            name="ck_snapshot_projection_support_type_nonempty",
        ),
        Index(
            "ix_snapshot_projection_supports_projection",
            "snapshot_id",
            "projection_type",
            "projection_row_id",
        ),
        Index(
            "ix_snapshot_projection_supports_fact",
            "snapshot_id",
            "fact_support_id",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    projection_type: str = Field(max_length=50)
    projection_row_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    fact_support_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbSnapshotEntityNode(SQLModel, table=True):
    """Frozen canonical-entity node membership for one knowledge snapshot."""

    __tablename__ = "kb_snapshot_entity_nodes"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "entity_id",
            name="uq_kb_snapshot_entity_node_membership",
        ),
        UniqueConstraint(
            "org_id",
            "kb_id",
            "snapshot_id",
            "entity_id",
            name="uq_kb_snapshot_entity_node_tenant_membership",
        ),
        CheckConstraint(
            "length(btrim(entity_type)) > 0",
            name="ck_kb_snapshot_entity_node_type_nonempty",
        ),
        CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_kb_snapshot_entity_node_name_nonempty",
        ),
        CheckConstraint(
            "support_status IN ('supported', 'pinned')",
            name="ck_kb_snapshot_entity_node_support_status",
        ),
        CheckConstraint(
            "support_source IN ('fact', 'edge', 'pin', 'mixed')",
            name="ck_kb_snapshot_entity_node_support_source",
        ),
        CheckConstraint(
            "support_count >= 0",
            name="ck_kb_snapshot_entity_node_support_count",
        ),
        CheckConstraint(
            "(support_status = 'pinned' AND pinned_at_build AND support_count = 0) "
            "OR (support_status = 'supported' AND support_count > 0)",
            name="ck_kb_snapshot_entity_node_support_shape",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_snapshot_entity_node_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_kb_snapshot_entity_node_entity_tenant",
        ),
        Index(
            "ix_kb_snapshot_entity_nodes_snapshot_status",
            "snapshot_id",
            "support_status",
        ),
        Index(
            "ix_kb_snapshot_entity_nodes_entity",
            "entity_id",
            "snapshot_id",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    entity_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    entity_type: str = Field(max_length=50)
    display_name: str = Field(max_length=300)
    support_status: str = Field(max_length=20)
    support_source: str = Field(max_length=20)
    pinned_at_build: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    support_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbSnapshotImageAsset(SQLModel, table=True):
    """Frozen media membership for an immutable knowledge snapshot.

    Object keys are represented only by SHA-256 fingerprints. The raw private
    storage locator remains on ``KbImageAsset`` and must never enter snapshot
    state.
    """

    __tablename__ = "kb_snapshot_image_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_snapshot_image_asset_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            ["image_asset_id", "org_id", "kb_id"],
            [
                "kb_image_assets.id",
                "kb_image_assets.org_id",
                "kb_image_assets.kb_id",
            ],
            name="fk_kb_snapshot_image_asset_asset_tenant",
        ),
        ForeignKeyConstraint(
            ["revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_snapshot_image_asset_revision_tenant",
        ),
        UniqueConstraint(
            "snapshot_id",
            "image_asset_id",
            name="uq_kb_snapshot_image_asset_membership",
        ),
        UniqueConstraint(
            "snapshot_id",
            "image_chunk_id",
            name="uq_kb_snapshot_image_asset_chunk",
        ),
        CheckConstraint(
            "enrichment_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_snapshot_image_asset_enrichment_fingerprint",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' "
            "AND image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND extraction_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND original_object_key_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND thumbnail_object_key_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND thumbnail_sha256 ~ '^[0-9a-f]{64}$' "
            "AND citation_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_snapshot_image_asset_integrity_hashes",
        ),
        CheckConstraint(
            "ocr_fingerprint IS NULL OR ocr_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_snapshot_image_asset_ocr_fingerprint",
        ),
        CheckConstraint(
            "caption_fingerprint IS NULL "
            "OR caption_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_snapshot_image_asset_caption_fingerprint",
        ),
        CheckConstraint(
            "config_fingerprint IS NULL "
            "OR config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kb_snapshot_image_asset_config_fingerprint",
        ),
        CheckConstraint(
            "accepted_at_build",
            name="ck_kb_snapshot_image_asset_accepted_at_build",
        ),
        CheckConstraint(
            "asset_lock_version >= 1",
            name="ck_kb_snapshot_image_asset_lock_version",
        ),
        CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp') "
            "AND size_bytes > 0 AND width > 0 AND height > 0",
            name="ck_kb_snapshot_image_asset_original_metadata",
        ),
        CheckConstraint(
            "thumbnail_content_type IN ('image/png', 'image/jpeg', 'image/webp') "
            "AND thumbnail_size_bytes > 0 "
            "AND thumbnail_width > 0 AND thumbnail_height > 0",
            name="ck_kb_snapshot_image_asset_thumbnail_metadata",
        ),
        CheckConstraint(
            "page IS NULL OR page >= 1",
            name="ck_kb_snapshot_image_asset_page",
        ),
        CheckConstraint(
            "slide IS NULL OR slide >= 1",
            name="ck_kb_snapshot_image_asset_slide",
        ),
        CheckConstraint(
            "page IS NULL OR slide IS NULL",
            name="ck_kb_snapshot_image_asset_page_slide_exclusive",
        ),
        CheckConstraint(
            "reading_order IS NULL OR reading_order >= 0",
            name="ck_kb_snapshot_image_asset_reading_order",
        ),
        CheckConstraint(
            """
            CASE
                WHEN bbox IS NULL THEN TRUE
                WHEN jsonb_typeof(bbox) <> 'object' THEN FALSE
                WHEN NOT (
                    bbox ? 'left' AND bbox ? 'top'
                    AND bbox ? 'right' AND bbox ? 'bottom'
                ) THEN FALSE
                WHEN jsonb_typeof(bbox -> 'left') <> 'number'
                    OR jsonb_typeof(bbox -> 'top') <> 'number'
                    OR jsonb_typeof(bbox -> 'right') <> 'number'
                    OR jsonb_typeof(bbox -> 'bottom') <> 'number'
                    THEN FALSE
                ELSE
                    (bbox ->> 'left')::numeric >= 0
                    AND (bbox ->> 'top')::numeric >= 0
                    AND (bbox ->> 'right')::numeric <= 1
                    AND (bbox ->> 'bottom')::numeric <= 1
                    AND (bbox ->> 'right')::numeric > (bbox ->> 'left')::numeric
                    AND (bbox ->> 'bottom')::numeric > (bbox ->> 'top')::numeric
            END
            """,
            name="ck_kb_snapshot_image_asset_bbox",
        ),
        Index(
            "ix_kb_snapshot_image_assets_asset",
            "image_asset_id",
            "snapshot_id",
        ),
        Index(
            "ix_kb_snapshot_image_assets_chunk",
            "image_chunk_id",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    image_asset_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    image_chunk_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    source_doc_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    revision_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    source_sha256: str | None = Field(default=None, max_length=64)
    source_occurrence: str | None = Field(default=None, max_length=500)
    parser_name: str | None = Field(default=None, max_length=50)
    parser_item_ref: str | None = Field(default=None, max_length=500)
    page: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    slide: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    reading_order: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    bbox: dict[str, float] | None = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    image_sha256: str | None = Field(default=None, max_length=64)
    content_type: str | None = Field(default=None, max_length=50)
    size_bytes: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    width: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    height: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    original_object_key_fingerprint: str | None = Field(default=None, max_length=64)
    thumbnail_object_key_fingerprint: str | None = Field(default=None, max_length=64)
    thumbnail_sha256: str | None = Field(default=None, max_length=64)
    thumbnail_content_type: str | None = Field(default=None, max_length=50)
    thumbnail_size_bytes: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    thumbnail_width: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    thumbnail_height: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    extraction_fingerprint: str | None = Field(default=None, max_length=64)
    ocr_fingerprint: str | None = Field(default=None, max_length=64)
    caption_fingerprint: str | None = Field(default=None, max_length=64)
    config_fingerprint: str | None = Field(default=None, max_length=64)
    enrichment_fingerprint: str = Field(max_length=64)
    citation_fingerprint: str | None = Field(default=None, max_length=64)
    accepted_at_build: bool | None = Field(
        default=None,
        sa_column=Column(Boolean, nullable=True, server_default=text("true")),
    )
    asset_lock_version: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class GraphEdge(SQLModel, table=True):
    """Governed, snapshot-scoped relationship between canonical entities."""

    __tablename__ = "kb_graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "org_id",
            "kb_id",
            "snapshot_id",
            name="uq_kb_graph_edge_tenant_snapshot_identity",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_graph_edges_snapshot",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "src_entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_kb_graph_edges_src_entity",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "dst_entity_id"],
            [
                "canonical_entities.org_id",
                "canonical_entities.kb_id",
                "canonical_entities.id",
            ],
            name="fk_kb_graph_edges_dst_entity",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id", "src_entity_id"],
            [
                "kb_snapshot_entity_nodes.org_id",
                "kb_snapshot_entity_nodes.kb_id",
                "kb_snapshot_entity_nodes.snapshot_id",
                "kb_snapshot_entity_nodes.entity_id",
            ],
            name="fk_kb_graph_edges_src_node",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id", "dst_entity_id"],
            [
                "kb_snapshot_entity_nodes.org_id",
                "kb_snapshot_entity_nodes.kb_id",
                "kb_snapshot_entity_nodes.snapshot_id",
                "kb_snapshot_entity_nodes.entity_id",
            ],
            name="fk_kb_graph_edges_dst_node",
        ),
        ForeignKeyConstraint(
            ["fact_claim_id", "org_id", "kb_id"],
            ["fact_claims.id", "fact_claims.org_id", "fact_claims.kb_id"],
            name="fk_kb_graph_edges_fact_claim",
        ),
        ForeignKeyConstraint(
            ["related_fact_claim_id", "org_id", "kb_id"],
            ["fact_claims.id", "fact_claims.org_id", "fact_claims.kb_id"],
            name="fk_kb_graph_edges_related_fact_claim",
        ),
        ForeignKeyConstraint(
            ["evidence_span_id", "org_id", "kb_id"],
            ["evidence_spans.id", "evidence_spans.org_id", "evidence_spans.kb_id"],
            name="fk_kb_graph_edges_evidence_span",
        ),
        ForeignKeyConstraint(
            ["related_evidence_span_id", "org_id", "kb_id"],
            ["evidence_spans.id", "evidence_spans.org_id", "evidence_spans.kb_id"],
            name="fk_kb_graph_edges_related_evidence_span",
        ),
        ForeignKeyConstraint(
            ["source_revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_graph_edges_source_revision",
        ),
        ForeignKeyConstraint(
            ["related_source_revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_graph_edges_related_source_revision",
        ),
        CheckConstraint(
            "src_entity_id <> dst_entity_id", name="ck_kb_graph_edge_distinct_endpoints"
        ),
        CheckConstraint(
            "predicate IN ('located_in', 'near', 'includes', 'part_of', 'serves', "
            "'supports', 'derived_from', 'related', 'shared_context')",
            name="ck_kb_graph_edge_predicate",
        ),
        CheckConstraint(
            "direction IN ('directed', 'undirected')",
            name="ck_kb_graph_edge_direction",
        ),
        CheckConstraint(
            "edge_kind IN ('direct', 'shared_source')",
            name="ck_kb_graph_edge_kind",
        ),
        CheckConstraint("weight > 0", name="ck_kb_graph_edge_weight"),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to",
            name="ck_kb_graph_edge_valid_range",
        ),
        CheckConstraint(
            "(edge_kind = 'direct' AND predicate <> 'shared_context' "
            "AND fact_claim_id IS NOT NULL AND evidence_span_id IS NOT NULL "
            "AND related_fact_claim_id IS NULL AND related_evidence_span_id IS NULL "
            "AND related_source_revision_id IS NULL) "
            "OR (edge_kind = 'shared_source' AND predicate = 'shared_context' "
            "AND direction = 'undirected' AND src_entity_id < dst_entity_id "
            "AND fact_claim_id IS NOT NULL AND evidence_span_id IS NOT NULL "
            "AND related_fact_claim_id IS NOT NULL "
            "AND related_evidence_span_id IS NOT NULL "
            "AND related_source_revision_id IS NOT NULL)",
            name="ck_kb_graph_edge_evidence_shape",
        ),
        Index(
            "ix_kb_graph_edges_snapshot_src",
            "snapshot_id",
            "src_entity_id",
        ),
        Index(
            "ix_kb_graph_edges_snapshot_dst",
            "snapshot_id",
            "dst_entity_id",
        ),
        Index(
            "ix_kb_graph_edges_related_source_revision",
            "related_source_revision_id",
            postgresql_where=text("related_source_revision_id IS NOT NULL"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    src_entity_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    dst_entity_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    predicate: GraphPredicate = Field(sa_column=Column(String(40), nullable=False))
    direction: GraphDirection = Field(sa_column=Column(String(20), nullable=False))
    edge_kind: GraphEdgeKind = Field(sa_column=Column(String(20), nullable=False))
    weight: float = Field(sa_column=Column(Float, nullable=False))
    valid_from: date | None = Field(default=None, sa_column=Column(Date, nullable=True))
    valid_to: date | None = Field(default=None, sa_column=Column(Date, nullable=True))
    fact_claim_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    evidence_span_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    related_fact_claim_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    related_evidence_span_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    source_revision_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    related_source_revision_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbSnapshotEntityNodeSupport(SQLModel, table=True):
    """One evidence, edge, or governed pin supporting a snapshot entity node."""

    __tablename__ = "kb_snapshot_entity_node_supports"
    __table_args__ = (
        CheckConstraint(
            "support_type IN ('fact', 'edge', 'pin')",
            name="ck_kb_snapshot_entity_node_support_type",
        ),
        CheckConstraint(
            "(support_type = 'fact' AND fact_support_id IS NOT NULL "
            "AND graph_edge_id IS NULL) "
            "OR (support_type = 'edge' AND fact_support_id IS NULL "
            "AND graph_edge_id IS NOT NULL) "
            "OR (support_type = 'pin' AND fact_support_id IS NULL "
            "AND graph_edge_id IS NULL)",
            name="ck_kb_snapshot_entity_node_support_shape",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id", "entity_id"],
            [
                "kb_snapshot_entity_nodes.org_id",
                "kb_snapshot_entity_nodes.kb_id",
                "kb_snapshot_entity_nodes.snapshot_id",
                "kb_snapshot_entity_nodes.entity_id",
            ],
            name="fk_kb_snapshot_entity_node_support_node",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["fact_support_id", "org_id", "kb_id", "snapshot_id"],
            [
                "snapshot_fact_supports.id",
                "snapshot_fact_supports.org_id",
                "snapshot_fact_supports.kb_id",
                "snapshot_fact_supports.snapshot_id",
            ],
            name="fk_kb_snapshot_entity_node_support_fact",
        ),
        ForeignKeyConstraint(
            ["graph_edge_id", "org_id", "kb_id", "snapshot_id"],
            [
                "kb_graph_edges.id",
                "kb_graph_edges.org_id",
                "kb_graph_edges.kb_id",
                "kb_graph_edges.snapshot_id",
            ],
            name="fk_kb_snapshot_entity_node_support_edge",
        ),
        Index(
            "uq_kb_snapshot_entity_node_support_fact",
            "snapshot_id",
            "entity_id",
            "fact_support_id",
            unique=True,
            postgresql_where=text("support_type = 'fact'"),
        ),
        Index(
            "uq_kb_snapshot_entity_node_support_edge",
            "snapshot_id",
            "entity_id",
            "graph_edge_id",
            unique=True,
            postgresql_where=text("support_type = 'edge'"),
        ),
        Index(
            "uq_kb_snapshot_entity_node_support_pin",
            "snapshot_id",
            "entity_id",
            unique=True,
            postgresql_where=text("support_type = 'pin'"),
        ),
        Index(
            "ix_kb_snapshot_entity_node_supports_node",
            "snapshot_id",
            "entity_id",
            "support_type",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    snapshot_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    entity_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    support_type: str = Field(max_length=20)
    fact_support_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    graph_edge_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class OutboxEvent(SQLModel, table=True):
    """Durable, idempotent event awaiting projection or garbage collection."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_event_idempotency_key"),
        CheckConstraint(
            "length(btrim(aggregate_type)) > 0",
            name="ck_outbox_event_aggregate_type_nonempty",
        ),
        CheckConstraint(
            "length(btrim(event_type)) > 0",
            name="ck_outbox_event_type_nonempty",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_outbox_event_idempotency_key_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_outbox_event_payload"
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_event_attempts"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_outbox_event_status",
        ),
        CheckConstraint(
            "(status = 'processing' AND claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL) "
            "OR (status <> 'processing' AND claimed_by IS NULL "
            "AND claimed_at IS NULL AND claim_expires_at IS NULL)",
            name="ck_outbox_event_claim_state",
        ),
        CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) "
            "OR (status <> 'published' AND published_at IS NULL)",
            name="ck_outbox_event_published_state",
        ),
        CheckConstraint(
            "status <> 'dead_letter' OR (last_error IS NOT NULL "
            "AND length(btrim(last_error)) > 0)",
            name="ck_outbox_event_dead_letter_state",
        ),
        Index(
            "ix_outbox_events_pending",
            "available_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_outbox_events_processing",
            "claim_expires_at",
            postgresql_where=text("status = 'processing'"),
        ),
        Index(
            "ix_outbox_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "created_at",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    aggregate_type: str = Field(max_length=50)
    aggregate_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    event_type: str = Field(max_length=100)
    idempotency_key: str = Field(max_length=200)
    payload: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    status: OutboxStatus = Field(
        default=OutboxStatus.PENDING,
        sa_column=Column(
            String(20), nullable=False, server_default=text("'pending'")
        ),
    )
    attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    available_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    claimed_by: str | None = Field(default=None, max_length=200)
    claimed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    claim_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class KbChunk(SQLModel, table=True):
    """非结构化文本切片 + 向量。embedding_model 记录 模型名:版本,换模型全量重建。"""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "kb_id",
            "id",
            name="uq_kb_chunk_tenant_identity",
        ),
        ForeignKeyConstraint(
            ["revision_id", "org_id", "kb_id"],
            [
                "document_revisions.id",
                "document_revisions.org_id",
                "document_revisions.kb_id",
            ],
            name="fk_kb_chunk_revision_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "kb_id", "snapshot_id"],
            [
                "knowledge_snapshots.org_id",
                "knowledge_snapshots.kb_id",
                "knowledge_snapshots.id",
            ],
            name="fk_kb_chunk_snapshot_tenant",
        ),
        ForeignKeyConstraint(
            ["image_asset_id", "org_id", "kb_id", "revision_id"],
            [
                "kb_image_assets.id",
                "kb_image_assets.org_id",
                "kb_image_assets.kb_id",
                "kb_image_assets.revision_id",
            ],
            name="fk_kb_chunk_image_asset_revision",
        ),
        CheckConstraint(
            "content_kind IN ('text', 'image')",
            name="ck_kb_chunk_content_kind",
        ),
        CheckConstraint(
            "(content_kind = 'text' AND image_asset_id IS NULL) "
            "OR (content_kind = 'image' AND image_asset_id IS NOT NULL "
            "AND revision_id IS NOT NULL)",
            name="ck_kb_chunk_image_asset_state",
        ),
        Index("ix_kb_chunks_kb_snapshot", "kb_id", "snapshot_id"),
        Index("ix_kb_chunks_revision_id", "revision_id"),
        Index("ix_kb_chunks_image_asset_id", "image_asset_id"),
        Index("ix_kb_chunks_tsv_gin", "tsv", postgresql_using="gin"),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    revision_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    snapshot_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    source_doc_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("source_documents.id"), nullable=True),
    )
    content_kind: str = Field(
        default="text",
        sa_column=Column(String(20), nullable=False, server_default=text("'text'")),
    )
    image_asset_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    content: str = Field(sa_column=Column(Text, nullable=False))
    # Contextual Retrieval:meta->>'context_text' 一并进 FTS(sparse 通道
    # 自动受益);表达式必须与 baseline 迁移里的 Computed 定义逐字一致。
    tsv: str | None = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(
                f"to_tsvector('{KB_FTS_REGCONFIG}'::regconfig, "
                "COALESCE(heading_path, '') || ' ' || "
                "COALESCE(meta->>'context_text', '') || ' ' || content)",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(EMBEDDING_DIM), nullable=True)
    )
    embedding_model: str | None = Field(default=None, max_length=200)
    quarantined: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    source_ref: str | None = Field(default=None, max_length=500)
    # 溯源锚点(KB-2):标题面包屑 / markdown 全文 1-based 行区间 / 页锚;
    # fixed 策略或历史数据为 None。chunk_index = 文档内切片序号(列表稳定排序)
    heading_path: str | None = Field(default=None, max_length=500)
    start_line: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    end_line: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    page: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    chunk_index: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    meta: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class KbChunkEmbedding(SQLModel, table=True):
    """Versioned embedding for one chunk and one provider/model fingerprint."""

    __tablename__ = "kb_chunk_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "kb_id", "chunk_id"],
            ["kb_chunks.org_id", "kb_chunks.kb_id", "kb_chunks.id"],
            name="fk_kb_chunk_embedding_chunk",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "chunk_id",
            "provider",
            "model",
            "dim",
            name="uq_kb_chunk_embedding_version",
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0",
            name="ck_kb_chunk_embedding_provider_nonempty",
        ),
        CheckConstraint(
            "length(btrim(model)) > 0",
            name="ck_kb_chunk_embedding_model_nonempty",
        ),
        CheckConstraint(
            f"dim = {EMBEDDING_DIM}",
            name="ck_kb_chunk_embedding_dim",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_kb_chunk_embedding_content_hash",
        ),
        Index("ix_kb_chunk_embeddings_tenant_kb", "org_id", "kb_id"),
        Index(
            "ix_kb_chunk_embeddings_fingerprint",
            "provider",
            "model",
            "dim",
        ),
        Index(
            "ix_kb_chunk_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    kb_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    chunk_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    provider: str = Field(max_length=100)
    model: str = Field(max_length=200)
    dim: int = Field(
        default=EMBEDDING_DIM,
        sa_column=Column(Integer, nullable=False, server_default=text(str(EMBEDDING_DIM))),
    )
    content_hash: str = Field(max_length=64)
    embedding: list[float] = Field(sa_column=Column(Vector(EMBEDDING_DIM), nullable=False))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class EmbeddingMigrationCampaign(SQLModel, table=True):
    """Global orchestration record for one embedding configuration migration."""

    __tablename__ = "embedding_migration_campaigns"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(source_config) = 'object'",
            name="ck_embedding_campaign_source_config",
        ),
        CheckConstraint(
            "jsonb_typeof(target_config) = 'object'",
            name="ck_embedding_campaign_target_config",
        ),
        CheckConstraint(
            "jsonb_typeof(source_fingerprint) = 'object'",
            name="ck_embedding_campaign_source_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(target_fingerprint) = 'object'",
            name="ck_embedding_campaign_target_fingerprint",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'dual_read', 'completed', 'failed', 'canceled')",
            name="ck_embedding_campaign_status",
        ),
        CheckConstraint(
            "total_jobs >= 0 AND ready_jobs >= 0 AND failed_jobs >= 0 "
            "AND ready_jobs + failed_jobs <= total_jobs",
            name="ck_embedding_campaign_job_counts",
        ),
        CheckConstraint(
            "dual_read_seconds > 0",
            name="ck_embedding_campaign_dual_read_seconds",
        ),
        Index(
            "uq_embedding_migration_campaign_active",
            text("(1)"),
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'dual_read')"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    source_config: dict = Field(sa_column=Column(JSONB, nullable=False))
    target_config: dict = Field(sa_column=Column(JSONB, nullable=False))
    source_fingerprint: dict = Field(sa_column=Column(JSONB, nullable=False))
    target_fingerprint: dict = Field(sa_column=Column(JSONB, nullable=False))
    status: EmbeddingMigrationStatus = Field(
        default=EmbeddingMigrationStatus.QUEUED,
        sa_column=Column(String(20), nullable=False, server_default=text("'queued'")),
    )
    total_jobs: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    ready_jobs: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    failed_jobs: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    dual_read_seconds: int = Field(
        default=86400,
        sa_column=Column(Integer, nullable=False, server_default=text("86400")),
    )
    dual_read_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class EmbeddingReindexJob(SQLModel, table=True):
    """Leased per-KB unit of work for an embedding migration campaign."""

    __tablename__ = "embedding_reindex_jobs"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "kb_id",
            name="uq_embedding_reindex_job_campaign_kb",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed', 'canceled')",
            name="ck_embedding_reindex_job_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND total_chunks >= 0 AND embedded_chunks >= 0 "
            "AND embedded_chunks <= total_chunks",
            name="ck_embedding_reindex_job_counts",
        ),
        Index("ix_embedding_reindex_jobs_tenant_kb", "org_id", "kb_id"),
        Index(
            "ix_embedding_reindex_jobs_queue",
            "status",
            "lease_expires_at",
            "created_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    campaign_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("embedding_migration_campaigns.id"),
            nullable=False,
        )
    )
    org_id: UUID = Field(sa_column=_org_id())
    kb_id: UUID = Field(sa_column=_kb_id())
    status: EmbeddingReindexJobStatus = Field(
        default=EmbeddingReindexJobStatus.QUEUED,
        sa_column=Column(String(20), nullable=False, server_default=text("'queued'")),
    )
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    heartbeat_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    total_chunks: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    embedded_chunks: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())
