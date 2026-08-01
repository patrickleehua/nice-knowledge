import { api } from "@/lib/api";

export const KB_STATUS_BOARD_QUERY_KEY = ["kb-status-board", "active"] as const;

export type KbPrimaryState =
  | "blocked"
  | "running"
  | "queued"
  | "release_ready"
  | "review_required"
  | "classification_required"
  | "needs_attention"
  | "ready"
  | "unpublished"
  | "empty";

export type KbStatusBoardAlertCode =
  | "ingest_lease_expired"
  | "ingest_heartbeat_stale"
  | "ingest_queue_stalled"
  | "ingest_processing_stalled"
  | "snapshot_build_stalled"
  | "document_failed"
  | "snapshot_failed"
  | "document_operation_failed";

export interface KbStatusBoardAlert {
  code: KbStatusBoardAlertCode;
  severity: "info" | "warning" | "error";
  count: number;
}

export interface KbStatusBoardDocumentCounts {
  total: number;
  ingested: number;
  remaining: number;
  staged: number;
  queued: number;
  running: number;
  awaiting_review: number;
  completed: number;
  failed: number;
  paused: number;
  canceled: number;
}

export interface KbStatusBoardStage {
  stage: string;
  document_count: number;
  done: number;
  total: number;
}

export interface KbStatusBoardReview {
  suggested_facts: number;
  orphaned_facts: number;
  images_needing_review: number;
  total: number;
}

export interface KbStatusBoardRelease {
  state: "unpublished" | "building" | "ready" | "active" | "failed";
  active_snapshot_id: string | null;
  candidate_snapshot_id: string | null;
}

export interface KbStatusBoardOperation {
  kind: "withdrawal" | "reingestion" | "purge";
  stage: string | null;
  status: "pending" | "processing";
  count: number;
}

export interface KbStatusBoardItem {
  kb_id: string;
  primary_state: KbPrimaryState;
  alerts: KbStatusBoardAlert[];
  document_counts: KbStatusBoardDocumentCounts;
  stages: KbStatusBoardStage[];
  review: KbStatusBoardReview;
  release: KbStatusBoardRelease;
  operation: KbStatusBoardOperation | null;
  latest_activity_at: string | null;
}

export interface KbStatusBoardResponse {
  generated_at: string;
  poll_after_ms: 2_000 | 15_000 | 30_000;
  has_active_work: boolean;
  items: KbStatusBoardItem[];
}

type StatusBoardQueryLike = {
  state: { data?: KbStatusBoardResponse };
};

export function kbStatusBoardRefetchInterval(
  query: StatusBoardQueryLike,
): number | false {
  return query.state.data?.poll_after_ms ?? false;
}

export const kbStatusBoardApi = {
  active: () =>
    api.get<KbStatusBoardResponse>(
      "/kb/bases/status-board?lifecycle_status=active",
    ),
};
