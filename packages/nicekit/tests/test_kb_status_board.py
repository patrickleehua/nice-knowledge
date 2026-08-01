from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nicekit.api.deps import get_org_session
from nicekit.api.v1 import kb_status
from nicekit.kb import status_board

STATUS_BOARD_URL = "/api/v1/kb/bases/status-board"


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _SequenceSession:
    def __init__(self, *results: list[SimpleNamespace]) -> None:
        self._results = list(results)
        self.statements: list[str] = []

    async def execute(self, statement) -> _Result:
        self.statements.append(str(statement))
        assert self._results, f"unexpected query: {statement}"
        return _Result(self._results.pop(0))


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        kb_health_processing_old_age_seconds=1_800,
        kb_health_ingest_lease_old_age_seconds=180,
        kb_health_uploaded_old_age_seconds=900,
        kb_health_snapshot_build_old_age_seconds=1_800,
    )


def _row(**values) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _empty_aggregate_results(
    knowledge_bases: list[SimpleNamespace],
) -> tuple[list[SimpleNamespace], ...]:
    return (knowledge_bases, [], [], [], [], [], [], [])


@pytest.fixture(autouse=True)
def fixed_status_settings(monkeypatch) -> None:
    monkeypatch.setattr(status_board, "get_settings", _settings)


async def test_multiple_kbs_are_aggregated_with_business_counts_and_priority() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    running_kb, ready_kb = uuid4(), uuid4()
    active_snapshot_id, candidate_snapshot_id = uuid4(), uuid4()
    session = _SequenceSession(
        [
            _row(id=running_kb, active_snapshot_id=None, created_at=now),
            _row(
                id=ready_kb,
                active_snapshot_id=active_snapshot_id,
                created_at=now,
            ),
        ],
        [
            _row(
                kb_id=running_kb,
                status="staged",
                progress_stage=None,
                document_count=1,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                status="uploaded",
                progress_stage=None,
                document_count=2,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                status="parsing",
                progress_stage="image",
                document_count=2,
                progress_done=6,
                progress_total=10,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                status="awaiting_review",
                progress_stage=None,
                document_count=3,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                status="completed",
                progress_stage=None,
                document_count=4,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                status="failed",
                progress_stage=None,
                document_count=1,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=ready_kb,
                status="completed",
                progress_stage=None,
                document_count=1,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
        ],
        [
            _row(
                kb_id=running_kb,
                expired_lease_count=0,
                stale_heartbeat_count=0,
                stale_queue_count=0,
                latest_activity_at=now,
            )
        ],
        [
            _row(
                kb_id=running_kb,
                stage="entity_extract",
                document_count=1,
                latest_activity_at=now,
            )
        ],
        [
            _row(
                kb_id=running_kb,
                review_status="suggested",
                claim_count=5,
                latest_activity_at=now,
            ),
            _row(
                kb_id=running_kb,
                review_status="orphaned",
                claim_count=2,
                latest_activity_at=now,
            ),
        ],
        [
            _row(
                kb_id=running_kb,
                image_count=3,
                latest_activity_at=now,
            )
        ],
        [
            _row(
                kb_id=running_kb,
                id=candidate_snapshot_id,
                status="ready",
                latest_activity_at=now,
                row_number=1,
                status_count=1,
            )
        ],
        [
            _row(
                kb_id=running_kb,
                operation_type="withdrawal",
                stage="delete_projections",
                status="processing",
                operation_count=2,
                latest_activity_at=now,
            )
        ],
    )

    board = await status_board.collect_status_board(session, now=now)

    assert len(session.statements) == status_board.STATUS_BOARD_QUERY_COUNT
    assert board["has_active_work"] is True
    assert board["poll_after_ms"] == 2_000
    assert [item["kb_id"] for item in board["items"]] == [running_kb, ready_kb]

    running = board["items"][0]
    assert running["document_counts"] == {
        "total": 13,
        "ingested": 7,
        "remaining": 6,
        "staged": 1,
        "queued": 2,
        "running": 2,
        "awaiting_review": 3,
        "completed": 4,
        "failed": 1,
        "paused": 0,
        "canceled": 0,
    }
    assert running["primary_state"] == "running"
    assert running["stages"] == [
        {
            "stage": "entity_extraction",
            "document_count": 1,
            "done": 0,
            "total": 0,
        },
        {
            "stage": "image_understanding",
            "document_count": 2,
            "done": 6,
            "total": 10,
        },
    ]
    assert running["review"] == {
        "suggested_facts": 5,
        "orphaned_facts": 2,
        "images_needing_review": 3,
        "total": 10,
    }
    assert running["release"] == {
        "state": "ready",
        "active_snapshot_id": None,
        "candidate_snapshot_id": candidate_snapshot_id,
    }
    assert running["operation"] == {
        "kind": "withdrawal",
        "stage": "delete_projections",
        "status": "processing",
        "count": 2,
    }
    assert running["alerts"] == [
        {"code": "document_failed", "severity": "warning", "count": 1}
    ]

    ready = board["items"][1]
    assert ready["primary_state"] == "ready"
    assert ready["release"]["state"] == "active"


async def test_ready_snapshot_remains_publishable_while_new_snapshot_builds() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    kb_id, ready_snapshot_id, building_snapshot_id = uuid4(), uuid4(), uuid4()
    session = _SequenceSession(
        [_row(id=kb_id, active_snapshot_id=None, created_at=now)],
        [],
        [],
        [],
        [],
        [],
        [
            _row(
                kb_id=kb_id,
                id=building_snapshot_id,
                status="building",
                latest_activity_at=now,
                row_number=1,
                status_count=1,
            ),
            _row(
                kb_id=kb_id,
                id=ready_snapshot_id,
                status="ready",
                latest_activity_at=now,
                row_number=1,
                status_count=1,
            ),
        ],
        [],
    )

    board = await status_board.collect_status_board(session, now=now)
    item = board["items"][0]

    assert item["primary_state"] == "running"
    assert item["release"] == {
        "state": "ready",
        "active_snapshot_id": None,
        "candidate_snapshot_id": ready_snapshot_id,
    }
    assert board["has_active_work"] is True
    assert board["poll_after_ms"] == 2_000


async def test_historical_snapshot_failures_are_superseded_by_active_release() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    kb_id, active_snapshot_id, failed_snapshot_id = uuid4(), uuid4(), uuid4()
    session = _SequenceSession(
        [
            _row(
                id=kb_id,
                active_snapshot_id=active_snapshot_id,
                created_at=now - timedelta(days=1),
            )
        ],
        [],
        [],
        [],
        [],
        [],
        [
            _row(
                kb_id=kb_id,
                id=failed_snapshot_id,
                status="failed",
                latest_activity_at=now - timedelta(hours=1),
                row_number=1,
                status_count=3,
            ),
            _row(
                kb_id=kb_id,
                id=active_snapshot_id,
                status="active",
                latest_activity_at=now,
                row_number=1,
                status_count=1,
            ),
        ],
        [],
    )

    board = await status_board.collect_status_board(session, now=now)
    item = board["items"][0]

    assert item["primary_state"] == "ready"
    assert item["release"] == {
        "state": "active",
        "active_snapshot_id": active_snapshot_id,
        "candidate_snapshot_id": None,
    }
    assert item["alerts"] == []
    assert board["has_active_work"] is False
    assert board["poll_after_ms"] == 30_000


async def test_newer_snapshot_failure_remains_current_after_active_release() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    kb_id, active_snapshot_id, failed_snapshot_id = uuid4(), uuid4(), uuid4()
    session = _SequenceSession(
        [
            _row(
                id=kb_id,
                active_snapshot_id=active_snapshot_id,
                created_at=now - timedelta(days=1),
            )
        ],
        [],
        [],
        [],
        [],
        [],
        [
            _row(
                kb_id=kb_id,
                id=active_snapshot_id,
                status="active",
                latest_activity_at=now - timedelta(hours=1),
                row_number=1,
                status_count=1,
            ),
            _row(
                kb_id=kb_id,
                id=failed_snapshot_id,
                status="failed",
                latest_activity_at=now,
                row_number=1,
                status_count=3,
            ),
        ],
        [],
    )

    board = await status_board.collect_status_board(session, now=now)
    item = board["items"][0]

    assert item["primary_state"] == "needs_attention"
    assert item["release"] == {
        "state": "failed",
        "active_snapshot_id": active_snapshot_id,
        "candidate_snapshot_id": failed_snapshot_id,
    }
    assert item["alerts"] == [
        {"code": "snapshot_failed", "severity": "warning", "count": 1}
    ]
    assert board["has_active_work"] is False
    assert board["poll_after_ms"] == 15_000


async def test_expired_root_lease_blocks_running_state_with_sanitized_alert() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    kb_id = uuid4()
    session = _SequenceSession(
        [_row(id=kb_id, active_snapshot_id=None, created_at=now)],
        [
            _row(
                kb_id=kb_id,
                status="parsing",
                progress_stage="parse",
                document_count=1,
                progress_done=0,
                progress_total=1,
                stalled_processing_count=0,
                latest_activity_at=now,
            )
        ],
        [
            _row(
                kb_id=kb_id,
                expired_lease_count=1,
                stale_heartbeat_count=0,
                stale_queue_count=0,
                latest_activity_at=now,
            )
        ],
        [],
        [],
        [],
        [],
        [],
    )

    board = await status_board.collect_status_board(session, now=now)
    item = board["items"][0]

    assert item["primary_state"] == "blocked"
    assert item["alerts"] == [
        {"code": "ingest_lease_expired", "severity": "error", "count": 1}
    ]
    assert set(item["alerts"][0]) == {"code", "severity", "count"}


async def test_visible_scope_is_authoritative_and_query_count_is_not_per_kb() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    visible_kb, hidden_kb = uuid4(), uuid4()
    session = _SequenceSession(
        [_row(id=visible_kb, active_snapshot_id=None, created_at=now)],
        [
            _row(
                kb_id=visible_kb,
                status="completed",
                progress_stage=None,
                document_count=1,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
            _row(
                kb_id=hidden_kb,
                status="failed",
                progress_stage=None,
                document_count=99,
                progress_done=0,
                progress_total=0,
                stalled_processing_count=0,
                latest_activity_at=now,
            ),
        ],
        [],
        [],
        [],
        [],
        [],
        [],
    )

    board = await status_board.collect_status_board(session, now=now)

    assert [item["kb_id"] for item in board["items"]] == [visible_kb]
    assert board["items"][0]["document_counts"]["total"] == 1
    assert len(session.statements) == status_board.STATUS_BOARD_QUERY_COUNT
    assert "knowledge_bases.lifecycle_status" in session.statements[0]
    assert all(
        "kb_id" in statement for statement in session.statements[1:]
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"blocked_count": 1}, "blocked"),
        ({"has_building_snapshot": True}, "running"),
        ({"counts": {"running": 1}}, "running"),
        ({"counts": {"queued": 1}}, "queued"),
        ({"release_state": "ready"}, "release_ready"),
        ({"review_total": 1}, "review_required"),
        ({"counts": {"staged": 1}}, "classification_required"),
        ({"failure_count": 1}, "needs_attention"),
        ({"active_snapshot_id": UUID(int=1)}, "ready"),
        ({"counts": {"total": 1}}, "unpublished"),
        ({}, "empty"),
    ],
)
def test_primary_state_priority(kwargs: dict, expected: str) -> None:
    counts = {
        "total": 0,
        "staged": 0,
        "queued": 0,
        "running": 0,
    }
    counts.update(kwargs.pop("counts", {}))
    values = {
        "blocked_count": 0,
        "counts": counts,
        "review_total": 0,
        "release_state": "unpublished",
        "has_building_snapshot": False,
        "has_running_operation": False,
        "has_queued_operation": False,
        "failure_count": 0,
        "active_snapshot_id": None,
    }
    values.update(kwargs)

    assert status_board._primary_state(**values) == expected


def test_status_board_endpoint_returns_exact_public_shape() -> None:
    now = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    kb_id = uuid4()
    session = _SequenceSession(
        *_empty_aggregate_results(
            [_row(id=kb_id, active_snapshot_id=None, created_at=now)]
        )
    )
    app = FastAPI()
    app.include_router(kb_status.router, prefix="/api/v1")

    async def session_override():
        yield session

    app.dependency_overrides[get_org_session] = session_override
    response = TestClient(app).get(STATUS_BOARD_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {
        "generated_at",
        "poll_after_ms",
        "has_active_work",
        "items",
    }
    assert payload["poll_after_ms"] == 30_000
    assert payload["has_active_work"] is False
    assert set(payload["items"][0]) == {
        "kb_id",
        "primary_state",
        "alerts",
        "document_counts",
        "stages",
        "review",
        "release",
        "operation",
        "latest_activity_at",
    }
    assert payload["items"][0]["kb_id"] == str(kb_id)
    assert payload["items"][0]["primary_state"] == "empty"


def test_status_board_rejects_non_active_lifecycle() -> None:
    app = FastAPI()
    app.include_router(kb_status.router, prefix="/api/v1")

    async def session_override():
        yield _SequenceSession()

    app.dependency_overrides[get_org_session] = session_override

    response = TestClient(app).get(
        STATUS_BOARD_URL,
        params={"lifecycle_status": "archived"},
    )

    assert response.status_code == 422
