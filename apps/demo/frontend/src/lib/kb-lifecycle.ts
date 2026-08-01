import { ApiError, api } from "@/lib/api";
import type { KnowledgeBase } from "@/lib/types";

export type KnowledgeBaseLifecycleStatus =
  "active" | "archived" | "purge_pending" | "purged";

export type KnowledgeBaseDeletionAction =
  "archive" | "restore" | "empty_delete" | "purge";

export interface KnowledgeBaseDeletionBlocker {
  code: string;
  count: number;
  identifiers: string[];
  resolution_hint: string | null;
  /** 保留期等可重试 blocker 的解除时刻(ISO 字符串),其余 blocker 无此字段 */
  retry_at?: string | null;
}

export interface KnowledgeBaseProjectReference {
  id: string;
  name: string;
  scope_mode: "explicit" | "default";
}

export interface KnowledgeBaseLifecycleOperation {
  id: string;
  kb_id: string;
  operation_type: "purge";
  status: "pending" | "processing" | "failed" | "dead_letter" | "completed";
  phase:
    | "revalidate_and_quiesce"
    | "delete_objects"
    | "cleanup_metadata"
    | "finalize_audit_shell"
    | "completed";
  attempt_count: number;
  impact_summary: Record<string, number>;
  last_error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface KnowledgeBaseDeletionPreview {
  version: string;
  kb_id: string;
  kb_name: string;
  owner_org_id: string;
  lifecycle_status: KnowledgeBaseLifecycleStatus;
  consumption_epoch: number;
  complete: boolean;
  allowed_actions: KnowledgeBaseDeletionAction[];
  impact_counts: Record<string, number>;
  project_references: KnowledgeBaseProjectReference[];
  blockers: KnowledgeBaseDeletionBlocker[];
  plan_hash: string | null;
  expires_at: string | null;
  latest_operation: KnowledgeBaseLifecycleOperation | null;
}

// ---- 生命周期管理台聚合接口(/org/kb/lifecycle)契约 ----

/** 看板行内嵌的最近操作摘要(GET /kb/lifecycle/board items[].latest_operation) */
export interface KnowledgeBaseBoardOperation {
  id: string;
  status: "pending" | "processing" | "completed" | "failed" | "dead_letter";
  phase: string;
  requested_at: string;
  completed_at: string | null;
  last_error_message: string | null;
}

export interface KnowledgeBaseBoardItem {
  kb_id: string;
  name: string;
  lifecycle_status: KnowledgeBaseLifecycleStatus;
  archived_at: string | null;
  purged_at: string | null;
  retention_due_at: string | null;
  /** 保留期已到期(后端按 retention_days 计算,前端不重复推导) */
  purge_due: boolean;
  latest_operation: KnowledgeBaseBoardOperation | null;
}

export interface KnowledgeBaseLifecycleBoard {
  generated_at: string;
  purge_worker_enabled: boolean;
  retention_days: number;
  /** 已按到期优先排序 */
  items: KnowledgeBaseBoardItem[];
}

export type LifecycleQueueKind = "kb_purge" | "document";

/** 统一操作队列行(库清理 + 文档删除,GET /kb/lifecycle/operations) */
export interface LifecycleQueueItem {
  id: string;
  kind: LifecycleQueueKind;
  kb_id: string;
  kb_name: string;
  target: string;
  status: string;
  phase: string | null;
  requested_at: string;
  completed_at: string | null;
  last_error_message: string | null;
  retryable: boolean;
}

export interface LifecycleOperationQueue {
  purge_worker_enabled: boolean;
  items: LifecycleQueueItem[];
}

/** 保留期到期清理候选(GET /kb/lifecycle/purge-due) */
export interface PurgeDueItem {
  kb_id: string;
  name: string;
  archived_at: string;
  due_at: string;
  plan_hash: string | null;
  preview_complete: boolean;
  blockers: { code: string; count: number }[];
}

export interface PurgeDueList {
  items: PurgeDueItem[];
}

// ---- 孤儿数据巡检(/kb/integrity/orphans)契约 ----

export type OrphanCategoryKey =
  | "purged_document_chunks"
  | "purged_kb_residual_rows"
  | "purge_registry_stale_objects";

export const ORPHAN_CATEGORY_KEYS: OrphanCategoryKey[] = [
  "purged_document_chunks",
  "purged_kb_residual_rows",
  "purge_registry_stale_objects",
];

export interface OrphanCategoryReport {
  count: number;
  sample_ids: string[];
  /** 按表名细分的残留计数 */
  detail: Record<string, number>;
}

export interface OrphanAuditReport {
  generated_at: string;
  total: number;
  categories: Record<OrphanCategoryKey, OrphanCategoryReport>;
}

export interface OrphanCleanupCategoryResult {
  deleted_count: number;
  /** 按表名细分的删除计数 */
  detail: Record<string, number>;
  skipped: boolean;
  skip_reason: string | null;
}

export interface OrphanCleanupResult {
  executed_at: string;
  total_deleted: number;
  categories: Record<OrphanCategoryKey, OrphanCleanupCategoryResult>;
}

export interface ArchiveKnowledgeBaseRequest {
  expected_plan_hash: string;
  reason: string;
  confirm_project_unlinks: boolean;
}

export interface PurgeKnowledgeBaseRequest {
  expected_plan_hash: string;
  confirmation_name: string;
  reason: string;
  /** 管理员强制清理:跳过保留期与引用类拦截(法律保留/共享对象/活动任务仍拦) */
  force?: boolean;
}

/** 强制清理可跳过的 blocker(公开 code 口径,与后端 FORCE_BYPASSABLE_BLOCKER_CODES 对齐) */
export const FORCE_BYPASSABLE_BLOCKER_CODES = new Set([
  "RETENTION_PERIOD_ACTIVE",
  "HISTORICAL_RETRIEVAL_REFERENCE",
  "BUSINESS_ARTIFACT_REFERENCE",
  "KNOWLEDGE_SNAPSHOT_REFERENCE",
  "FEEDBACK_OR_CITATION_REFERENCE",
  "MANUAL_FACT_REFERENCE",
  "PINNED_ENTITY_REFERENCE",
]);

const LIFECYCLE_ERROR_MESSAGES: Record<string, string> = {
  forbidden_owner_action: "只有知识库所有者组织的管理员可以执行此操作",
  kb_not_empty: "该知识库已经产生资料或引用，请先归档",
  deletion_plan_stale: "删除影响已经变化，请重新预检后再次确认",
  deletion_inventory_incomplete: "删除清单不完整，当前不能安全执行",
  purge_blocked: "仍有受保护引用，当前不能永久清理",
  invalid_lifecycle_transition: "知识库当前状态不允许执行此操作",
};

export function kbLifecycleErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return (
      (error.code && LIFECYCLE_ERROR_MESSAGES[error.code]) || error.message
    );
  }
  return error instanceof Error ? error.message : "知识库生命周期操作失败";
}

export function isDeletionPlanStale(error: unknown): boolean {
  return error instanceof ApiError && error.code === "deletion_plan_stale";
}

/** 孤儿清理 409:巡检报告已过期(计数变化),需要重新巡检后再确认 */
export function isOrphanReportStale(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === "orphan_report_stale" || error.status === 409)
  );
}

export const kbLifecycleApi = {
  list: (status: "active" | "archived") =>
    api.get<KnowledgeBase[]>(
      status === "active" ? "/kb/bases" : "/kb/bases?lifecycle_status=archived",
    ),
  preview: (kbId: string) =>
    api.get<KnowledgeBaseDeletionPreview>(`/kb/bases/${kbId}/deletion-preview`),
  archive: (kbId: string, body: ArchiveKnowledgeBaseRequest) =>
    api.post<KnowledgeBase>(`/kb/bases/${kbId}/archive`, body),
  restore: (kbId: string) =>
    api.post<KnowledgeBase>(`/kb/bases/${kbId}/restore`),
  deleteEmpty: (kbId: string, planHash: string) =>
    api.deleteWithHeaders<void>(`/kb/bases/${kbId}`, {
      "If-Match": `"${planHash}"`,
    }),
  purge: (
    kbId: string,
    body: PurgeKnowledgeBaseRequest,
    idempotencyKey: string,
  ) =>
    api.postWithHeaders<KnowledgeBaseLifecycleOperation>(
      `/kb/bases/${kbId}/purge`,
      body,
      { "Idempotency-Key": idempotencyKey },
    ),
  operation: (operationId: string) =>
    api.get<KnowledgeBaseLifecycleOperation>(
      `/kb/lifecycle-operations/${operationId}`,
    ),
  retryOperation: (operationId: string) =>
    api.post<KnowledgeBaseLifecycleOperation>(
      `/kb/lifecycle-operations/${operationId}/retry`,
    ),
  // ---- 生命周期管理台聚合读 ----
  board: () => api.get<KnowledgeBaseLifecycleBoard>("/kb/lifecycle/board"),
  operationQueue: (limit = 50) =>
    api.get<LifecycleOperationQueue>(`/kb/lifecycle/operations?limit=${limit}`),
  purgeDue: () => api.get<PurgeDueList>("/kb/lifecycle/purge-due"),
  /** 文档删除操作重试(库清理重试走上面的 retryOperation) */
  retryDocumentOperation: (operationId: string) =>
    api.post<void>(`/kb/document-operations/${operationId}/retry`),
  // ---- 孤儿数据巡检与清理 ----
  orphanReport: () => api.get<OrphanAuditReport>("/kb/integrity/orphans"),
  /** expected_counts 必须取自当前巡检报告;不一致时后端返回 409 orphan_report_stale */
  cleanupOrphans: (expectedCounts: Record<OrphanCategoryKey, number>) =>
    api.post<OrphanCleanupResult>("/kb/integrity/orphans/cleanup", {
      confirm: true,
      expected_counts: expectedCounts,
    }),
};
