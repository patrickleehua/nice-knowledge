// KB 子系统的前端 DTO(与 nicekit/api/v1/kb*.py 一一对应)。
//
// SDK 化改造(MIGRATION-PLAN §5.8 / B1-B3):
// - 五张旅游专表(Destination/HotelEntry/CostEntry/PoiEntry/RouteEntry)与
//   RouteKnowledgeDiagnostics 已整体删除 —— 通用实体统一走 EntityType +
//   GenericEntity(attributes JSONB + 类型注册表驱动的 filterable_fields);
// - `kb_type` 后端已是自由 tag,`DocType` 只剩 unclassified/general +
//   任意已注册实体类型 key,因此预设表只作"中性示例",不构成穷举。

// 与后端 Pydantic 契约对齐的 DTO(architecture.md §10)

// KB 的图片增强就绪度会回显模型能力来源,该枚举属于模型层(admin-types)
import type { ModelCapabilitySource } from "@/lib/admin-types";

export interface KnowledgeBase {
  id: string;
  org_id: string;
  name: string;
  kb_type: string;
  description: string | null;
  /** per-KB 摄入切片配置(KB-2);null = 用后端默认值 */
  ingest_profile: IngestProfile | null;
  /** Wiki 页面树的团队共享稳定键顺序；未记录的新增页按接口默认顺序追加。 */
  wiki_navigation?: { page_order: string[] } | null;
  /** M3a:非空表示库处于快照管理,实体写操作走摄入→审核→投影链路(可选,旧后端无此字段) */
  active_snapshot_id?: string | null;
  lifecycle_status?: "active" | "archived" | "purge_pending" | "purged";
  consumption_epoch?: number;
  archived_at?: string | null;
  purge_requested_at?: string | null;
  purged_at?: string | null;
  created_at: string;
}

// ---- M3a KB 通用化:实体类型注册表与通用实体 ---------------------------------

/** 实体类型的可过滤字段声明 */
export interface FilterableField {
  field: string;
  type: "text" | "number" | "date";
  label?: string | null;
}

/** GET /kb/entity-types 条目(EntityTypeOut) */
export interface EntityType {
  id: string;
  type_key: string;
  display_name: string;
  description: string | null;
  /** JSON Schema(type=object,必含必填字符串属性 name) */
  field_schema: Record<string, unknown>;
  filterable_fields: FilterableField[];
  card_template: string | null;
  review_policy: "auto" | "ai" | "human";
  is_builtin: boolean;
  is_own: boolean;
}

/** GET /kb/bases/{kb_id}/entities 条目(EntityOut) */
export interface GenericEntity {
  id: string;
  kb_id: string;
  entity_type_key: string;
  name: string;
  attributes: Record<string, unknown>;
  snapshot_id: string | null;
  created_at: string;
}

/** 摄入切片配置(与后端 domain.kb.IngestProfile 对齐) */
export interface IngestProfile {
  parser: "fast" | "docling";
  chunk_strategy: "structure" | "fixed";
  chunk_max_chars: number;
  chunk_overlap_chars: number;
  table_mode: "row" | "whole";
  parent_child: boolean;
  caption_images: boolean;
  /** Both null/omitted means use the governed platform caption default. */
  caption_provider?: string | null;
  caption_model?: string | null;
  auto_wiki: boolean;
}

export interface IngestProfilePresets {
  default: IngestProfile;
  presets: Record<string, { label: string; profile: IngestProfile }>;
}

export interface KbShare {
  id: string;
  kb_id: string;
  grantee_org_id: string;
  /** 被授权组织的可读标识;组织已删除时为 null,不编造占位名 */
  grantee_org_name: string | null;
  grantee_org_slug: string | null;
  created_at: string;
}

export interface SourceDocument {
  id: string;
  kb_id: string;
  filename: string;
  doc_type: string;
  status:
    | "staged"
    | "uploaded"
    | "parsing"
    | "awaiting_review"
    | "completed"
    | "failed"
    | "canceled"
    | "paused";
  lifecycle_status:
    | "active"
    | "withdrawal_pending"
    | "withdrawn"
    | "reingestion_pending"
    | "purge_pending"
    | "purged";
  legal_hold_active: boolean;
  legal_hold_at: string | null;
  latest_operation?: KbDocumentOperation | null;
  /** 最近一次基于不可变修订产物发起的结构化重新抽取。 */
  latest_reclassification?: DocumentReclassification | null;
  error: string | null;
  rel_path: string | null;
  progress: number;
  /** 当前摄入阶段(parse/image/chunk/extract);非解析中为 null */
  progress_stage: string | null;
  /** 当前阶段内的已完成数与总数,如图片理解的 76/244 */
  progress_done: number;
  progress_total: number;
  expires_at: string | null;
  /** 统一 Markdown 中间表示产物 key(KB-1);解析前为 null */
  markdown_key: string | null;
  /** 实际完成解析的后端(fast/docling) */
  parser_name: string | null;
  created_at: string;
}

export interface DocumentReclassification {
  run_id: string;
  revision_id: string;
  previous_doc_type: string;
  target_doc_type: string;
  status: "queued" | "running" | "staged" | "succeeded" | "failed" | "canceled";
  error: string | null;
  retryable: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export type DocumentReclassificationAccepted = DocumentReclassification;

export interface DocumentClassificationItem {
  document_id: string;
  doc_type: string;
}

export interface DocumentClassificationResult {
  updated: Array<{
    document_id: string;
    doc_type: string;
    idempotent: boolean;
  }>;
  skipped: Array<{
    document_id: string;
    code: string;
    reason: string;
  }>;
}

export interface DocumentIngestionQueueItem {
  document_id: string;
}

export interface DocumentIngestionQueueResult {
  queued: Array<{
    document_id: string;
    doc_type: string;
    rel_path: string | null;
    revision_id: string;
    run_id: string;
    run_status: string;
    idempotent: boolean;
    reason_code: "already_queued" | null;
  }>;
  skipped: Array<{
    document_id: string;
    code: string;
    reason: string;
  }>;
}

export interface KbDocumentOperation {
  id: string;
  document_id: string;
  operation_type: "withdrawal" | "reingestion" | "purge";
  status: "pending" | "processing" | "completed" | "failed" | "dead_letter";
  phase: string | null;
  requested_revision_id: string | null;
  target_snapshot_id: string | null;
  attempts: number;
  reason: string;
  impact_summary: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface DocumentWithdrawalImpact {
  document_id: string;
  revision_count: number;
  chunk_count: number;
  image_count: number;
  exclusive_fact_count: number;
  shared_fact_count: number;
  orphaned_fact_count: number;
  exclusive_entity_count: number;
  shared_entity_count: number;
  exclusive_relation_count: number;
  shared_relation_count: number;
}

export interface DocumentPurgeBlocker {
  code: string;
  count: number;
  retry_at?: string | null;
}

export interface DocumentPurgePreview {
  version: string;
  document_id: string;
  plan_hash: string;
  eligible: boolean;
  blockers: DocumentPurgeBlocker[];
  delete_counts: Record<string, number>;
  retain_counts: Record<string, number>;
  retention_deadline: string | null;
}

/** GET /kb/documents/{id}/markdown 响应 */
export interface DocumentMarkdown {
  document_id: string;
  markdown: string;
  parser: string | null;
}

/** GET /kb/documents/{id}/chunks 条目(带溯源锚点,行号为 markdown 全文 1-based) */
export interface KbChunk {
  id: string;
  chunk_index: number | null;
  content: string;
  heading_path: string | null;
  start_line: number | null;
  end_line: number | null;
  page: number | null;
  has_embedding: boolean;
}

export interface IngestSettings {
  max_concurrency: number;
  active: number;
  upload_max_file_bytes: number;
  upload_max_batch_files: number;
}

// ---- 摄入耗时(GET /kb/documents/{id}/ingestion-status,GET /kb/bases/{id}/ingest-stats) ----

/** 图片富化阶段的整体状态(每张图一条 run,这里是折叠后的汇总) */
export interface ImageStageStatus {
  state: "pending" | "processing" | "needs_review" | "failed" | "completed";
  total: number;
  pending: number;
  processing: number;
  needs_review: number;
  failed: number;
  completed: number;
  publishable: boolean;
}

/** 单条 ingest run 的状态与耗时;未开工时 started_at / duration_seconds 为 null */
export interface IngestRunStageStatus {
  id: string;
  stage: string;
  status: string;
  heartbeat_at: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  failure_code: string | null;
}

/** 阶段耗时;image 阶段的 run_count 即图片张数 */
export interface IngestStageTiming {
  stage: string;
  run_count: number;
  duration_seconds: number;
  running: boolean;
}

/** 端到端墙钟耗时。仍在跑时 finished_at 为 null,elapsed_seconds 是「已运行多久」 */
export interface DocumentIngestionTiming {
  started_at: string | null;
  finished_at: string | null;
  elapsed_seconds: number | null;
  running: boolean;
  /** 按首次 started_at 升序 */
  stages: IngestStageTiming[];
}

/**
 * 响应里还有后端直接序列化的 source_document / revision 两个 ORM 模型,
 * 前端没有消费者,这里不声明,免得跟着后端表结构走。
 */
export interface DocumentIngestionStatus {
  ingest_runs: IngestRunStageStatus[];
  image_stage: ImageStageStatus;
  timing: DocumentIngestionTiming;
  publishable: boolean;
}

export interface KbIngestStageAverage {
  stage: string;
  document_count: number;
  average_seconds: number;
}

/**
 * 全库摄入耗时基线。统计单位是「修订」:同一份文档重跑两次算两个样本,
 * 因为用户要的是"再传一份这样的文件大概多久",而不是文档去重后的头均值。
 * 空数据时数值为 0 / null,不会报错。
 */
export interface KbIngestStats {
  completed_documents: number;
  running_documents: number;
  average_document_seconds: number | null;
  median_document_seconds: number | null;
  total_document_seconds: number;
  /** 按平均值降序,方便直接取最慢的阶段 */
  average_stage_seconds: KbIngestStageAverage[];
}

/**
 * GET /kb/documents/ingest-durations 条目:最新 revision 的端到端墙钟。
 * elapsed_seconds 为 null 表示还没有任何已开工的 run(排队中 ≠ 0 秒)。
 */
export interface DocIngestDuration {
  doc_id: string;
  elapsed_seconds: number | null;
  running: boolean;
}

/** 批量耗时:整页文档一次请求,供终态行常驻显示"耗时 X"(禁止按行发 N 个请求) */
export interface DocIngestDurations {
  items: DocIngestDuration[];
}

export interface BatchResult {
  done: string[];
  skipped: { id: string; reason: string }[];
}

export interface EntityAlias {
  id: string;
  entity_id: string;
  alias: string;
  normalized_alias: string;
  locale: string | null;
  source: string;
  created_at: string;
}

export interface CanonicalEntity {
  id: string;
  org_id: string;
  kb_id: string;
  entity_type: string;
  canonical_name: string;
  metadata: Record<string, unknown>;
  support_status: "supported" | "unsupported";
  support_status_reason: string | null;
  support_status_changed_at: string | null;
  support_status_snapshot_id: string | null;
  unsupported_at: string | null;
  is_pinned: boolean;
  pinned_at: string | null;
  pinned_by: string | null;
  pin_reason: string | null;
  merged_into_entity_id: string | null;
  merged_at: string | null;
  merged_by: string | null;
  merge_reason: string | null;
  created_at: string;
  updated_at: string;
  aliases: EntityAlias[];
}

export interface KbPage {
  id: string;
  kb_id: string;
  page_type: string;
  title: string;
  content: string | null;
  /** 后端并行线新增列("human"/"llm"),未合并前接口不返回该字段 */
  origin?: "human" | "llm" | string | null;
  created_at: string;
  /** 后端暂无该列,预留;展示时回退 created_at */
  updated_at?: string | null;
}

/** wiki 结构体检(KB-5C):GET /kb/bases/{kb_id}/lint,后端未合并时 404 */
export interface KbLintIssue {
  type: string;
  severity: "error" | "warning" | "info" | string;
  page_id: string;
  page_title: string;
  message: string;
  suggestions: string[];
}

export interface KbLintReport {
  issues: KbLintIssue[];
  stats: Record<string, number>;
}

export interface GraphData {
  nodes: { id: string; type: string; name: string; kb_id: string }[];
  edges: GraphEdge[];
}

export interface GraphEdgeEvidence {
  fact_claim_id?: string | null;
  evidence_span_id?: string | null;
  source_doc_id?: string | null;
  source_filename?: string | null;
  revision_id?: string | null;
  source_sha256?: string | null;
  quote_text?: string | null;
  page?: number | null;
  start_line?: number | null;
  end_line?: number | null;
  cell_ref?: string | null;
}

export interface GraphEdge {
  src: string;
  dst: string;
  link_type?: string;
  predicate?: string;
  direction?: string;
  weight: number;
  origin: string;
  status: string;
  valid_from?: string | null;
  valid_to?: string | null;
  evidence?: GraphEdgeEvidence[] | GraphEdgeEvidence | null;
}

export interface FactClaimEvidence {
  id: string;
  revision_id: string;
  doc_id: string;
  filename: string;
  chunk_id: string | null;
  page: number | null;
  start_line: number | null;
  end_line: number | null;
  cell_ref: string | null;
  quote_text: string;
}

export interface FactClaimReview {
  id: string;
  kb_id: string;
  subject_entity_id: string | null;
  object_entity_id: string | null;
  subject_type: string;
  subject_id: string;
  predicate: string;
  value_json: Record<string, unknown>;
  raw_payload: Record<string, unknown>;
  corrected_payload: Record<string, unknown> | null;
  effective_payload: Record<string, unknown>;
  valid_from: string | null;
  valid_to: string | null;
  confidence: number | null;
  review_status: "suggested" | "confirmed" | "rejected" | "orphaned";
  reviewed_by: string | null;
  review_note: string | null;
  model_name: string | null;
  prompt_version: string | null;
  created_at: string | null;
  updated_at: string | null;
  evidence: FactClaimEvidence[];
}

/** 导航摘要沿用实体类型分组；FactClaim 以 predicate 作为稳定分组键。 */
export type FactClaimQueueItem = FactClaimReview & { entity_type: string };

export interface GraphInsights {
  node_count: number;
  edge_count: number;
  isolated_count: number;
  isolated_nodes: { id: string; type: string; name: string }[];
  avg_degree: number;
  communities: {
    size: number;
    density: number;
    sparse: boolean;
    core_nodes: { id: string; type: string; name: string }[];
  }[];
  sparse_community_count: number;
}

export interface SearchHit {
  kind: string;
  layer: "tenant" | "shared" | "platform";
  kb_id: string;
  source: string;
  confidence: number;
  data: SearchHitData;
  citation: SearchCitation | null;
  /** Additive for compatibility with text-only and historical responses. */
  media_refs?: KnowledgeMediaReference[];
}

export interface KnowledgeAnswerSource {
  ref: number;
  hit: SearchHit;
}

export interface KnowledgeAnswerResponse {
  answer: string | null;
  sources: KnowledgeAnswerSource[];
  reason: "no_evidence" | null;
}

export interface SearchScores {
  native: Record<string, number>;
  rrf: number;
  rerank: number | null;
}

export type SearchHitData = Record<string, unknown> & {
  scores: SearchScores;
};

interface SearchCitationBase {
  kind: "source_span" | "fact_evidence" | "image_source";
  revision_id: string;
  source_doc_id: string;
  source_sha256: string;
  quote_text: string;
}

export interface SourceSpanCitation extends SearchCitationBase {
  kind: "source_span";
  chunk_id: string;
  page: number | null;
  start_line: number | null;
  end_line: number | null;
  cell_ref: string | null;
}

export interface FactEvidenceCitation extends SearchCitationBase {
  kind: "fact_evidence";
  fact_claim_id: string;
  evidence_span_id: string;
  chunk_id: string | null;
  page: number | null;
  start_line: number | null;
  end_line: number | null;
  cell_ref: string | null;
}

export interface NormalizedBBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface ImageSourceCitation extends SearchCitationBase {
  kind: "image_source";
  asset_id: string;
  image_sha256: string;
  page: number | null;
  slide: number | null;
  bbox: NormalizedBBox | null;
}

export interface KnowledgeMediaReferenceSnapshot {
  kind: "image";
  asset_id: string;
  alt_text: string;
  content_type: "image/png" | "image/jpeg" | "image/webp";
  width: number;
  height: number;
  page: number | null;
  slide: number | null;
  bbox: NormalizedBBox | null;
  citation: ImageSourceCitation;
}

/**
 * Response-time URLs are additive. Project retrieval snapshots intentionally
 * omit them; consumers derive the authenticated application route from asset_id.
 */
export interface KnowledgeMediaReference extends KnowledgeMediaReferenceSnapshot {
  thumbnail_url?: string | null;
  content_url?: string | null;
}

export interface SelectedKnowledgeMedia extends KnowledgeMediaReferenceSnapshot {
  snapshot_id: string;
}

export type SearchCitation =
  SourceSpanCitation | FactEvidenceCitation | ImageSourceCitation;

export type ImageEnrichmentStatus =
  "pending" | "processing" | "succeeded" | "failed";

export type ImageReviewStatus =
  "needs_review" | "accepted" | "excluded" | "rejected";

export type ImageAssetIssueKind =
  | "duplicate"
  | "unsupported"
  | "oversized"
  | "missing_object"
  | "provider_failed"
  | "unresolved";

/** 与 Cherry Studio 的模型类型词表对齐；`generation` 为本平台承重能力。 */

export interface KbImageEnrichmentReadiness {
  enabled: boolean;
  ready: boolean;
  code: string;
  ocr_provider: string;
  ocr_model: string;
  caption_provider: string | null;
  caption_model: string | null;
  selection_source: "platform_default" | "kb_override";
  capability_source: ModelCapabilitySource | null;
  registry_revision: string | null;
  acceptance_policy: "human_review_required";
  config_fingerprint: string;
}

export interface KbImageAssetUsageSummary {
  snapshot_count: number;
  image_chunk_count: number;
  evidence_span_count: number;
  business_artifact_count: number;
  business_artifact_support: "unsupported";
}

export interface KbImageAsset {
  asset_id: string;
  kb_id: string;
  source_document_id: string;
  source_filename: string;
  revision_id: string;
  revision_number: number;
  revision_sha256: string;
  ingest_run_id: string | null;
  source_occurrence: string;
  parser_name: string;
  parser_item_ref: string | null;
  page: number | null;
  slide: number | null;
  bbox: NormalizedBBox | null;
  reading_order: number | null;
  surrounding_text: string | null;
  image_sha256: string;
  size_bytes: number;
  content_type: "image/png" | "image/jpeg" | "image/webp" | null;
  width: number | null;
  height: number | null;
  thumbnail_sha256: string | null;
  thumbnail_content_type: "image/png" | "image/jpeg" | "image/webp" | null;
  thumbnail_size_bytes: number | null;
  thumbnail_width: number | null;
  thumbnail_height: number | null;
  content_url: string | null;
  thumbnail_url: string | null;
  ocr_text: string | null;
  caption: string | null;
  ocr_provider: string | null;
  ocr_model: string | null;
  caption_provider: string | null;
  caption_model: string | null;
  extraction_fingerprint: string;
  ocr_fingerprint: string | null;
  caption_fingerprint: string | null;
  config_fingerprint: string | null;
  enrichment_status: ImageEnrichmentStatus;
  review_status: ImageReviewStatus;
  failure_code: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_source: string | null;
  review_note: string | null;
  lock_version: number;
  usage: KbImageAssetUsageSummary;
  created_at: string;
  updated_at: string;
}

export interface KbImageAssetPage {
  items: KbImageAsset[];
  total: number;
  page: number;
  page_size: number;
}

export type ImageReviewAction =
  "accept" | "edit_and_accept" | "reject" | "exclude";

export interface KbImageReviewRequest {
  action: ImageReviewAction;
  expected_lock_version: number;
  reason: string;
  caption?: string | null;
  ocr_text?: string | null;
}

/** 批量审核请求;不传 asset_ids 即对整个筛选结果生效(跨页) */
export interface KbImageBulkReviewRequest {
  action: "accept" | "exclude";
  reason: string;
  asset_ids?: string[] | null;
  document_id?: string | null;
  review_status?: ImageReviewStatus | null;
  enrichment_status?: ImageEnrichmentStatus | null;
  max_assets?: number;
}

export interface KbImageBulkReviewResult {
  matched: number;
  updated: number;
  skipped: number;
  truncated: boolean;
}

export interface KbImageRetryRequest {
  expected_lock_version: number;
  reason: string;
}

export interface KbImageRetryAccepted {
  asset_id: string;
  run_id: string;
  lock_version: number;
  status: "queued";
}

export interface KbImageAssetAuditState {
  caption: string | null;
  ocr_text: string | null;
  enrichment_status: string | null;
  review_status: string | null;
  failure_code: string | null;
  review_source: string | null;
  reviewed_by: string | null;
  lock_version: number | null;
  image_sha256: string | null;
  config_fingerprint: string | null;
  result_fingerprint: string | null;
}

export interface KbImageAssetAuditEvent {
  event_version: number;
  action: string;
  actor_kind: "human" | "policy" | "system" | "provider";
  actor_user_id: string | null;
  reason: string | null;
  before_state: KbImageAssetAuditState | null;
  after_state: KbImageAssetAuditState | null;
  created_at: string;
}

export interface KbImageAssetAuditPage {
  items: KbImageAssetAuditEvent[];
  total: number;
  page: number;
  page_size: number;
}


/**
 * 新建知识库时的 kb_type 建议值。
 *
 * **后端已把 kb_type 降级为自由 tag**(models/kb.py:`kb_type: str`),不做任何
 * 校验也不驱动分支,这里只是给个能填的中性示例,宿主换成自己的一组即可。
 */
export const KB_TYPE_PRESETS = [
  { value: "mixed", label: "混合库" },
  { value: "document", label: "文档库(RAG)" },
  { value: "reference", label: "参考资料库" },
] as const;

/**
 * 文档抽取类型的内置两档(nicekit/models/kb.py::DocType)。
 *
 * - `unclassified`:已上传未分类,不进解析队列;
 * - `general`:只切 chunk 做检索,不做结构化抽取。
 *
 * 除这两个内置值外,`doc_type` 可以是**任意已注册的实体类型 key**
 * (走 typed 抽取链路 `extract_typed:{doc_type}`),因此使用方必须把
 * `/kb/entity-types` 的结果并进来一起渲染,不能拿这张表当穷举。
 */
export const DOC_TYPES = [
  { value: "unclassified", label: "未分类(暂不解析)" },
  { value: "general", label: "通用文档(仅检索)" },
] as const;
