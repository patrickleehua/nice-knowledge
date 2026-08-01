from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from nicekit.api.deps import OrgContext
from nicekit.api.v1 import kb as kb_api
from nicekit.kb.ingestion import enqueue_document_ingestion
from nicekit.models.kb import (
    DocStatus,
    DocType,
    DocumentRevision,
    IngestRun,
    IngestRunStatus,
    RevisionStatus,
    SourceDocument,
)
from nicekit.models.tenancy import Role

#: 宿主注册的实体类型 key。MIGRATION-PLAN B3:DocType 只剩 unclassified/general
#: 两个内置档,其余取值一律来自 kb_entity_types 注册表,SDK 不预设行业词表。
_CUSTOM_TYPE = "equipment"
_OTHER_CUSTOM_TYPE = "contract"


def _allow_custom_doc_types(monkeypatch) -> None:
    """让 _resolve_doc_type 直接回显请求的类型 key(等价于该类型已注册)。"""

    async def _echo(session, org_id, doc_type: str) -> str:
        return doc_type

    monkeypatch.setattr(kb_api, "_resolve_doc_type", _echo)


def _document(
    *,
    doc_type: str = DocType.UNCLASSIFIED.value,
    status: DocStatus = DocStatus.STAGED,
) -> SourceDocument:
    return SourceDocument(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        filename="source.docx",
        object_key="source/original.docx",
        sha256="c" * 64,
        doc_type=doc_type,
        status=status,
        rel_path="制度/结算",
    )


def _revision(document: SourceDocument) -> DocumentRevision:
    return DocumentRevision(
        id=uuid4(),
        org_id=document.org_id,
        kb_id=document.kb_id,
        doc_id=document.id,
        revision_no=1,
        sha256=document.sha256,
        original_object_key=document.object_key,
        status=RevisionStatus.UPLOADED,
    )


def _run(
    document: SourceDocument,
    revision: DocumentRevision,
    run_status: str = IngestRunStatus.QUEUED.value,
) -> IngestRun:
    return IngestRun(
        id=uuid4(),
        org_id=document.org_id,
        kb_id=document.kb_id,
        revision_id=revision.id,
        stage="document",
        segment_no=0,
        status=run_status,
        idempotency_key=f"{revision.id}:document:0",
    )


def _ctx(org_id):
    return OrgContext(user_id=uuid4(), org_id=org_id, role=Role.ORG_ADMIN)


def _session(*scalar_results):
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=scalar_results)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _allow_kb(monkeypatch, document: SourceDocument) -> AsyncMock:
    require_kb = AsyncMock(return_value=SimpleNamespace(id=document.kb_id))
    monkeypatch.setattr(kb_api, "_require_own_kb", require_kb)
    return require_kb


async def test_unclassified_document_cannot_be_queued_directly() -> None:
    document = _document()
    session = MagicMock()

    with pytest.raises(ValueError, match="classified"):
        await enqueue_document_ingestion(session, document)

    session.execute.assert_not_called()


def test_staged_source_is_not_retryable_cancelable_or_pausable() -> None:
    document = _document(doc_type=DocType.GENERAL.value)

    assert kb_api._is_retryable(document) is False
    assert DocStatus.STAGED not in kb_api._CANCELABLE
    assert DocStatus.STAGED not in kb_api._PAUSABLE


async def test_classification_is_persisted_without_enqueue_or_dispatch(
    monkeypatch,
) -> None:
    document = _document()
    session = _session(document)
    _allow_kb(monkeypatch, document)
    _allow_custom_doc_types(monkeypatch)
    enqueue = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(kb_api, "enqueue_document_ingestion", enqueue)
    monkeypatch.setattr(kb_api, "dispatch_kb_ingest_run", dispatch)

    result = await kb_api.classify_documents(
        body=kb_api.DocumentClassificationsBody(
            items=[
                {
                    "document_id": document.id,
                    "doc_type": _CUSTOM_TYPE,
                }
            ]
        ),
        ctx=_ctx(document.org_id),
        session=session,
    )

    assert result.skipped == []
    assert result.updated[0].document_id == document.id
    assert result.updated[0].doc_type == _CUSTOM_TYPE
    assert result.updated[0].idempotent is False
    assert document.doc_type == _CUSTOM_TYPE
    assert document.status == DocStatus.STAGED
    assert document.rel_path == "制度/结算"
    session.commit.assert_awaited_once()
    enqueue.assert_not_awaited()
    dispatch.assert_not_awaited()


async def test_classification_supports_multiple_groups_and_idempotent_refresh(
    monkeypatch,
) -> None:
    first = _document()
    second = _document(doc_type=DocType.GENERAL.value)
    second.org_id = first.org_id
    second.kb_id = first.kb_id
    session = _session(first, second)
    _allow_kb(monkeypatch, first)
    _allow_custom_doc_types(monkeypatch)

    result = await kb_api.classify_documents(
        body=kb_api.DocumentClassificationsBody(
            items=[
                {
                    "document_id": first.id,
                    "doc_type": _CUSTOM_TYPE,
                },
                {
                    "document_id": second.id,
                    "doc_type": DocType.GENERAL.value,
                },
            ]
        ),
        ctx=_ctx(first.org_id),
        session=session,
    )

    assert [item.doc_type for item in result.updated] == [
        _CUSTOM_TYPE,
        DocType.GENERAL.value,
    ]
    assert [item.idempotent for item in result.updated] == [False, True]
    assert first.doc_type == _CUSTOM_TYPE
    assert second.doc_type == DocType.GENERAL.value


async def test_classification_returns_stable_skips_per_item(monkeypatch) -> None:
    document = _document(status=DocStatus.COMPLETED)
    session = _session(document)
    _allow_kb(monkeypatch, document)
    _allow_custom_doc_types(monkeypatch)

    result = await kb_api.classify_documents(
        body=kb_api.DocumentClassificationsBody(
            items=[
                {
                    "document_id": document.id,
                    "doc_type": _CUSTOM_TYPE,
                },
                {
                    "document_id": document.id,
                    "doc_type": _OTHER_CUSTOM_TYPE,
                },
            ]
        ),
        ctx=_ctx(document.org_id),
        session=session,
    )

    assert result.updated == []
    assert [item.code for item in result.skipped] == [
        "document_not_staged",
        "duplicate_document",
    ]
    assert document.doc_type == DocType.UNCLASSIFIED.value


async def test_unknown_registered_type_is_rejected_without_mutation(
    monkeypatch,
) -> None:
    document = _document()
    session = _session()
    resolve = AsyncMock(
        side_effect=HTTPException(
            status_code=422,
            detail="unknown type",
        )
    )
    monkeypatch.setattr(kb_api, "_resolve_doc_type", resolve)

    result = await kb_api.classify_documents(
        body=kb_api.DocumentClassificationsBody(
            items=[{"document_id": document.id, "doc_type": "disabled_type"}]
        ),
        ctx=_ctx(document.org_id),
        session=session,
    )

    assert result.updated == []
    assert result.skipped[0].code == "invalid_doc_type"
    assert document.doc_type == DocType.UNCLASSIFIED.value


async def test_explicit_queue_commits_before_dispatch(monkeypatch) -> None:
    document = _document(doc_type=_CUSTOM_TYPE)
    revision = _revision(document)
    run = _run(document, revision)
    session = _session(document, revision, None)
    events: list[str] = []
    session.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    _allow_kb(monkeypatch, document)
    _allow_custom_doc_types(monkeypatch)
    enqueue = AsyncMock(return_value=(revision, run))
    dispatch = AsyncMock(side_effect=lambda *_args: events.append("dispatch"))
    monkeypatch.setattr(kb_api, "enqueue_document_ingestion", enqueue)
    monkeypatch.setattr(kb_api, "dispatch_kb_ingest_run", dispatch)

    result = await kb_api.enqueue_documents(
        body=kb_api.DocumentIngestionQueueBody(items=[{"document_id": document.id}]),
        background=BackgroundTasks(),
        ctx=_ctx(document.org_id),
        session=session,
    )

    assert result.skipped == []
    assert result.queued[0].document_id == document.id
    assert result.queued[0].idempotent is False
    assert result.queued[0].run_status == IngestRunStatus.QUEUED.value
    assert document.status == DocStatus.UPLOADED
    assert events == ["commit", "dispatch"]
    enqueue.assert_awaited_once_with(session, document, revision=revision)


async def test_queue_skips_unclassified_item_but_queues_valid_item(
    monkeypatch,
) -> None:
    unclassified = _document()
    classified = _document(doc_type=DocType.GENERAL.value)
    classified.org_id = unclassified.org_id
    classified.kb_id = unclassified.kb_id
    revision = _revision(classified)
    run = _run(classified, revision)
    session = _session(unclassified, classified, revision, None)
    _allow_kb(monkeypatch, classified)
    _allow_custom_doc_types(monkeypatch)
    enqueue = AsyncMock(return_value=(revision, run))
    dispatch = AsyncMock()
    monkeypatch.setattr(kb_api, "enqueue_document_ingestion", enqueue)
    monkeypatch.setattr(kb_api, "dispatch_kb_ingest_run", dispatch)

    result = await kb_api.enqueue_documents(
        body=kb_api.DocumentIngestionQueueBody(
            items=[
                {"document_id": unclassified.id},
                {"document_id": classified.id},
            ]
        ),
        background=BackgroundTasks(),
        ctx=_ctx(classified.org_id),
        session=session,
    )

    assert [item.code for item in result.skipped] == ["classification_required"]
    assert [item.document_id for item in result.queued] == [classified.id]
    assert unclassified.status == DocStatus.STAGED
    assert classified.status == DocStatus.UPLOADED
    dispatch.assert_awaited_once()


async def test_repeated_queue_returns_existing_root_without_dispatch(
    monkeypatch,
) -> None:
    document = _document(
        doc_type=_CUSTOM_TYPE,
        status=DocStatus.UPLOADED,
    )
    revision = _revision(document)
    run = _run(document, revision, IngestRunStatus.RUNNING.value)
    session = _session(document, revision, run)
    _allow_kb(monkeypatch, document)
    _allow_custom_doc_types(monkeypatch)
    enqueue = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(kb_api, "enqueue_document_ingestion", enqueue)
    monkeypatch.setattr(kb_api, "dispatch_kb_ingest_run", dispatch)

    result = await kb_api.enqueue_documents(
        body=kb_api.DocumentIngestionQueueBody(items=[{"document_id": document.id}]),
        background=BackgroundTasks(),
        ctx=_ctx(document.org_id),
        session=session,
    )

    assert result.skipped == []
    assert result.queued[0].run_id == run.id
    assert result.queued[0].idempotent is True
    assert result.queued[0].reason_code == "already_queued"
    enqueue.assert_not_awaited()
    dispatch.assert_not_awaited()


async def test_cross_tenant_or_missing_document_is_not_disclosed(monkeypatch) -> None:
    document_id = uuid4()
    session = _session(None)
    require_kb = AsyncMock()
    monkeypatch.setattr(kb_api, "_require_own_kb", require_kb)

    result = await kb_api.enqueue_documents(
        body=kb_api.DocumentIngestionQueueBody(items=[{"document_id": document_id}]),
        background=BackgroundTasks(),
        ctx=_ctx(uuid4()),
        session=session,
    )

    assert result.queued == []
    assert result.skipped[0].document_id == document_id
    assert result.skipped[0].code == "document_not_found"
    require_kb.assert_not_awaited()
