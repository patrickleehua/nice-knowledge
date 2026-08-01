from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import get_org_session
from nicekit.kb.status_board import collect_status_board

router = APIRouter(prefix="/kb")

PrimaryState = Literal[
    "blocked",
    "running",
    "queued",
    "release_ready",
    "review_required",
    "classification_required",
    "needs_attention",
    "ready",
    "unpublished",
    "empty",
]
AlertCode = Literal[
    "ingest_lease_expired",
    "ingest_heartbeat_stale",
    "ingest_queue_stalled",
    "ingest_processing_stalled",
    "snapshot_build_stalled",
    "document_failed",
    "snapshot_failed",
    "document_operation_failed",
]
AlertSeverity = Literal["info", "warning", "error"]
ReleaseState = Literal["unpublished", "building", "ready", "active", "failed"]


class StatusBoardAlertOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: AlertCode
    severity: AlertSeverity
    count: int = Field(ge=1)


class StatusBoardDocumentCountsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    ingested: int = Field(ge=0)
    remaining: int = Field(ge=0)
    staged: int = Field(ge=0)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    awaiting_review: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    paused: int = Field(ge=0)
    canceled: int = Field(ge=0)


class StatusBoardStageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1, max_length=50)
    document_count: int = Field(ge=1)
    done: int = Field(ge=0)
    total: int = Field(ge=0)


class StatusBoardReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_facts: int = Field(ge=0)
    orphaned_facts: int = Field(ge=0)
    images_needing_review: int = Field(ge=0)
    total: int = Field(ge=0)


class StatusBoardReleaseOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ReleaseState
    active_snapshot_id: UUID | None
    candidate_snapshot_id: UUID | None


class StatusBoardOperationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["withdrawal", "reingestion", "purge"]
    stage: str | None
    status: Literal["pending", "processing"]
    count: int = Field(ge=1)


class StatusBoardItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    primary_state: PrimaryState
    alerts: list[StatusBoardAlertOut]
    document_counts: StatusBoardDocumentCountsOut
    stages: list[StatusBoardStageOut]
    review: StatusBoardReviewOut
    release: StatusBoardReleaseOut
    operation: StatusBoardOperationOut | None
    latest_activity_at: datetime | None


class KnowledgeBaseStatusBoardOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    poll_after_ms: Literal[2_000, 15_000, 30_000]
    has_active_work: bool
    items: list[StatusBoardItemOut]


@router.get("/bases/status-board", response_model=KnowledgeBaseStatusBoardOut)
async def get_status_board(
    session: Annotated[AsyncSession, Depends(get_org_session)],
    lifecycle_status: Literal["active"] = Query(default="active"),
) -> KnowledgeBaseStatusBoardOut:
    board = await collect_status_board(
        session,
        lifecycle_status=lifecycle_status,
    )
    return KnowledgeBaseStatusBoardOut.model_validate(board)
