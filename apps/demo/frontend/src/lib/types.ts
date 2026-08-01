// 与后端 Pydantic 契约对齐的 DTO(architecture.md §10)

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

export interface RouteKnowledgeDiagnostics {
  kb_id: string;
  source_counts: {
    total: number;
    active: number;
    terminal: number;
    by_doc_type: Record<string, number>;
    by_status: Record<string, number>;
  };
  eligible_general_document_ids: string[];
  route_claim_counts: {
    total: number;
    suggested: number;
    confirmed: number;
    rejected: number;
    orphaned: number;
  };
  active_snapshot_id: string | null;
  ready_snapshot_id: string | null;
  active_route_count: number;
  ready_route_count: number;
  business_asset_count: number;
  reason_code:
    | "published"
    | "no_sources"
    | "classification_required"
    | "wrong_extraction_type"
    | "extraction_in_progress"
    | "review_required"
    | "snapshot_build_required"
    | "snapshot_activation_required"
    | "no_route_claims";
  next_action:
    | "none"
    | "upload_route_document"
    | "classify_and_enqueue"
    | "reclassify"
    | "wait"
    | "review_claims"
    | "build_snapshot"
    | "activate_snapshot";
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

export interface Destination {
  id: string;
  kb_id: string;
  name: string;
  country: string | null;
  parent_id: string | null;
  created_at: string;
}

export interface HotelEntry {
  id: string;
  kb_id: string;
  destination_id: string | null;
  city: string;
  name: string;
  star: string | null;
  room_type: string | null;
  price_ref: number | null;
  currency: string | null;
  notes: string | null;
  created_at: string;
}

export interface CostEntry {
  id: string;
  kb_id: string;
  destination_id: string | null;
  category: string;
  name: string;
  city: string | null;
  unit: string | null;
  unit_cost: number | null;
  currency: string | null;
  notes: string | null;
  created_at: string;
}

export interface PoiEntry {
  id: string;
  kb_id: string;
  destination_id: string | null;
  name: string;
  city: string | null;
  poi_type: string | null;
  ticket_info: string | null;
  visit_minutes: number | null;
  description: string | null;
  created_at: string;
}

export interface RouteEntry {
  id: string;
  kb_id: string;
  name: string;
  days: number | null;
  structure: Record<string, unknown>;
  created_at: string;
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
export type ModelCapability =
  | "generation"
  | "vision"
  | "web_search"
  | "reasoning"
  | "function_call"
  | "embedding"
  | "rerank";

export type ModelCapabilitySource =
  "manual" | "provider" | "registry" | "unclassified";

export type ModelInputModality = "text" | "image";

/** Per-model capability record stored on `LlmProviderDto.model_metadata`. */
export interface ProviderModelMetadata {
  capabilities: ModelCapability[];
  input_modalities: ModelInputModality[];
  capability_source: ModelCapabilitySource;
  registry_revision: string | null;
  provider_metadata: Record<string, string | number>;
}

/** Eligible row returned by GET /kb/image-enrichment/models. */
export interface ProviderModelCatalogItem extends ProviderModelMetadata {
  provider: string;
  model: string;
  /** Grouping hint derived from the model ID; never persisted. */
  vendor: string | null;
}

/** Result of POST /admin/providers/{id}/test-connection|test-model. */
export interface ProviderConnectivityDto {
  provider: string;
  model: string | null;
  scope: "instance" | "model";
  ok: boolean;
  latency_ms: number | null;
  /** Secret-free code, e.g. `http_error;status=401`. Null on success. */
  error_code: string | null;
  model_count: number | null;
  probed_capability: ModelCapability | null;
}

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

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  org: { id: string; slug: string; name: string; role: string };
  orgs: { id: string; slug: string; name: string; role: string }[];
}

export interface AdminOrg {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  member_count: number;
  created_at: string | null;
}

export interface ModelRouteDto {
  id: string;
  org_id: string | null;
  task: string;
  primary_provider: string;
  primary_model: string;
  fallback_chain: { provider: string; model: string }[] | null;
  max_tokens: number;
  timeout_seconds: number;
  is_active: boolean;
  created_at: string | null;
}

export interface ModelRouteTaskDto {
  task: string;
  label: string;
  description: string;
  category: string;
  is_system: boolean;
}

/** 系统能力槽位的配置就绪（不发网络请求）。 */
export interface CapabilitySlotDto {
  task: string;
  required_capabilities: ModelCapability[];
  configured: boolean;
  is_active: boolean;
  primary_provider: string | null;
  primary_model: string | null;
  fallback_hops: number;
  /** 重新做能力校验后实际会被调用的那一跳；null = 当前不可用。 */
  resolved_provider: string | null;
  resolved_model: string | null;
  ready: boolean;
}

/** 定时探测落下的上游健康状态。 */
export interface ProviderProbeDto {
  capability: string;
  provider: string | null;
  model: string | null;
  configuration_ready: boolean;
  status: string;
  last_probe_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  probe_age_seconds: number | null;
  latency_ms: number | null;
  consecutive_failures: number;
  error_code: string | null;
}

export interface HeartbeatDto {
  role: "api" | "worker" | "beat";
  required: boolean;
  status: "healthy" | "expired" | "missing";
  instance: string | null;
  version: string | null;
  recorded_at: string | null;
  age_seconds: number | null;
}

export interface McpDiagnosticDto {
  server_id: string;
  org_id: string | null;
  name: string;
  transport: string;
  enabled: boolean;
  status: "online" | "offline" | "unchecked" | "disabled";
  latency_ms: number | null;
  checked_at: string | null;
  tools_count: number;
  bound_agents: number;
  error_code: string | null;
}

export interface AgentDiagnosticDto {
  org_id: string;
  org_name: string;
  status: "healthy" | "degraded";
  total_cards: number;
  enabled_cards: number;
  ready_cards: number;
  running_sessions: number;
  issues: {
    code: string;
    card: string;
    items: string[];
  }[];
}

export interface SystemScheduleDiagnosticDto {
  name: string;
  label: string;
  task: string;
  interval_seconds: number;
  enabled: boolean;
  runner: "api" | "beat" | "manual" | null;
  status: "healthy" | "unavailable" | "manual_only" | "disabled";
}

export interface CustomScheduleDiagnosticDto {
  org_id: string;
  org_name: string;
  active_count: number;
  paused_count: number;
  archived_count: number;
  overdue_count: number;
  failed_runs_24h: number;
}

export interface OrgHealthDiagnosticDto {
  outbox: {
    pending_count: number;
    oldest_pending_age_seconds: number | null;
    dead_letter_count: number;
    oldest_dead_letter_age_seconds: number | null;
  };
  documents: {
    uploaded_count: number;
    oldest_uploaded_age_seconds: number | null;
    processing_count: number;
    oldest_processing_age_seconds: number | null;
    failed_count: number;
  };
  ingest_leases: { count: number; oldest_age_seconds: number | null };
  image_enrichment: { count: number; oldest_age_seconds: number | null };
  snapshot_builds: { count: number; oldest_age_seconds: number | null };
  object_metadata_inconsistencies: {
    count: number;
    oldest_age_seconds: number | null;
  };
  media_projection_failures: {
    count: number;
    oldest_age_seconds: number | null;
  };
  vectorless_chunks: number;
  pending_claims: number;
}

export interface OperationsDiagnosticsDto {
  generated_at: string;
  runtime: {
    dispatch_mode: "inline" | "celery";
    service_version: string;
    heartbeat_expiry_seconds: number;
  };
  capability_slots: CapabilitySlotDto[];
  heartbeats: HeartbeatDto[];
  providers: ProviderProbeDto[];
  skills: {
    status: "healthy" | "degraded";
    total: number;
    invalid: { slug: string; code: string }[];
  };
  mcp_servers: McpDiagnosticDto[];
  agents: AgentDiagnosticDto[];
  schedules: {
    system: SystemScheduleDiagnosticDto[];
    custom: CustomScheduleDiagnosticDto[];
  };
  organizations: {
    org_id: string;
    org_name: string;
    health: OrgHealthDiagnosticDto;
    remediation: string[];
  }[];
}

export interface PromptDto {
  id: string;
  task: string;
  version: number;
  content: string;
  is_active: boolean;
  created_at: string | null;
}

// GET /admin/prompts 的列表项:DB 行 + 内置目录展示元信息。
// POST/PATCH 仍返回裸 PromptDto(不带目录字段),故单独扩展而非改旧接口。
export interface PromptWithCatalogDto extends PromptDto {
  /** 内置目录的中文名;未登记目录时后端回落为 task 原文 */
  name_zh: string;
  /** 该 prompt 在哪个链路里干什么;未登记目录时为空串 */
  description: string;
  /** 任务分类(task 点号前缀):kb / agent,自定义无点号任务归 other */
  category: string;
  /** true=源码 seed 的内置任务;false=业务方自定义登记 */
  builtin: boolean;
  /** true=该 task 在 prompt_catalog_entries 有在线目录登记(与 builtin 正交) */
  registered: boolean;
  /** content 中扫出的 {{xxx}} 占位符,仅展示用:运行时不做变量替换,原样发给模型 */
  variables: string[];
}

// ---------- 系统 Prompt 资源(随源码发布,只读) ----------
// 契约:GET /admin/prompt-resources(列表,不含 content)/
// GET /admin/prompt-resources/{id}(含 content)。
// 修改资源需编辑 backend/app/services/agent/prompts/resources/ 并重启后端。

export interface PromptResourceDto {
  id: string;
  title: string;
  /** global=全局段 / agent=角色段 / memory=记忆(前端做中文映射,未知值原样显示) */
  category: string;
  description: string;
  variables: string[];
  source: string;
  content_chars: number;
}

export interface PromptResourceDetailDto extends PromptResourceDto {
  content: string;
}

/** POST /admin/prompt-resources/preview 的请求块:values 缺省时变量按占位符原样保留 */
export interface PromptPreviewBlockInput {
  id: string;
  values?: Record<string, string>;
}

export interface PromptPreviewDto {
  text: string;
}

// ---------- 按 agent 卡的 system prompt 组装预览 ----------
// 契约:GET /admin/prompt-assembly?card_id=<uuid>

export interface PromptAssemblyBlockDto {
  id: string;
  chars: number;
}

/** 仅运行时才注入的动态块(如记忆/上下文),静态组装预览里不含其内容 */
export interface PromptRuntimeOnlyBlockDto {
  id: string;
  title: string;
  description: string;
}

export interface PromptAssemblyDto {
  card_id: string;
  card_name: string;
  system_prompt_chars: number;
  blocks: PromptAssemblyBlockDto[];
  text: string;
  runtime_only_blocks: PromptRuntimeOnlyBlockDto[];
}

export interface Member {
  membership_id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string | null;
}

export interface InviteOut {
  id: string;
  email: string;
  role: string;
  expires_at: string;
  created_at: string | null;
}

export interface InviteCreated extends InviteOut {
  token: string;
}

export interface InviteInfo {
  email: string;
  role: string;
  org_name: string;
  org_slug: string;
  user_exists: boolean;
}

export const ROLE_LABELS: Record<string, string> = {
  platform_admin: "平台管理员",
  org_admin: "组织管理员",
  sales: "销售",
  operator: "运营",
  pricer: "报价",
  reviewer: "审核",
};

// 租户内可授予的角色(platform_admin 仅平台侧产生)
export const ASSIGNABLE_ROLES = [
  "org_admin",
  "sales",
  "operator",
  "pricer",
  "reviewer",
] as const;

export const KB_TYPE_PRESETS = [
  { value: "destination", label: "目的地基础库" },
  { value: "hotel", label: "参考酒店池" },
  { value: "cost", label: "成本参考库" },
  { value: "route", label: "成熟线路库" },
  { value: "document", label: "文档库(RAG)" },
  { value: "mixed", label: "混合库" },
] as const;

export const DOC_TYPES = [
  { value: "hotel", label: "参考酒店表" },
  { value: "cost", label: "成本参考表" },
  { value: "poi", label: "景点资料" },
  { value: "route_template", label: "成熟线路" },
  { value: "general", label: "通用文档(仅检索)" },
] as const;

// ---------- 模板管理(stage-c 02,FR-011) ----------

/** 模板变量清单:variables 为已接线变量(名 → 中文说明),unknown 为模板里存在但目录未收录的占位符 */
export interface TemplateVariablesSchema {
  kind: string;
  variables: Record<string, string>;
  unknown?: string[];
}

export interface TemplateOut {
  id: string;
  kind: string;
  name: string;
  is_platform: boolean;
  is_active: boolean;
  current_version: number | null;
  variables_schema: TemplateVariablesSchema | null;
  created_at: string | null;
}

export interface TemplateVersionOut {
  id: string;
  version: number;
  approval_status: "draft" | "pending" | "approved" | "rejected";
  changelog: string | null;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string | null;
  /** 版本源规格(后端并行开发中,未上线时缺省) */
  source_spec?: Record<string, unknown> | null;
}

// ---------- 结构化模板 spec(可视化编辑器 ↔ 后端 render/template_spec.py) ----------
// 后端 Pydantic 全部 extra=forbid:序列化时只能带该块类型该有的键,多一个键即 422。

export interface TemplateHeadingBlock {
  type: "heading";
  text: string;
  /** 1-3 */
  level: number;
}

export interface TemplateParagraphBlock {
  type: "paragraph";
  text: string;
}

/** 渲染为「label:{{ var }}」 */
export interface TemplateFieldLineBlock {
  type: "field_line";
  label: string;
  var: string;
}

/** var(list[str] 变量循环)与 items(静态条目,可含 {{变量}})必须恰好其一 */
export interface TemplateBulletListBlock {
  type: "bullet_list";
  var?: string;
  items?: string[];
}

export interface TemplateTableColumn {
  header: string;
  cell: string;
}

/** 循环表:行上下文别名固定为 row,cell 内用 {{ row.字段 }} */
export interface TemplateTableBlock {
  type: "table";
  loop_var: string;
  /** 1-8 列 */
  columns: TemplateTableColumn[];
}

export interface TemplateDividerBlock {
  type: "divider";
}

/** loop 子块可用类型(仅一层嵌套:子块不得再含 loop / table) */
export type TemplateLeafBlock =
  | TemplateHeadingBlock
  | TemplateParagraphBlock
  | TemplateFieldLineBlock
  | TemplateBulletListBlock
  | TemplateDividerBlock;

export interface TemplateLoopBlock {
  type: "loop";
  var: string;
  alias: string;
  /** 1-20 个子块,子块内用 {{ alias.字段 }} */
  blocks: TemplateLeafBlock[];
}

export type TemplateBlock =
  TemplateLeafBlock | TemplateTableBlock | TemplateLoopBlock;

export type TemplateBlockType = TemplateBlock["type"];

/** 结构化模板定义:1-40 个块,文本字段 ≤500 字且只允许 {{ }} 不允许 {% %} */
export interface TemplateSpec {
  kind: string;
  title?: string;
  blocks: TemplateBlock[];
}

/**
 * 编辑器内部块模型:所有块类型共用一组扁平字段(切换类型不丢内容,增删排序不必收窄),
 * 保存时由 template-spec-validate.mjs 的 toSpec 收敛成上面的判别联合。
 * varName 同时承载 field_line.var / bullet_list.var / table.loop_var / loop.var。
 */
export interface TemplateEditorBlock {
  id: string;
  type: TemplateBlockType;
  text: string;
  level: number;
  label: string;
  varName: string;
  bulletMode: "var" | "items";
  items: string[];
  columns: TemplateTableColumn[];
  alias: string;
  children: TemplateEditorBlock[];
}

/** POST /templates/from-spec 与 /templates/{id}/versions/from-spec 的 201 应答 */
export interface TemplateSpecDraftOut {
  template_id: string;
  kind: string;
  name: string;
  version: number;
  approval_status: "draft" | "pending" | "approved" | "rejected";
  variables: string[];
}

export const TEMPLATE_BLOCK_LABELS: Record<TemplateBlockType, string> = {
  heading: "标题",
  paragraph: "正文段落",
  field_line: "字段行",
  bullet_list: "项目符号",
  table: "循环表格",
  loop: "循环区块",
  divider: "分隔线",
};

export const TEMPLATE_KINDS = [
  { value: "quote_docx", label: "对外报价单" },
  { value: "handover_docx", label: "地接确认件" },
  { value: "ppt", label: "销售PPT" },
  { value: "itinerary_docx", label: "行程手册" },
  { value: "brief_docx", label: "简要行程" },
  { value: "detailed_docx", label: "详细行程彩页" },
] as const;

export const TEMPLATE_KIND_LABELS: Record<string, string> = Object.fromEntries(
  TEMPLATE_KINDS.map((k) => [k.value, k.label]),
);

/** 可用结构化编辑器起草的类型:ppt 不是 docx 模板,后端 spec 编译器直接 422 */
export const SPEC_TEMPLATE_KINDS = TEMPLATE_KINDS.filter(
  (k) => k.value !== "ppt",
);

export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  pending: "待审批",
  approved: "已批准",
  rejected: "已驳回",
};

// ---------- 产物输出工作台(文档工厂 P2,只追加不重排) ----------

/** DELETE /render-kinds/custom/{key} 应答:软删停用,notice 给用户看的中文说明 */
export interface RenderKindDeleteOut {
  kind: string;
  artifact_count: number;
  notice: string;
}

/**
 * POST /templates/analyze(FormData 上传 docx)与 POST /templates/markdown-analyze
 * 的体检结果:known=目录内变量 / unknown=模板自有变量(可渲染,自动登记)/
 * missing=目录有但模板没用 / suggestions=拼错建议(写错的占位符 → 目录近似变量)。
 */
export interface TemplateAnalyzeOut {
  kind: string;
  /** 目录内变量 → 中文说明(analyze 与 markdown-analyze 同形) */
  known: Record<string, string>;
  unknown: string[];
  missing: string[];
  suggestions: Record<string, string>;
  /** 仅 markdown-analyze 返回:解析出的 spec 块,用于本地近似预览 */
  blocks_preview?: TemplateBlock[];
}

/** POST /templates/from-markdown 与 /{id}/versions/from-markdown 的 201 应答(TemplateVersion 同形) */
export type TemplateMarkdownDraftOut = TemplateVersionOut & {
  template_id?: string;
};

/** POST /templates/ai-draft 应答:spec 与 markdown 至少其一非空,notes 为 AI 的中文说明 */
export interface TemplateAiDraftOut {
  spec: TemplateSpec | null;
  markdown: string | null;
  notes: string;
}

// ---------- Agent / MCP admin ----------

export interface AgentVersionDto {
  id: string;
  card_id: string;
  version: number;
  system_prompt: string;
  model_task: string;
  max_turns: number;
  timeout_seconds: number;
  tools: string[];
  mcp_bindings: { server_id: string; enabled_tools: string[] | null }[];
  skills: string[];
  is_active: boolean;
  status: "draft" | "active" | "archived";
  created_at: string | null;
}

export interface AgentCardDto {
  id: string;
  org_id: string | null;
  is_platform: boolean;
  name: string;
  description: string | null;
  icon: string | null;
  sort_order: number;
  is_active: boolean;
  created_at: string | null;
  active_version: AgentVersionDto | null;
}

export interface AgentToolDto {
  name: string;
  label: string;
  category: string;
  description: string;
  side_effect: "read" | "write" | "unknown";
}
export interface SkillDto {
  slug: string;
  name: string;
  description: string;
  version: string;
  category: string;
  files: string[];
}
export interface SkillDetailDto {
  slug: string;
  content: string;
  files: string[];
}
export interface McpServerDto {
  id: string;
  org_id: string | null;
  name: string;
  transport: "streamable_http" | "sse" | "stdio";
  endpoint_url: string;
  headers: Record<string, string>;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  connect_timeout_seconds: number | null;
  call_timeout_seconds: number | null;
  enabled_tools: string[] | null;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

// 外部服务配置：admin「外部服务」页只维护联网搜索和图片生成。
export type ServiceConfigsDto = Record<string, Record<string, unknown>>;

// 模型提供商实例(admin「模型提供商」页,Cherry Studio 式多实例):
// 路由降级链按 name 引用;内置 openai/anthropic 凭证空 = 回落 .env
export interface LlmProviderDto {
  id: string;
  name: string;
  protocol: "openai" | "anthropic";
  api_key: string;
  base_url: string;
  // 该实例的模型列表(路由候选):网关导入或手动维护
  models: string[];
  // Legacy responses may omit metadata; the UI treats those models as unclassified.
  model_metadata?: Record<string, ProviderModelMetadata>;
  enabled: boolean;
  is_builtin: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface McpToolDto {
  name: string;
  description: string;
  schema: Record<string, unknown>;
}

export interface McpServerStatusDto {
  server_id: string;
  status: "online" | "offline" | "disabled";
  latency_ms: number | null;
  checked_at: string | null;
  error: string | null;
  tools: McpToolDto[];
}
