"""Image inventory list/detail HTTP query contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb_media
from nicekit.domain.kb_media import (
    ImageAssetIssueKind,
    ImageEnrichmentStatus,
    ImageReviewStatus,
    KbImageAssetAuditEventRead,
    KbImageAssetAuditPage,
    KbImageAssetAuditState,
    KbImageAssetPage,
    KbImageAssetRead,
    KbImageAssetUsageSummary,
)
from nicekit.kb.image_assets import KbImageAssetUnavailableError
from nicekit.kb.image_review import (
    KbImageRetrySchedule,
    KbImageReviewConflictError,
)
from nicekit.models.tenancy import Role

# TF 经验:不 import 组装好的 app(也不用 with TestClient(app)——会触发
# lifespan 卡死)。轻量 FastAPI 只挂本用例需要的 router,依赖全部由
# dependency_overrides 假注入。
app = FastAPI()
app.include_router(kb_media.router, prefix="/api/v1")


def _asset_read() -> KbImageAssetRead:
    now = datetime.now(UTC)
    asset_id = uuid4()
    return KbImageAssetRead(
        asset_id=asset_id,
        kb_id=uuid4(),
        source_document_id=uuid4(),
        source_filename="inventory.pdf",
        revision_id=uuid4(),
        revision_number=2,
        revision_sha256="a" * 64,
        source_occurrence="page:1/picture:1",
        parser_name="docling",
        page=1,
        image_sha256="b" * 64,
        size_bytes=512,
        content_type="image/png",
        width=100,
        height=80,
        thumbnail_sha256="c" * 64,
        thumbnail_content_type="image/webp",
        thumbnail_size_bytes=128,
        thumbnail_width=100,
        thumbnail_height=80,
        content_url=f"/api/v1/kb/image-assets/{asset_id}/content",
        thumbnail_url=f"/api/v1/kb/image-assets/{asset_id}/thumbnail",
        extraction_fingerprint="d" * 64,
        enrichment_status=ImageEnrichmentStatus.SUCCEEDED,
        review_status=ImageReviewStatus.NEEDS_REVIEW,
        lock_version=1,
        usage=KbImageAssetUsageSummary(
            snapshot_count=1,
            image_chunk_count=2,
            evidence_span_count=3,
        ),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def inventory_client():
    org_id = uuid4()

    class FakeSession:
        async def commit(self) -> None:
            return None

    async def context_override() -> OrgContext:
        return OrgContext(user_id=uuid4(), org_id=org_id, role=Role.ORG_ADMIN)

    async def session_override():
        yield FakeSession()

    app.dependency_overrides[get_org_context] = context_override
    app.dependency_overrides[get_org_session] = session_override
    try:
        # 不进 TestClient 上下文管理器:那会触发 app lifespan(连库 + 起常驻后台任务),
        # 本用例的依赖全部由 dependency_overrides 假注入,不需要真实基础设施。
        # 同目录 test_kb_image_media_api.py 是同一写法。
        client = TestClient(app)
        yield client, org_id
    finally:
        app.dependency_overrides.pop(get_org_context, None)
        app.dependency_overrides.pop(get_org_session, None)


def test_inventory_list_forwards_bounded_filters_and_pagination(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, org_id = inventory_client
    item = _asset_read()
    captured: dict[str, object] = {}

    async def list_assets(session, **kwargs):
        captured.update(kwargs)
        return KbImageAssetPage(items=[item], total=21, page=2, page_size=5)

    monkeypatch.setattr(kb_media, "list_image_assets", list_assets)
    kb_id, document_id = uuid4(), uuid4()

    response = client.get(
        "/api/v1/kb/image-assets",
        params={
            "kb_id": str(kb_id),
            "page": 2,
            "page_size": 5,
            "enrichment_status": "failed",
            "review_status": "needs_review",
            "failure_code": "caption_provider_unavailable",
            "document_id": str(document_id),
            "issue_kind": "provider_failed",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 21
    assert response.json()["items"][0]["usage"]["business_artifact_support"] == (
        "unsupported"
    )
    assert captured == {
        "request_org_id": org_id,
        "kb_id": kb_id,
        "page": 2,
        "page_size": 5,
        "enrichment_status": ImageEnrichmentStatus.FAILED,
        "review_status": ImageReviewStatus.NEEDS_REVIEW,
        "failure_code": "caption_provider_unavailable",
        "document_id": document_id,
        "issue_kind": ImageAssetIssueKind.PROVIDER_FAILED,
    }
    assert "object_key" not in response.text
    assert "failure_detail" not in response.text


def test_inventory_rejects_unbounded_or_invalid_query_values(
    inventory_client,
) -> None:
    client, _ = inventory_client
    kb_id = uuid4()

    assert (
        client.get(
            "/api/v1/kb/image-assets",
            params={"kb_id": str(kb_id), "page_size": 101},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/kb/image-assets",
            params={"kb_id": str(kb_id), "failure_code": "INVALID CODE"},
        ).status_code
        == 422
    )


def test_inventory_detail_uses_uniform_not_found(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = inventory_client

    async def unavailable(session, **kwargs):
        raise KbImageAssetUnavailableError("cross_tenant")

    monkeypatch.setattr(kb_media, "get_image_asset_record", unavailable)
    response = client.get(f"/api/v1/kb/image-assets/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "图片资产不存在"}
    assert "cross_tenant" not in response.text


def test_review_endpoint_returns_updated_asset_and_maps_lock_conflict(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = inventory_client
    item = _asset_read()
    calls: list[int] = []

    async def review(session, **kwargs):
        calls.append(kwargs["request"].expected_lock_version)

    async def detail(session, **kwargs):
        return item

    monkeypatch.setattr(kb_media, "review_image_asset", review)
    monkeypatch.setattr(kb_media, "get_image_asset_record", detail)
    response = client.post(
        f"/api/v1/kb/image-assets/{item.asset_id}/review",
        json={
            "action": "edit_and_accept",
            "expected_lock_version": 1,
            "reason": "人工校正",
            "caption": "校正后的图片描述",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["asset_id"] == str(item.asset_id)
    assert calls == [1]

    async def conflict(session, **kwargs):
        raise KbImageReviewConflictError("lock_version_conflict")

    monkeypatch.setattr(kb_media, "review_image_asset", conflict)
    stale = client.post(
        f"/api/v1/kb/image-assets/{item.asset_id}/review",
        json={
            "action": "accept",
            "expected_lock_version": 1,
            "reason": "过期操作",
        },
    )
    assert stale.status_code == 409
    assert stale.json() == {"detail": "图片资产已被其他操作更新"}


def test_retry_endpoint_dispatches_persisted_root_and_returns_202(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = inventory_client
    asset_id, run_id = uuid4(), uuid4()
    dispatched: list[tuple[object, object]] = []

    async def retry(session, **kwargs):
        return KbImageRetrySchedule(
            asset_id=asset_id,
            run_id=run_id,
            lock_version=4,
        )

    async def dispatch(selected_run_id, org_id, background):
        dispatched.append((selected_run_id, org_id))
        return True

    monkeypatch.setattr(kb_media, "retry_image_asset", retry)
    monkeypatch.setattr(kb_media, "dispatch_kb_ingest_run", dispatch)
    response = client.post(
        f"/api/v1/kb/image-assets/{asset_id}/retry",
        json={"expected_lock_version": 3, "reason": "重新调用图片 provider"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "asset_id": str(asset_id),
        "run_id": str(run_id),
        "lock_version": 4,
        "status": "queued",
    }
    assert dispatched and dispatched[0][0] == run_id


def test_audit_endpoint_is_scoped_and_returns_only_safe_state(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, org_id = inventory_client
    asset_id = uuid4()
    captured: dict[str, object] = {}

    async def audit(session, **kwargs):
        captured.update(kwargs)
        return KbImageAssetAuditPage(
            items=[
                KbImageAssetAuditEventRead(
                    event_version=2,
                    action="edit_and_accept",
                    actor_kind="human",
                    actor_user_id=uuid4(),
                    reason="修正文案",
                    before_state=KbImageAssetAuditState(caption="旧描述"),
                    after_state=KbImageAssetAuditState(
                        caption="新描述",
                        review_status="accepted",
                        lock_version=2,
                    ),
                    created_at=datetime.now(UTC),
                )
            ],
            total=1,
            page=1,
            page_size=25,
        )

    monkeypatch.setattr(kb_media, "list_image_asset_audit", audit)
    response = client.get(
        f"/api/v1/kb/image-assets/{asset_id}/audit",
        params={"page": 1, "page_size": 25},
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["after_state"]["caption"] == "新描述"
    assert captured == {
        "request_org_id": org_id,
        "asset_id": asset_id,
        "page": 1,
        "page_size": 25,
    }
    assert "object_key" not in response.text
    assert "failure_detail" not in response.text
    assert "idempotency_key" not in response.text


def test_audit_endpoint_uses_uniform_cross_tenant_not_found(
    inventory_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = inventory_client

    async def unavailable(session, **kwargs):
        raise KbImageAssetUnavailableError("cross_tenant")

    monkeypatch.setattr(kb_media, "list_image_asset_audit", unavailable)
    response = client.get(f"/api/v1/kb/image-assets/{uuid4()}/audit")

    assert response.status_code == 404
    assert response.json() == {"detail": "图片资产不存在"}
    assert "cross_tenant" not in response.text


def test_review_mutations_require_kb_writer_role(
    inventory_client,
) -> None:
    client, org_id = inventory_client

    async def member_context() -> OrgContext:
        return OrgContext(user_id=uuid4(), org_id=org_id, role=Role.MEMBER)

    app.dependency_overrides[get_org_context] = member_context
    response = client.post(
        f"/api/v1/kb/image-assets/{uuid4()}/review",
        json={
            "action": "reject",
            "expected_lock_version": 1,
            "reason": "无权限",
        },
    )

    assert response.status_code == 403

    audit_response = client.get(f"/api/v1/kb/image-assets/{uuid4()}/audit")
    assert audit_response.status_code == 403
