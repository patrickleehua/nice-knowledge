"""Authenticated delivery of governed KB originals and thumbnails."""

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import OrgContext, get_org_context, get_org_session, require_role
from nicekit.domain.kb_media import (
    ImageAssetIssueKind,
    ImageEnrichmentStatus,
    ImageReviewStatus,
    KbImageAssetAuditPage,
    KbImageAssetPage,
    KbImageAssetRead,
    KbImageBulkReviewRequest,
    KbImageBulkReviewResult,
    KbImageRetryAccepted,
    KbImageRetryRequest,
    KbImageReviewRequest,
)
from nicekit.kb.image_assets import (
    ImageVariant,
    KbImageAssetUnavailableError,
    ResolvedImageObject,
    resolve_image_object,
)
from nicekit.kb.image_inventory import (
    get_image_asset as get_image_asset_record,
)
from nicekit.kb.image_inventory import list_image_asset_audit, list_image_assets
from nicekit.kb.image_review import (
    KbImageReviewConflictError,
    bulk_review_image_assets,
    retry_image_asset,
    review_image_asset,
)
from nicekit.models.tenancy import Role
from nicekit.runtime.dispatch import dispatch_kb_ingest_run

router = APIRouter(prefix="/kb/image-assets")

Ctx = Annotated[OrgContext, Depends(get_org_context)]
Session = Annotated[AsyncSession, Depends(get_org_session)]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]
ReviewCtx = Annotated[
    OrgContext,
    Depends(require_role(Role.PLATFORM_ADMIN, Role.ORG_ADMIN)),
]

_PRIVATE_CACHE = "private, max-age=300, must-revalidate"
_ASSET_NOT_FOUND = "图片资产不存在"


def _etag_matches(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    candidates = {candidate.strip() for candidate in value.split(",")}
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates


def _response_headers(image: ResolvedImageObject) -> dict[str, str]:
    return {
        "Cache-Control": _PRIVATE_CACHE,
        "Content-Disposition": f'inline; filename="{image.filename}"',
        "ETag": image.etag,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }


async def _image_response(
    *,
    asset_id: UUID,
    variant: ImageVariant,
    snapshot_id: UUID | None,
    if_none_match: str | None,
    ctx: OrgContext,
    session: AsyncSession,
) -> Response:
    try:
        image = await resolve_image_object(
            session,
            request_org_id=ctx.org_id,
            asset_id=asset_id,
            variant=variant,
            snapshot_id=snapshot_id,
        )
    except KbImageAssetUnavailableError as exc:
        # A resolved asset can append a sanitized operational consistency
        # incident before failing closed; commit it even though the media result
        # itself is a 404. Invisible/cross-tenant IDs append nothing.
        if isinstance(session, AsyncSession):
            await session.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, _ASSET_NOT_FOUND) from exc
    headers = _response_headers(image)
    if _etag_matches(if_none_match, image.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers=headers,
    )


@router.get("", response_model=KbImageAssetPage)
async def get_image_asset_inventory(
    kb_id: UUID,
    ctx: Ctx,
    session: Session,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    enrichment_status: ImageEnrichmentStatus | None = None,
    review_status: ImageReviewStatus | None = None,
    failure_code: Annotated[
        str | None,
        Query(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$"),
    ] = None,
    document_id: UUID | None = None,
    issue_kind: ImageAssetIssueKind | None = None,
) -> KbImageAssetPage:
    return await list_image_assets(
        session,
        request_org_id=ctx.org_id,
        kb_id=kb_id,
        page=page,
        page_size=page_size,
        enrichment_status=enrichment_status,
        review_status=review_status,
        failure_code=failure_code,
        document_id=document_id,
        issue_kind=issue_kind,
    )


@router.post("/bulk-review", response_model=KbImageBulkReviewResult)
async def bulk_review_image_assets_endpoint(
    kb_id: UUID,
    body: KbImageBulkReviewRequest,
    ctx: ReviewCtx,
    session: Session,
) -> KbImageBulkReviewResult:
    """按筛选条件批量通过/排除图片。不传 asset_ids 即对整个筛选结果生效。

    一份研报能出上百张插图,逐张点通过不具可维护性;审计与快照失效仍逐条落库。
    """
    result = await bulk_review_image_assets(
        session,
        request_org_id=ctx.org_id,
        kb_id=kb_id,
        actor_user_id=ctx.user_id,
        request=body,
    )
    await session.commit()
    return result


def _review_conflict(exc: KbImageReviewConflictError) -> HTTPException:
    if exc.code == "lock_version_conflict":
        detail = "图片资产已被其他操作更新"
    else:
        detail = "图片资产当前状态不允许此操作"
    return HTTPException(status.HTTP_409_CONFLICT, detail)


@router.post("/{asset_id}/review", response_model=KbImageAssetRead)
async def review_image_asset_endpoint(
    asset_id: UUID,
    body: KbImageReviewRequest,
    ctx: ReviewCtx,
    session: Session,
) -> KbImageAssetRead:
    try:
        await review_image_asset(
            session,
            request_org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            asset_id=asset_id,
            request=body,
        )
    except KbImageAssetUnavailableError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _ASSET_NOT_FOUND) from exc
    except KbImageReviewConflictError as exc:
        raise _review_conflict(exc) from exc
    await session.commit()
    return await get_image_asset_record(
        session,
        request_org_id=ctx.org_id,
        asset_id=asset_id,
    )


@router.post(
    "/{asset_id}/retry",
    response_model=KbImageRetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_image_asset_endpoint(
    asset_id: UUID,
    body: KbImageRetryRequest,
    background: BackgroundTasks,
    ctx: ReviewCtx,
    session: Session,
) -> KbImageRetryAccepted:
    try:
        schedule = await retry_image_asset(
            session,
            request_org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            asset_id=asset_id,
            request=body,
        )
    except KbImageAssetUnavailableError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _ASSET_NOT_FOUND) from exc
    except KbImageReviewConflictError as exc:
        raise _review_conflict(exc) from exc
    await session.commit()
    await dispatch_kb_ingest_run(schedule.run_id, ctx.org_id, background)
    return KbImageRetryAccepted(
        asset_id=schedule.asset_id,
        run_id=schedule.run_id,
        lock_version=schedule.lock_version,
    )


@router.get("/{asset_id}/audit", response_model=KbImageAssetAuditPage)
async def get_image_asset_audit(
    asset_id: UUID,
    ctx: ReviewCtx,
    session: Session,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> KbImageAssetAuditPage:
    try:
        return await list_image_asset_audit(
            session,
            request_org_id=ctx.org_id,
            asset_id=asset_id,
            page=page,
            page_size=page_size,
        )
    except KbImageAssetUnavailableError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _ASSET_NOT_FOUND) from exc


@router.get("/{asset_id}", response_model=KbImageAssetRead)
async def get_image_asset_detail(
    asset_id: UUID,
    ctx: Ctx,
    session: Session,
) -> KbImageAssetRead:
    try:
        return await get_image_asset_record(
            session,
            request_org_id=ctx.org_id,
            asset_id=asset_id,
        )
    except KbImageAssetUnavailableError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _ASSET_NOT_FOUND) from exc


@router.get("/{asset_id}/content")
async def get_image_content(
    asset_id: UUID,
    ctx: Ctx,
    session: Session,
    if_none_match: IfNoneMatch = None,
    snapshot_id: UUID | None = None,
) -> Response:
    return await _image_response(
        asset_id=asset_id,
        variant="original",
        snapshot_id=snapshot_id,
        if_none_match=if_none_match,
        ctx=ctx,
        session=session,
    )


@router.get("/{asset_id}/thumbnail")
async def get_image_thumbnail(
    asset_id: UUID,
    ctx: Ctx,
    session: Session,
    if_none_match: IfNoneMatch = None,
    snapshot_id: UUID | None = None,
) -> Response:
    return await _image_response(
        asset_id=asset_id,
        variant="thumbnail",
        snapshot_id=snapshot_id,
        if_none_match=if_none_match,
        ctx=ctx,
        session=session,
    )
