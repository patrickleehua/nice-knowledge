// Agent workbench contract: docs/20260711-AI助手Agent化改造任务单.md, card E;
// tool.confirm / plan.update: docs/20260712-Agent业务操作与计划依据融合任务单.md, card K/L.

import { api, postSse } from "@/lib/api";

// 待批的 legacy 单项调用；新客户端优先使用 ApprovalBundle。
export interface LegacyPendingConfirmation {
  kind?: never;
  run_id?: string;
  tool_call_id: string;
  name: string;
  input: Record<string, unknown>;
  summary: string;
  requested_at?: string;
}

export type PendingConfirmation = LegacyPendingConfirmation | ApprovalBundle;

export interface ApprovalItemDecision {
  tool_call_id: string;
  decision: "approve" | "deny";
  scope: "once" | "session_tool";
  note?: string;
}

export type ConfirmationAction =
  | { tool_call_id: string; approved: boolean }
  | { bundle_id: string; decisions: ApprovalItemDecision[] };

export interface ReviewerOverrideAction {
  candidate_id: string;
  action_hash: string;
}

export interface UserInputOption {
  label: string;
  description: string | null;
}

export interface UserInputQuestion {
  id: string;
  header: string;
  question: string;
  options: UserInputOption[];
  multi_select: boolean;
}

export interface PendingUserInput {
  kind: "user_input";
  request_id: string;
  run_id: string | null;
  requested_at: string;
  questions: UserInputQuestion[];
}

export interface UserInputAnswer {
  question_id: string;
  selected: string[];
  other?: string;
}

export interface UserInputAction {
  request_id: string;
  answers: UserInputAnswer[];
}

export interface PlanContinuationAction {
  step_id: string;
}

export type AgentContinuationAction =
  | ConfirmationAction
  | ReviewerOverrideAction
  | UserInputAction
  | PlanContinuationAction;

export function isReviewerOverrideAction(
  value: AgentContinuationAction | undefined,
): value is ReviewerOverrideAction {
  return !!value && "candidate_id" in value;
}

export function isUserInputAction(
  value: AgentContinuationAction | undefined,
): value is UserInputAction {
  return !!value && "request_id" in value;
}

export function isPlanContinuationAction(
  value: AgentContinuationAction | undefined,
): value is PlanContinuationAction {
  return !!value && "step_id" in value;
}

export function isConfirmationAction(
  value: AgentContinuationAction | undefined,
): value is ConfirmationAction {
  return (
    !!value &&
    !isReviewerOverrideAction(value) &&
    !isUserInputAction(value) &&
    !isPlanContinuationAction(value)
  );
}

export function isApprovalBundle(
  value: PendingConfirmation | null | undefined,
): value is ApprovalBundle {
  return value?.kind === "approval_bundle" && Array.isArray(value.items);
}

// agent 执行计划步骤(plan_update 全量覆盖);failed 可能来自模型自述,
// 也可能是后端在轮次中断后替模型收敛出来的(note 以「执行中断」开头)
export interface PlanStep {
  id: string;
  title: string;
  status: "pending" | "in_progress" | "done" | "skipped" | "failed";
  note: string | null;
}

export interface ChatSessionOut {
  id: string;
  agent_card_id: string;
  project_id: string | null;
  /** 只绑客户不绑旅行计划 = 售前咨询会话(客户未立项也能沉淀客户记忆) */
  customer_id: string | null;
  title: string;
  status: string;
  pending_confirmation: PendingConfirmation | null;
  pending_user_input: PendingUserInput | null;
  plan: PlanStep[] | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ChatSessionListOut {
  items: ChatSessionOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface AgentCardOut {
  id: string;
  name: string;
  description: string | null;
  icon: string | null;
  active_version: number;
  tools: string[];
  max_turns: number;
  is_platform: boolean;
}

export interface ChatMessageOut {
  id: string;
  /** Present on current records; legacy rows may not have a recoverable run. */
  run_id?: string | null;
  sequence: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_name: string | null;
  tool_call_id: string | null;
  tool_input: Record<string, unknown> | null;
  tool_output: Record<string, unknown> | null;
  trace_id: string | null;
  created_at: string | null;
}

// 运行中输入队列(run 进行中发消息 → 202 入队,loop 在安全检查点消费)
export type QueuedInputStatus = "queued" | "consumed" | "skipped";

export interface QueuedInputOut {
  id: string;
  run_id: string;
  position: number;
  status: QueuedInputStatus;
  content: string;
  // 这条排队输入自带的"本轮工具":消费它那一轮才临时放开,不改 Agent 配置
  runtime_tools?: string[];
  consumed_message_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface QueuedInputListOut {
  items: QueuedInputOut[];
}

// send_message 在 run 进行中的 202 回执
export interface QueuedAck {
  queued: true;
  queued_input_id: string;
  run_id: string;
  position: number;
}

// 会话目标与自动续跑:目标 active 时 agent 空闲会自动起新 run 继续推进,
// 直到模型调 update_goal 声明完成,或 token/续跑轮次预算耗尽
export type SessionGoalStatus =
  "active" | "completed" | "budget_limited" | "cancelled";

export interface SessionGoalOut {
  id: string;
  session_id: string;
  objective: string;
  status: SessionGoalStatus;
  token_budget: number;
  tokens_used: number;
  auto_runs: number;
  max_auto_runs: number;
  summary: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SessionGoalDetailOut {
  goal: SessionGoalOut | null;
}

export interface SessionGoalIn {
  objective: string;
  token_budget?: number;
  max_auto_runs?: number;
}

export const SESSION_GOAL_STATUS_LABELS: Record<SessionGoalStatus, string> = {
  active: "推进中",
  completed: "已完成",
  budget_limited: "预算用尽",
  cancelled: "已取消",
};

interface BaseEvent {
  seq: number;
}

export type PermissionProfile =
  "request_approval" | "auto_review" | "full_access" | "custom";
export type PermissionDecision = "allow" | "auto_review" | "ask_user" | "deny";
export type ToolRisk = "routine" | "sensitive" | "critical";
export type ToolCategory =
  | "local_data"
  | "network"
  | "external_cost"
  | "financial"
  | "destructive"
  | "workflow"
  | "export";

export interface PermissionActionScope {
  mode: "general" | "project";
  org_id: string;
  session_id: string;
  bound_project_id: string | null;
  target_project_id: string | null;
  resource_type: string | null;
  resource_id: string | null;
  creates_project: boolean;
  resolution_error: string | null;
}

export interface PermissionPolicySnapshot {
  policy_id: string | null;
  policy_version: number;
  profile: PermissionProfile;
  scope: "session" | "project" | "organization";
  expires_at: string | null;
  grant_ids: string[];
  allowed_profiles: PermissionProfile[];
  reviewer_enabled: boolean;
  max_grant_ttl_seconds: number;
  max_full_access_ttl_seconds: number;
  compatibility_mode: boolean;
  shadow_evaluation: boolean;
}

export interface ApprovalBundleItem {
  order: number;
  tool_call_id: string;
  name: string;
  label: string;
  summary: string;
  input: Record<string, unknown>;
  effect: "read" | "write" | "delete" | "transition" | null;
  risk: ToolRisk;
  categories: ToolCategory[];
  reversibility: "reversible" | "compensatable" | "irreversible" | null;
  delegation: "automatic" | "reviewable" | "user_required" | "agent_forbidden";
  scope: PermissionActionScope | null;
  argument_hash: string | null;
  action_hash: string | null;
  policy_decision: PermissionDecision;
  reason_code: string;
  decision_source: string;
  reviewer_rationale: string | null;
  reviewer_decision: "approve" | "deny" | "escalate" | null;
  reviewer_risk_flags: string[];
  override_eligible: boolean;
  circuit_breaker: boolean;
  eligible_scopes: ("once" | "session_tool")[];
  status: "pending" | "allowed" | "approved" | "denied";
  decision: "approve" | "deny" | null;
  decision_scope: "once" | "session_tool" | null;
  user_note: string | null;
}

export interface ApprovalBundle {
  kind: "approval_bundle";
  bundle_id: string;
  run_id?: string;
  model_hop: number;
  status: "pending" | "ready" | "completed";
  requested_at: string;
  policy_snapshot: PermissionPolicySnapshot;
  assistant_text: string | null;
  items: ApprovalBundleItem[];
}

export interface ReviewerOverride {
  kind: "reviewer_override";
  candidate_id: string;
  run_id: string | null;
  model_hop: number;
  tool_call_id: string;
  name: string;
  label: string;
  input: Record<string, unknown>;
  argument_hash: string;
  action_hash: string;
  scope: PermissionActionScope;
  categories: ToolCategory[];
  risk: ToolRisk;
  reviewer_rationale: string | null;
  reviewer_risk_flags: string[];
  reason_code: string;
  policy_snapshot: PermissionPolicySnapshot;
  created_at: string;
  expires_at: string;
}

export type AgentEvent =
  // token 级增量:仅实时流,不落库不参与重放;reset=true 时丢弃已收增量(后端降级重试)
  | (BaseEvent & { type: "text.delta"; text: string; reset?: boolean })
  // 思考过程增量(thinking block / reasoning summary),规则同 text.delta
  | (BaseEvent & { type: "thought.delta"; text: string; reset?: boolean })
  | (BaseEvent & { type: "thought"; text: string })
  // 模型内置联网搜索(provider 服务端执行,没有对应的 tool.call/tool.result)
  | (BaseEvent & {
      type: "websearch.builtin";
      query: string;
      results: { title: string; url: string; page_age?: string | null }[];
    })
  // 模型在工具调用轮输出的公开过程说明；不是 reasoning，不得渲染为“思考”
  | (BaseEvent & { type: "assistant.message"; text: string })
  | (BaseEvent & {
      type: "tool.call";
      tool_call_id: string;
      name: string;
      input: Record<string, unknown>;
    })
  | (BaseEvent & {
      type: "tool.progress";
      parent_tool_call_id: string;
      text: string;
    })
  | (BaseEvent & {
      type: "tool.result";
      tool_call_id: string;
      name: string;
      ok: boolean;
      output: unknown;
    })
  | (BaseEvent & { type: "text"; text: string })
  | (BaseEvent & {
      type: "user.input.required";
      request: PendingUserInput;
    })
  | (BaseEvent & {
      type: "user.input.resolved";
      request_id: string;
      answers: {
        question_id: string;
        question: string;
        values: string[];
      }[];
    })
  // 高危工具截停等用户批准(卡 K):批准/拒绝经 sendChatMessage 的 confirm 参数答复
  | (BaseEvent & {
      type: "tool.confirm";
      tool_call_id: string;
      name: string;
      input: Record<string, unknown>;
      summary: string;
    })
  | (BaseEvent & { type: "policy.snapshot" } & PermissionPolicySnapshot)
  | (BaseEvent & {
      type: "policy.decision";
      tool_call_id: string;
      name: string;
      decision: PermissionDecision;
      reason_code: string;
      source: string;
      matched_rule: string | null;
      grant_id: string | null;
      argument_hash: string | null;
      action_hash: string | null;
      scope: PermissionActionScope | null;
      risk: ToolRisk | null;
      categories: ToolCategory[];
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
      phase: "route" | "final";
    })
  | (BaseEvent & {
      type: "policy.shadow_evaluation";
      tool_call_id: string;
      name: string;
      enforced_decision: PermissionDecision;
      enforced_reason_code: string;
      enforced_source: string;
      shadow_decision: PermissionDecision;
      shadow_reason_code: string;
      shadow_source: string;
      shadow_matched_rule: string | null;
      differs: boolean;
      argument_hash: string | null;
      action_hash: string | null;
      scope: PermissionActionScope | null;
      risk: ToolRisk | null;
      categories: ToolCategory[];
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
    })
  | (BaseEvent & {
      type: "reviewer.requested";
      tool_call_id: string;
      name: string;
      reason_code: string;
      matched_rule: string | null;
      action_hash: string | null;
      risk: ToolRisk | null;
      categories: ToolCategory[];
    })
  | (BaseEvent & {
      type: "reviewer.decision";
      tool_call_id: string;
      name: string;
      decision: "approve" | "deny" | "escalate";
      source: "reviewer";
      reason_code: string;
      rationale: string | null;
      risk_flags: string[];
      available: boolean | null;
      override_eligible: boolean;
      circuit_breaker: boolean;
      argument_hash: string | null;
      action_hash: string | null;
      scope: PermissionActionScope | null;
      risk: ToolRisk | null;
      categories: ToolCategory[];
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
    })
  | (BaseEvent & {
      type: "reviewer.circuit_breaker";
      tool_call_id: string;
      name: string;
      denial_count: number;
      action_hash: string | null;
    })
  | (BaseEvent & {
      type: "reviewer.override.available" | "reviewer.override.consumed";
      override: ReviewerOverride;
    })
  | (BaseEvent & {
      type: "reviewer.override.completed";
      candidate_id: string;
      tool_call_id: string;
      override_of: string;
      name: string;
      ok: boolean;
      reason_code: string | null;
      risk: ToolRisk;
      categories: ToolCategory[];
      argument_hash: string;
      action_hash: string;
      scope: PermissionActionScope;
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
    })
  | (BaseEvent & {
      type:
        | "approval.bundle.requested"
        | "approval.bundle.updated"
        | "approval.bundle.completed";
      bundle: ApprovalBundle;
    })
  | (BaseEvent & {
      type: "approval.bundle.decision";
      bundle_id: string;
      tool_call_id: string;
      name: string;
      decision: "approve" | "deny";
      decision_source: "user";
      scope: "once" | "session_tool";
      note?: string | null;
      reason_code: string;
      policy_reason_code: string;
      risk: ToolRisk;
      categories: ToolCategory[];
      argument_hash: string;
      action_hash: string;
      action_scope: PermissionActionScope;
      grant_id?: string;
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
    })
  | (BaseEvent & {
      type: "approval.legacy.decision";
      tool_call_id: string;
      name: string;
      decision: "approve" | "deny";
      decision_source: "user";
      scope: "once";
      reason_code: string;
      risk?: ToolRisk | null;
      categories: ToolCategory[];
      argument_hash?: string | null;
      action_hash?: string | null;
      action_scope?: PermissionActionScope | null;
      policy_id: string | null;
      policy_version: number;
      profile: PermissionProfile;
      permission_scope: "session" | "project" | "organization";
    })
  // 运行中输入队列:input.queued/updated/deleted 由 API 直落 chat_events(重放可见),
  // input.consuming/consumed/skipped 走 run 的实时 SSE 通道
  | (BaseEvent & {
      type:
        "input.queued" | "input.updated" | "input.deleted" | "input.consuming";
      queued_input_id: string;
      session_id: string;
      run_id: string;
      position: number;
      status: QueuedInputStatus;
      content: string;
    })
  | (BaseEvent & {
      type: "input.consumed";
      queued_input_id: string;
      session_id: string;
      run_id: string;
      position: number;
      status: QueuedInputStatus;
      content: string;
      message_id: string;
    })
  | (BaseEvent & {
      type: "input.skipped";
      queued_input_id: string;
      session_id: string;
      run_id: string;
      position: number;
      status: QueuedInputStatus;
      content: string;
      reason: string;
    })
  // 会话目标状态变化:模型调 update_goal 时走 run 的实时 SSE 通道,
  // 设/取消目标由 API 直落 chat_events(重放可见)
  | (BaseEvent & {
      type: "goal.updated";
      goal_id: string;
      session_id: string;
      objective: string;
      status: SessionGoalStatus;
      token_budget: number;
      tokens_used: number;
      auto_runs: number;
      max_auto_runs: number;
      summary: string | null;
    })
  // 运行时临时工具开关的逐项判定:被拒项(写工具/不存在)要让用户当场看到原因,
  // 否则他只会发现"我明明勾了却没用上"
  | (BaseEvent & {
      type: "runtime.tools";
      source: RuntimeToolSource;
      queued_input_id?: string;
      requests: RuntimeToolRequest[];
    })
  // agent 执行计划全量更新(卡 L)
  | (BaseEvent & { type: "plan.update"; steps: PlanStep[] })
  | (BaseEvent & { type: "plan.step.continued"; step_id: string })
  | (BaseEvent & {
      type: "stage.update";
      project_id: string;
      project_status: string;
      stage: "demand" | "itinerary" | "quote" | "docs";
    })
  | (BaseEvent & {
      type: "turn.done";
      reason:
        | "end_turn"
        | "ask_user"
        | "confirm"
        | "max_turns"
        | "cancelled"
        | "error";
      usage: { input_tokens: number; output_tokens: number };
    })
  | (BaseEvent & { type: "error"; message: string });

// 思考等级:档位表由后端 GET /chat/thinking-levels 下发(settings 可配置),
// 前端不写死;undefined = 后端 provider 默认行为
export interface ThinkingLevelsOut {
  levels: { value: string; label: string }[];
  default: string;
}

export interface RuntimeModelSelection {
  provider: string;
  model: string;
}

export interface RuntimeModelOption extends RuntimeModelSelection {
  is_default: boolean;
}

export interface RuntimeModelsOut {
  default: RuntimeModelSelection;
  models: RuntimeModelOption[];
}

// 联网方式:可选项由后端 GET /chat/websearch-options 下发(取决于管理端配了
// 哪几家搜索源),前端不写死服务商列表;undefined = 跟随管理端配置
export interface WebSearchOption {
  value: string;
  label: string;
  ready: boolean;
  hint: string;
}

export interface WebSearchOptionsOut {
  options: WebSearchOption[];
  default_provider: string | null;
  ready: boolean;
}

export const WEB_SEARCH_OFF = "off";

// 运行时临时工具:用户为"本轮"临时点开的只读工具,不写 agent 卡白名单。
// 候选由后端 GET /chat/runtime-tools 按当前 agent 卡下发(只读且不在卡白名单里),
// 前端不硬编码工具名——写工具永远不会出现在候选里,判定底线在后端。
export type RuntimeToolSource = "user_input" | "queued_input";

export interface RuntimeToolCandidate {
  name: string;
  label: string;
  category: string;
  hint: string;
  side_effect: string;
}

export interface RuntimeToolsOut {
  tools: RuntimeToolCandidate[];
  notice: string;
}

export interface RuntimeToolRequest {
  name: string;
  label: string;
  source: RuntimeToolSource;
  reason: string;
  accepted: boolean;
  reject_reason: string | null;
  hint: string;
}

export function sendChatMessage(
  sessionId: string,
  content: string,
  context: { project_id?: string } | undefined,
  onEvent: (event: AgentEvent) => void,
  onRun: (runId: string) => void,
  signal?: AbortSignal,
  action?: AgentContinuationAction,
  thinking?: string,
  model?: RuntimeModelSelection,
  webSearch?: string,
  // run 进行中服务端以 202 入队(不开新流)时回调;未提供则忽略该回执
  onQueued?: (ack: QueuedAck) => void,
  // 本轮临时开启的工具名;入队时随队列行落库,消费那一轮才生效
  runtimeTools?: string[],
): Promise<void> {
  return postSse(
    `/chat/sessions/${sessionId}/messages`,
    {
      content,
      context,
      confirm: isConfirmationAction(action) ? action : undefined,
      override: isReviewerOverrideAction(action) ? action : undefined,
      user_input: isUserInputAction(action) ? action : undefined,
      continue_plan: isPlanContinuationAction(action) ? action : undefined,
      thinking,
      model,
      web_search: webSearch,
      runtime_tools: runtimeTools?.length ? runtimeTools : undefined,
    },
    (data) => onEvent(data as AgentEvent),
    signal,
    true,
    (response) => {
      const runId = response.headers.get("x-agent-run-id");
      if (runId) onRun(runId);
    },
    onQueued
      ? (status, data) => {
          const ack = data as QueuedAck | null;
          if (status === 202 && ack?.queued) onQueued(ack);
        }
      : undefined,
  );
}

export function listQueuedInputs(
  sessionId: string,
): Promise<QueuedInputListOut> {
  return api.get<QueuedInputListOut>(
    `/chat/sessions/${sessionId}/queued-inputs`,
  );
}

export function updateQueuedInput(
  sessionId: string,
  inputId: string,
  content: string,
): Promise<QueuedInputOut> {
  return api.patch<QueuedInputOut>(
    `/chat/sessions/${sessionId}/queued-inputs/${inputId}`,
    { content },
  );
}

export function deleteQueuedInput(
  sessionId: string,
  inputId: string,
): Promise<void> {
  return api.delete(`/chat/sessions/${sessionId}/queued-inputs/${inputId}`);
}

export function reorderQueuedInputs(
  sessionId: string,
  ids: string[],
): Promise<QueuedInputListOut> {
  return api.put<QueuedInputListOut>(
    `/chat/sessions/${sessionId}/queued-inputs/order`,
    { ids },
  );
}

export function getSessionGoal(
  sessionId: string,
): Promise<SessionGoalDetailOut> {
  return api.get<SessionGoalDetailOut>(`/chat/sessions/${sessionId}/goal`);
}

export function setSessionGoal(
  sessionId: string,
  body: SessionGoalIn,
): Promise<SessionGoalOut> {
  return api.put<SessionGoalOut>(`/chat/sessions/${sessionId}/goal`, body);
}

export function cancelSessionGoal(sessionId: string): Promise<SessionGoalOut> {
  return api.post<SessionGoalOut>(`/chat/sessions/${sessionId}/goal/cancel`);
}

// 用户手动标完成:与模型调 update_goal 落到同一种终态,只是 summary 由人给
// (留空时后端注明"由用户手动标记完成")。目标已是终态时后端返回 409。
export function completeSessionGoal(
  sessionId: string,
  summary?: string,
): Promise<SessionGoalOut> {
  return api.post<SessionGoalOut>(
    `/chat/sessions/${sessionId}/goal/complete`,
    summary ? { summary } : {},
  );
}

export const TOOL_LABELS: Record<string, string> = {
  kb_search: "知识库检索",
  project_create: "创建旅行计划",
  project_get: "旅行计划详情",
  project_list: "旅行计划列表",
  project_search: "旅行计划检索",
  customer_search: "客户检索",
  customer_get: "客户档案",
  customer_history: "客户历史",
  demand_parse: "需求解析",
  demand_patch: "回填需求",
  kb_retrieve: "需求驱动检索",
  kb_image_inspect: "核验知识图片",
  business_media_select: "选择业务知识图片",
  itinerary_generate: "生成行程方案",
  itinerary_select: "选定并细化方案",
  itinerary_confirm: "确认行程",
  itinerary_detail_get: "行程明细",
  itinerary_acknowledge: "确认行程事项",
  itinerary_validate: "校验行程",
  quote_generate: "生成报价",
  quote_get: "报价详情",
  quote_cost_item_add: "新增成本行",
  quote_cost_item_update: "修改成本行",
  quote_cost_item_delete: "删除成本行",
  quote_cost_item_lock: "锁定成本行",
  quote_cost_item_unlock: "解锁成本行",
  quote_recalculate: "重算报价",
  quote_validate: "校验报价",
  quote_submit_review: "提交审价",
  quote_comment_add: "审核留言",
  quote_versions_get: "报价版本历史",
  quote_review_trail_get: "审价轨迹",
  ota_compare_run: "发起OTA比价",
  ota_compare_get: "查看比价快照",
  ota_apply: "回填OTA价格",
  itinerary_detail_patch: "编辑行程终版",
  itinerary_unconfirm: "撤销行程确认",
  itinerary_versions_get: "行程版本历史",
  project_status_update: "推进旅行计划状态",
  retrieval_get: "检索依据",
  pipeline_list: "任务列表",
  pipeline_retry: "重试任务",
  pipeline_cancel: "取消任务",
  render_generate: "生成文档",
  render_job_retry: "重试文档任务",
  template_draft: "起草模板",
  template_list: "查看模板",
  export_list: "文档列表",
  export_bundle_create: "打包导出",
  skills_list: "技能列表",
  skill_view: "读取技能",
  ask_user: "向用户确认",
  plan_update: "更新计划",
  update_goal: "更新会话目标",
  cronjob: "定时任务",
  web_search: "联网搜索",
  web_fetch: "网页读取",
  image_generate: "生成图片",
  // 子 agent 委派:后端运行时注入的工具(不在任何卡白名单里)
  Agent: "委派专员",
};

// "Agent" 工具的结果载荷(backend/app/services/agent/sub_agents.py::run_sub_agent);
// 失败/超时只回 error,其余字段可能缺席
export interface SubAgentResultOut {
  agent?: string;
  output?: string;
  turns?: number;
  input_tokens?: number;
  output_tokens?: number;
  stop?: string;
  error?: string;
}

export function toolLabel(name: string): string {
  if (name.startsWith("mcp__"))
    return `MCP · ${name.split("__").slice(1).join(" / ")}`;
  return TOOL_LABELS[name] ?? name;
}
