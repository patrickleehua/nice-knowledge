"""知识库 API。

写操作要求 org_admin / platform_admin(宿主注册的业务角色可经 _KB_WRITERS
追加);RLS 负责数据隔离(平台层条目对租户只读由 RLS 写策略强制,API 无需特判)。
"""

import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import case, extract, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from nicekit.api.deps import OrgContext, get_org_context, get_org_session, require_role
from nicekit.core.config import get_settings
from nicekit.domain.kb import INGEST_PROFILE_PRESETS, IngestProfile
from nicekit.domain.kb_media import (
    KbImageEnrichmentReadinessRead,
    KnowledgeMediaReference,
)
from nicekit.domain.model_catalog import ProviderModelCatalogEntry
from nicekit.kb import storage
from nicekit.kb.answer import (
    KnowledgeAnswerGenerationError,
    generate_grounded_answer,
    stream_grounded_answer,
)
from nicekit.kb.caption import CaptionModelSelection
from nicekit.kb.dedup_suggestions import suggest_merge_candidates
from nicekit.kb.document_lifecycle import (
    DocumentDeletionError,
    delete_source_document,
    preview_document_withdrawal,
    retry_document_operation,
)
from nicekit.kb.document_purge import (
    DocumentPurgeBlocked,
    DocumentPurgeError,
    DocumentPurgeForbidden,
    DocumentPurgeNotFound,
    DocumentPurgePlanDrift,
    preview_document_purge,
    public_document_purge_operation_detail,
    submit_document_purge,
)
from nicekit.kb.document_reingestion import (
    DocumentReingestionError,
    start_or_retry_document_reingestion,
)
from nicekit.kb.effective_scope import live_snapshot_projection_filter
from nicekit.kb.eligibility import (
    ActiveKnowledgeBaseLease,
    active_knowledge_base_lease_is_current,
    capture_active_knowledge_base_lease,
    capture_active_knowledge_base_leases,
)
from nicekit.kb.embedding import EmbeddingUnavailableError
from nicekit.kb.entity_binding import (
    RELATION_PREDICATES,
    bind_claim_entities,
    is_bindable,
)
from nicekit.kb.entity_resolution import (
    EntityConflictError,
    EntityNotFoundError,
    MergeResult,
    add_entity_alias,
    bind_claim_to_entity,
    create_canonical_entity,
    delete_entity_alias,
    merge_entities,
    normalize_alias,
    rename_canonical_entity,
    validate_entity_binding,
)
from nicekit.kb.entity_types import get_entity_type
from nicekit.kb.fact_review_ai import ai_review_fact_claims
from nicekit.kb.image_enrichment import get_kb_image_enrichment_service
from nicekit.kb.image_ingestion import revision_image_stage
from nicekit.kb.ingestion import (
    IMAGE_ENRICHMENT_ERROR,
    assess_typed_reextraction,
    enqueue_document_ingestion,
    enqueue_typed_reextraction,
    get_active_count,
    get_max_concurrency,
    set_max_concurrency,
)
from nicekit.kb.lifecycle import (
    KnowledgeBaseLifecycleError,
    archive_knowledge_base,
    get_lifecycle_operation,
    hard_delete_empty_knowledge_base,
    preview_knowledge_base_deletion,
    public_deletion_preview,
    public_lifecycle_blockers,
    public_lifecycle_operation,
    restore_knowledge_base,
    retry_lifecycle_operation,
    submit_knowledge_base_purge,
)
from nicekit.kb.parsers.fast import SUPPORTED_DOCUMENT_SUFFIXES
from nicekit.kb.projections import active_projection_filter, projection_row_id
from nicekit.kb.review_settlement import (
    settle_documents_for_claims,
    settle_org_awaiting_documents,
)
from nicekit.kb.search import (
    StructuredFilter,
    StructuredFilterError,
    StructuredSearchQuery,
    default_embedder,
    search_kb,
)
from nicekit.kb.snapshot import (
    SnapshotError,
    SnapshotRollbackCapability,
    SnapshotTransitionBlocked,
    activate_snapshot,
    build_snapshot,
    rollback_snapshot,
    snapshot_rollback_capability,
)
from nicekit.kb.wiki_gen import (
    WikiSnapshotManagedError,
    WikiSourceUnavailableError,
    refresh_kb_overview,
    update_wiki_for_document,
)
from nicekit.kb.wiki_review import (
    WikiDraftStateError,
    publish_wiki_draft,
    record_wiki_claim_decision,
    reject_wiki_draft,
)
from nicekit.llm.model_catalog import (
    ModelCatalogLookupError,
    build_model_catalog,
    lookup_eligible_provider_model_session,
)
from nicekit.llm.providers import ProviderError
from nicekit.llm.service import (
    AllProvidersFailedError,
    LlmBudgetExceededError,
    NoRouteError,
    get_llm_service,
)
from nicekit.models.kb import (
    CanonicalEntity,
    DocStatus,
    DocType,
    DocumentLifecycleStatus,
    DocumentOperationStatus,
    DocumentOperationType,
    DocumentRevision,
    EntityAlias,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbChunk,
    KbDocumentOperation,
    KbPage,
    KbShare,
    KnowledgeBase,
    KnowledgeBaseLifecycleOperationStatus,
    KnowledgeBaseLifecyclePhase,
    KnowledgeBaseLifecycleStatus,
    KnowledgeSnapshot,
    OutboxEvent,
    OutboxStatus,
    RevisionStatus,
    SnapshotStatus,
    SourceDocument,
)
from nicekit.models.llm_provider import LlmProvider
from nicekit.models.tenancy import AuditLog, Organization, Role
from nicekit.tenancy.usage import record_usage

router = APIRouter(prefix="/kb")

# 内置角色只有三个(MIGRATION-PLAN §5.2);宿主注册的业务角色可经
# set_kb_writer_roles() 追加进写权限集合,require_role 按字符串值比较。
_KB_WRITERS: tuple[str, ...] = (Role.PLATFORM_ADMIN, Role.ORG_ADMIN)
_KB_SHARERS: tuple[str, ...] = (Role.PLATFORM_ADMIN, Role.ORG_ADMIN)
_ENTITY_ALIAS_UNIQUE_CONSTRAINT = "uq_entity_alias_kb_normalized_locale"


# ---- 任务派发(P4 接线点)---------------------------------------------------
#
# 统一派发器属 runtime 装配层(MIGRATION-PLAN §5.7,`runtime/dispatch.py`),
# 本波次尚未搬入。这里保留 TF 的语义(celery 优先、失败回退 BackgroundTasks),
# runtime.dispatch 存在时直接委派过去,不存在时走进程内 inline 回退。
# P4 搬入 runtime/dispatch.py 后本节可整体删除,改回顶层 import。


async def _dispatch_kb_ingest_run(
    run_id: UUID, org_id: UUID, background: BackgroundTasks | None = None
) -> bool:
    """派发已持久化的 root ingest run;恢复扫描走同一入口。"""
    try:
        from nicekit.runtime.dispatch import dispatch_kb_ingest_run
    except ImportError:
        pass
    else:
        return await dispatch_kb_ingest_run(run_id, org_id, background)

    from nicekit.core.db import get_session_factory
    from nicekit.kb.ingestion import ingest_document
    from nicekit.kb.search import default_embedder

    if background is None:
        return False
    background.add_task(
        ingest_document,
        run_id,
        org_id,
        session_factory=get_session_factory(),
        llm=get_llm_service(),
        embedder=default_embedder(),
    )
    return True


async def _dispatch_kb_reextract_run(
    run_id: UUID, org_id: UUID, background: BackgroundTasks | None = None
) -> bool:
    """派发已持久化的 typed 重抽取 root run。"""
    try:
        from nicekit.runtime.dispatch import dispatch_kb_reextract_run
    except ImportError:
        pass
    else:
        return await dispatch_kb_reextract_run(run_id, org_id, background)

    from nicekit.core.db import get_session_factory
    from nicekit.kb.ingestion import reextract_document

    if background is None:
        return False
    background.add_task(
        reextract_document,
        run_id,
        org_id,
        session_factory=get_session_factory(),
        llm=get_llm_service(),
    )
    return True

def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    current: object | None = exc.orig
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


async def _handle_entity_integrity_error(
    session: AsyncSession,
    exc: IntegrityError,
) -> None:
    await session.rollback()
    if _integrity_constraint_name(exc) == _ENTITY_ALIAS_UNIQUE_CONSTRAINT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "别名已被其他实体使用",
        ) from exc
    raise exc


async def _require_own_kb(
    session: AsyncSession,
    ctx: OrgContext,
    kb_id: UUID,
    *,
    allow_non_active: bool = False,
) -> KnowledgeBase:
    """写入实体前校验:kb 必须存在且归当前 org 所有(分享库/平台库只读)。"""
    kb = await session.get(
        KnowledgeBase,
        kb_id,
        populate_existing=True,
        with_for_update={"read": True, "key_share": True},
    )
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    if kb.org_id != ctx.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该知识库对当前组织只读")
    if not allow_non_active and kb.lifecycle_status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "kb_not_active",
                "message": "知识库当前不可写",
                "lifecycle_status": kb.lifecycle_status,
            },
        )
    return kb


async def _require_visible_active_kb(
    session: AsyncSession,
    kb_id: UUID,
) -> None:
    if (
        await capture_active_knowledge_base_lease(
            session,
            kb_id=kb_id,
            lock=True,
        )
        is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在或不可用")


def _raise_lifecycle_http(exc: KnowledgeBaseLifecycleError) -> NoReturn:
    status_code = {
        "knowledge_base_not_found": status.HTTP_404_NOT_FOUND,
        "lifecycle_operation_not_found": status.HTTP_404_NOT_FOUND,
        "deletion_inventory_incomplete": status.HTTP_503_SERVICE_UNAVAILABLE,
        "idempotency_key_required": status.HTTP_428_PRECONDITION_REQUIRED,
    }.get(exc.code, status.HTTP_409_CONFLICT)
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
    }
    if exc.blockers:
        detail["blockers"] = public_lifecycle_blockers(exc.blockers)
    if exc.preview is not None:
        detail["preview"] = (
            public_deletion_preview(exc.preview)
            if "counts" in exc.preview
            else exc.preview
        )
    raise HTTPException(status_code, detail=detail) from exc


# ---- 知识库对象(一等公民):CRUD + 分享 -----------------------------------


#: kb_type 的兜底取值。MIGRATION-PLAN B2:KbType 枚举已删除,该列是自由 tag,
#: 宿主想给 UI 提供预置清单就自己下发,SDK 只做 slug 形态校验。
DEFAULT_KB_TYPE = "mixed"


def _validate_kb_type(v: str) -> str:
    """开放类型:任意 ≤30 字符 slug(如 visa/faq),SDK 不预设行业词表。"""
    v = (v or "").strip().lower()
    if not v or len(v) > 30 or not v.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "kb_type 需为 ≤30 字符的 slug(字母/数字/下划线/连字符)",
        )
    return v


def _validate_ingest_profile(v: dict | None) -> dict | None:
    """ingest_profile 必须过 Pydantic 契约(IngestProfile)才允许落库。"""
    if v is None:
        return None
    try:
        return IngestProfile.model_validate(v).model_dump()
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"ingest_profile 不合法: {detail}"
        ) from exc


def _caption_selection(
    profile: IngestProfile | None,
) -> tuple[CaptionModelSelection | None, Literal["platform_default", "kb_override"]]:
    if (
        profile is not None
        and profile.caption_provider is not None
        and profile.caption_model is not None
    ):
        return (
            CaptionModelSelection(
                provider=profile.caption_provider,
                model=profile.caption_model,
            ),
            "kb_override",
        )
    return None, "platform_default"


async def _image_enrichment_readiness(
    session: AsyncSession,
    profile: IngestProfile | None = None,
) -> KbImageEnrichmentReadinessRead:
    selection, selection_source = _caption_selection(profile)
    readiness = get_kb_image_enrichment_service().readiness(selection)
    caption_provider = readiness.caption_provider.strip() or None
    caption_model = readiness.caption_model.strip() or None
    catalog_entry: ProviderModelCatalogEntry | None = None
    catalog_code: str | None = None
    if caption_provider is not None and caption_model is not None:
        try:
            catalog_entry = await lookup_eligible_provider_model_session(
                session,
                provider=caption_provider,
                model=caption_model,
            )
        except ModelCatalogLookupError as exc:
            catalog_code = exc.code
    enabled = (
        profile.caption_images
        if profile is not None
        else caption_provider is not None and caption_model is not None
    )
    return KbImageEnrichmentReadinessRead(
        enabled=enabled,
        ready=readiness.ready and catalog_entry is not None,
        code=catalog_code or readiness.code,
        ocr_provider=readiness.ocr_provider,
        ocr_model=readiness.ocr_model,
        caption_provider=caption_provider,
        caption_model=caption_model,
        selection_source=selection_source,
        capability_source=(catalog_entry.capability_source if catalog_entry is not None else None),
        registry_revision=(catalog_entry.registry_revision if catalog_entry is not None else None),
        config_fingerprint=readiness.config_fingerprint,
    )


async def _require_enabled_image_enrichment(
    session: AsyncSession,
    profile: dict | None,
) -> None:
    if profile is None or profile.get("caption_images") is not True:
        return
    readiness = await _image_enrichment_readiness(
        session,
        IngestProfile.model_validate(profile),
    )
    if not readiness.ready:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": readiness.code,
                "message": "图片视觉描述配置尚未就绪",
            },
        )


class KbCreateBody(BaseModel):
    name: str
    kb_type: str = DEFAULT_KB_TYPE
    description: str | None = None
    ingest_profile: dict | None = None


class KnowledgeBaseLifecycleBlockerCode(StrEnum):
    KNOWLEDGE_BASE_REFERENCE = "KNOWLEDGE_BASE_REFERENCE"
    EXTERNAL_SCOPE_REFERENCE = "EXTERNAL_SCOPE_REFERENCE"
    ACTIVE_TASK_REFERENCE = "ACTIVE_TASK_REFERENCE"
    BUSINESS_ARTIFACT_REFERENCE = "BUSINESS_ARTIFACT_REFERENCE"
    KNOWLEDGE_SNAPSHOT_REFERENCE = "KNOWLEDGE_SNAPSHOT_REFERENCE"
    FEEDBACK_OR_CITATION_REFERENCE = "FEEDBACK_OR_CITATION_REFERENCE"
    RETENTION_PERIOD_ACTIVE = "RETENTION_PERIOD_ACTIVE"
    LEGAL_HOLD_ACTIVE = "LEGAL_HOLD_ACTIVE"
    PINNED_ENTITY_REFERENCE = "PINNED_ENTITY_REFERENCE"
    MANUAL_FACT_REFERENCE = "MANUAL_FACT_REFERENCE"
    SHARED_OBJECT_REFERENCE = "SHARED_OBJECT_REFERENCE"
    REFERENCE_REGISTRY_UNAVAILABLE = "REFERENCE_REGISTRY_UNAVAILABLE"
    OBJECT_INVENTORY_UNAVAILABLE = "OBJECT_INVENTORY_UNAVAILABLE"


class KnowledgeBaseLifecycleErrorCode(StrEnum):
    FORBIDDEN_OWNER_ACTION = "forbidden_owner_action"
    KB_NOT_EMPTY = "kb_not_empty"
    DELETION_PLAN_STALE = "deletion_plan_stale"
    DELETION_INVENTORY_INCOMPLETE = "deletion_inventory_incomplete"
    PURGE_BLOCKED = "purge_blocked"
    INVALID_LIFECYCLE_TRANSITION = "invalid_lifecycle_transition"


class KnowledgeBaseLifecycleAuditEvent(StrEnum):
    ARCHIVED = "kb.lifecycle.archived"
    RESTORED = "kb.lifecycle.restored"
    PURGE_REQUESTED = "kb.lifecycle.purge_requested"
    PURGED = "kb.lifecycle.purged"
    EMPTY_DELETED = "kb.lifecycle.empty_deleted"


class KnowledgeBaseLifecycleBlockerOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: KnowledgeBaseLifecycleBlockerCode
    count: int = Field(ge=0)
    identifiers: list[str] = Field(default_factory=list, max_length=100)
    resolution_hint: str | None = Field(default=None, max_length=500)
    # 保留期/可重试类 blocker 附带的解除时刻(ISO 字符串);缺字段曾让
    # extra="forbid" 在 archived+保留期场景把预检响应打成 500
    retry_at: str | None = Field(default=None, max_length=50)


class KnowledgeBaseDeletionPreviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=50)
    kb_id: UUID
    kb_name: str = Field(min_length=1)
    owner_org_id: UUID
    lifecycle_status: KnowledgeBaseLifecycleStatus
    consumption_epoch: int = Field(ge=0)
    complete: bool
    allowed_actions: list[
        Literal["archive", "restore", "empty_delete", "purge"]
    ] = Field(default_factory=list)
    blockers: list[KnowledgeBaseLifecycleBlockerOut] = Field(default_factory=list)
    impact_counts: dict[str, int] = Field(default_factory=dict)
    plan_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    expires_at: datetime | None = None
    latest_operation: dict[str, Any] | None = None


class KnowledgeBaseLifecycleOperationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kb_id: UUID
    operation_type: Literal["purge"]
    status: KnowledgeBaseLifecycleOperationStatus
    phase: KnowledgeBaseLifecyclePhase
    attempt_count: int = Field(ge=0)
    impact_summary: dict[str, Any]
    last_error_code: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{0,99}$",
    )
    error_message: str | None = Field(default=None, max_length=500)
    retryable: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class KnowledgeBaseLifecycleErrorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: KnowledgeBaseLifecycleErrorCode
    message: str = Field(min_length=1, max_length=500)
    blockers: list[KnowledgeBaseLifecycleBlockerOut] = Field(default_factory=list)


class KnowledgeBaseLifecycleAuditDetail(BaseModel):
    """API-safe payload persisted in AuditLog.detail for KB lifecycle actions."""

    model_config = ConfigDict(extra="forbid")

    event_code: KnowledgeBaseLifecycleAuditEvent
    previous_lifecycle_status: KnowledgeBaseLifecycleStatus | None
    lifecycle_status: KnowledgeBaseLifecycleStatus | None
    previous_consumption_epoch: int | None = Field(default=None, ge=0)
    consumption_epoch: int | None = Field(default=None, ge=0)
    operation_id: UUID | None = None
    plan_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason: str | None = Field(default=None, max_length=500)
    unlinked_external_count: int = Field(default=0, ge=0)
    revoked_share_count: int = Field(default=0, ge=0)


class WikiNavigationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_order: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("page_order")
    @classmethod
    def valid_page_order(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("page_order 不能包含重复页面")
        if any(not item or len(item) > 700 for item in value):
            raise ValueError("page_order 包含无效页面标识")
        return value


class KbUpdateBody(BaseModel):
    name: str | None = None
    kb_type: str | None = None
    description: str | None = None
    ingest_profile: dict | None = None  # 显式传 null = 清除配置(回到默认)
    wiki_navigation: WikiNavigationBody | None = None


class KbArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    expected_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 知识库被外部业务对象引用时,归档需显式确认"由宿主解除关联"(A8)
    acknowledge_external_unlink: bool = False


class KbRestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class KbPurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    expected_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_name: str
    # 管理员强制清理:跳过保留期与引用类拦截(法律保留/共享对象/活动任务仍拦)
    force: bool = False


@router.get("/bases", response_model=list[KnowledgeBase])
async def list_bases(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    lifecycle: Literal["active", "archived"] = Query(
        default="active",
        alias="lifecycle_status",
    ),
):
    """RLS 可见范围:自有 + 平台层 + 已分享给我的库。"""
    return (
        (
            await session.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.lifecycle_status == lifecycle)
                .order_by(KnowledgeBase.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@router.post("/bases", response_model=KnowledgeBase, status_code=status.HTTP_201_CREATED)
async def create_base(
    body: KbCreateBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    ingest_profile = _validate_ingest_profile(body.ingest_profile)
    await _require_enabled_image_enrichment(session, ingest_profile)
    kb = KnowledgeBase(
        org_id=ctx.org_id,
        name=body.name,
        kb_type=_validate_kb_type(body.kb_type),
        description=body.description,
        created_by=ctx.user_id,
        ingest_profile=ingest_profile,
    )
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return kb


@router.patch("/bases/{kb_id}", response_model=KnowledgeBase)
async def update_base(
    kb_id: UUID,
    body: KbUpdateBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    kb = await _require_own_kb(session, ctx, kb_id)
    # ingest_profile 允许显式 null 清除,故用 fields_set 而非 exclude_none 判断
    if "ingest_profile" in body.model_fields_set:
        ingest_profile = _validate_ingest_profile(body.ingest_profile)
        await _require_enabled_image_enrichment(session, ingest_profile)
        kb.ingest_profile = ingest_profile
    if "wiki_navigation" in body.model_fields_set:
        kb.wiki_navigation = (
            body.wiki_navigation.model_dump(mode="json")
            if body.wiki_navigation is not None
            else None
        )
    for k, v in body.model_dump(
        exclude_none=True,
        exclude={"ingest_profile", "wiki_navigation"},
    ).items():
        setattr(kb, k, _validate_kb_type(v) if k == "kb_type" else v)
    session.add(kb)
    await session.commit()
    await session.refresh(kb)
    return kb


@router.get(
    "/bases/{kb_id}/deletion-preview",
    response_model=KnowledgeBaseDeletionPreviewOut,
)
async def deletion_preview(
    kb_id: UUID,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> dict[str, Any]:
    await _require_own_kb(session, ctx, kb_id, allow_non_active=True)
    try:
        preview = await preview_knowledge_base_deletion(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
        )
    except KnowledgeBaseLifecycleError as exc:
        _raise_lifecycle_http(exc)
    return public_deletion_preview(preview)


@router.post("/bases/{kb_id}/archive", response_model=KnowledgeBase)
async def archive_base(
    kb_id: UUID,
    body: KbArchiveBody,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> KnowledgeBase:
    try:
        kb = await archive_knowledge_base(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            actor_id=ctx.user_id,
            reason=body.reason,
            expected_plan_hash=body.expected_plan_hash,
            acknowledge_external_unlink=body.acknowledge_external_unlink,
        )
        await session.commit()
        await session.refresh(kb)
        return kb
    except KnowledgeBaseLifecycleError as exc:
        await session.rollback()
        _raise_lifecycle_http(exc)


@router.post("/bases/{kb_id}/restore", response_model=KnowledgeBase)
async def restore_base(
    kb_id: UUID,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    body: KbRestoreBody | None = None,
) -> KnowledgeBase:
    try:
        kb = await restore_knowledge_base(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            actor_id=ctx.user_id,
            reason=body.reason if body is not None else "manual restore",
        )
        await session.commit()
        await session.refresh(kb)
        return kb
    except KnowledgeBaseLifecycleError as exc:
        await session.rollback()
        _raise_lifecycle_http(exc)


@router.delete("/bases/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_base(
    kb_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    if if_match is None:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            detail={
                "code": "deletion_plan_required",
                "message": "请先执行删除影响预检并通过 If-Match 提交计划哈希",
            },
        )
    try:
        await hard_delete_empty_knowledge_base(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            actor_id=ctx.user_id,
            expected_plan_hash=if_match,
        )
        await session.commit()
    except KnowledgeBaseLifecycleError as exc:
        await session.rollback()
        _raise_lifecycle_http(exc)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "kb_not_empty",
                "message": "知识库新增了引用，请重新预检",
            },
        ) from exc


@router.post(
    "/bases/{kb_id}/purge",
    response_model=KnowledgeBaseLifecycleOperationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def purge_base(
    kb_id: UUID,
    body: KbPurgeBody,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> dict[str, Any]:
    try:
        operation = await submit_knowledge_base_purge(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            actor_id=ctx.user_id,
            expected_plan_hash=body.expected_plan_hash,
            name_confirmation=body.confirmation_name,
            reason=body.reason,
            idempotency_key=idempotency_key or "",
            force=body.force,
        )
        await session.commit()
        await session.refresh(operation)
        return public_lifecycle_operation(operation)
    except KnowledgeBaseLifecycleError as exc:
        await session.rollback()
        _raise_lifecycle_http(exc)


@router.get(
    "/lifecycle-operations/{operation_id}",
    response_model=KnowledgeBaseLifecycleOperationOut,
)
async def lifecycle_operation(
    operation_id: UUID,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> dict[str, Any]:
    try:
        operation = await get_lifecycle_operation(
            session,
            org_id=ctx.org_id,
            operation_id=operation_id,
        )
        return public_lifecycle_operation(operation)
    except KnowledgeBaseLifecycleError as exc:
        _raise_lifecycle_http(exc)


@router.post(
    "/lifecycle-operations/{operation_id}/retry",
    response_model=KnowledgeBaseLifecycleOperationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_kb_lifecycle_operation(
    operation_id: UUID,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> dict[str, Any]:
    try:
        operation = await retry_lifecycle_operation(
            session,
            org_id=ctx.org_id,
            operation_id=operation_id,
        )
        await session.commit()
        await session.refresh(operation)
        return public_lifecycle_operation(operation)
    except KnowledgeBaseLifecycleError as exc:
        await session.rollback()
        _raise_lifecycle_http(exc)


class ShareBody(BaseModel):
    grantee_org_slug: str


class KbShareOut(BaseModel):
    """分享记录 + 被授权组织的可读标识。

    前端此前只能拿到 grantee_org_id 这个裸 UUID,列表里根本认不出分享给了谁;
    这里把组织名与 slug 一并带出。组织可能已被删除,故两字段可空,不编造占位名。
    """

    id: UUID
    kb_id: UUID
    grantee_org_id: UUID
    grantee_org_name: str | None = None
    grantee_org_slug: str | None = None
    created_at: datetime | None = None


def _share_out(share: KbShare, grantee: Organization | None) -> KbShareOut:
    return KbShareOut(
        id=share.id,
        kb_id=share.kb_id,
        grantee_org_id=share.grantee_org_id,
        grantee_org_name=grantee.name if grantee else None,
        grantee_org_slug=grantee.slug if grantee else None,
        created_at=share.created_at,
    )


@router.post("/bases/{kb_id}/shares", response_model=KbShareOut, status_code=201)
async def share_base(
    kb_id: UUID,
    body: ShareBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_SHARERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    kb = await _require_own_kb(session, ctx, kb_id)
    grantee = (
        await session.execute(
            select(Organization).where(Organization.slug == body.grantee_org_slug)
        )
    ).scalar_one_or_none()
    if grantee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "目标组织不存在")
    if grantee.id == ctx.org_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能分享给自己")
    existing = (
        await session.execute(
            select(KbShare).where(KbShare.kb_id == kb.id, KbShare.grantee_org_id == grantee.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _share_out(existing, grantee)
    share = KbShare(
        org_id=ctx.org_id, kb_id=kb.id, grantee_org_id=grantee.id, created_by=ctx.user_id
    )
    session.add(share)
    await session.commit()
    await session.refresh(share)
    return _share_out(share, grantee)


@router.get("/bases/{kb_id}/shares", response_model=list[KbShareOut])
async def list_shares(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    # 左连接组织表:组织被删时仍要列出这条分享,只是名字为空
    rows = (
        await session.execute(
            select(KbShare, Organization)
            .join(
                Organization,
                Organization.id == KbShare.grantee_org_id,
                isouter=True,
            )
            .where(KbShare.kb_id == kb_id)
        )
    ).all()
    return [_share_out(share, grantee) for share, grantee in rows]


@router.delete("/bases/{kb_id}/shares/{grantee_org_id}", status_code=204)
async def unshare_base(
    kb_id: UUID,
    grantee_org_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_SHARERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    await _require_own_kb(session, ctx, kb_id)
    share = (
        await session.execute(
            select(KbShare).where(KbShare.kb_id == kb_id, KbShare.grantee_org_id == grantee_org_id)
        )
    ).scalar_one_or_none()
    if share is not None:
        await session.delete(share)
        await session.commit()


class SnapshotBuildBody(BaseModel):
    revision_ids: list[UUID] | None = None
    reason: str = Field(default="manual snapshot build", min_length=1, max_length=500)


class SnapshotTransitionBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    operation_id: UUID | None = None


class SnapshotRollbackCapabilityOut(BaseModel):
    allowed: bool
    code: str | None
    message: str | None


class KnowledgeSnapshotOut(BaseModel):
    id: UUID
    org_id: UUID
    kb_id: UUID
    revision_set_hash: str
    embedding_fingerprint: dict
    config_fingerprint: str
    revision_manifest: list[dict]
    config_manifest: dict
    build_stats: dict
    status: SnapshotStatus
    ready_at: datetime | None
    activated_at: datetime | None
    retired_at: datetime | None
    failed_at: datetime | None
    error: str | None
    created_at: datetime | None
    updated_at: datetime | None
    rollback_capability: SnapshotRollbackCapabilityOut


def _snapshot_out(
    snapshot: KnowledgeSnapshot,
    capability: SnapshotRollbackCapability,
) -> KnowledgeSnapshotOut:
    return KnowledgeSnapshotOut(
        **snapshot.model_dump(),
        rollback_capability=SnapshotRollbackCapabilityOut(
            allowed=capability.allowed,
            code=capability.code,
            message=capability.message,
        ),
    )


@router.get("/bases/{kb_id}/snapshots", response_model=list[KnowledgeSnapshotOut])
async def list_snapshots(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    snapshots = (
        (
            await session.execute(
                select(KnowledgeSnapshot)
                .where(KnowledgeSnapshot.kb_id == kb_id)
                .order_by(KnowledgeSnapshot.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        _snapshot_out(
            snapshot,
            await snapshot_rollback_capability(
                session,
                kb=kb,
                snapshot=snapshot,
            ),
        )
        for snapshot in snapshots
    ]


@router.post("/bases/{kb_id}/snapshots", response_model=KnowledgeSnapshot)
async def create_snapshot(
    kb_id: UUID,
    body: SnapshotBuildBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    try:
        snapshot = await build_snapshot(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            revision_ids=body.revision_ids,
            actor_id=ctx.user_id,
            reason=body.reason,
        )
        await session.commit()
    except SnapshotError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.refresh(snapshot)
    return snapshot


@router.post(
    "/bases/{kb_id}/snapshots/{snapshot_id}/activate",
    response_model=KnowledgeSnapshot,
)
async def activate_snapshot_endpoint(
    kb_id: UUID,
    snapshot_id: UUID,
    body: SnapshotTransitionBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_SHARERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    try:
        snapshot = await activate_snapshot(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            snapshot_id=snapshot_id,
            actor_id=ctx.user_id,
            reason=body.reason,
            operation_id=body.operation_id,
        )
        await session.commit()
    except SnapshotError as exc:
        if str(exc).startswith("media_projection_invalid:"):
            # Media validation runs before any pointer/status mutation. Commit
            # only its sanitized operational incident; every other transition
            # error remains a full rollback.
            await session.commit()
        else:
            await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.refresh(snapshot)
    return snapshot


@router.post(
    "/bases/{kb_id}/snapshots/{snapshot_id}/rollback",
    response_model=KnowledgeSnapshot,
)
async def rollback_snapshot_endpoint(
    kb_id: UUID,
    snapshot_id: UUID,
    body: SnapshotTransitionBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_SHARERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    try:
        snapshot = await rollback_snapshot(
            session,
            org_id=ctx.org_id,
            kb_id=kb_id,
            target_snapshot_id=snapshot_id,
            actor_id=ctx.user_id,
            reason=body.reason,
            operation_id=body.operation_id,
        )
        await session.commit()
    except SnapshotError as exc:
        if str(exc).startswith("media_projection_invalid:"):
            await session.commit()
        else:
            await session.rollback()
        detail: str | dict[str, str] = str(exc)
        if isinstance(exc, SnapshotTransitionBlocked):
            detail = {"code": exc.code, "message": exc.public_message}
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    await session.refresh(snapshot)
    return snapshot


# ---- 通用投影实体 CRUD ----------------------------------------------------
#
# MIGRATION-PLAN B29:TF 用这套工厂同构生成六组端点,其中五组是行业专表
# (destinations/pois/hotels/costs/routes)。SDK 不带任何行业表,那五组端点整体
# 删除——通用实体(kb_entities)的 CRUD 统一由 api/v1/kb_entity_types.py 承担。
# 这里只保留 wiki 页(kb_pages):它是 SDK 自带的通用投影表,page_type 开放。

_ENTITIES: dict[str, type[SQLModel]] = {
    "pages": KbPage,  # 通用 wiki 页:page_type 开放,承载任意自定义类型知识
}

_PROJECTION_SUPPORT_TYPES: dict[type[SQLModel], str] = {
    KbPage: "wiki_page",
}


class EntityMoveBody(BaseModel):
    target_kb_id: UUID


def _writable_fields(model: type[SQLModel]) -> set[str]:
    return set(model.model_fields) - {
        "id",
        "org_id",
        "kb_id",
        "snapshot_id",
        "created_at",
    }


def _creatable_fields(model: type[SQLModel]) -> set[str]:
    return _writable_fields(model) | {"kb_id"}


def _require_legacy_projection_mutable(row: SQLModel) -> None:
    if getattr(row, "snapshot_id", None) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "快照投影不可原地修改；请通过 FactClaim 审核并发布新快照",
        )


async def _move_entity(
    session: AsyncSession,
    ctx: OrgContext,
    row: SQLModel,
    *,
    target_kb_id: UUID,
) -> None:
    source_kb_id = row.kb_id
    await _require_own_kb(session, ctx, source_kb_id)
    await _require_own_kb(session, ctx, target_kb_id)
    if source_kb_id == target_kb_id:
        return
    row.kb_id = target_kb_id
    session.add(row)


def _register_entity_crud(name: str, model: type[SQLModel]) -> None:
    @router.get(f"/{name}", response_model=list[model])
    async def list_items(
        session: Annotated[AsyncSession, Depends(get_org_session)],
        kb_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        stmt = (
            select(model)
            .where(
                active_projection_filter(model),
                live_snapshot_projection_filter(
                    model,
                    _PROJECTION_SUPPORT_TYPES[model],
                ),
            )
            .order_by(model.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if kb_id is not None:
            stmt = stmt.where(model.kb_id == kb_id)
        return (await session.execute(stmt)).scalars().all()

    @router.post(f"/{name}", response_model=model, status_code=status.HTTP_201_CREATED)
    async def create_item(
        body: dict,
        ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
        session: Annotated[AsyncSession, Depends(get_org_session)],
    ):
        if not body.get("kb_id"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 kb_id:实体必须挂在知识库下")
        kb = await _require_own_kb(session, ctx, UUID(str(body["kb_id"])))
        if kb.active_snapshot_id is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "该知识库已启用快照发布；请通过 FactClaim 审核流新增知识",
            )
        fields = {k: v for k, v in body.items() if k in _creatable_fields(model)}
        # id 显式生成:表模型 id 的 uuid4 默认只在 Column 层,
        # model_validate(pydantic 层)会因缺 id 直接拒绝
        row = model.model_validate({**fields, "id": uuid4(), "org_id": ctx.org_id})
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    @router.patch(f"/{name}/{{item_id}}", response_model=model)
    async def update_item(
        item_id: UUID,
        body: dict,
        ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
        session: Annotated[AsyncSession, Depends(get_org_session)],
    ):
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} 不存在")
        _require_legacy_projection_mutable(row)
        await _require_own_kb(session, ctx, row.kb_id)
        for k, v in body.items():
            if k in _writable_fields(model):
                setattr(row, k, v)
        session.add(row)
        try:
            await session.commit()
        except Exception as exc:  # RLS 写策略拒绝平台层条目 → 表现为约束错误
            raise HTTPException(status.HTTP_403_FORBIDDEN, "平台层条目对租户只读") from exc
        await session.refresh(row)
        return row

    @router.post(f"/{name}/{{item_id}}/move", response_model=model)
    async def move_item(
        item_id: UUID,
        body: EntityMoveBody,
        ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
        session: Annotated[AsyncSession, Depends(get_org_session)],
    ):
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} 不存在")
        _require_legacy_projection_mutable(row)
        await _move_entity(session, ctx, row, target_kb_id=body.target_kb_id)
        await session.commit()
        await session.refresh(row)
        return row

    @router.delete(f"/{name}/{{item_id}}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_item(
        item_id: UUID,
        ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
        session: Annotated[AsyncSession, Depends(get_org_session)],
    ):
        row = await session.get(model, item_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} 不存在")
        _require_legacy_projection_mutable(row)
        await _require_own_kb(session, ctx, row.kb_id)
        await session.delete(row)
        await session.commit()


for _name, _model in _ENTITIES.items():
    _register_entity_crud(_name, _model)


async def _get_owned_page(session: AsyncSession, ctx: OrgContext, page_id: UUID) -> KbPage:
    page = (
        await session.execute(
            select(KbPage)
            .where(
                KbPage.id == page_id,
                live_snapshot_projection_filter(KbPage, "wiki_page"),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "页面不存在")
    await _require_own_kb(session, ctx, page.kb_id)
    return page


async def _record_snapshot_page_decision(
    session: AsyncSession,
    page: KbPage,
    decision: Literal["published", "rejected"],
) -> None:
    if page.snapshot_id is None:
        return
    claim_ids = list(
        (
            await session.execute(
                select(FactClaim.id).where(
                    FactClaim.org_id == page.org_id,
                    FactClaim.kb_id == page.kb_id,
                    FactClaim.predicate == "wiki_page",
                    FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                )
            )
        )
        .scalars()
        .all()
    )
    claim_id = next(
        (
            item
            for item in claim_ids
            if projection_row_id(page.snapshot_id, "wiki_page", item) == page.id
        ),
        None,
    )
    if claim_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "无法定位页面对应的 Wiki 事实，请重新构建快照后再审核",
        )
    claim = (
        await session.execute(select(FactClaim).where(FactClaim.id == claim_id).with_for_update())
    ).scalar_one_or_none()
    if claim is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "页面对应的 Wiki 事实已变化，请刷新后重试",
        )
    record_wiki_claim_decision(claim, decision)
    session.add(claim)


async def _enable_snapshot_wiki_review_write(session: AsyncSession) -> None:
    await session.execute(select(func.set_config("app.wiki_review_write", "enabled", True)))


@router.post("/pages/{page_id}/draft/publish", response_model=KbPage)
async def publish_page_draft(
    page_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    page = await _get_owned_page(session, ctx, page_id)
    try:
        publish_wiki_draft(page)
        await _record_snapshot_page_decision(session, page, "published")
        await _enable_snapshot_wiki_review_write(session)
    except WikiDraftStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.add(page)
    await session.commit()
    await session.refresh(page)
    return page


@router.post("/pages/{page_id}/draft/reject", response_model=KbPage)
async def reject_page_draft(
    page_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    page = await _get_owned_page(session, ctx, page_id)
    try:
        reject_wiki_draft(page)
        await _record_snapshot_page_decision(session, page, "rejected")
        await _enable_snapshot_wiki_review_write(session)
    except WikiDraftStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.add(page)
    await session.commit()
    await session.refresh(page)
    return page


# ---- 文档摄入(FR-112)----------------------------------------------------


def _clean_rel_path(rel_path: str | None) -> str | None:
    """文件夹上传的库内相对路径:归一分隔符、剥离首尾斜杠、拒绝路径穿越。"""
    if not rel_path:
        return None
    cleaned = rel_path.replace("\\", "/").strip("/")
    if not cleaned:
        return None
    if ".." in cleaned.split("/") or len(cleaned) > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法 rel_path")
    return cleaned


def _require_supported_document(filename: str | None) -> str:
    resolved = filename or "unnamed"
    suffix = PurePosixPath(resolved.lower()).suffix
    if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
        supported = ", ".join(SUPPORTED_DOCUMENT_SUFFIXES)
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"不支持的文件类型: {suffix or resolved}; 支持: {supported}",
        )
    return resolved


async def _read_document_upload(file: UploadFile) -> bytes:
    max_bytes = get_settings().kb_upload_max_file_bytes
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "空文件")
    if len(data) > max_bytes:
        max_mib = max_bytes / (1024 * 1024)
        display_limit = f"{max_mib:g} MiB"
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超过 {display_limit} 上限",
        )
    return data


async def _resolve_doc_type(session: AsyncSession, org_id: UUID, doc_type: str) -> str:
    """doc_type 开放化(M3a):内置 DocType 之外,允许已注册的实体类型 key
    (通用抽取链路);未注册的一律 422。"""
    if doc_type in {member.value for member in DocType}:
        return doc_type
    entity_type = await get_entity_type(session, org_id, doc_type)
    if entity_type is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"未知文档类型:{doc_type}(需为内置类型或已注册的实体类型 key)",
        )
    return entity_type.type_key


@router.post("/documents", response_model=SourceDocument, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile,
    kb_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    rel_path: str | None = None,
):
    await _require_own_kb(session, ctx, kb_id)
    rel_path = _clean_rel_path(rel_path)
    filename = _require_supported_document(file.filename)
    data = await _read_document_upload(file)
    sha256 = hashlib.sha256(data).hexdigest()

    existing = (
        (
            await session.execute(
                select(SourceDocument).where(
                    SourceDocument.kb_id == kb_id, SourceDocument.sha256 == sha256
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"内容相同的文档已存在: {existing.id}")

    doc = SourceDocument(
        org_id=ctx.org_id,
        kb_id=kb_id,
        filename=filename,
        object_key="",
        sha256=sha256,
        doc_type=DocType.UNCLASSIFIED,
        status=DocStatus.STAGED,
        rel_path=rel_path,
    )
    try:
        async with session.begin_nested():
            session.add(doc)
            await session.flush()
    except IntegrityError as exc:
        duplicate = await session.scalar(
            select(SourceDocument.id).where(
                SourceDocument.kb_id == kb_id,
                SourceDocument.sha256 == sha256,
            )
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"内容相同的文档已存在: {duplicate}",
        ) from exc
    revision_id = uuid4()
    doc.object_key = storage.kb_revision_object_key(ctx.org_id, doc.id, revision_id, doc.filename)
    await storage.put_object(doc.object_key, data, file.content_type or "application/octet-stream")
    revision = DocumentRevision(
        id=revision_id,
        org_id=ctx.org_id,
        kb_id=kb_id,
        doc_id=doc.id,
        revision_no=1,
        sha256=sha256,
        original_object_key=doc.object_key,
    )
    session.add(revision)
    session.add(doc)
    # 上传只持久化不可变 source/revision；分类与排队由显式 API 完成。
    await record_usage(
        session,
        org_id=ctx.org_id,
        task="kb.upload",
        model=DocType.UNCLASSIFIED.value,
        quantity=len(data),
    )
    await session.commit()
    await session.refresh(doc)
    return doc


async def _latest_revision_is_tombstoned(session: AsyncSession, document_id: UUID) -> bool:
    latest_status = await session.scalar(
        select(DocumentRevision.status)
        .where(DocumentRevision.doc_id == document_id)
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    return str(getattr(latest_status, "value", latest_status)) == RevisionStatus.TOMBSTONED.value


@router.get("/documents/{doc_id}/revisions", response_model=list[DocumentRevision])
async def list_document_revisions(
    doc_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_visible_active_kb(session, doc.kb_id)
    return (
        (
            await session.execute(
                select(DocumentRevision)
                .where(
                    DocumentRevision.doc_id == doc.id,
                    DocumentRevision.org_id == doc.org_id,
                    DocumentRevision.kb_id == doc.kb_id,
                )
                .order_by(DocumentRevision.revision_no.desc(), DocumentRevision.id)
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/documents/{doc_id}/revisions",
    response_model=DocumentRevision,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document_revision(
    doc_id: UUID,
    file: UploadFile,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    if doc.lifecycle_status != DocumentLifecycleStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "仅活动文档可上传新修订",
        )
    if await _latest_revision_is_tombstoned(session, doc.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "文档已撤回，必须通过显式恢复流程后才能上传新修订",
        )
    if doc.status in (DocStatus.STAGED, DocStatus.UPLOADED, DocStatus.PARSING):
        raise HTTPException(status.HTTP_409_CONFLICT, "当前修订仍在摄入中")
    filename = _require_supported_document(file.filename or doc.filename)
    data = await _read_document_upload(file)
    sha256 = hashlib.sha256(data).hexdigest()
    duplicate = await session.scalar(
        select(DocumentRevision.id).where(
            DocumentRevision.doc_id == doc.id,
            DocumentRevision.sha256 == sha256,
        )
    )
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"相同内容修订已存在: {duplicate}")
    active_run = await session.scalar(
        select(IngestRun.id)
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .where(
            DocumentRevision.doc_id == doc.id,
            IngestRun.stage == "document",
            IngestRun.status.in_((IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)),
        )
        .limit(1)
    )
    if active_run is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前修订已有待执行摄入任务")

    # The revision id is part of the immutable object key. Uploading first can
    # leave only an unreferenced object if a concurrent duplicate wins; it never
    # overwrites an existing revision artifact.
    revision_id = uuid4()
    object_key = storage.kb_revision_object_key(ctx.org_id, doc.id, revision_id, filename)
    await storage.put_object(object_key, data, file.content_type or "application/octet-stream")

    doc = (
        await session.execute(
            select(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if doc.lifecycle_status != DocumentLifecycleStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "仅活动文档可上传新修订",
        )
    if doc.status in (DocStatus.STAGED, DocStatus.UPLOADED, DocStatus.PARSING):
        raise HTTPException(status.HTTP_409_CONFLICT, "当前修订仍在摄入中")
    if await _latest_revision_is_tombstoned(session, doc.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "文档已撤回，必须通过显式恢复流程后才能上传新修订",
        )
    active_run = await session.scalar(
        select(IngestRun.id)
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .where(
            DocumentRevision.doc_id == doc.id,
            IngestRun.stage == "document",
            IngestRun.status.in_((IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)),
        )
        .limit(1)
    )
    if active_run is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前修订已有待执行摄入任务")
    duplicate = await session.scalar(
        select(DocumentRevision.id).where(
            DocumentRevision.doc_id == doc.id,
            DocumentRevision.sha256 == sha256,
        )
    )
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"相同内容修订已存在: {duplicate}")
    revision_no = int(
        await session.scalar(
            select(func.coalesce(func.max(DocumentRevision.revision_no), 0) + 1).where(
                DocumentRevision.doc_id == doc.id
            )
        )
        or 1
    )
    revision = DocumentRevision(
        id=revision_id,
        org_id=doc.org_id,
        kb_id=doc.kb_id,
        doc_id=doc.id,
        revision_no=revision_no,
        sha256=sha256,
        original_object_key=object_key,
    )
    session.add(revision)
    doc.filename = filename
    doc.object_key = object_key
    doc.sha256 = sha256
    doc.markdown_key = None
    doc.parser_name = None
    doc.doc_type = DocType.UNCLASSIFIED
    doc.status = DocStatus.STAGED
    doc.error = None
    doc.progress = 0
    doc.parsing_started_at = None
    session.add(doc)
    await record_usage(
        session,
        org_id=ctx.org_id,
        task="kb.upload",
        model=DocType.UNCLASSIFIED.value,
        quantity=len(data),
    )
    await session.commit()
    await session.refresh(revision)
    return revision


class DocumentOperationOut(BaseModel):
    id: UUID
    document_id: UUID
    operation_type: str
    status: str
    phase: str | None
    requested_revision_id: UUID | None
    target_snapshot_id: UUID | None
    attempts: int
    reason: str
    impact_summary: dict[str, Any]
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class DocumentReclassificationOut(BaseModel):
    run_id: UUID
    revision_id: UUID
    previous_doc_type: str
    target_doc_type: str
    status: str
    error: str | None
    retryable: bool
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class SourceDocumentOut(BaseModel):
    id: UUID
    kb_id: UUID
    filename: str
    doc_type: str
    classification_state: Literal["unclassified", "classified"]
    latest_ingest_run_status: str | None
    status: str
    lifecycle_status: str
    legal_hold_active: bool
    legal_hold_at: datetime | None
    latest_operation: DocumentOperationOut | None
    latest_reclassification: DocumentReclassificationOut | None
    error: str | None
    rel_path: str | None
    progress: int
    progress_stage: str | None
    progress_done: int
    progress_total: int
    expires_at: datetime | None
    markdown_key: str | None
    parser_name: str | None
    created_at: datetime | None


def _document_operation_out(operation: KbDocumentOperation) -> DocumentOperationOut:
    operation_type = str(getattr(operation.operation_type, "value", operation.operation_type))
    impact_summary = (
        public_document_purge_operation_detail(operation)
        if operation_type == "purge"
        else operation.impact_summary
    )
    return DocumentOperationOut(
        id=operation.id,
        document_id=operation.document_id,
        operation_type=operation_type,
        status=str(getattr(operation.status, "value", operation.status)),
        phase=operation.stage,
        requested_revision_id=operation.revision_id,
        target_snapshot_id=operation.target_snapshot_id,
        attempts=operation.attempts,
        reason=operation.reason,
        impact_summary=impact_summary,
        error_code=operation.last_error_code,
        error_message=operation.last_error,
        retryable=operation.retryable,
        created_at=operation.created_at or datetime.now(UTC),
        started_at=operation.started_at,
        completed_at=operation.completed_at,
    )


def _reclassification_out(run: IngestRun) -> DocumentReclassificationOut:
    stats = run.stats if isinstance(run.stats, dict) else {}
    previous_doc_type = stats.get("previous_doc_type")
    target_doc_type = stats.get("target_doc_type")
    if not isinstance(previous_doc_type, str) or not isinstance(target_doc_type, str):
        raise RuntimeError(f"typed re-extraction run has invalid stats: {run.id}")
    run_status = str(getattr(run.status, "value", run.status))
    error = None
    if run.error:
        error = (
            run.error
            if run.error
            in {
                "document_not_active",
                "document_stop_requested",
                "parse_artifact_not_staged",
                "revision_not_staged",
                "revision_tombstoned",
            }
            else "typed_extraction_failed"
        )
    return DocumentReclassificationOut(
        run_id=run.id,
        revision_id=run.revision_id,
        previous_doc_type=previous_doc_type,
        target_doc_type=target_doc_type,
        status=run_status,
        error=error,
        retryable=run_status == IngestRunStatus.FAILED.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


async def _documents_with_latest_operation(
    session: AsyncSession,
    documents: list[SourceDocument],
) -> list[SourceDocumentOut]:
    document_ids = [document.id for document in documents]
    operations = (
        (
            (
                await session.execute(
                    select(KbDocumentOperation)
                    .where(KbDocumentOperation.document_id.in_(document_ids))
                    .order_by(
                        KbDocumentOperation.document_id,
                        KbDocumentOperation.created_at.desc(),
                        KbDocumentOperation.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if document_ids
        else []
    )
    latest_by_document: dict[UUID, KbDocumentOperation] = {}
    for operation in operations:
        latest_by_document.setdefault(operation.document_id, operation)
    reclassification_rows = (
        (
            await session.execute(
                select(IngestRun, DocumentRevision.doc_id)
                .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
                .where(
                    DocumentRevision.doc_id.in_(document_ids),
                    IngestRun.stage.like("reextract:%"),
                    IngestRun.segment_no == 0,
                )
                .order_by(
                    DocumentRevision.doc_id,
                    IngestRun.created_at.desc(),
                    IngestRun.id.desc(),
                )
            )
        ).all()
        if document_ids
        else []
    )
    latest_reclassification_by_document: dict[UUID, IngestRun] = {}
    for run, document_id in reclassification_rows:
        latest_reclassification_by_document.setdefault(document_id, run)
    root_run_rows = (
        (
            await session.execute(
                select(IngestRun, DocumentRevision.doc_id)
                .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
                .where(
                    DocumentRevision.doc_id.in_(document_ids),
                    IngestRun.stage == "document",
                    IngestRun.segment_no == 0,
                )
                .order_by(
                    DocumentRevision.doc_id,
                    DocumentRevision.revision_no.desc(),
                    IngestRun.created_at.desc(),
                    IngestRun.id.desc(),
                )
            )
        ).all()
        if document_ids
        else []
    )
    latest_root_run_by_document: dict[UUID, IngestRun] = {}
    for run, document_id in root_run_rows:
        latest_root_run_by_document.setdefault(document_id, run)
    return [
        SourceDocumentOut(
            id=document.id,
            kb_id=document.kb_id,
            filename=document.filename,
            doc_type=str(getattr(document.doc_type, "value", document.doc_type)),
            classification_state=(
                "unclassified"
                if str(document.doc_type) == DocType.UNCLASSIFIED.value
                else "classified"
            ),
            latest_ingest_run_status=(
                str(
                    getattr(
                        latest_root_run_by_document[document.id].status,
                        "value",
                        latest_root_run_by_document[document.id].status,
                    )
                )
                if document.id in latest_root_run_by_document
                else None
            ),
            status=str(getattr(document.status, "value", document.status)),
            lifecycle_status=str(
                getattr(
                    document.lifecycle_status,
                    "value",
                    document.lifecycle_status,
                )
            ),
            legal_hold_active=document.legal_hold_at is not None,
            legal_hold_at=document.legal_hold_at,
            latest_operation=(
                _document_operation_out(latest_by_document[document.id]).model_dump()
                if document.id in latest_by_document
                else None
            ),
            latest_reclassification=(
                _reclassification_out(
                    latest_reclassification_by_document[document.id]
                ).model_dump()
                if document.id in latest_reclassification_by_document
                else None
            ),
            error=document.error,
            rel_path=document.rel_path,
            progress=document.progress,
            progress_stage=document.progress_stage,
            progress_done=document.progress_done,
            progress_total=document.progress_total,
            expires_at=document.expires_at,
            markdown_key=document.markdown_key,
            parser_name=document.parser_name,
            created_at=document.created_at,
        )
        for document in documents
    ]


@router.get("/documents", response_model=list[SourceDocumentOut])
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    kb_id: UUID | None = None,
    doc_status: Annotated[DocStatus | None, Query(alias="status")] = None,
    limit: int = 100,
    offset: int = 0,
):
    if kb_id is not None:
        await _require_visible_active_kb(session, kb_id)
    stmt = (
        select(SourceDocument)
        .where(
            SourceDocument.kb_id.in_(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.lifecycle_status == "active"
                )
            )
        )
        .order_by(SourceDocument.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if kb_id is not None:
        stmt = stmt.where(SourceDocument.kb_id == kb_id)
    if doc_status is not None:
        stmt = stmt.where(SourceDocument.status == doc_status.value)
    documents = list((await session.execute(stmt)).scalars().all())
    leases = await capture_active_knowledge_base_leases(
        session,
        kb_ids={document.kb_id for document in documents},
        lock=True,
    )
    documents = [
        document for document in documents if document.kb_id in leases
    ]
    return await _documents_with_latest_operation(session, documents)


class DocumentClassificationItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    doc_type: str = Field(min_length=1, max_length=50)


class DocumentClassificationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DocumentClassificationItemBody] = Field(min_length=1, max_length=100)


class DocumentClassificationUpdatedOut(BaseModel):
    document_id: UUID
    doc_type: str
    idempotent: bool


class DocumentCommandSkippedOut(BaseModel):
    document_id: UUID
    code: str
    reason: str


class DocumentClassificationsOut(BaseModel):
    updated: list[DocumentClassificationUpdatedOut]
    skipped: list[DocumentCommandSkippedOut]


class DocumentQueueItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID


class DocumentIngestionQueueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DocumentQueueItemBody] = Field(min_length=1, max_length=100)


class DocumentQueuedOut(BaseModel):
    document_id: UUID
    revision_id: UUID
    doc_type: str
    rel_path: str | None
    run_id: UUID
    run_status: str
    idempotent: bool
    reason_code: str | None = None


class DocumentIngestionQueueOut(BaseModel):
    queued: list[DocumentQueuedOut]
    skipped: list[DocumentCommandSkippedOut]


_DOCUMENT_COMMAND_REASONS = {
    "duplicate_document": "同一批次不能重复选择同一文档",
    "document_not_found": "文档不存在或无权访问",
    "document_not_active": "文档当前生命周期不允许此操作",
    "document_not_staged": "文档已离开待处理阶段",
    "classification_required": "需要先选择文档划分",
    "classification_unavailable": "当前文档划分已不可用，请重新分类",
    "revision_missing": "文档没有可入队的修订",
    "revision_not_uploaded": "最新修订已离开待处理阶段",
    "revision_tombstoned": "最新修订已撤回",
    "already_queued": "文档已经排入解析队列",
    "target_unclassified": "必须选择实际文档处理类型",
    "invalid_doc_type": "文档处理类型不存在或不可用",
    "ingest_run_conflict": "文档已存在其他摄入运行",
}


def _document_command_skipped(
    document_id: UUID,
    code: str,
) -> DocumentCommandSkippedOut:
    return DocumentCommandSkippedOut(
        document_id=document_id,
        code=code,
        reason=_DOCUMENT_COMMAND_REASONS[code],
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


async def _lock_source_document(
    session: AsyncSession,
    document_id: UUID,
) -> SourceDocument | None:
    return await session.scalar(
        select(SourceDocument)
        .where(SourceDocument.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _latest_document_revision(
    session: AsyncSession,
    document: SourceDocument,
) -> DocumentRevision | None:
    return await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.doc_id == document.id,
            DocumentRevision.org_id == document.org_id,
            DocumentRevision.kb_id == document.kb_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )


async def _document_root_ingest_run(
    session: AsyncSession,
    revision: DocumentRevision,
) -> IngestRun | None:
    return await session.scalar(
        select(IngestRun).where(
            IngestRun.revision_id == revision.id,
            IngestRun.stage == "document",
            IngestRun.segment_no == 0,
        )
    )


@router.post(
    "/documents/classifications",
    response_model=DocumentClassificationsOut,
)
async def classify_documents(
    body: DocumentClassificationsBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> DocumentClassificationsOut:
    """Persist per-source extraction types without creating ingest runs."""
    updated: list[DocumentClassificationUpdatedOut] = []
    skipped: list[DocumentCommandSkippedOut] = []
    seen: set[UUID] = set()

    for item in body.items:
        if item.document_id in seen:
            skipped.append(
                _document_command_skipped(item.document_id, "duplicate_document")
            )
            continue
        seen.add(item.document_id)

        requested_type = item.doc_type.strip()
        if requested_type == DocType.UNCLASSIFIED.value:
            skipped.append(
                _document_command_skipped(item.document_id, "target_unclassified")
            )
            continue
        try:
            target = await _resolve_doc_type(session, ctx.org_id, requested_type)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
                raise
            skipped.append(
                _document_command_skipped(item.document_id, "invalid_doc_type")
            )
            continue

        document = await _lock_source_document(session, item.document_id)
        if document is None:
            skipped.append(
                _document_command_skipped(item.document_id, "document_not_found")
            )
            continue
        await _require_own_kb(session, ctx, document.kb_id)
        if _enum_value(document.lifecycle_status) != DocumentLifecycleStatus.ACTIVE.value:
            skipped.append(
                _document_command_skipped(document.id, "document_not_active")
            )
            continue
        if _enum_value(document.status) != DocStatus.STAGED.value:
            skipped.append(
                _document_command_skipped(document.id, "document_not_staged")
            )
            continue

        current_type = _enum_value(document.doc_type)
        idempotent = current_type == target
        if not idempotent:
            document.doc_type = target
            session.add(document)
        updated.append(
            DocumentClassificationUpdatedOut(
                document_id=document.id,
                doc_type=target,
                idempotent=idempotent,
            )
        )

    await session.commit()
    return DocumentClassificationsOut(updated=updated, skipped=skipped)


@router.post(
    "/documents/ingestion-queue",
    response_model=DocumentIngestionQueueOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_documents(
    body: DocumentIngestionQueueBody,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> DocumentIngestionQueueOut:
    """Explicitly enqueue classified staged sources and dispatch after commit."""
    queued: list[DocumentQueuedOut] = []
    skipped: list[DocumentCommandSkippedOut] = []
    dispatches: list[tuple[UUID, UUID]] = []
    seen: set[UUID] = set()

    for item in body.items:
        if item.document_id in seen:
            skipped.append(
                _document_command_skipped(item.document_id, "duplicate_document")
            )
            continue
        seen.add(item.document_id)

        document = await _lock_source_document(session, item.document_id)
        if document is None:
            skipped.append(
                _document_command_skipped(item.document_id, "document_not_found")
            )
            continue
        await _require_own_kb(session, ctx, document.kb_id)
        if _enum_value(document.lifecycle_status) != DocumentLifecycleStatus.ACTIVE.value:
            skipped.append(
                _document_command_skipped(document.id, "document_not_active")
            )
            continue

        doc_type = _enum_value(document.doc_type)
        if doc_type == DocType.UNCLASSIFIED.value:
            skipped.append(
                _document_command_skipped(document.id, "classification_required")
            )
            continue
        try:
            resolved_type = await _resolve_doc_type(session, ctx.org_id, doc_type)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
                raise
            skipped.append(
                _document_command_skipped(document.id, "classification_unavailable")
            )
            continue

        revision = await _latest_document_revision(session, document)
        if revision is None:
            skipped.append(_document_command_skipped(document.id, "revision_missing"))
            continue
        revision_status = _enum_value(revision.status)
        if (
            revision_status == RevisionStatus.TOMBSTONED.value
            or revision.tombstoned_at is not None
        ):
            skipped.append(
                _document_command_skipped(document.id, "revision_tombstoned")
            )
            continue

        existing_run = await _document_root_ingest_run(session, revision)
        if existing_run is not None:
            existing_status = _enum_value(existing_run.status)
            if existing_status in {
                IngestRunStatus.QUEUED.value,
                IngestRunStatus.RUNNING.value,
                IngestRunStatus.STAGED.value,
                IngestRunStatus.SUCCEEDED.value,
            }:
                queued.append(
                    DocumentQueuedOut(
                        document_id=document.id,
                        revision_id=revision.id,
                        doc_type=resolved_type,
                        rel_path=document.rel_path,
                        run_id=existing_run.id,
                        run_status=existing_status,
                        idempotent=True,
                        reason_code="already_queued",
                    )
                )
            else:
                skipped.append(
                    _document_command_skipped(document.id, "ingest_run_conflict")
                )
            continue

        if _enum_value(document.status) != DocStatus.STAGED.value:
            skipped.append(
                _document_command_skipped(document.id, "document_not_staged")
            )
            continue
        if revision_status != RevisionStatus.UPLOADED.value:
            skipped.append(
                _document_command_skipped(document.id, "revision_not_uploaded")
            )
            continue

        document.status = DocStatus.UPLOADED
        document.error = None
        document.progress = 0
        session.add(document)
        _, run = await enqueue_document_ingestion(
            session,
            document,
            revision=revision,
        )
        queued.append(
            DocumentQueuedOut(
                document_id=document.id,
                revision_id=revision.id,
                doc_type=resolved_type,
                rel_path=document.rel_path,
                run_id=run.id,
                run_status=IngestRunStatus.QUEUED.value,
                idempotent=False,
            )
        )
        dispatches.append((run.id, document.org_id))

    await session.commit()
    for run_id, org_id in dispatches:
        await _dispatch_kb_ingest_run(run_id, org_id, background)
    return DocumentIngestionQueueOut(queued=queued, skipped=skipped)


class DocumentReclassificationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_doc_type: str = Field(min_length=1, max_length=50)


_RECLASSIFICATION_REASON_MESSAGES = {
    "document_not_active": "文档不处于可用生命周期",
    "revision_missing": "文档没有可用修订",
    "same_extraction_type": "文档已经是目标抽取类型",
    "document_not_terminal": "文档尚未完成原摄入流程",
    "revision_tombstoned": "最新修订已撤回",
    "revision_not_staged": "最新修订尚未形成可复用的暂存产物",
    "markdown_artifact_missing": "最新修订缺少 Markdown 暂存产物",
    "parse_artifact_not_staged": "解析阶段尚未成功暂存",
    "structured_artifact_missing": "Docling 结构化暂存产物缺失",
    "reclassification_in_progress": "该修订已有其他重分类正在执行",
}


@router.post(
    "/documents/{doc_id}/reclassify",
    response_model=DocumentReclassificationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reclassify_document(
    doc_id: UUID,
    body: DocumentReclassificationBody,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> DocumentReclassificationOut:
    target_doc_type = await _resolve_doc_type(
        session, ctx.org_id, body.target_doc_type.strip()
    )
    if target_doc_type in {
        DocType.GENERAL.value,
        DocType.UNCLASSIFIED.value,
    }:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "target_not_structured",
                "message": "重分类目标必须是结构化抽取类型",
            },
        )
    doc = await session.scalar(
        select(SourceDocument)
        .where(SourceDocument.id == doc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    if (
        _enum_value(doc.status) == DocStatus.STAGED.value
        or _enum_value(doc.doc_type) == DocType.UNCLASSIFIED.value
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "classification_required",
                "message": "新上传文档必须先通过待处理区完成分类并显式入队",
            },
        )
    eligibility = await assess_typed_reextraction(
        session,
        doc,
        target_doc_type,
    )
    if not eligibility.eligible or eligibility.revision is None:
        code = eligibility.reason_code or "reclassification_ineligible"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": code,
                "message": _RECLASSIFICATION_REASON_MESSAGES.get(
                    code, "当前文档不能执行重分类"
                ),
            },
        )
    existing = eligibility.existing_run
    if existing is not None and str(existing.status) in {
        IngestRunStatus.QUEUED.value,
        IngestRunStatus.RUNNING.value,
        IngestRunStatus.STAGED.value,
        IngestRunStatus.SUCCEEDED.value,
    }:
        return _reclassification_out(existing)
    run = await enqueue_typed_reextraction(
        session,
        doc=doc,
        revision=eligibility.revision,
        target_doc_type=target_doc_type,
    )
    doc.doc_type = target_doc_type
    session.add(doc)
    await session.commit()
    await session.refresh(run)
    await _dispatch_kb_reextract_run(run.id, doc.org_id, background)
    return _reclassification_out(run)


@router.get(
    "/documents/{doc_id}/withdrawal-impact",
    response_model=None,
)
async def get_document_withdrawal_impact(
    doc_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    try:
        impact = await preview_document_withdrawal(
            session,
            org_id=ctx.org_id,
            document_id=doc_id,
        )
    except DocumentDeletionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return impact.as_dict()


class DocumentLegalHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


async def _change_document_legal_hold(
    session: AsyncSession,
    *,
    ctx: OrgContext,
    document_id: UUID,
    reason: str,
    active: bool,
) -> SourceDocumentOut:
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.org_id == ctx.org_id,
        )
        .with_for_update()
    )
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source document not found")
    lifecycle = str(
        getattr(
            document.lifecycle_status,
            "value",
            document.lifecycle_status,
        )
    )
    if lifecycle == "purged":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "purged document legal hold cannot be changed",
        )
    changed = (document.legal_hold_at is not None) != active
    if changed:
        if active:
            document.legal_hold_at = datetime.now(UTC)
            document.legal_hold_by = ctx.user_id
            document.legal_hold_reason = reason
            action = "kb.document.legal_hold.applied"
        else:
            document.legal_hold_at = None
            document.legal_hold_by = None
            document.legal_hold_reason = None
            action = "kb.document.legal_hold.released"
        session.add_all(
            [
                document,
                AuditLog(
                    org_id=ctx.org_id,
                    user_id=ctx.user_id,
                    action=action,
                    entity_type="source_document",
                    entity_id=str(document.id),
                    detail={
                        "kb_id": str(document.kb_id),
                        "reason": reason,
                    },
                ),
            ]
        )
    await session.commit()
    return (await _documents_with_latest_operation(session, [document]))[0]


@router.post(
    "/documents/{doc_id}/legal-hold",
    response_model=SourceDocumentOut,
)
async def apply_document_legal_hold(
    doc_id: UUID,
    body: DocumentLegalHoldRequest,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    return await _change_document_legal_hold(
        session,
        ctx=ctx,
        document_id=doc_id,
        reason=body.reason,
        active=True,
    )


@router.post(
    "/documents/{doc_id}/legal-hold/release",
    response_model=SourceDocumentOut,
)
async def release_document_legal_hold(
    doc_id: UUID,
    body: DocumentLegalHoldRequest,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    return await _change_document_legal_hold(
        session,
        ctx=ctx,
        document_id=doc_id,
        reason=body.reason,
        active=False,
    )


class DocumentPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_plan_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason: str = Field(min_length=1, max_length=500)
    confirm_irreversible: bool
    # 管理员强制清理:跳过保留期与引用类拦截(法律保留/共享对象/未撤回门禁仍拦)
    force: bool = False

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class DocumentPurgeBlockerOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    count: int = Field(ge=0)
    retry_at: datetime | None = None


class DocumentPurgePlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    document_id: UUID
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible: bool
    blockers: list[DocumentPurgeBlockerOut]
    delete_counts: dict[str, int]
    retain_counts: dict[str, int]
    retention_deadline: datetime | None


async def _ensure_document_purge_dispatch(
    session: AsyncSession,
    *,
    operation: KbDocumentOperation,
    actor_id: UUID,
) -> None:
    if str(getattr(operation.status, "value", operation.status)) == "completed":
        return
    raw_generation = operation.impact_summary.get("purge_dispatch_generation", 0)
    generation = (
        raw_generation
        if isinstance(raw_generation, int)
        and not isinstance(raw_generation, bool)
        and raw_generation >= 0
        else 0
    )
    idempotency_key = (
        f"document:{operation.org_id.hex}:{operation.document_id.hex}:purge-dispatch:{generation}"
    )
    existing = await session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.org_id == operation.org_id,
            OutboxEvent.kb_id == operation.kb_id,
            OutboxEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        existing_status = str(getattr(existing.status, "value", existing.status))
        if existing_status not in {
            OutboxStatus.PUBLISHED.value,
            OutboxStatus.DEAD_LETTER.value,
        }:
            return
        generation += 1
        operation.impact_summary = {
            **operation.impact_summary,
            "purge_dispatch_generation": generation,
        }
        session.add(operation)
        idempotency_key = (
            f"document:{operation.org_id.hex}:{operation.document_id.hex}:"
            f"purge-dispatch:{generation}"
        )
    session.add(
        OutboxEvent(
            id=uuid4(),
            org_id=operation.org_id,
            kb_id=operation.kb_id,
            aggregate_type="source_document",
            aggregate_id=operation.document_id,
            event_type="document.purge.requested",
            idempotency_key=idempotency_key,
            payload={
                "document_id": str(operation.document_id),
                "operation_id": str(operation.id),
                "actor": str(actor_id),
            },
        )
    )
    await session.flush()


def _raise_document_purge_error(exc: DocumentPurgeError) -> NoReturn:
    if isinstance(exc, DocumentPurgeNotFound):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {
                "code": "PURGE_NOT_FOUND",
                "message": "source document or purge operation not found",
            },
        ) from exc
    if isinstance(exc, DocumentPurgeForbidden):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "code": "PURGE_FORBIDDEN",
                "message": "document purge permission is required",
            },
        ) from exc
    if isinstance(exc, DocumentPurgeBlocked):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "PURGE_BLOCKED",
                "plan": exc.plan.as_public_dict(),
            },
        ) from exc
    if isinstance(exc, DocumentPurgePlanDrift):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "PURGE_PLAN_DRIFT",
                "plan": exc.plan.as_public_dict(),
            },
        ) from exc
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        {
            "code": "PURGE_REQUEST_INVALID",
            "message": "document purge request is invalid",
        },
    ) from exc


@router.get(
    "/documents/{doc_id}/purge-preview",
    response_model=DocumentPurgePlanOut,
)
async def get_document_purge_preview(
    doc_id: UUID,
    _ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    try:
        plan = await preview_document_purge(
            session,
            org_id=_ctx.org_id,
            document_id=doc_id,
        )
    except DocumentPurgeError as exc:
        _raise_document_purge_error(exc)
    return DocumentPurgePlanOut.model_validate(plan.as_public_dict())


@router.post(
    "/documents/{doc_id}/purge",
    response_model=DocumentOperationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_document_purge(
    doc_id: UUID,
    body: DocumentPurgeRequest,
    ctx: Annotated[
        OrgContext,
        Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)),
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    if not body.confirm_irreversible:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "code": "PURGE_CONFIRMATION_REQUIRED",
                "message": "explicit irreversible-action confirmation is required",
            },
        )
    try:
        operation = await submit_document_purge(
            session,
            org_id=ctx.org_id,
            document_id=doc_id,
            actor_id=ctx.user_id,
            reason=body.reason,
            expected_plan_hash=body.expected_plan_hash,
            authorized=True,
            force=body.force,
        )
        await _ensure_document_purge_dispatch(
            session,
            operation=operation,
            actor_id=ctx.user_id,
        )
        await session.commit()
    except DocumentPurgeError as exc:
        await session.rollback()
        _raise_document_purge_error(exc)
    return _document_operation_out(operation)


@router.delete("/documents/{doc_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_document(
    doc_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    reason: str = "document withdrawn",
):
    try:
        result = await delete_source_document(
            session,
            org_id=ctx.org_id,
            document_id=doc_id,
            actor_id=ctx.user_id,
            reason=reason,
        )
        await session.commit()
    except DocumentDeletionError as exc:
        await session.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if "does not exist" in str(exc) or "no revision" in str(exc)
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, str(exc)) from exc
    operation_payload = _document_operation_out(result.operation).model_dump()
    return {
        **operation_payload,
        "operation_id": str(result.operation_id),
        "document_id": str(result.document_id),
        "revision_id": str(result.revision_id),
        "tombstoned_at": result.tombstoned_at.isoformat(),
        "orphaned_claim_count": result.orphaned_claim_count,
        "already_tombstoned": result.already_tombstoned,
    }


@router.get(
    "/document-operations/{operation_id}",
    response_model=DocumentOperationOut,
)
async def get_document_operation(
    operation_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    operation = await session.scalar(
        select(KbDocumentOperation).where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == ctx.org_id,
        )
    )
    if operation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document operation not found")
    operation_type = str(getattr(operation.operation_type, "value", operation.operation_type))
    if operation_type == DocumentOperationType.PURGE.value and ctx.role not in {
        Role.ORG_ADMIN,
        Role.PLATFORM_ADMIN,
    }:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    return _document_operation_out(operation)


@router.post(
    "/document-operations/{operation_id}/retry",
    response_model=DocumentOperationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_document_operation_endpoint(
    operation_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    selected = await session.scalar(
        select(KbDocumentOperation).where(
            KbDocumentOperation.id == operation_id,
            KbDocumentOperation.org_id == ctx.org_id,
        )
    )
    if selected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document operation not found")
    operation_type = str(getattr(selected.operation_type, "value", selected.operation_type))
    if operation_type == DocumentOperationType.PURGE.value:
        if ctx.role not in {Role.ORG_ADMIN, Role.PLATFORM_ADMIN}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        operation = await session.scalar(
            select(KbDocumentOperation)
            .where(
                KbDocumentOperation.id == operation_id,
                KbDocumentOperation.org_id == ctx.org_id,
                KbDocumentOperation.operation_type == DocumentOperationType.PURGE.value,
            )
            .with_for_update()
        )
        if operation is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "document operation not found",
            )
        operation_status = str(getattr(operation.status, "value", operation.status))
        if (
            operation_status
            not in {
                DocumentOperationStatus.FAILED.value,
                DocumentOperationStatus.DEAD_LETTER.value,
            }
            or not operation.retryable
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "PURGE_NOT_RETRYABLE",
                    "message": "document purge operation is not retryable",
                },
            )
        operation.status = DocumentOperationStatus.PENDING
        operation.stage = "retry_scheduled"
        operation.retryable = False
        operation.last_error_code = None
        operation.last_error = None
        operation.failed_at = None
        operation.completed_at = None
        session.add(operation)
        await _ensure_document_purge_dispatch(
            session,
            operation=operation,
            actor_id=ctx.user_id,
        )
        await session.commit()
        return _document_operation_out(operation)
    if operation_type == DocumentOperationType.REINGESTION.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "请通过文档重新摄入入口重试该 operation",
        )
    try:
        operation = await retry_document_operation(
            session,
            org_id=ctx.org_id,
            operation_id=operation_id,
        )
        await session.commit()
    except DocumentDeletionError as exc:
        await session.rollback()
        code = (
            status.HTTP_404_NOT_FOUND if "does not exist" in str(exc) else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, str(exc)) from exc
    return _document_operation_out(operation)


# ---- 实体归一与人工合并 -------------------------------------------------


class EntityAliasCreateBody(BaseModel):
    alias: str = Field(min_length=1, max_length=300)
    locale: str = Field(default="und", min_length=1, max_length=35)


class EntityAliasOut(BaseModel):
    id: UUID
    entity_id: UUID
    alias: str
    normalized_alias: str
    locale: str
    source: str
    created_at: datetime | None


class CanonicalEntityCreateBody(BaseModel):
    kb_id: UUID
    entity_type: str = Field(min_length=1, max_length=50)
    canonical_name: str = Field(min_length=1, max_length=300)
    metadata: dict = Field(default_factory=dict)


class CanonicalEntityPatchBody(BaseModel):
    canonical_name: str | None = Field(default=None, min_length=1, max_length=300)
    metadata: dict | None = None


class CanonicalEntityOut(BaseModel):
    id: UUID
    org_id: UUID
    kb_id: UUID
    entity_type: str
    canonical_name: str
    metadata: dict
    support_status: str
    support_status_reason: str | None
    support_status_changed_at: datetime | None
    support_status_snapshot_id: UUID | None
    unsupported_at: datetime | None
    is_pinned: bool
    pinned_at: datetime | None
    pinned_by: UUID | None
    pin_reason: str | None
    merged_into_entity_id: UUID | None
    merged_at: datetime | None
    merged_by: UUID | None
    merge_reason: str | None
    aliases: list[EntityAliasOut]
    created_at: datetime | None
    updated_at: datetime | None


class EntityMergeBody(BaseModel):
    target_entity_id: UUID


class EntityMergeOut(BaseModel):
    survivor: CanonicalEntityOut
    aliases_moved: int
    claims_redirected: int
    snapshot_rebuild_required: bool = True


async def _canonical_entity_outputs(
    session: AsyncSession,
    entities: list[CanonicalEntity],
) -> list[CanonicalEntityOut]:
    aliases_by_entity: dict[UUID, list[EntityAliasOut]] = {entity.id: [] for entity in entities}
    if entities:
        aliases = list(
            (
                await session.execute(
                    select(EntityAlias)
                    .where(EntityAlias.entity_id.in_(aliases_by_entity))
                    .order_by(
                        EntityAlias.entity_id,
                        EntityAlias.locale,
                        EntityAlias.normalized_alias,
                        EntityAlias.id,
                    )
                )
            ).scalars()
        )
        for alias in aliases:
            aliases_by_entity[alias.entity_id].append(
                EntityAliasOut(
                    id=alias.id,
                    entity_id=alias.entity_id,
                    alias=alias.alias,
                    normalized_alias=alias.normalized_alias,
                    locale=alias.locale,
                    source=alias.source,
                    created_at=alias.created_at,
                )
            )
    return [
        CanonicalEntityOut(
            id=entity.id,
            org_id=entity.org_id,
            kb_id=entity.kb_id,
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            metadata=entity.metadata_,
            support_status=entity.support_status,
            support_status_reason=entity.support_status_reason,
            support_status_changed_at=entity.support_status_changed_at,
            support_status_snapshot_id=entity.support_status_snapshot_id,
            unsupported_at=entity.unsupported_at,
            is_pinned=entity.is_pinned,
            pinned_at=entity.pinned_at,
            pinned_by=entity.pinned_by,
            pin_reason=entity.pin_reason,
            merged_into_entity_id=entity.merged_into_entity_id,
            merged_at=entity.merged_at,
            merged_by=entity.merged_by,
            merge_reason=entity.merge_reason,
            aliases=aliases_by_entity[entity.id],
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
        for entity in entities
    ]


def _raise_entity_error(exc: Exception) -> None:
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, EntityNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    raise HTTPException(code, str(exc)) from exc


@router.get("/canonical-entities", response_model=list[CanonicalEntityOut])
async def list_canonical_entities(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    kb_id: UUID,
    q: str | None = None,
    entity_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    if not 1 <= limit <= 500 or offset < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit/offset 超出允许范围")
    await _require_visible_active_kb(session, kb_id)
    stmt = (
        select(CanonicalEntity)
        .where(CanonicalEntity.kb_id == kb_id)
        .order_by(
            CanonicalEntity.entity_type,
            CanonicalEntity.canonical_name,
            CanonicalEntity.id,
        )
        .limit(limit)
        .offset(offset)
    )
    if entity_type:
        stmt = stmt.where(CanonicalEntity.entity_type == entity_type)
    if q:
        try:
            normalized = normalize_alias(q)
        except EntityConflictError as exc:
            _raise_entity_error(exc)
        stmt = stmt.where(
            CanonicalEntity.id.in_(
                select(EntityAlias.entity_id).where(
                    EntityAlias.kb_id == kb_id,
                    EntityAlias.normalized_alias.contains(
                        normalized,
                        autoescape=True,
                    ),
                )
            )
        )
    entities = list((await session.execute(stmt)).scalars())
    return await _canonical_entity_outputs(session, entities)


@router.post(
    "/canonical-entities",
    response_model=CanonicalEntityOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_canonical_entity_endpoint(
    body: CanonicalEntityCreateBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    await _require_own_kb(session, ctx, body.kb_id)
    try:
        entity = await create_canonical_entity(
            session,
            org_id=ctx.org_id,
            kb_id=body.kb_id,
            entity_type=body.entity_type,
            canonical_name=body.canonical_name,
            metadata=body.metadata,
        )
        await session.commit()
    except (EntityConflictError, EntityNotFoundError) as exc:
        await session.rollback()
        _raise_entity_error(exc)
    except IntegrityError as exc:
        await _handle_entity_integrity_error(session, exc)
    await session.refresh(entity)
    return (await _canonical_entity_outputs(session, [entity]))[0]


@router.patch("/canonical-entities/{entity_id}", response_model=CanonicalEntityOut)
async def patch_canonical_entity(
    entity_id: UUID,
    body: CanonicalEntityPatchBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    entity = await session.scalar(
        select(CanonicalEntity).where(CanonicalEntity.id == entity_id).with_for_update()
    )
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体不存在")
    await _require_own_kb(session, ctx, entity.kb_id)
    try:
        if body.canonical_name is not None:
            await rename_canonical_entity(
                session,
                entity=entity,
                canonical_name=body.canonical_name,
            )
        if "metadata" in body.model_fields_set:
            entity.metadata_ = body.metadata or {}
            session.add(entity)
        await session.commit()
    except EntityConflictError as exc:
        await session.rollback()
        _raise_entity_error(exc)
    except IntegrityError as exc:
        await _handle_entity_integrity_error(session, exc)
    await session.refresh(entity)
    return (await _canonical_entity_outputs(session, [entity]))[0]


@router.post(
    "/canonical-entities/{entity_id}/aliases",
    response_model=EntityAliasOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_entity_alias_endpoint(
    entity_id: UUID,
    body: EntityAliasCreateBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    entity = await session.scalar(
        select(CanonicalEntity).where(CanonicalEntity.id == entity_id).with_for_update()
    )
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体不存在")
    await _require_own_kb(session, ctx, entity.kb_id)
    try:
        alias = await add_entity_alias(
            session,
            entity=entity,
            alias=body.alias,
            locale=body.locale,
        )
        await session.commit()
    except EntityConflictError as exc:
        await session.rollback()
        _raise_entity_error(exc)
    except IntegrityError as exc:
        await _handle_entity_integrity_error(session, exc)
    await session.refresh(alias)
    return EntityAliasOut(
        id=alias.id,
        entity_id=alias.entity_id,
        alias=alias.alias,
        normalized_alias=alias.normalized_alias,
        locale=alias.locale,
        source=alias.source,
        created_at=alias.created_at,
    )


@router.delete("/entity-aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity_alias_endpoint(
    alias_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    alias = await session.get(EntityAlias, alias_id)
    if alias is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体别名不存在")
    await _require_own_kb(session, ctx, alias.kb_id)
    try:
        await delete_entity_alias(session, alias_id=alias_id)
        await session.commit()
    except (EntityConflictError, EntityNotFoundError) as exc:
        await session.rollback()
        _raise_entity_error(exc)
    except IntegrityError as exc:
        await _handle_entity_integrity_error(session, exc)


@router.post(
    "/canonical-entities/{source_id}/merge",
    response_model=EntityMergeOut,
)
async def merge_canonical_entity_endpoint(
    source_id: UUID,
    body: EntityMergeBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    source = await session.get(CanonicalEntity, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "源实体不存在")
    await _require_own_kb(session, ctx, source.kb_id)
    try:
        result: MergeResult = await merge_entities(
            session,
            source_id=source_id,
            target_id=body.target_entity_id,
            actor_id=ctx.user_id,
        )
        await session.commit()
    except (EntityConflictError, EntityNotFoundError) as exc:
        await session.rollback()
        _raise_entity_error(exc)
    await session.refresh(result.survivor)
    survivor = (await _canonical_entity_outputs(session, [result.survivor]))[0]
    return EntityMergeOut(
        survivor=survivor,
        aliases_moved=result.aliases_moved,
        claims_redirected=result.claims_redirected,
    )


class MergeSuggestionOut(BaseModel):
    source_entity: CanonicalEntityOut
    target_entity: CanonicalEntityOut
    confidence: float
    reasons: list[str]


@router.get(
    "/bases/{kb_id}/canonical-entities/merge-suggestions",
    response_model=list[MergeSuggestionOut],
)
async def list_merge_suggestions(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
    entity_type: str | None = None,
    limit: int = 50,
):
    """相似实体合并候选(只读建议,human-in-the-loop):绝不自动合并,
    确认后走 POST /kb/canonical-entities/{source_id}/merge。读权限即可(RLS 定界)。"""
    if not 1 <= limit <= 200:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit 超出允许范围(1~200)")
    await _require_visible_active_kb(session, kb_id)
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:  # pragma: no cover - active lease and identity are one row
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在或不可用")
    suggestions = await suggest_merge_candidates(
        session,
        org_id=kb.org_id,
        kb_id=kb_id,
        entity_type=entity_type,
        limit=limit,
    )
    unique_entities: dict[UUID, CanonicalEntity] = {}
    for suggestion in suggestions:
        unique_entities[suggestion.source.id] = suggestion.source
        unique_entities[suggestion.target.id] = suggestion.target
    outputs = await _canonical_entity_outputs(session, list(unique_entities.values()))
    outputs_by_id = {output.id: output for output in outputs}
    return [
        MergeSuggestionOut(
            source_entity=outputs_by_id[suggestion.source.id],
            target_entity=outputs_by_id[suggestion.target.id],
            confidence=suggestion.confidence,
            reasons=list(suggestion.reasons),
        )
        for suggestion in suggestions
    ]


# ---- 清洗审核队列(FR-113)------------------------------------------------


class FactEvidenceOut(BaseModel):
    id: UUID
    revision_id: UUID
    doc_id: UUID
    filename: str
    chunk_id: UUID | None
    page: int | None
    start_line: int | None
    end_line: int | None
    cell_ref: str | None
    quote_text: str


class FactClaimOut(BaseModel):
    id: UUID
    kb_id: UUID
    subject_entity_id: UUID | None
    object_entity_id: UUID | None
    subject_type: str
    subject_id: UUID
    predicate: str
    value_json: dict
    raw_payload: dict
    corrected_payload: dict | None
    effective_payload: dict
    valid_from: date | None
    valid_to: date | None
    confidence: float | None
    review_status: FactReviewStatus
    reviewed_by: str | None = None
    review_note: str | None = None
    model_name: str | None
    prompt_version: str | None
    created_at: datetime | None
    updated_at: datetime | None
    evidence: list[FactEvidenceOut]


async def _fact_claim_outputs(session: AsyncSession, claims: list[FactClaim]) -> list[FactClaimOut]:
    evidence_by_claim: dict[UUID, list[FactEvidenceOut]] = {claim.id: [] for claim in claims}
    if claims:
        rows = (
            await session.execute(
                select(EvidenceSpan, DocumentRevision, SourceDocument)
                .join(DocumentRevision, DocumentRevision.id == EvidenceSpan.revision_id)
                .join(SourceDocument, SourceDocument.id == DocumentRevision.doc_id)
                .where(
                    EvidenceSpan.fact_claim_id.in_(evidence_by_claim),
                    EvidenceSpan.org_id == DocumentRevision.org_id,
                    EvidenceSpan.kb_id == DocumentRevision.kb_id,
                    DocumentRevision.org_id == SourceDocument.org_id,
                    DocumentRevision.kb_id == SourceDocument.kb_id,
                )
                .order_by(
                    EvidenceSpan.fact_claim_id,
                    EvidenceSpan.page.asc().nulls_last(),
                    EvidenceSpan.start_line.asc().nulls_last(),
                    EvidenceSpan.id,
                )
            )
        ).all()
        for span, revision, document in rows:
            evidence_by_claim[span.fact_claim_id].append(
                FactEvidenceOut(
                    id=span.id,
                    revision_id=revision.id,
                    doc_id=document.id,
                    filename=document.filename,
                    chunk_id=span.chunk_id,
                    page=span.page,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    cell_ref=span.cell_ref,
                    quote_text=span.quote_text,
                )
            )
    return [
        FactClaimOut(
            id=claim.id,
            kb_id=claim.kb_id,
            subject_entity_id=claim.subject_entity_id,
            object_entity_id=claim.object_entity_id,
            subject_type=claim.subject_type,
            subject_id=claim.subject_id,
            predicate=claim.predicate,
            value_json=claim.value_json,
            raw_payload=claim.raw_payload,
            corrected_payload=claim.corrected_payload,
            effective_payload=(
                claim.corrected_payload if claim.corrected_payload is not None else claim.value_json
            ),
            valid_from=claim.valid_from,
            valid_to=claim.valid_to,
            confidence=claim.confidence,
            review_status=claim.review_status,
            reviewed_by=claim.reviewed_by,
            review_note=claim.review_note,
            model_name=claim.model_name,
            prompt_version=claim.prompt_version,
            created_at=claim.created_at,
            updated_at=claim.updated_at,
            evidence=evidence_by_claim[claim.id],
        )
        for claim in claims
    ]


@router.post(
    "/canonical-entities/{entity_id}/claims/{claim_id}",
    response_model=FactClaimOut,
)
async def bind_fact_claim_entity(
    entity_id: UUID,
    claim_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    entity = await session.get(CanonicalEntity, entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "实体不存在")
    await _require_own_kb(session, ctx, entity.kb_id)
    try:
        claim = await bind_claim_to_entity(
            session,
            claim_id=claim_id,
            entity_id=entity_id,
        )
        await session.commit()
    except (EntityConflictError, EntityNotFoundError) as exc:
        await session.rollback()
        _raise_entity_error(exc)
    await session.refresh(claim)
    return (await _fact_claim_outputs(session, [claim]))[0]


@router.get("/fact-claims", response_model=list[FactClaimOut])
async def list_fact_claims(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    kb_id: UUID | None = None,
    review_status: FactReviewStatus | None = None,
    limit: int = 100,
    offset: int = 0,
):
    if not 1 <= limit <= 500 or offset < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit/offset 超出允许范围")
    stmt = (
        select(FactClaim)
        .where(
            FactClaim.kb_id.in_(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.lifecycle_status == "active"
                )
            )
        )
        .order_by(FactClaim.created_at, FactClaim.id)
        .limit(limit)
        .offset(offset)
    )
    if kb_id is not None:
        await _require_visible_active_kb(session, kb_id)
        stmt = stmt.where(FactClaim.kb_id == kb_id)
    if review_status is not None:
        stmt = stmt.where(FactClaim.review_status == review_status.value)
    claims = list((await session.execute(stmt)).scalars().all())
    return await _fact_claim_outputs(session, claims)


class FactClaimReviewBody(BaseModel):
    action: Literal["confirm", "reject"]
    corrected_payload: dict | None = None
    subject_entity_id: UUID | None = None
    object_entity_id: UUID | None = None


async def _review_fact_claim(
    session: AsyncSession,
    ctx: OrgContext,
    claim_id: UUID,
    body: FactClaimReviewBody,
) -> FactClaim:
    claim = (
        await session.execute(select(FactClaim).where(FactClaim.id == claim_id).with_for_update())
    ).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "事实不存在")
    await _require_own_kb(session, ctx, claim.kb_id)
    current_status = str(getattr(claim.review_status, "value", claim.review_status))
    if current_status not in {
        FactReviewStatus.SUGGESTED.value,
        FactReviewStatus.ORPHANED.value,
    }:
        raise HTTPException(status.HTTP_409_CONFLICT, "仅 suggested/orphaned 事实可审核")
    if body.corrected_payload is not None:
        claim.corrected_payload = body.corrected_payload
    claim.reviewed_by = f"human:{ctx.user_id}"
    if body.action == "confirm":
        valid_evidence = await session.scalar(
            select(EvidenceSpan.id)
            .join(DocumentRevision, DocumentRevision.id == EvidenceSpan.revision_id)
            .where(
                EvidenceSpan.fact_claim_id == claim.id,
                EvidenceSpan.org_id == claim.org_id,
                EvidenceSpan.kb_id == claim.kb_id,
                DocumentRevision.org_id == claim.org_id,
                DocumentRevision.kb_id == claim.kb_id,
                DocumentRevision.status.in_(
                    (RevisionStatus.STAGED.value, RevisionStatus.ACTIVE.value)
                ),
            )
            .limit(1)
        )
        if valid_evidence is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "确认事实必须包含有效证据")

        if claim.predicate in RELATION_PREDICATES:
            selected_entities: dict[str, CanonicalEntity] = {}
            for endpoint, entity_id in (
                ("subject", body.subject_entity_id),
                ("object", body.object_entity_id),
            ):
                if entity_id is None:
                    continue
                entity = await session.scalar(
                    select(CanonicalEntity)
                    .where(
                        CanonicalEntity.id == entity_id,
                        CanonicalEntity.org_id == claim.org_id,
                        CanonicalEntity.kb_id == claim.kb_id,
                    )
                    .with_for_update()
                )
                if entity is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "关系端点实体不存在")
                selected_entities[endpoint] = entity
            if "subject" in selected_entities:
                claim.subject_entity_id = selected_entities["subject"].id
            if "object" in selected_entities:
                claim.object_entity_id = selected_entities["object"].id
                effective_payload = dict(
                    claim.corrected_payload
                    if claim.corrected_payload is not None
                    else claim.value_json
                )
                claim.corrected_payload = {
                    **effective_payload,
                    "target_entity_id": str(selected_entities["object"].id),
                }
            failure = await bind_claim_entities(session, claim, org_id=ctx.org_id)
            if failure is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, f"实体归一失败:{failure}")
            if claim.subject_entity_id is None or claim.object_entity_id is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "确认关系事实必须绑定主体和目标实体",
                )
        elif body.object_entity_id is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "非关系事实不能绑定目标实体",
            )
        elif body.subject_entity_id is not None:
            binding_entity = await session.scalar(
                select(CanonicalEntity)
                .where(
                    CanonicalEntity.id == body.subject_entity_id,
                    CanonicalEntity.org_id == claim.org_id,
                    CanonicalEntity.kb_id == claim.kb_id,
                )
                .with_for_update()
            )
            if binding_entity is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "归一实体不存在")
            try:
                validate_entity_binding(claim=claim, entity=binding_entity)
            except EntityConflictError as exc:
                _raise_entity_error(exc)
            claim.subject_entity_id = binding_entity.id
        elif is_bindable(claim):
            # 实体/关系事实自动归一绑定:未绑定的关系事实会让 graph 投影失败
            failure = await bind_claim_entities(session, claim, org_id=ctx.org_id)
            if failure is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, f"实体归一失败:{failure}")
        claim.review_status = FactReviewStatus.CONFIRMED
    else:
        claim.review_status = FactReviewStatus.REJECTED
    session.add(claim)
    await session.flush()
    return claim


@router.post("/fact-claims/{claim_id}/review", response_model=FactClaimOut)
async def review_fact_claim(
    claim_id: UUID,
    body: FactClaimReviewBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    claim = await _review_fact_claim(session, ctx, claim_id, body)
    # 审完最后一条待审项时把文档从 awaiting_review 收回 completed
    await settle_documents_for_claims(session, org_id=ctx.org_id, claim_ids=[claim.id])
    await session.commit()
    await session.refresh(claim)
    return (await _fact_claim_outputs(session, [claim]))[0]


class FactClaimBatchBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)
    action: Literal["confirm", "reject"]


class FactClaimAiReviewBody(BaseModel):
    kb_id: UUID | None = None
    limit: int = Field(default=200, ge=1, le=500)


@router.post("/fact-claims/ai-review")
async def ai_review_fact_claims_endpoint(
    body: FactClaimAiReviewBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """AI 自动审核 suggested 事实:confirm/reject 直接落库,拿不准的留队列并附原因。"""
    if body.kb_id is not None:
        await _require_own_kb(session, ctx, body.kb_id)
    summary = await ai_review_fact_claims(
        session, org_id=ctx.org_id, kb_id=body.kb_id, limit=body.limit
    )
    await settle_org_awaiting_documents(session, org_id=ctx.org_id)
    await session.commit()
    return summary.as_dict()


@router.post("/fact-claims/batch")
async def batch_review_fact_claims(
    body: FactClaimBatchBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    done: list[str] = []
    skipped: list[dict[str, str]] = []
    reviewed: list[UUID] = []
    review = FactClaimReviewBody(action=body.action)
    for claim_id in sorted(set(body.ids), key=str):
        try:
            await _review_fact_claim(session, ctx, claim_id, review)
            done.append(str(claim_id))
            reviewed.append(claim_id)
        except HTTPException as exc:
            skipped.append({"id": str(claim_id), "reason": str(exc.detail)})
    await settle_documents_for_claims(session, org_id=ctx.org_id, claim_ids=reviewed)
    await session.commit()
    return {"done": done, "skipped": skipped}


# ---- 统一检索(FR-114)----------------------------------------------------


class StructuredFilterIn(BaseModel):
    """一条领域无关的结构化过滤条件(MIGRATION-PLAN B9-B14)。

    TF 的 hotel_star / route_days 等行业参数已整体删除:``field`` 必须在目标
    ``KbEntityType.filterable_fields`` 中声明过,否则一律 422,防任意 JSONB 探测。
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=100)
    op: Literal["eq", "min", "max"] = "eq"
    value: Any = None


class StructuredQueryIn(BaseModel):
    """结构化召回通道的调用契约(对应 kb.search.StructuredSearchQuery)。"""

    model_config = ConfigDict(extra="forbid")

    type_keys: list[str] = Field(default_factory=list, max_length=50)
    filters: list[StructuredFilterIn] = Field(default_factory=list, max_length=20)
    match_terms: list[str] = Field(default_factory=list, max_length=20)
    must_include: list[str] = Field(default_factory=list, max_length=20)

    def to_query(self) -> StructuredSearchQuery | None:
        if not (self.type_keys or self.filters or self.match_terms or self.must_include):
            return None
        try:
            return StructuredSearchQuery(
                type_keys=tuple(self.type_keys),
                filters=tuple(
                    StructuredFilter(field=item.field, op=item.op, value=item.value)
                    for item in self.filters
                ),
                match_terms=tuple(self.match_terms),
                must_include=tuple(self.must_include),
            )
        except StructuredFilterError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_structured_filter", "message": str(exc)},
            ) from exc


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=10, ge=1, le=100)
    kb_ids: list[UUID] | None = None  # 不传 = 全部可见库
    structured: StructuredQueryIn | None = None


class SearchHitOut(BaseModel):
    kind: str
    layer: str
    kb_id: str
    source: str
    confidence: float
    data: dict
    citation: dict | None = None
    media_refs: list[KnowledgeMediaReference] = Field(default_factory=list)


class KnowledgeAnswerBody(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    kb_ids: list[UUID] | None = None
    structured: StructuredQueryIn | None = None


class KnowledgeAnswerSourceOut(BaseModel):
    ref: int
    hit: SearchHitOut


class KnowledgeAnswerOut(BaseModel):
    answer: str | None
    sources: list[KnowledgeAnswerSourceOut] = Field(default_factory=list)
    reason: Literal["no_evidence"] | None = None


def _leases_for_search_hits(
    leases: dict[UUID, ActiveKnowledgeBaseLease],
    hits: list[Any],
) -> dict[UUID, ActiveKnowledgeBaseLease]:
    hit_kb_ids = {UUID(str(hit.kb_id)) for hit in hits}
    return {kb_id: lease for kb_id, lease in leases.items() if kb_id in hit_kb_ids}


async def _run_search_kb(
    session: AsyncSession,
    org_id: UUID,
    body: SearchBody | KnowledgeAnswerBody,
    *,
    top_k: int,
):
    """search / answer 共用的检索入口:声明式结构化过滤在此编译并归一错误。"""
    structured = body.structured.to_query() if body.structured is not None else None
    try:
        return await search_kb(
            session,
            org_id,
            body.query,
            top_k=top_k,
            kb_ids=body.kb_ids,
            embedder=default_embedder(),
            structured_filters=structured,
        )
    except StructuredFilterError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_structured_filter", "message": str(exc)},
        ) from exc


async def _search_hit_leases_are_current(
    session: AsyncSession,
    leases: dict[UUID, ActiveKnowledgeBaseLease],
    *,
    lock: bool,
) -> bool:
    for kb_id in sorted(leases, key=str):
        if not await active_knowledge_base_lease_is_current(
            session,
            leases[kb_id],
            lock=lock,
        ):
            return False
    return True


@router.post("/search", response_model=list[SearchHitOut])
async def search(
    body: SearchBody,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    scope_leases = await capture_active_knowledge_base_leases(
        session,
        kb_ids=body.kb_ids,
    )
    await session.commit()
    hits = await _run_search_kb(session, ctx.org_id, body, top_k=body.top_k)
    leases = _leases_for_search_hits(scope_leases, hits)
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in leases]
    await session.commit()
    current_kb_ids = {
        kb_id
        for kb_id, lease in leases.items()
        if await active_knowledge_base_lease_is_current(
            session,
            lease,
            lock=True,
        )
    }
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in current_kb_ids]
    return [SearchHitOut(**hit.__dict__) for hit in hits]


@router.post("/answer", response_model=KnowledgeAnswerOut)
async def answer_knowledge_question(
    body: KnowledgeAnswerBody,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """Answer from cited, permission-scoped KB evidence without exposing write tools."""

    scope_leases = await capture_active_knowledge_base_leases(
        session,
        kb_ids=body.kb_ids,
    )
    await session.commit()
    hits = await _run_search_kb(session, ctx.org_id, body, top_k=12)
    leases = _leases_for_search_hits(scope_leases, hits)
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in leases]
    # Retrieval opened a read transaction. Do not keep it idle while the model runs.
    await session.commit()
    try:
        result = await generate_grounded_answer(
            query=body.query,
            hits=hits,
            org_id=ctx.org_id,
        )
    except LlmBudgetExceededError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "组织今日 AI 额度已用完，请稍后再试或切换到查原文",
        ) from exc
    except (AllProvidersFailedError, NoRouteError, ProviderError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI 知识解答暂时不可用，请稍后再试或切换到查原文",
        ) from exc
    except KnowledgeAnswerGenerationError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "AI 知识解答未能生成可核验的答案，请重试或切换到查原文",
        ) from exc
    if not await _search_hit_leases_are_current(session, leases, lock=True):
        return KnowledgeAnswerOut(answer=None, sources=[], reason="no_evidence")
    return KnowledgeAnswerOut(
        answer=result.answer,
        reason=result.reason,
        sources=[
            KnowledgeAnswerSourceOut(
                ref=ref,
                hit=SearchHitOut(**hit.__dict__),
            )
            for ref, hit in result.sources
        ],
    )


@router.post("/answer/stream")
async def answer_knowledge_question_stream(
    body: KnowledgeAnswerBody,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """/kb/answer 的 SSE 版:检索仍在流外做完(可正常抛 HTTP 异常),
    生成阶段的异常由服务层归一为 error 帧——响应头一旦发出就没有第二种表达。"""

    scope_leases = await capture_active_knowledge_base_leases(
        session,
        kb_ids=body.kb_ids,
    )
    await session.commit()
    hits = await _run_search_kb(session, ctx.org_id, body, top_k=12)
    leases = _leases_for_search_hits(scope_leases, hits)
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in leases]
    # Retrieval opened a read transaction. Do not keep it idle while the model runs.
    await session.commit()

    async def sse():
        try:
            async for event in stream_grounded_answer(
                query=body.query, hits=hits, org_id=ctx.org_id
            ):
                if not await _search_hit_leases_are_current(
                    session,
                    leases,
                    lock=True,
                ):
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "error",
                                "code": "knowledge_boundary_changed",
                                "message": "知识库状态已变化，请重新提问",
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    await session.commit()
                    return
                if event["type"] == "sources":
                    # 服务层只产语义帧,SearchHit → 出参模型的序列化归 API 层
                    event = {
                        "type": "sources",
                        "sources": [
                            {
                                "ref": ref,
                                "hit": SearchHitOut(**hit.__dict__).model_dump(mode="json"),
                            }
                            for ref, hit in event["sources"]
                        ],
                    }
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                await session.commit()
        except Exception:
            # 兜底:服务层未归一的意外异常也不能中断裸流,统一按不可用收尾
            fallback = {
                "type": "error",
                "code": "unavailable",
                "message": "AI 知识解答暂时不可用，请稍后再试或切换到查原文",
            }
            yield f"data: {json.dumps(fallback, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---- 活动快照的类型化知识图谱 ---------------------------------------------


@router.get("/bases/{kb_id}/graph")
async def get_graph(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """Return canonical nodes and evidenced edges from the active snapshot."""
    from nicekit.kb.graph import load_graph

    await _require_visible_active_kb(session, kb_id)
    g = await load_graph(session, [kb_id])
    return {
        "nodes": [n.__dict__ for n in g.nodes.values()],
        "edges": [e.__dict__ for e in g.edges],
    }


@router.get("/bases/{kb_id}/insights")
async def get_insights(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """知识库健康度:社区/孤立节点/稀疏社区(维护面板)。"""
    from nicekit.kb.graph import graph_insights, load_graph

    await _require_visible_active_kb(session, kb_id)
    return graph_insights(await load_graph(session, [kb_id]))


@router.get("/bases/{kb_id}/lint")
async def get_lint(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """wiki 页结构体检(KB-5B):broken_link/orphan/no_outlinks + 建议(纯本地零 LLM)。"""
    from nicekit.kb.lint import run_structural_lint

    await _require_visible_active_kb(session, kb_id)
    issues, stats = await run_structural_lint(session, kb_id)
    return {"issues": [i.as_dict() for i in issues], "stats": stats}


@router.get("/bases/{kb_id}/related/{entity_type}/{entity_id}")
async def get_related(
    kb_id: UUID,
    entity_type: str,
    entity_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
    top_k: int = 10,
):
    """Return related nodes scored from the same active typed graph."""
    from nicekit.kb.graph import load_graph, node_id, related_nodes

    await _require_visible_active_kb(session, kb_id)
    g = await load_graph(session, [kb_id])
    return related_nodes(g, node_id(entity_type, entity_id), top_k=top_k)


class DocIngestDurationOut(BaseModel):
    doc_id: UUID
    # 最新 revision 的端到端墙钟;没有任何已开工 run 时为 None(排队中 ≠ 0 秒)
    elapsed_seconds: float | None
    running: bool


class DocIngestDurationsOut(BaseModel):
    items: list[DocIngestDurationOut]


# 批量耗时一次最多算多少份文档:与前端列表软上限(KB_DOCUMENTS_SOFT_LIMIT)对齐
_INGEST_DURATION_MAX_DOCS = 500


# 注意:必须注册在 GET /documents/{doc_id} 之前,否则 "ingest-durations"
# 会被那条动态路由按 UUID 解析吞掉(FastAPI 按注册顺序匹配,不回溯)。
@router.get("/documents/ingest-durations", response_model=DocIngestDurationsOut)
async def get_document_ingest_durations(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    kb_id: UUID,
    doc_ids: Annotated[list[UUID] | None, Query()] = None,
) -> DocIngestDurationsOut:
    """整页文档的摄入耗时,一次请求、一条聚合 SQL。

    文档列表禁止按行发 N 个 ingestion-status 请求(会打满浏览器并发连接),
    终态行的"耗时"由这里批量供给。口径与单文档端点的 timing 完全一致:
    每文档取最新 revision,耗时 = max(finished_at or now) - min(started_at)
    的墙钟跨度,绝不累加各 run(图片富化/分段抽取并发跑,累加会虚高数倍)。

    doc_ids 不传时按 kb 全量;两种入参都以最近上传的
    _INGEST_DURATION_MAX_DOCS 份为上限截断。
    """
    await _require_visible_active_kb(session, kb_id)

    doc_scope = select(SourceDocument.id, SourceDocument.created_at).where(
        SourceDocument.kb_id == kb_id
    )
    if doc_ids:
        # 先在入参侧截断,免得超长 IN 列表原样进 SQL
        doc_scope = doc_scope.where(SourceDocument.id.in_(doc_ids[:_INGEST_DURATION_MAX_DOCS]))
    doc_scope = (
        doc_scope.order_by(SourceDocument.created_at.desc(), SourceDocument.id)
        .limit(_INGEST_DURATION_MAX_DOCS)
        .subquery()
    )

    # 每文档的最新 revision:窗口函数取 revision_no 最大的一条,
    # 排序键与 /documents/{doc_id}/ingestion-status 完全一致,两处口径不能漂移
    ranked_revisions = (
        select(
            DocumentRevision.id.label("revision_id"),
            DocumentRevision.doc_id.label("doc_id"),
            func.row_number()
            .over(
                partition_by=DocumentRevision.doc_id,
                order_by=(
                    DocumentRevision.revision_no.desc(),
                    DocumentRevision.created_at.desc(),
                    DocumentRevision.id.desc(),
                ),
            )
            .label("recency"),
        )
        .where(DocumentRevision.kb_id == kb_id)
        .subquery()
    )
    latest_revision = (
        select(ranked_revisions.c.revision_id, ranked_revisions.c.doc_id)
        .where(ranked_revisions.c.recency == 1)
        .subquery()
    )

    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(
                doc_scope.c.id,
                # min 聚合天然忽略 NULL:未开工的 run 不参与起点
                func.min(IngestRun.started_at),
                # 终点只看已开工的 run(排队中 ≠ 秒完成);在飞的以"此刻"为终点
                func.max(func.coalesce(IngestRun.finished_at, now)).filter(
                    IngestRun.started_at.is_not(None)
                ),
                func.coalesce(
                    func.bool_or(IngestRun.status.in_(_ACTIVE_RUN_STATUSES)),
                    False,
                ),
            )
            .select_from(doc_scope)
            .outerjoin(latest_revision, latest_revision.c.doc_id == doc_scope.c.id)
            .outerjoin(IngestRun, IngestRun.revision_id == latest_revision.c.revision_id)
            .group_by(doc_scope.c.id, doc_scope.c.created_at)
            .order_by(doc_scope.c.created_at.desc(), doc_scope.c.id)
        )
    ).all()

    return DocIngestDurationsOut(
        items=[
            DocIngestDurationOut(
                doc_id=row_doc_id,
                elapsed_seconds=(
                    None if started_at is None else max((end_at - started_at).total_seconds(), 0.0)
                ),
                running=bool(running),
            )
            for row_doc_id, started_at, end_at, running in rows
        ]
    )


# 供文档状态轮询使用
@router.get("/documents/{doc_id}", response_model=SourceDocumentOut)
async def get_document(
    doc_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_visible_active_kb(session, doc.kb_id)
    return (await _documents_with_latest_operation(session, [doc]))[0]


class ImageStageStatusOut(BaseModel):
    state: Literal[
        "pending",
        "processing",
        "needs_review",
        "failed",
        "completed",
    ]
    total: int
    pending: int
    processing: int
    needs_review: int
    failed: int
    completed: int
    publishable: bool


class IngestRunStageStatusOut(BaseModel):
    id: UUID
    stage: str
    status: str
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime | None = None
    finished_at: datetime | None
    duration_seconds: float | None = None
    failure_code: str | None = None


class StageTimingOut(BaseModel):
    stage: str
    run_count: int
    duration_seconds: float
    running: bool


class DocumentIngestionTimingOut(BaseModel):
    started_at: datetime | None
    finished_at: datetime | None
    elapsed_seconds: float | None
    running: bool
    stages: list[StageTimingOut]


class DocumentIngestionStatusOut(BaseModel):
    source_document: SourceDocument
    classification_state: Literal["unclassified", "classified"]
    revision: DocumentRevision | None
    ingest_runs: list[IngestRunStageStatusOut]
    latest_reclassification: DocumentReclassificationOut | None
    image_stage: ImageStageStatusOut
    timing: DocumentIngestionTimingOut
    publishable: bool


# 图片富化每张图一条 run(stage=image_enrich:<hash>:<hash>),按张统计对用户
# 毫无意义,统计口径上一律折叠成单个 image 阶段
_IMAGE_STAGE_PREFIX = "image_enrich:"
_IMAGE_STAGE_NAME = "image"
_DOCUMENT_STAGE = "document"
# 这两个状态说明这条 run 还没落终态,耗时要按"到此刻为止"继续走
_ACTIVE_RUN_STATUSES = frozenset({IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value})


def _normalize_stage(stage: str) -> str:
    return _IMAGE_STAGE_NAME if stage.startswith(_IMAGE_STAGE_PREFIX) else stage


def _stage_case():
    """SQL 侧的同款归并,放在库里算才能避免把上千条 run 拉回 Python。"""
    return case(
        (IngestRun.stage.like(f"{_IMAGE_STAGE_PREFIX}%"), _IMAGE_STAGE_NAME),
        else_=IngestRun.stage,
    )


def _run_status_value(run: IngestRun) -> str:
    return str(getattr(run.status, "value", run.status))


def _as_utc(value: datetime | None) -> datetime | None:
    """列是 timestamptz,但 ORM 里可能留着调用方塞进来的 naive 值,
    统一补 UTC,否则与 now() 相减会直接 TypeError。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _run_duration_seconds(run: IngestRun, now: datetime) -> float | None:
    """未开工(started_at 为 NULL)的 run 没有耗时可言,返回 None 而非 0,
    免得前端把"排队中"画成"秒完成"。"""
    started_at = _as_utc(run.started_at)
    if started_at is None:
        return None
    end = _as_utc(run.finished_at) or now
    return max((end - started_at).total_seconds(), 0.0)


def _wall_clock_span(
    runs: list[IngestRun], now: datetime
) -> tuple[datetime | None, datetime | None, float, bool]:
    """一组 run 的墙钟跨度:max(finished_at or now) - min(started_at)。

    刻意不累加各 run 耗时:图片富化与分段抽取是并发跑的,累加会按并发度
    成倍虚高(4 路并发下约 4 倍),给出的"还要多久"会完全失真。
    """
    running = any(_run_status_value(run) in _ACTIVE_RUN_STATUSES for run in runs)
    starts = [s for run in runs if (s := _as_utc(run.started_at)) is not None]
    if not starts:
        return None, None, 0.0, running
    started_at = min(starts)
    ends = [_as_utc(run.finished_at) or now for run in runs if run.started_at is not None]
    finished_at = max(ends)
    # 还有 run 在飞时终点是"此刻",落到出参上就不该报一个 finished_at
    return (
        started_at,
        None if running else finished_at,
        max((finished_at - started_at).total_seconds(), 0.0),
        running,
    )


def _build_ingestion_timing(runs: list[IngestRun], now: datetime) -> DocumentIngestionTimingOut:
    by_stage: dict[str, list[IngestRun]] = {}
    for run in runs:
        by_stage.setdefault(_normalize_stage(run.stage), []).append(run)

    stages: list[tuple[datetime | None, StageTimingOut]] = []
    for stage, stage_runs in by_stage.items():
        started_at, _finished_at, duration, running = _wall_clock_span(stage_runs, now)
        stages.append(
            (
                started_at,
                StageTimingOut(
                    stage=stage,
                    run_count=len(stage_runs),
                    duration_seconds=duration,
                    running=running,
                ),
            )
        )
    # 还没开工的阶段没有排序键,一律垫到末尾(它们本来就排在后面)
    stages.sort(key=lambda item: (item[0] is None, item[0] or now))

    started_at, finished_at, elapsed, running = _wall_clock_span(runs, now)
    return DocumentIngestionTimingOut(
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=None if started_at is None else elapsed,
        running=running,
        stages=[item[1] for item in stages],
    )


@router.get(
    "/documents/{doc_id}/ingestion-status",
    response_model=DocumentIngestionStatusOut,
)
async def get_document_ingestion_status(
    doc_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> DocumentIngestionStatusOut:
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_visible_active_kb(session, doc.kb_id)
    revision = await session.scalar(
        select(DocumentRevision)
        .where(
            DocumentRevision.doc_id == doc.id,
            DocumentRevision.org_id == doc.org_id,
            DocumentRevision.kb_id == doc.kb_id,
        )
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    if revision is None:
        image_stage = await revision_image_stage(session, UUID(int=0))
        runs: list[IngestRun] = []
    else:
        image_stage = await revision_image_stage(session, revision.id)
        runs = list(
            (
                await session.scalars(
                    select(IngestRun)
                    .where(IngestRun.revision_id == revision.id)
                    .order_by(IngestRun.created_at, IngestRun.id)
                )
            ).all()
        )
    now = datetime.now(UTC)
    run_statuses = [
        IngestRunStageStatusOut(
            id=run.id,
            stage=run.stage,
            status=_run_status_value(run),
            heartbeat_at=run.heartbeat_at,
            lease_expires_at=run.lease_expires_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=_run_duration_seconds(run, now),
            failure_code=(
                run.error if run.stage.startswith(_IMAGE_STAGE_PREFIX) and run.error else None
            ),
        )
        for run in runs
    ]
    image_status = ImageStageStatusOut(
        state=image_stage.state,
        total=image_stage.total,
        pending=image_stage.pending,
        processing=image_stage.processing,
        needs_review=image_stage.needs_review,
        failed=image_stage.failed,
        completed=image_stage.completed,
        publishable=image_stage.publishable,
    )
    revision_status = (
        str(getattr(revision.status, "value", revision.status)) if revision is not None else None
    )
    latest_reclassification = next(
        (
            _reclassification_out(run)
            for run in reversed(runs)
            if run.stage.startswith("reextract:")
        ),
        None,
    )
    return DocumentIngestionStatusOut(
        source_document=doc,
        classification_state=(
            "unclassified"
            if str(doc.doc_type) == DocType.UNCLASSIFIED.value
            else "classified"
        ),
        revision=revision,
        ingest_runs=run_statuses,
        latest_reclassification=latest_reclassification,
        image_stage=image_status,
        timing=_build_ingestion_timing(runs, now),
        publishable=(
            revision_status in {RevisionStatus.STAGED.value, RevisionStatus.ACTIVE.value}
            and image_stage.publishable
        ),
    )


class StageTimingAverageOut(BaseModel):
    stage: str
    document_count: int
    average_seconds: float


class KbIngestStatsOut(BaseModel):
    completed_documents: int
    running_documents: int
    average_document_seconds: float | None
    median_document_seconds: float | None
    total_document_seconds: float
    average_stage_seconds: list[StageTimingAverageOut]


@router.get("/bases/{kb_id}/ingest-stats", response_model=KbIngestStatsOut)
async def get_kb_ingest_stats(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> KbIngestStatsOut:
    """全库摄入耗时基线:回答"这类文档平均要跑多久"。

    只认 document 根 run 已 succeeded 的 revision —— 失败/取消的半截数据混进
    均值会让基线毫无参考价值。所有聚合都压在 SQL 里做,文档上千也只是三条查询。
    """
    await _require_visible_active_kb(session, kb_id)

    completed_revisions = (
        select(IngestRun.revision_id)
        .where(
            IngestRun.kb_id == kb_id,
            IngestRun.stage == _DOCUMENT_STAGE,
            IngestRun.status == IngestRunStatus.SUCCEEDED.value,
        )
        .scalar_subquery()
    )
    # 未开工的 run(started_at 为 NULL)不参与跨度,否则会把 min 拉成 NULL
    timed_runs = (
        IngestRun.kb_id == kb_id,
        IngestRun.revision_id.in_(completed_revisions),
        IngestRun.started_at.is_not(None),
    )

    document_span = (
        select(
            func.min(IngestRun.started_at).label("started_at"),
            func.max(IngestRun.finished_at).label("finished_at"),
        )
        .where(*timed_runs)
        .group_by(IngestRun.revision_id)
        .subquery()
    )
    document_seconds = extract("epoch", document_span.c.finished_at - document_span.c.started_at)
    completed, average, median, total = (
        await session.execute(
            select(
                func.count(),
                func.avg(document_seconds),
                func.percentile_cont(0.5).within_group(document_seconds),
                func.coalesce(func.sum(document_seconds), 0.0),
            ).where(document_span.c.finished_at.is_not(None))
        )
    ).one()

    running_documents = await session.scalar(
        select(func.count())
        .select_from(IngestRun)
        .where(
            IngestRun.kb_id == kb_id,
            IngestRun.stage == _DOCUMENT_STAGE,
            IngestRun.status == IngestRunStatus.RUNNING.value,
        )
    )

    stage_case = _stage_case()
    stage_span = (
        select(
            stage_case.label("stage"),
            func.min(IngestRun.started_at).label("started_at"),
            func.max(IngestRun.finished_at).label("finished_at"),
        )
        .where(*timed_runs)
        .group_by(IngestRun.revision_id, stage_case)
        .subquery()
    )
    stage_seconds = extract("epoch", stage_span.c.finished_at - stage_span.c.started_at)
    stage_rows = (
        await session.execute(
            select(
                stage_span.c.stage,
                func.count(),
                func.avg(stage_seconds),
            )
            .where(stage_span.c.finished_at.is_not(None))
            .group_by(stage_span.c.stage)
            .order_by(func.avg(stage_seconds).desc())
        )
    ).all()

    return KbIngestStatsOut(
        completed_documents=int(completed or 0),
        running_documents=int(running_documents or 0),
        average_document_seconds=None if average is None else float(average),
        median_document_seconds=None if median is None else float(median),
        total_document_seconds=float(total or 0.0),
        average_stage_seconds=[
            StageTimingAverageOut(
                stage=stage,
                document_count=int(document_count),
                average_seconds=float(average_seconds),
            )
            for stage, document_count, average_seconds in stage_rows
        ],
    )


@router.get("/documents/{document_id}/markdown")
async def get_document_markdown(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """统一 Markdown 中间表示预览(KB-1)。JSON 返回,刻意不设
    Content-Disposition(本机安全软件会拦截下载响应,项目已知坑)。"""
    doc = await session.get(SourceDocument, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_visible_active_kb(session, doc.kb_id)
    if not doc.markdown_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档尚未解析出 Markdown(重新摄入可生成)")
    try:
        data = await storage.get_object(doc.markdown_key)
    except Exception as exc:  # noqa: BLE001 - 对象缺失/存储不可达统一 404
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Markdown 产物不可用") from exc
    return {
        "document_id": str(doc.id),
        "markdown": data.decode("utf-8", errors="replace"),
        "parser": doc.parser_name,
    }


# ---- chunk 管理(KB-2):锚点查看 / 人工修订重嵌 / 删除 ---------------------


class ChunkOut(BaseModel):
    id: str
    chunk_index: int | None
    content: str
    heading_path: str | None
    start_line: int | None
    end_line: int | None
    page: int | None
    has_embedding: bool


def _chunk_out(chunk: KbChunk) -> ChunkOut:
    return ChunkOut(
        id=str(chunk.id),
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        heading_path=chunk.heading_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        page=chunk.page,
        has_embedding=chunk.embedding is not None,
    )


@router.get("/documents/{doc_id}/chunks", response_model=list[ChunkOut])
async def list_document_chunks(
    doc_id: UUID,
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """该文档全部 chunk(含溯源锚点与有无向量),按 chunk_index 排序
    (历史数据无 index 时按创建时间兜底)。"""
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_visible_active_kb(session, doc.kb_id)
    rows = (
        (
            await session.execute(
                select(KbChunk)
                .where(KbChunk.source_doc_id == doc_id)
                .order_by(KbChunk.chunk_index.asc().nulls_last(), KbChunk.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_chunk_out(c) for c in rows]


class ChunkPatchBody(BaseModel):
    content: str


@router.patch("/chunks/{chunk_id}", response_model=ChunkOut)
async def update_chunk(
    chunk_id: UUID,
    body: ChunkPatchBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """人工修订 chunk 内容并同步重算 embedding(标题面包屑继续参与嵌入文本);
    embedder 不可用时向量置空如实返回(has_embedding=false),不假装成功。"""
    if not body.content.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content 不能为空")
    chunk = await session.get(KbChunk, chunk_id)
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk 不存在")
    _require_legacy_projection_mutable(chunk)
    await _require_own_kb(session, ctx, chunk.kb_id)
    chunk.content = body.content
    chunk.embedding = None
    chunk.embedding_model = None
    embedder = default_embedder()
    if embedder is not None:
        embed_text = f"{chunk.heading_path}\n{body.content}" if chunk.heading_path else body.content
        try:
            [vector] = await embedder.embed(
                [embed_text],
                org_id=ctx.org_id,
                task="kb.chunk.embedding",
            )
            chunk.embedding = vector
            chunk.embedding_model = embedder.label
        except EmbeddingUnavailableError:
            pass  # 向量留空,响应里 has_embedding=false 如实体现
    session.add(chunk)
    await session.commit()
    await session.refresh(chunk)
    return _chunk_out(chunk)


@router.delete("/chunks/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chunk(
    chunk_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    chunk = await session.get(KbChunk, chunk_id)
    if chunk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chunk 不存在")
    _require_legacy_projection_mutable(chunk)
    await _require_own_kb(session, ctx, chunk.kb_id)
    await session.delete(chunk)
    await session.commit()


# ---- wiki 自动生成(KB-5A):手动重跑 ---------------------------------------


@router.post("/documents/{doc_id}/wiki/refresh")
async def refresh_document_wiki(
    doc_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """重跑该文档的 wiki 更新(两步思维链:分析→生成→草稿落库→综述草稿)。
    同步执行并返回变更摘要;失败如实抛错(自动触发路径才是 best-effort)。"""
    from nicekit.core.db import get_session_factory

    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    try:
        result = await update_wiki_for_document(
            doc.id, ctx.org_id, session_factory=get_session_factory(), llm=get_llm_service()
        )
    except (WikiSourceUnavailableError, WikiSnapshotManagedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return result.as_dict()


@router.post("/bases/{kb_id}/wiki/overview/refresh")
async def refresh_kb_wiki_overview(
    kb_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """单独生成该库的「知识库总览」待审核草稿。"""
    from nicekit.core.db import get_session_factory

    await _require_own_kb(session, ctx, kb_id)
    try:
        page = await refresh_kb_overview(
            kb_id, ctx.org_id, session_factory=get_session_factory(), llm=get_llm_service()
        )
    except WikiSnapshotManagedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "page_id": str(page.id),
        "title": page.title,
        "page_type": page.page_type,
        "origin": page.origin,
        "content": page.content,
        "draft_content": page.draft_content,
        "draft_status": page.draft_status,
    }


# 可重跑 = 终态且未产生待审核条目风险;可取消 = 还没跑完
_RETRYABLE = (DocStatus.FAILED, DocStatus.CANCELED, DocStatus.PAUSED)
_CANCELABLE = (DocStatus.UPLOADED, DocStatus.PARSING, DocStatus.PAUSED)
# 只有真正在跑的文档需要暂停;uploaded 还没开工,直接取消即可
_PAUSABLE = (DocStatus.PARSING,)
# 图片链路与文本链路解耦后,图片富化失败不再把文档打成 failed(文本仍可用),但这类
# 文档过不了发布门禁。若不放行重试它就永远既不可发布也不可重跑,所以带图片失败标记的
# 成功终态同样可重跑:已 staged 的解析/切片/抽取阶段按幂等键跳过,只补跑失败的图。
_IMAGE_FAILURE_RETRYABLE = (DocStatus.COMPLETED, DocStatus.AWAITING_REVIEW)


def _is_retryable(doc: SourceDocument) -> bool:
    if str(doc.doc_type) == DocType.UNCLASSIFIED.value:
        return False
    if doc.status in _RETRYABLE:
        return True
    return doc.status in _IMAGE_FAILURE_RETRYABLE and doc.error == IMAGE_ENRICHMENT_ERROR


async def _cancel_document_ingestion(session: AsyncSession, doc: SourceDocument) -> None:
    doc = (
        await session.execute(
            select(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if doc.status not in _CANCELABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"仅 uploaded/parsing/paused 可取消,当前 {doc.status}"
        )
    doc.status = DocStatus.CANCELED
    session.add(doc)
    await session.execute(
        update(IngestRun)
        .where(
            IngestRun.revision_id.in_(
                select(DocumentRevision.id).where(DocumentRevision.doc_id == doc.id)
            ),
            IngestRun.status.in_((IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)),
        )
        .values(
            status=IngestRunStatus.CANCELED.value,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=None,
            error="source document canceled before execution",
            finished_at=datetime.now(UTC),
        )
    )
    revision = await session.scalar(
        select(DocumentRevision)
        .where(DocumentRevision.doc_id == doc.id)
        .order_by(
            DocumentRevision.revision_no.desc(),
            DocumentRevision.created_at.desc(),
            DocumentRevision.id.desc(),
        )
        .limit(1)
    )
    if revision is not None:
        from nicekit.kb.document_reingestion import (
            settle_reingestion_ingest_result,
        )

        await settle_reingestion_ingest_result(
            session,
            document=doc,
            revision=revision,
        )


async def _pause_document_ingestion(session: AsyncSession, doc: SourceDocument) -> None:
    """协作式暂停:摄入在阶段边界停下,已 staged 的阶段产物全部保留待续跑。"""
    doc = (
        await session.execute(
            select(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if doc.status not in _PAUSABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅 parsing 可暂停,当前 {doc.status}")
    doc.status = DocStatus.PAUSED
    session.add(doc)
    # 排队中尚未被 worker 认领的段直接收掉;在飞的段由 worker 在边界自行退出
    await session.execute(
        update(IngestRun)
        .where(
            IngestRun.revision_id.in_(
                select(DocumentRevision.id).where(DocumentRevision.doc_id == doc.id)
            ),
            IngestRun.status == IngestRunStatus.QUEUED.value,
        )
        .values(
            status=IngestRunStatus.CANCELED.value,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=None,
            error="source document paused before execution",
            finished_at=datetime.now(UTC),
        )
    )


async def _retry_doc(
    session: AsyncSession, ctx: OrgContext, background: BackgroundTasks, doc: SourceDocument
) -> None:
    doc = (
        await session.execute(
            select(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    lifecycle = str(getattr(doc.lifecycle_status, "value", doc.lifecycle_status))
    if lifecycle not in {
        DocumentLifecycleStatus.ACTIVE.value,
        DocumentLifecycleStatus.REINGESTION_PENDING.value,
    }:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "已撤回文档必须通过显式重新摄入流程恢复",
        )
    if not _is_retryable(doc):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"仅 failed/canceled/paused 可重跑,当前 {doc.status}"
        )
    active_run = await session.scalar(
        select(IngestRun.id)
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .where(
            DocumentRevision.doc_id == doc.id,
            IngestRun.stage == "document",
            IngestRun.status.in_((IngestRunStatus.QUEUED.value, IngestRunStatus.RUNNING.value)),
        )
        .limit(1)
    )
    if active_run is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "摄入任务已排队或仍在运行")
    doc.status = DocStatus.UPLOADED
    doc.error = None
    doc.progress = 0
    session.add(doc)
    revision, run = await enqueue_document_ingestion(session, doc)
    await _reset_canceled_ingest_segments(session, revision=revision, root_run=run)
    await session.commit()
    await _dispatch_kb_ingest_run(run.id, doc.org_id, background)


async def _reset_canceled_ingest_segments(
    session: AsyncSession,
    *,
    revision: DocumentRevision,
    root_run: IngestRun,
) -> None:
    await session.execute(
        update(IngestRun)
        .where(
            IngestRun.revision_id == revision.id,
            IngestRun.id != root_run.id,
            IngestRun.status == IngestRunStatus.CANCELED.value,
        )
        .values(
            status=IngestRunStatus.FAILED.value,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=None,
            available_at=None,
            error="ingest segment reset after document retry",
            finished_at=datetime.now(UTC),
        )
    )
class DocExpiryBody(BaseModel):
    expires_at: datetime | None = None  # null = 清除有效期(长期有效)


@router.patch("/documents/{doc_id}", response_model=SourceDocument)
async def update_document_expiry(
    doc_id: UUID,
    body: DocExpiryBody,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """设置/清除文档有效期(prod-readiness-4):过期后检索命中带 stale 标注,
    并触发一次过期提醒。重设有效期会清掉已提醒标记(允许再次提醒)。"""
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    doc.expires_at = body.expires_at
    doc.expiry_notified_at = None
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


@router.post("/documents/{doc_id}/reingest", response_model=SourceDocument)
async def reingest_document(
    doc_id: UUID,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """Retry an active failed ingest or restore a withdrawn document through a new revision."""
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    lifecycle = str(getattr(doc.lifecycle_status, "value", doc.lifecycle_status))
    if lifecycle in {
        DocumentLifecycleStatus.WITHDRAWN.value,
        DocumentLifecycleStatus.REINGESTION_PENDING.value,
    }:
        try:
            request = await start_or_retry_document_reingestion(
                session,
                org_id=ctx.org_id,
                document_id=doc.id,
                actor_id=ctx.user_id,
            )
            run: IngestRun | None = None
            if request.enqueue_ingestion:
                _, run = await enqueue_document_ingestion(
                    session,
                    request.document,
                    revision=request.revision,
                )
                await _reset_canceled_ingest_segments(
                    session,
                    revision=request.revision,
                    root_run=run,
                )
            await session.commit()
        except DocumentReingestionError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await session.refresh(request.document)
        if run is not None:
            await _dispatch_kb_ingest_run(run.id, request.document.org_id, background)
        return request.document
    if lifecycle != DocumentLifecycleStatus.ACTIVE.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"当前生命周期 {lifecycle} 不允许重新摄入",
        )
    if not _is_retryable(doc):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"仅 failed/canceled/paused 可重跑,当前 {doc.status}"
        )
    await _retry_doc(session, ctx, background, doc)
    await session.refresh(doc)
    return doc


@router.post("/documents/{doc_id}/cancel", response_model=SourceDocument)
async def cancel_document(
    doc_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """取消排队/解析中/已暂停的文档:排队中立即生效;解析中在分段间协作式停止
    (当前段的 LLM 调用会完成,已入队的待审核条目保留)。"""
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    if doc.status not in _CANCELABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"仅 uploaded/parsing/paused 可取消,当前 {doc.status}"
        )
    await _cancel_document_ingestion(session, doc)
    await session.commit()
    await session.refresh(doc)
    return doc


@router.post("/documents/{doc_id}/pause", response_model=SourceDocument)
async def pause_document(
    doc_id: UUID,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """暂停解析中的文档:在阶段边界协作式停止,已完成阶段的产物全部保留。

    与取消的区别是意图 —— 暂停默认要继续,续跑时已 staged 的解析/图片/分段
    直接复用,不会重复调用 OCR 与 LLM。
    """
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    if doc.status not in _PAUSABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅 parsing 可暂停,当前 {doc.status}")
    await _pause_document_ingestion(session, doc)
    await session.commit()
    await session.refresh(doc)
    return doc


@router.post("/documents/{doc_id}/resume", response_model=SourceDocument)
async def resume_document(
    doc_id: UUID,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """继续已暂停的文档:重新入队,已 staged 的阶段按幂等键跳过,不重复采集。"""
    doc = await session.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await _require_own_kb(session, ctx, doc.kb_id)
    if doc.status != DocStatus.PAUSED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅 paused 可继续,当前 {doc.status}")
    # 暂停是协作式的:worker 要跑到阶段边界才退出(图片阶段是一张图,解析阶段
    # 是整次 Docling 转换)。此时抢跑会双跑同一份文档,如实告诉用户稍候。
    inflight = await session.scalar(
        select(IngestRun.id)
        .join(DocumentRevision, DocumentRevision.id == IngestRun.revision_id)
        .where(
            DocumentRevision.doc_id == doc.id,
            IngestRun.stage == "document",
            IngestRun.status == IngestRunStatus.RUNNING.value,
        )
        .limit(1)
    )
    if inflight is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "上一次摄入正在阶段边界收尾,请稍候再继续",
        )
    await _retry_doc(session, ctx, background, doc)
    await session.refresh(doc)
    return doc


class DocBatchBody(BaseModel):
    ids: list[UUID]
    action: str  # retry / cancel


@router.post("/documents/batch")
async def batch_documents(
    body: DocBatchBody,
    background: BackgroundTasks,
    ctx: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
    session: Annotated[AsyncSession, Depends(get_org_session)],
):
    """批量重试/取消/暂停/继续。逐条处理,状态不符的条目跳过并报告,不整体失败。"""
    if body.action not in ("retry", "cancel", "pause", "resume"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action 仅支持 retry/cancel/pause/resume")
    done, skipped = [], []
    for doc_id in body.ids:
        doc = await session.get(SourceDocument, doc_id)
        if doc is None:
            skipped.append({"id": str(doc_id), "reason": "不存在"})
            continue
        kb = await session.get(KnowledgeBase, doc.kb_id)
        if kb is None or kb.org_id != ctx.org_id:
            skipped.append({"id": str(doc_id), "reason": "只读库"})
            continue
        if body.action in ("retry", "resume"):
            eligible = (
                _is_retryable(doc) if body.action == "retry" else doc.status == DocStatus.PAUSED
            )
            if not eligible:
                skipped.append(
                    {"id": str(doc_id), "reason": f"状态 {doc.status} 不可{body.action}"}
                )
                continue
            try:
                await _retry_doc(session, ctx, background, doc)
            except HTTPException as exc:
                await session.rollback()
                skipped.append({"id": str(doc_id), "reason": str(exc.detail)})
                continue
        elif body.action == "pause":
            if doc.status not in _PAUSABLE:
                skipped.append({"id": str(doc_id), "reason": f"状态 {doc.status} 不可暂停"})
                continue
            try:
                await _pause_document_ingestion(session, doc)
            except HTTPException as exc:
                await session.rollback()
                skipped.append({"id": str(doc_id), "reason": str(exc.detail)})
                continue
            await session.commit()
        else:
            if doc.status not in _CANCELABLE:
                skipped.append({"id": str(doc_id), "reason": f"状态 {doc.status} 不可取消"})
                continue
            try:
                await _cancel_document_ingestion(session, doc)
            except HTTPException as exc:
                await session.rollback()
                skipped.append({"id": str(doc_id), "reason": str(exc.detail)})
                continue
            await session.commit()
        done.append(str(doc_id))
    return {"done": done, "skipped": skipped}


@router.get(
    "/image-enrichment/readiness",
    response_model=KbImageEnrichmentReadinessRead,
)
async def get_image_enrichment_readiness(
    _: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    kb_id: UUID | None = None,
) -> KbImageEnrichmentReadinessRead:
    """Return the effective non-secret OCR/caption readiness for a KB or default."""
    profile: IngestProfile | None = None
    if kb_id is not None:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
        try:
            profile = (
                IngestProfile.model_validate(kb.ingest_profile)
                if kb.ingest_profile is not None
                else IngestProfile()
            )
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "caption_profile_invalid",
                    "message": "知识库图片描述配置不合法",
                },
            ) from exc
    return await _image_enrichment_readiness(session, profile)


@router.get(
    "/image-enrichment/models",
    response_model=list[ProviderModelCatalogEntry],
)
async def get_image_enrichment_models(
    _: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ProviderModelCatalogEntry]:
    """Return enabled generation-plus-vision models without provider secrets."""
    providers = (await session.execute(select(LlmProvider))).scalars().all()
    entries = [
        entry
        for entry in build_model_catalog(providers, capability="vision")
        if "generation" in entry.capabilities
    ]
    normalized_search = (search or "").strip().casefold()
    if normalized_search:
        entries = [
            entry
            for entry in entries
            if normalized_search in entry.provider.casefold()
            or normalized_search in entry.model.casefold()
        ]
    return entries[:limit]


@router.get("/ingest/profiles/presets")
async def get_ingest_profile_presets(
    _: Annotated[OrgContext, Depends(get_org_context)],
):
    """摄入切片配置预设模板(KB-2):前端建库/改库时一键套用。
    default 为无 profile 时摄入实际采用的默认值。"""
    return {
        "default": IngestProfile().model_dump(),
        "presets": INGEST_PROFILE_PRESETS,
    }


class IngestSettingsBody(BaseModel):
    max_concurrency: int


def _ingest_settings_payload() -> dict[str, int]:
    settings = get_settings()
    return {
        "max_concurrency": get_max_concurrency(),
        "active": get_active_count(),
        "upload_max_file_bytes": settings.kb_upload_max_file_bytes,
        "upload_max_batch_files": settings.kb_upload_max_batch_files,
    }


@router.get("/ingest/settings")
async def get_ingest_settings(
    _: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
):
    return _ingest_settings_payload()


@router.put("/ingest/settings")
async def put_ingest_settings(
    body: IngestSettingsBody,
    _: Annotated[OrgContext, Depends(require_role(*_KB_WRITERS))],
):
    if not 1 <= body.max_concurrency <= 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "并行度范围 1-8")
    await set_max_concurrency(body.max_concurrency)
    return _ingest_settings_payload()
