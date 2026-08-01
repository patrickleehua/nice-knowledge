"""Safe image-enrichment readiness and ingest-profile gating contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.api.v1 import kb
from nicekit.domain.kb_media import KbImageEnrichmentReadinessRead
from nicekit.models.kb import KnowledgeBase
from nicekit.models.llm_provider import LlmProvider
from nicekit.models.tenancy import Role

# TF 经验:不 import 组装好的 app(也不用 with TestClient(app)——会触发
# lifespan 卡死)。轻量 FastAPI 只挂本用例需要的 router,依赖全部由
# dependency_overrides 假注入。
app = FastAPI()
app.include_router(kb.router, prefix="/api/v1")


def _readiness(*, ready: bool, code: str) -> KbImageEnrichmentReadinessRead:
    return KbImageEnrichmentReadinessRead(
        enabled=True,
        ready=ready,
        code=code,
        ocr_provider="docling",
        ocr_model="rapidocr:onnxruntime",
        caption_provider="openai",
        caption_model="configured-vlm",
        config_fingerprint="a" * 64,
    )


@pytest.fixture
def settings_client(monkeypatch: pytest.MonkeyPatch):
    org_id, user_id = uuid4(), uuid4()
    selected_kb = KnowledgeBase(
        id=uuid4(),
        org_id=org_id,
        name="媒体知识库",
        kb_type="mixed",
        created_by=user_id,
        created_at=datetime.now(UTC),
    )

    class FakeSession:
        def add(self, value) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, value) -> None:
            return None

        async def get(self, model, row_id):
            if model is KnowledgeBase and row_id == selected_kb.id:
                return selected_kb
            return None

    async def context_override() -> OrgContext:
        return OrgContext(user_id=user_id, org_id=org_id, role=Role.ORG_ADMIN)

    async def session_override():
        yield FakeSession()

    async def require_own(session, ctx, kb_id):
        assert kb_id == selected_kb.id
        assert ctx.org_id == org_id
        return selected_kb

    monkeypatch.setattr(kb, "_require_own_kb", require_own)
    app.dependency_overrides[get_org_context] = context_override
    app.dependency_overrides[get_org_session] = session_override
    try:
        # 不进 TestClient 上下文管理器:那会触发 app lifespan(连库 + 起常驻后台任务),
        # 本用例的依赖全部由 dependency_overrides 假注入,不需要真实基础设施。
        # 同目录 test_kb_image_media_api.py 是同一写法。
        client = TestClient(app)
        yield client, selected_kb
    finally:
        app.dependency_overrides.pop(get_org_context, None)
        app.dependency_overrides.pop(get_org_session, None)


def test_readiness_endpoint_returns_selected_non_secret_configuration(
    settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = settings_client

    async def readiness(*_args, **_kwargs):
        return _readiness(ready=True, code="ready")

    monkeypatch.setattr(
        kb,
        "_image_enrichment_readiness",
        readiness,
    )

    response = client.get("/api/v1/kb/image-enrichment/readiness")

    assert response.status_code == 200, response.text
    assert response.json()["caption_provider"] == "openai"
    assert response.json()["caption_model"] == "configured-vlm"
    assert response.json()["acceptance_policy"] == "human_review_required"
    for secret_name in ("api_key", "credential", "base_url", "endpoint"):
        assert secret_name not in response.text.lower()


def test_readiness_endpoint_resolves_kb_override(
    settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, selected_kb = settings_client
    selected_kb.ingest_profile = {
        **kb.IngestProfile().model_dump(),
        "caption_provider": "vllm",
        "caption_model": "gpt-5.6",
    }

    async def readiness(_session, profile):
        assert profile.caption_provider == "vllm"
        assert profile.caption_model == "gpt-5.6"
        return KbImageEnrichmentReadinessRead.model_validate(
            {
                **_readiness(ready=True, code="ready").model_dump(),
                "selection_source": "kb_override",
                "capability_source": "registry",
                "registry_revision": "2026-07-25.1",
            }
        )

    monkeypatch.setattr(kb, "_image_enrichment_readiness", readiness)
    response = client.get(
        "/api/v1/kb/image-enrichment/readiness",
        params={"kb_id": str(selected_kb.id)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["selection_source"] == "kb_override"
    assert response.json()["capability_source"] == "registry"


def test_saving_enabled_caption_profile_is_blocked_until_ready(
    settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, selected_kb = settings_client

    async def readiness(*_args, **_kwargs):
        return _readiness(
            ready=False,
            code="caption_provider_credentials_missing",
        )

    monkeypatch.setattr(
        kb,
        "_image_enrichment_readiness",
        readiness,
    )
    body = {
        "ingest_profile": {
            "parser": "docling",
            "chunk_strategy": "structure",
            "chunk_max_chars": 1200,
            "chunk_overlap_chars": 150,
            "table_mode": "row",
            "parent_child": False,
            "caption_images": True,
            "auto_wiki": True,
        }
    }

    response = client.patch(f"/api/v1/kb/bases/{selected_kb.id}", json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "caption_provider_credentials_missing",
        "message": "图片视觉描述配置尚未就绪",
    }
    assert selected_kb.ingest_profile is None


def test_saving_enabled_caption_profile_persists_exact_visual_selection(
    settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, selected_kb = settings_client

    async def readiness(_session, profile):
        assert profile.caption_provider == "vllm"
        assert profile.caption_model == "gpt-5.6"
        return _readiness(ready=True, code="ready")

    monkeypatch.setattr(kb, "_image_enrichment_readiness", readiness)
    profile = {
        **kb.IngestProfile().model_dump(),
        "caption_provider": "vllm",
        "caption_model": "gpt-5.6",
    }

    response = client.patch(
        f"/api/v1/kb/bases/{selected_kb.id}",
        json={"ingest_profile": profile},
    )

    assert response.status_code == 200, response.text
    assert response.json()["ingest_profile"]["caption_provider"] == "vllm"
    assert response.json()["ingest_profile"]["caption_model"] == "gpt-5.6"


def test_saving_caption_disabled_profile_does_not_require_provider(
    settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, selected_kb = settings_client

    async def readiness(*_args, **_kwargs):
        pytest.fail("disabled caption must not evaluate provider readiness")

    monkeypatch.setattr(
        kb,
        "_image_enrichment_readiness",
        readiness,
    )
    body = {
        "ingest_profile": {
            "parser": "fast",
            "chunk_strategy": "fixed",
            "chunk_max_chars": 900,
            "chunk_overlap_chars": 100,
            "table_mode": "row",
            "parent_child": False,
            "caption_images": False,
            "auto_wiki": False,
        }
    }

    response = client.patch(f"/api/v1/kb/bases/{selected_kb.id}", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["ingest_profile"]["caption_images"] is False


async def test_effective_readiness_requires_catalog_visual_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LlmProvider(
        name="vllm",
        protocol="openai",
        models=["text-only"],
        model_metadata={
            "text-only": {
                "capabilities": ["generation"],
                "input_modalities": ["text"],
                "capability_source": "manual",
                "registry_revision": None,
                "provider_metadata": {},
            }
        },
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [provider]

    class Session:
        async def execute(self, _statement):
            return Result()

    class Service:
        def readiness(self, selection):
            assert selection.provider == "vllm"
            assert selection.model == "text-only"
            return SimpleNamespace(
                ready=True,
                code="ready",
                ocr_provider="docling",
                ocr_model="rapidocr",
                caption_provider=selection.provider,
                caption_model=selection.model,
                config_fingerprint="a" * 64,
            )

    monkeypatch.setattr(kb, "get_kb_image_enrichment_service", Service)
    profile = kb.IngestProfile(
        caption_provider="vllm",
        caption_model="text-only",
    )

    readiness = await kb._image_enrichment_readiness(Session(), profile)

    assert readiness.ready is False
    assert readiness.code == "caption_model_ineligible"
    assert readiness.selection_source == "kb_override"
