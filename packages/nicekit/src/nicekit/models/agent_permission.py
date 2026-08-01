"""Organization, user, conversation, and grant persistence for Agent permissions.

迁移自 TF backend/app/models/agent_permission.py(MIGRATION-PLAN §5.4)。
唯一结构改动:AgentPermissionGrant 的 `project_id` 硬外键改为
`scope_type` + `scope_id`(无外键),与 chat_sessions 同一口径。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from nicekit.domain.agent_permission import PermissionProfile, PermissionScope, ToolCategory
from nicekit.models.chat import SCOPE_TYPE_MAX_LENGTH


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated_at() -> Column:
    return Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
    )


class AgentApprovalPolicy(SQLModel, table=True):
    """Immutable organization policy version; exactly one version is active per org."""

    __tablename__ = "agent_approval_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "version", name="uq_agent_approval_policies_org_version"),
        Index(
            "uq_agent_approval_policies_active_org",
            "org_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("version > 0", name="ck_agent_approval_policies_version"),
        CheckConstraint(
            "max_grant_ttl_seconds > 0",
            name="ck_agent_approval_policies_grant_ttl",
        ),
        CheckConstraint(
            "max_full_access_ttl_seconds > 0",
            name="ck_agent_approval_policies_full_access_ttl",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_profiles) = 'array'",
            name="ck_agent_approval_policies_allowed_profiles",
        ),
        CheckConstraint(
            "allowed_profiles ? default_profile",
            name="ck_agent_approval_policies_default_allowed",
        ),
        CheckConstraint(
            "jsonb_typeof(hard_rules) = 'object'",
            name="ck_agent_approval_policies_hard_rules",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    version: int = Field(sa_column=Column(Integer, nullable=False))
    default_profile: PermissionProfile = Field(
        default=PermissionProfile.REQUEST_APPROVAL,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'request_approval'"),
        ),
    )
    allowed_profiles: list[str] = Field(
        default_factory=lambda: [PermissionProfile.REQUEST_APPROVAL.value],
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[\"request_approval\"]'::jsonb"),
        ),
    )
    hard_rules: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    reviewer_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    reviewer_eligible_categories: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    reviewer_eligible_tools: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    max_scope: PermissionScope = Field(
        default=PermissionScope.RESOURCE,
        sa_column=Column(String(32), nullable=False, server_default=text("'resource'")),
    )
    max_grant_ttl_seconds: int = Field(
        default=28800,
        sa_column=Column(Integer, nullable=False, server_default=text("28800")),
    )
    max_full_access_ttl_seconds: int = Field(
        default=3600,
        sa_column=Column(Integer, nullable=False, server_default=text("3600")),
    )
    is_enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    shadow_evaluation: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    is_active: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    created_by: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())


class AgentPermissionPreference(SQLModel, table=True):
    """A user's default selection inside one organization."""

    __tablename__ = "agent_permission_preferences"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "user_id", name="uq_agent_permission_preferences_org_user"
        ),
        CheckConstraint(
            "jsonb_typeof(custom_rules) = 'object'",
            name="ck_agent_permission_preferences_custom_rules",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    user_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    default_profile: PermissionProfile = Field(
        default=PermissionProfile.REQUEST_APPROVAL,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'request_approval'"),
        ),
    )
    preferred_scope: PermissionScope = Field(
        default=PermissionScope.SESSION,
        sa_column=Column(String(32), nullable=False, server_default=text("'session'")),
    )
    custom_rules: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    updated_at: datetime | None = Field(default=None, sa_column=_updated_at())


class AgentPermissionGrant(SQLModel, table=True):
    """Expiring permission grant bound to a user and a canonical action scope.

    scope_type/scope_id 取代 TF 的 project_id 外键:授权可以钉在宿主的某个
    业务作用域上,但 SDK 不引用宿主的表(无 FK,靠服务层保证一致)。
    """

    __tablename__ = "agent_permission_grants"
    __table_args__ = (
        Index(
            "ix_agent_permission_grants_match",
            "org_id",
            "user_id",
            "session_id",
            "tool_name",
            "scope_id",
        ),
        Index(
            "ix_agent_permission_grants_active_expiry",
            "org_id",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            "(tool_name IS NULL) <> (category IS NULL)",
            name="ck_agent_permission_grants_one_subject",
        ),
        CheckConstraint(
            "scope <> 'session' OR session_id IS NOT NULL",
            name="ck_agent_permission_grants_session_scope",
        ),
        # PermissionScope.RESOURCE = "宿主业务作用域"这一档(见 domain/agent_permission.py):
        # 选它就必须钉住一个具体的 scope_id,否则授权范围无从界定。
        CheckConstraint(
            "scope <> 'resource' OR scope_id IS NOT NULL",
            name="ck_agent_permission_grants_scope_binding",
        ),
        CheckConstraint(
            "length(action_fingerprint) = 64",
            name="ck_agent_permission_grants_fingerprint",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_agent_permission_grants_expiry",
        ),
    )

    id: UUID = Field(sa_column=_uuid_pk())
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    user_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    session_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    scope_type: str | None = Field(
        default=None, sa_column=Column(String(SCOPE_TYPE_MAX_LENGTH), nullable=True)
    )
    scope_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    tool_name: str | None = Field(default=None, max_length=100)
    category: ToolCategory | None = Field(
        default=None, sa_column=Column(String(32), nullable=True)
    )
    scope: PermissionScope = Field(
        sa_column=Column(String(32), nullable=False)
    )
    resource_type: str | None = Field(default=None, max_length=50)
    resource_id: str | None = Field(default=None, max_length=100)
    action_fingerprint: str = Field(max_length=64)
    policy_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_approval_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    policy_version: int = Field(sa_column=Column(Integer, nullable=False))
    created_by: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    created_at: datetime | None = Field(default=None, sa_column=_created_at())
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    revoked_by: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
