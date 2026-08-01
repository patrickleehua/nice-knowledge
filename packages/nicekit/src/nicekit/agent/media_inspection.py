"""Agent 对"已发布知识库图片"的授权像素审读(agent 侧唯一的多模态读图入口)。

迁移自 TF backend/app/services/agent/media_inspection.py。SDK 化改造(§5.4):
- TF 按 `Project.kb_scope` 判定"这张图在不在本项目的知识范围内";SDK 不认识
  宿主的业务表,改为调用方传入 `kb_scope`(允许的 kb_id 集合,None = 不限制)。
  错误码 `project_unavailable` / `outside_project_scope` 随之收敛为
  `outside_kb_scope`。
- KB 检索链模块(kb.caption / kb.eligibility / kb.image_assets)一律**函数内
  延迟 import**:agent 子系统不该在导入期把整条 KB 链拖进来;KB 未装配时
  按 capability_unavailable 处理而不是抛 ImportError。

安全边界(勿回退):只读**当前 active 快照里、审核已 accepted、且冻结时确实
被接受**的图;审读前后各校验一次知识边界租约(active_knowledge_base_lease),
期间快照被切换/回滚一律判 knowledge_boundary_changed 并拒绝返回内容。
每一次审读(成功与失败)都落 AuditLog。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import NoReturn
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.domain.kb import IngestProfile
from nicekit.llm.model_catalog import (
    ModelCatalogLookupError,
    lookup_eligible_provider_model_session,
)
from nicekit.llm.service import get_llm_service
from nicekit.models.kb import (
    KbImageAsset,
    KbSnapshotImageAsset,
    KnowledgeBase,
    KnowledgeSnapshot,
    SourceDocument,
)
from nicekit.models.tenancy import AuditLog


class AgentMediaInspectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _add_audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    asset_id: UUID,
    status: str,
    code: str,
    snapshot_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    byte_size: int | None = None,
) -> None:
    session.add(
        AuditLog(
            org_id=org_id,
            user_id=user_id,
            action="agent.kb_image_inspect",
            entity_type="kb_image_asset",
            entity_id=str(asset_id),
            detail={
                "status": status,
                "code": code,
                "snapshot_id": str(snapshot_id) if snapshot_id else None,
                "provider": provider,
                "model": model,
                "byte_size": byte_size,
            },
        )
    )


def _raise_audited(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    asset_id: UUID,
    code: str,
    snapshot_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> NoReturn:
    _add_audit(
        session,
        org_id=org_id,
        user_id=user_id,
        asset_id=asset_id,
        status="failed",
        code=code,
        snapshot_id=snapshot_id,
        provider=provider,
        model=model,
    )
    raise AgentMediaInspectionError(code)


async def inspect_knowledge_image(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    asset_id: UUID,
    question: str | None,
    kb_scope: Collection[UUID] | None = None,
) -> dict:
    """审读一张已发布知识库图片,返回描述与可见文字。

    kb_scope:调用方(宿主的作用域解析)给出的可见知识库白名单;None = 不限制,
    只受 org 隔离与"active 快照 + accepted"这两道硬闸约束。
    """
    settings = get_settings()
    if not settings.agent_kb_image_inspection_enabled:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset_id,
            code="capability_unavailable",
        )
    try:
        # 延迟 import:KB 检索链未装配时按"能力不可用"处理,不炸 ImportError
        from nicekit.kb.caption import (
            CaptionExecutionError,
            CaptionModelSelection,
            inspect_image_with_metadata,
            platform_caption_selection,
        )
        from nicekit.kb.eligibility import (
            ActiveKnowledgeBaseLease,
            active_knowledge_base_lease_is_current,
        )
        from nicekit.kb.image_assets import (
            KbImageAssetUnavailableError,
            resolve_image_object,
        )
    except ImportError:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset_id,
            code="capability_unavailable",
        )

    row = (
        await session.execute(
            select(
                KbImageAsset,
                KbSnapshotImageAsset,
                KnowledgeBase,
                SourceDocument.filename,
            )
            .join(
                KbSnapshotImageAsset,
                and_(
                    KbSnapshotImageAsset.image_asset_id == KbImageAsset.id,
                    KbSnapshotImageAsset.org_id == KbImageAsset.org_id,
                    KbSnapshotImageAsset.kb_id == KbImageAsset.kb_id,
                ),
            )
            .join(
                KnowledgeBase,
                and_(
                    KnowledgeBase.id == KbImageAsset.kb_id,
                    KnowledgeBase.org_id == KbImageAsset.org_id,
                ),
            )
            .join(
                KnowledgeSnapshot,
                and_(
                    KnowledgeSnapshot.id == KbSnapshotImageAsset.snapshot_id,
                    KnowledgeSnapshot.org_id == KbSnapshotImageAsset.org_id,
                    KnowledgeSnapshot.kb_id == KbSnapshotImageAsset.kb_id,
                ),
            )
            .join(
                SourceDocument,
                and_(
                    SourceDocument.id == KbImageAsset.doc_id,
                    SourceDocument.org_id == KbImageAsset.org_id,
                ),
            )
            .where(
                KbImageAsset.id == asset_id,
                KbImageAsset.org_id == org_id,
                KbImageAsset.review_status == "accepted",
                KbSnapshotImageAsset.accepted_at_build.is_(True),
                KnowledgeBase.active_snapshot_id == KbSnapshotImageAsset.snapshot_id,
                KnowledgeBase.lifecycle_status == "active",
                KnowledgeSnapshot.status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset_id,
            code="asset_unavailable",
        )
    asset, frozen, kb, source_name = row
    try:
        profile = (
            IngestProfile.model_validate(kb.ingest_profile)
            if kb.ingest_profile is not None
            else None
        )
    except ValidationError:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            code="multimodal_model_configuration_invalid",
            snapshot_id=frozen.snapshot_id,
        )
    selection = (
        CaptionModelSelection(
            provider=profile.caption_provider,
            model=profile.caption_model,
        )
        if profile is not None
        and profile.caption_provider is not None
        and profile.caption_model is not None
        else None
    )
    effective_provider, effective_model = (
        (selection.provider, selection.model)
        if selection is not None
        else platform_caption_selection(settings)
    )
    if not effective_provider or not effective_model:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            code="multimodal_model_unconfigured",
            snapshot_id=frozen.snapshot_id,
            provider=effective_provider or None,
            model=effective_model or None,
        )
    exact_selection = CaptionModelSelection(
        provider=effective_provider,
        model=effective_model,
    )
    try:
        await lookup_eligible_provider_model_session(
            session,
            provider=effective_provider,
            model=effective_model,
        )
    except ModelCatalogLookupError as exc:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            code=exc.code,
            snapshot_id=frozen.snapshot_id,
            provider=effective_provider,
            model=effective_model,
        )
    if kb_scope is not None and kb.id not in kb_scope:
        _raise_audited(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            code="outside_kb_scope",
            snapshot_id=frozen.snapshot_id,
        )

    try:
        resolved = await resolve_image_object(
            session,
            request_org_id=org_id,
            asset_id=asset.id,
            variant="original",
            snapshot_id=frozen.snapshot_id,
            lock_lifecycle=False,
        )
    except KbImageAssetUnavailableError as exc:
        _add_audit(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            status="failed",
            code="object_unavailable",
            snapshot_id=frozen.snapshot_id,
        )
        raise AgentMediaInspectionError("object_unavailable") from exc
    if len(resolved.data) > settings.agent_kb_image_inspection_max_bytes:
        _add_audit(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            status="failed",
            code="byte_limit_exceeded",
            snapshot_id=frozen.snapshot_id,
            byte_size=len(resolved.data),
        )
        raise AgentMediaInspectionError("byte_limit_exceeded")
    lease = ActiveKnowledgeBaseLease(
        kb_id=kb.id,
        owner_org_id=kb.org_id,
        consumption_epoch=kb.consumption_epoch,
        active_snapshot_id=kb.active_snapshot_id,
    )
    await session.commit()
    try:
        generation = await inspect_image_with_metadata(
            resolved.data,
            llm=get_llm_service(),
            org_id=org_id,
            filename=str(source_name),
            question=question,
            content_type=resolved.content_type,
            settings=settings,
            selection=exact_selection,
        )
    except CaptionExecutionError as exc:
        _add_audit(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            status="failed",
            code=exc.code,
            snapshot_id=frozen.snapshot_id,
            byte_size=len(resolved.data),
        )
        raise AgentMediaInspectionError(exc.code) from exc

    if not await active_knowledge_base_lease_is_current(
        session,
        lease,
        lock=True,
    ):
        _add_audit(
            session,
            org_id=org_id,
            user_id=user_id,
            asset_id=asset.id,
            status="failed",
            code="knowledge_boundary_changed",
            snapshot_id=frozen.snapshot_id,
            provider=generation.provider,
            model=generation.model,
            byte_size=len(resolved.data),
        )
        raise AgentMediaInspectionError("knowledge_boundary_changed")

    _add_audit(
        session,
        org_id=org_id,
        user_id=user_id,
        asset_id=asset.id,
        status="success",
        code="inspected",
        snapshot_id=frozen.snapshot_id,
        provider=generation.provider,
        model=generation.model,
        byte_size=len(resolved.data),
    )
    return {
        "asset_id": str(asset.id),
        "snapshot_id": str(frozen.snapshot_id),
        "source_name": source_name,
        "page": frozen.page,
        "slide": frozen.slide,
        "inspection": {
            "description": generation.caption.description,
            "visible_text": generation.caption.visible_text,
        },
        "provider": generation.provider,
        "model": generation.model,
        "prompt_version": generation.prompt_version,
        "provenance": "retrieved_kb_source_pixel_inspection",
        "limits": {"bytes": settings.agent_kb_image_inspection_max_bytes},
    }


__all__ = [
    "AgentMediaInspectionError",
    "inspect_knowledge_image",
]
