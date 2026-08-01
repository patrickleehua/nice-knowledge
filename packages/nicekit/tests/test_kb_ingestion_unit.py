"""Ingestion policy and staged artifact unit tests(P3a 搬运适配版)。

TF 原文件的 `test_cost_fact_claim_*` 断言 EvidenceSpan 的行号定位口径,
真正被测的是 `nicekit.kb.evidence_locator`(P3b 才搬)。这里改为用
证据定位替身验证 `_enqueue_fact_claim` 自身的职责:raw_payload 全量留存、
confidence/evidence_quote 不进 value_json、有效期解析、FactClaim↔EvidenceSpan 配对。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from _kb_p3b_stubs import install_evidence_locator_stub

from nicekit.kb import storage
from nicekit.kb.ingestion import (
    LLM_THROTTLE_ERROR,
    _enqueue_fact_claim,
    _is_llm_throttled,
    _load_staged_parse_artifacts,
)
from nicekit.llm.providers import ProviderError
from nicekit.llm.service import AllProvidersFailedError, LlmBudgetExceededError
from nicekit.models.kb import EvidenceSpan, FactClaim


def test_budget_exceeded_is_throttled() -> None:
    exc = LlmBudgetExceededError(uuid4(), used=10_000, budget=8_000)
    assert _is_llm_throttled(exc)


def test_provider_rate_limit_code_is_throttled() -> None:
    assert _is_llm_throttled(ProviderError("rate_limit", retryable=True))
    assert _is_llm_throttled(
        ProviderError("http_error", retryable=False, status_code=429)
    )


def test_all_providers_failed_with_rate_limit_is_throttled() -> None:
    exc = AllProvidersFailedError("kb.extract.generic", ["rate_limit;status=429"])
    assert _is_llm_throttled(exc)
    mixed = AllProvidersFailedError(
        "kb.extract.generic", ["timeout", "rate_limit;status=429"]
    )
    assert _is_llm_throttled(mixed)


def test_non_throttle_errors_not_matched() -> None:
    assert not _is_llm_throttled(
        AllProvidersFailedError("t", ["timeout", "connection_error"])
    )
    assert not _is_llm_throttled(
        ProviderError("http_error", retryable=True, status_code=500)
    )
    assert not _is_llm_throttled(ValueError("对象缺失"))


def test_throttle_error_text_is_user_facing() -> None:
    assert LLM_THROTTLE_ERROR == "LLM 限流,已自动排队重试"


def test_revision_staging_keys_are_attempt_scoped() -> None:
    org_id, kb_id, doc_id, revision_id = uuid4(), uuid4(), uuid4(), uuid4()
    old_markdown = storage.kb_revision_markdown_key(
        org_id, kb_id, doc_id, revision_id, "old-attempt"
    )
    new_markdown = storage.kb_revision_markdown_key(
        org_id, kb_id, doc_id, revision_id, "new-attempt"
    )
    old_structured = storage.kb_revision_structured_json_key(
        org_id, kb_id, doc_id, revision_id, "old-attempt"
    )
    new_structured = storage.kb_revision_structured_json_key(
        org_id, kb_id, doc_id, revision_id, "new-attempt"
    )
    old_chunks = storage.kb_revision_chunks_key(
        org_id, kb_id, doc_id, revision_id, "old-attempt"
    )
    new_chunks = storage.kb_revision_chunks_key(
        org_id, kb_id, doc_id, revision_id, "new-attempt"
    )
    assert old_markdown != new_markdown
    assert old_structured != new_structured
    assert old_structured.endswith("/old-attempt/document.json")
    assert new_structured.endswith("/new-attempt/document.json")
    assert old_chunks != new_chunks


async def test_staged_docling_parse_requires_structured_pointer() -> None:
    revision = SimpleNamespace(
        id=uuid4(), markdown_key="artifact.md", structured_json_key=None
    )
    parse_run = SimpleNamespace(stats={"parser_name": "docling"})

    with pytest.raises(RuntimeError, match="has no structured artifact"):
        await _load_staged_parse_artifacts(revision, parse_run)


async def test_staged_docling_parse_validates_json_before_markdown(monkeypatch) -> None:
    revision = SimpleNamespace(
        id=uuid4(), markdown_key="artifact.md", structured_json_key="artifact.json"
    )
    parse_run = SimpleNamespace(stats={"parser_name": "docling"})
    reads: list[str] = []

    async def fake_get_object(key: str) -> bytes:
        reads.append(key)
        if key == "artifact.json":
            return json.dumps({"schema_name": "DoclingDocument"}).encode()
        return b"# Derived Markdown"

    monkeypatch.setattr(storage, "get_object", fake_get_object)

    markdown = await _load_staged_parse_artifacts(revision, parse_run)

    assert markdown == "# Derived Markdown"
    assert reads == ["artifact.json", "artifact.md"]


async def test_staged_fast_parse_does_not_require_structured_artifact(monkeypatch) -> None:
    revision = SimpleNamespace(
        id=uuid4(), markdown_key="artifact.md", structured_json_key=None
    )
    parse_run = SimpleNamespace(stats={"parser_name": "fast"})

    async def fake_get_object(key: str) -> bytes:
        assert key == "artifact.md"
        return b"# Fast Markdown"

    monkeypatch.setattr(storage, "get_object", fake_get_object)

    assert await _load_staged_parse_artifacts(revision, parse_run) == "# Fast Markdown"


async def test_fact_claim_keeps_raw_payload_and_pairs_evidence_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_evidence_locator_stub(
        monkeypatch,
        locate=lambda *_args, **_kwargs: SimpleNamespace(
            page=None, start_line=9, end_line=9, cell_ref=None, quote_text="A-100 | 1200"
        ),
    )
    added: list = []
    session = SimpleNamespace(add=added.append, flush=AsyncMock())
    doc = SimpleNamespace(id=uuid4(), org_id=uuid4(), kb_id=uuid4())
    revision = SimpleNamespace(id=uuid4())
    payload = {
        "name": "A-100",
        "amount": 1200,
        "currency": "CNY",
        "notes": None,
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "confidence": 0.96,
        "evidence_quote": "A-100 | 1200",
    }

    await _enqueue_fact_claim(
        session,
        doc=doc,
        revision=revision,
        ingest_run_id=uuid4(),
        entity_type="component",
        payload=payload,
        segment_markdown="A-100 | 1200",
        line_offset=8,
        structured_document=None,
        model_name="test:model",
        prompt_version="kb.extract.generic:v1",
    )

    claim, evidence = added
    assert isinstance(claim, FactClaim)
    assert isinstance(evidence, EvidenceSpan)
    assert claim.predicate == "component"
    assert claim.raw_payload == payload
    assert "confidence" not in claim.value_json
    assert "evidence_quote" not in claim.value_json
    assert claim.valid_from.isoformat() == "2026-01-01"
    assert claim.valid_to.isoformat() == "2026-12-31"
    assert evidence.fact_claim_id == claim.id
    assert evidence.revision_id == revision.id
    assert (evidence.start_line, evidence.end_line) == (9, 9)
