// 平台管理端(/admin/*)的前端 DTO:模型提供商/路由/诊断/Prompt/Agent 卡/技能。
// 与 nicekit/api/v1/admin.py 的 46 个端点对应。
//
// 密钥字段的读出面契约:providers / service-configs 一律返回 `********` 掩码
// 并带 `has_api_key: boolean`;"不修改密钥"的表达方式是把掩码原样回传。

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
  /**
   * **永远是掩码**(`********`)或空串,后端绝不回传明文/密文。
   * 把它原样 PATCH 回去 = "不修改密钥";要改就填新值,要清空就传空串。
   */
  api_key: string;
  /** 是否已配置密钥(掩码看不出"有没有",靠这个布尔判断) */
  has_api_key?: boolean;
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
