from uuid import uuid4

import pytest

from nicekit.kb.snapshot import (
    SnapshotBuildContext,
    _materialize_snapshot_fact_supports,
)


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.execute_count = 0
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return _Result(self._rows if self.execute_count == 2 else ())

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        self.flush_count += 1


def _context(*, claim_ids, revision_id):
    return SnapshotBuildContext(
        snapshot_id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        revision_manifest=(
            {
                "doc_id": str(uuid4()),
                "revision_id": str(revision_id),
                "revision_no": 1,
                "sha256": "a" * 64,
            },
        ),
        fact_claim_ids=tuple(claim_ids),
        embedding_fingerprint={"provider": "test", "model": "test", "dim": 1536},
        config_manifest={"consumption_epoch": 0},
    )


@pytest.mark.asyncio
async def test_materialize_snapshot_fact_supports_is_deterministic() -> None:
    revision_id = uuid4()
    claim_id = uuid4()
    evidence_ids = (uuid4(), uuid4())
    doc_id = uuid4()
    context = _context(claim_ids=(claim_id,), revision_id=revision_id)
    rows = [
        (claim_id, evidence_id, revision_id, doc_id)
        for evidence_id in evidence_ids
    ]

    first = _Session(rows)
    second = _Session(rows)
    assert await _materialize_snapshot_fact_supports(first, context) == 2
    assert await _materialize_snapshot_fact_supports(second, context) == 2

    assert first.execute_count == 2
    assert first.flush_count == 1
    assert [row.id for row in first.added] == [row.id for row in second.added]
    assert {
        (
            row.snapshot_id,
            row.fact_claim_id,
            row.evidence_span_id,
            row.revision_id,
            row.doc_id,
        )
        for row in first.added
    } == {
        (
            context.snapshot_id,
            claim_id,
            evidence_id,
            revision_id,
            doc_id,
        )
        for evidence_id in evidence_ids
    }


@pytest.mark.asyncio
async def test_materialize_snapshot_fact_supports_clears_empty_projection() -> None:
    context = _context(claim_ids=(), revision_id=uuid4())
    session = _Session()

    assert await _materialize_snapshot_fact_supports(session, context) == 0
    assert session.execute_count == 1
    assert session.added == []
    assert session.flush_count == 0
