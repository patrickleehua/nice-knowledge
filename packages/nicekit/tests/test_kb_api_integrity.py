import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from nicekit.api.deps import OrgContext
from nicekit.api.v1 import kb as kb_api
from nicekit.models.kb import KbChunk, KbPage, KnowledgeBase
from nicekit.models.tenancy import Role


def test_legacy_extraction_review_routes_are_removed() -> None:
    paths = {route.path for route in kb_api.router.routes}
    assert not any(path.startswith("/kb/extractions") for path in paths)
    assert "/kb/fact-claims" in paths
    assert "/kb/fact-claims/{claim_id}/review" in paths


def _context(org_id):
    return OrgContext(user_id=uuid4(), org_id=org_id, role=Role.ORG_ADMIN)


def _upload(filename: str, data: bytes = b"knowledge") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": "text/plain"}),
    )


def _session_with_get(row):
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    return session


@pytest.mark.parametrize(
    "filename",
    ["guide.txt", "guide.DOCX", "costs.xlsm", "slides.pptx", "scan.webp"],
)
def test_supported_document_formats_match_parser_contract(filename: str) -> None:
    assert kb_api._require_supported_document(filename) == filename


def test_unsupported_document_format_is_rejected_before_ingestion() -> None:
    with pytest.raises(HTTPException) as exc_info:
        kb_api._require_supported_document("archive.zip")

    assert exc_info.value.status_code == 415
    assert "不支持的文件类型" in exc_info.value.detail


@pytest.mark.parametrize(
    ("data", "expected_status"),
    [(b"", 400), (b"12345", 413)],
)
async def test_document_upload_rejects_empty_or_oversized_files(
    monkeypatch, data: bytes, expected_status: int
) -> None:
    upload = MagicMock()
    upload.read = AsyncMock(return_value=data)
    monkeypatch.setattr(
        kb_api,
        "get_settings",
        lambda: SimpleNamespace(kb_upload_max_file_bytes=4),
    )

    with pytest.raises(HTTPException) as exc_info:
        await kb_api._read_document_upload(upload)

    assert exc_info.value.status_code == expected_status
    upload.read.assert_awaited_once_with(5)


async def test_document_upload_accepts_exact_size_limit(monkeypatch) -> None:
    upload = MagicMock()
    upload.read = AsyncMock(return_value=b"1234")
    monkeypatch.setattr(
        kb_api,
        "get_settings",
        lambda: SimpleNamespace(kb_upload_max_file_bytes=4),
    )

    assert await kb_api._read_document_upload(upload) == b"1234"
    upload.read.assert_awaited_once_with(5)


def test_ingest_settings_expose_upload_contract(monkeypatch) -> None:
    monkeypatch.setattr(kb_api, "get_max_concurrency", lambda: 3)
    monkeypatch.setattr(kb_api, "get_active_count", lambda: 2)
    monkeypatch.setattr(
        kb_api,
        "get_settings",
        lambda: SimpleNamespace(
            kb_upload_max_file_bytes=1024,
            kb_upload_max_batch_files=7,
        ),
    )

    assert kb_api._ingest_settings_payload() == {
        "max_concurrency": 3,
        "active": 2,
        "upload_max_file_bytes": 1024,
        "upload_max_batch_files": 7,
    }


async def test_owned_wiki_page_is_locked_before_review(monkeypatch) -> None:
    org_id, kb_id = uuid4(), uuid4()
    page = KbPage(id=uuid4(), org_id=org_id, kb_id=kb_id, title="巴黎交通")
    result = MagicMock()
    result.scalar_one_or_none.return_value = page
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    require_owned = AsyncMock()
    monkeypatch.setattr(kb_api, "_require_own_kb", require_owned)
    ctx = _context(org_id)

    assert await kb_api._get_owned_page(session, ctx, page.id) is page

    statement = session.execute.await_args.args[0]
    assert "FOR UPDATE" in str(statement)
    require_owned.assert_awaited_once_with(session, ctx, kb_id)


async def test_snapshot_wiki_claim_is_locked_before_decision() -> None:
    org_id, kb_id, snapshot_id = uuid4(), uuid4(), uuid4()
    claim = kb_api.FactClaim(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        subject_type="source_document",
        subject_id=uuid4(),
        predicate="wiki_page",
        value_json={"title": "巴黎交通", "content_markdown": "草稿"},
        raw_payload={"title": "巴黎交通", "content_markdown": "草稿"},
        review_status="confirmed",
    )
    page = KbPage(
        id=kb_api.projection_row_id(snapshot_id, "wiki_page", claim.id),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=snapshot_id,
        title="巴黎交通",
    )
    ids_result = MagicMock()
    ids_result.scalars.return_value.all.return_value = [claim.id]
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = claim
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[ids_result, claim_result])
    session.add = MagicMock()

    await kb_api._record_snapshot_page_decision(session, page, "published")

    locked_statement = session.execute.await_args_list[1].args[0]
    assert "FOR UPDATE" in str(locked_statement)
    assert claim.corrected_payload["_wiki_publication_status"] == "published"
    session.add.assert_called_once_with(claim)


async def test_http_upload_only_stages_unclassified_revision(monkeypatch) -> None:
    org_id, kb_id = uuid4(), uuid4()
    kb = KnowledgeBase(id=kb_id, org_id=org_id, name="测试库", kb_type="general")
    session = _session_with_get(kb)
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested.return_value = transaction

    put_object = AsyncMock()
    record_usage = AsyncMock()
    monkeypatch.setattr(kb_api.storage, "put_object", put_object)
    monkeypatch.setattr(
        kb_api,
        "get_settings",
        lambda: SimpleNamespace(kb_upload_max_file_bytes=20 * 1024 * 1024),
    )
    monkeypatch.setattr(kb_api, "record_usage", record_usage)

    document = await kb_api.upload_document(
        file=_upload("hotel.txt"),
        kb_id=kb_id,
        ctx=_context(org_id),
        session=session,
    )

    assert document.doc_type == "unclassified"
    assert document.status == "staged"
    record_usage.assert_awaited_once_with(
        session,
        org_id=org_id,
        task="kb.upload",
        model="unclassified",
        quantity=len(b"knowledge"),
    )


@pytest.mark.parametrize("operation", ["patch", "delete"])
async def test_snapshot_managed_chunk_cannot_be_mutated(operation: str) -> None:
    org_id, kb_id = uuid4(), uuid4()
    chunk = KbChunk(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=uuid4(),
        content="published projection",
    )
    session = _session_with_get(chunk)

    with pytest.raises(HTTPException) as exc_info:
        if operation == "patch":
            await kb_api.update_chunk(
                chunk_id=chunk.id,
                body=kb_api.ChunkPatchBody(content="mutated"),
                ctx=_context(org_id),
                session=session,
            )
        else:
            await kb_api.delete_chunk(
                chunk_id=chunk.id,
                ctx=_context(org_id),
                session=session,
            )

    assert exc_info.value.status_code == 409
    assert "快照投影不可原地修改" in exc_info.value.detail
    assert chunk.content == "published projection"
    session.commit.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.parametrize("action", ["publish", "reject"])
async def test_snapshot_page_draft_requires_explicit_review_action(
    monkeypatch, action: str
) -> None:
    org_id, kb_id = uuid4(), uuid4()
    page = KbPage(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        snapshot_id=uuid4(),
        title="巴黎交通",
        content="人工正文",
        draft_content="AI 草稿",
        draft_status="pending_review",
        origin="human",
    )
    session = _session_with_get(page)
    monkeypatch.setattr(kb_api, "_require_own_kb", AsyncMock())
    monkeypatch.setattr(kb_api, "_get_owned_page", AsyncMock(return_value=page))
    record_decision = AsyncMock()
    monkeypatch.setattr(kb_api, "_record_snapshot_page_decision", record_decision)

    if action == "publish":
        await kb_api.publish_page_draft(page_id=page.id, ctx=_context(org_id), session=session)
        assert page.content == "AI 草稿"
    else:
        await kb_api.reject_page_draft(page_id=page.id, ctx=_context(org_id), session=session)
        assert page.content == "人工正文"

    assert page.draft_content is None and page.draft_status is None
    assert page.origin == "human"
    record_decision.assert_awaited_once_with(session, page, f"{action}ed")
    session.commit.assert_awaited_once()
