// 组织 Agent 权限策略(/org/settings/agent-permissions)的草稿模型与提交前校验。
// 校验逐条对齐后端 create_organization_policy_version 与 OrganizationPolicyUpdateIn,
// 让管理员在点保存前就看到问题,而不是提交后吃一个 422。

import type { PermissionScope } from "@/lib/agent-permissions";
import type {
  PermissionDecision,
  PermissionProfile,
  ToolCategory,
  ToolRisk,
} from "@/lib/chat";

export type ToolEffect = "read" | "write" | "delete" | "transition";

export type ToolDelegation =
  | "automatic"
  | "reviewable"
  | "user_required"
  | "agent_forbidden";

export interface ToolCatalogItem {
  name: string;
  label: string;
  group: string;
  description: string;
  effect: ToolEffect;
  risk: ToolRisk;
  categories: ToolCategory[];
  delegation: ToolDelegation;
}

export interface PolicyHardRules {
  compatibility_mode?: boolean;
  tool_decisions?: Record<string, PermissionDecision>;
  category_decisions?: Partial<Record<ToolCategory, PermissionDecision>>;
  denied_tools?: string[];
  denied_categories?: ToolCategory[];
  user_required_tools?: string[];
  user_required_categories?: ToolCategory[];
  reviewer_overridable_categories?: ToolCategory[];
}

export interface OrganizationPolicy {
  id: string | null;
  version: number;
  default_profile: PermissionProfile;
  allowed_profiles: PermissionProfile[];
  hard_rules: PolicyHardRules;
  reviewer_enabled: boolean;
  reviewer_eligible_categories: ToolCategory[];
  reviewer_eligible_tools: string[];
  max_scope: PermissionScope;
  max_grant_ttl_seconds: number;
  max_full_access_ttl_seconds: number;
  is_enabled: boolean;
  shadow_evaluation: boolean;
  is_active: boolean;
  created_at: string | null;
  reviewer_route_healthy: boolean;
}

/** 页面可编辑的字段;hard_rules 里不开放编辑的部分不进草稿,保存时原样回填。 */
export interface PolicyDraft {
  allowed_profiles: PermissionProfile[];
  default_profile: PermissionProfile;
  reviewer_enabled: boolean;
  reviewer_eligible_categories: ToolCategory[];
  reviewer_eligible_tools: string[];
  denied_categories: ToolCategory[];
  denied_tools: string[];
  user_required_categories: ToolCategory[];
  user_required_tools: string[];
  reviewer_overridable_categories: ToolCategory[];
  max_scope: PermissionScope;
  max_grant_ttl_seconds: number;
  max_full_access_ttl_seconds: number;
  is_enabled: boolean;
  shadow_evaluation: boolean;
}

export interface PolicyUpdatePayload {
  expected_version: number;
  default_profile: PermissionProfile;
  allowed_profiles: PermissionProfile[];
  hard_rules: PolicyHardRules;
  reviewer_enabled: boolean;
  reviewer_eligible_categories: ToolCategory[];
  reviewer_eligible_tools: string[];
  max_scope: PermissionScope;
  max_grant_ttl_seconds: number;
  max_full_access_ttl_seconds: number;
  is_enabled: boolean;
  shadow_evaluation: boolean;
}

export type PolicyIssueField =
  | "allowed_profiles"
  | "default_profile"
  | "reviewer"
  | "hard_rules"
  | "limits";

export interface PolicyIssue {
  field: PolicyIssueField;
  message: string;
}

// 与后端 OrganizationPolicyUpdateIn 的 Field 约束一致(ge=60 比 management 的 >0 更严)。
export const MIN_TTL_SECONDS = 60;
export const MAX_GRANT_TTL_SECONDS = 30 * 24 * 3600;
export const MAX_FULL_ACCESS_TTL_SECONDS = 24 * 3600;

export const TOOL_CATEGORY_LABELS: Record<ToolCategory, string> = {
  local_data: "数据写入",
  network: "外部检索",
  external_cost: "付费调用",
  financial: "财务变更",
  destructive: "破坏性操作",
  workflow: "流程状态",
  export: "导出与交付",
};

export const TOOL_CATEGORY_ORDER: ToolCategory[] = [
  "local_data",
  "network",
  "external_cost",
  "financial",
  "destructive",
  "workflow",
  "export",
];

export const PERMISSION_SCOPE_LABELS: Record<PermissionScope, string> = {
  session: "仅当前会话",
  project: "最大到旅行计划",
  organization: "最大到组织",
};

export const DELEGATION_LABELS: Record<ToolDelegation, string> = {
  automatic: "可自动执行",
  reviewable: "需审批",
  user_required: "必须用户确认",
  agent_forbidden: "禁止 Agent 调用",
};

export function policyToDraft(policy: OrganizationPolicy): PolicyDraft {
  const rules = policy.hard_rules ?? {};
  return {
    allowed_profiles: [...policy.allowed_profiles],
    default_profile: policy.default_profile,
    reviewer_enabled: policy.reviewer_enabled,
    reviewer_eligible_categories: [...policy.reviewer_eligible_categories],
    reviewer_eligible_tools: [...policy.reviewer_eligible_tools],
    denied_categories: [...(rules.denied_categories ?? [])],
    denied_tools: [...(rules.denied_tools ?? [])],
    user_required_categories: [...(rules.user_required_categories ?? [])],
    user_required_tools: [...(rules.user_required_tools ?? [])],
    reviewer_overridable_categories: [
      ...(rules.reviewer_overridable_categories ?? []),
    ],
    max_scope: policy.max_scope,
    max_grant_ttl_seconds: policy.max_grant_ttl_seconds,
    max_full_access_ttl_seconds: policy.max_full_access_ttl_seconds,
    is_enabled: policy.is_enabled,
    shadow_evaluation: policy.shadow_evaluation,
  };
}

/**
 * 草稿转提交体。PUT 会整体替换 hard_rules,因此页面不编辑的
 * compatibility_mode / tool_decisions / category_decisions 必须原样带回,
 * 否则一次保存就会把它们静默清空。
 */
export function draftToPayload(
  draft: PolicyDraft,
  policy: OrganizationPolicy,
): PolicyUpdatePayload {
  const rules = policy.hard_rules ?? {};
  const preserved: PolicyHardRules = {};
  if (rules.compatibility_mode !== undefined) {
    preserved.compatibility_mode = rules.compatibility_mode;
  }
  if (rules.tool_decisions) preserved.tool_decisions = rules.tool_decisions;
  if (rules.category_decisions) {
    preserved.category_decisions = rules.category_decisions;
  }
  return {
    expected_version: policy.version,
    default_profile: draft.default_profile,
    allowed_profiles: [...draft.allowed_profiles],
    hard_rules: {
      ...preserved,
      denied_categories: [...draft.denied_categories],
      denied_tools: [...draft.denied_tools],
      user_required_categories: [...draft.user_required_categories],
      user_required_tools: [...draft.user_required_tools],
      reviewer_overridable_categories: [
        ...draft.reviewer_overridable_categories,
      ],
    },
    reviewer_enabled: draft.reviewer_enabled,
    reviewer_eligible_categories: [...draft.reviewer_eligible_categories],
    reviewer_eligible_tools: [...draft.reviewer_eligible_tools],
    max_scope: draft.max_scope,
    max_grant_ttl_seconds: draft.max_grant_ttl_seconds,
    max_full_access_ttl_seconds: draft.max_full_access_ttl_seconds,
    is_enabled: draft.is_enabled,
    shadow_evaluation: draft.shadow_evaluation,
  };
}

function ttlIssue(value: number, maximum: number, label: string): string | null {
  if (!Number.isInteger(value)) return `${label}必须是整数秒`;
  if (value < MIN_TTL_SECONDS) return `${label}不得短于 ${MIN_TTL_SECONDS} 秒`;
  if (value > maximum) return `${label}不得超过上限`;
  return null;
}

export function validatePolicyDraft(
  draft: PolicyDraft,
  options: {
    reviewerRouteHealthy: boolean;
    knownTools: readonly string[];
  },
): PolicyIssue[] {
  const issues: PolicyIssue[] = [];
  const allowed = new Set(draft.allowed_profiles);

  if (!allowed.has("request_approval")) {
    issues.push({
      field: "allowed_profiles",
      message: "请求审批必须始终可用,不能关闭",
    });
  }
  if (!allowed.has(draft.default_profile)) {
    issues.push({
      field: "default_profile",
      message: "默认模式必须在已开放的模式之中",
    });
  }
  if (draft.default_profile === "full_access") {
    issues.push({
      field: "default_profile",
      message: "完全访问必须由用户在会话内显式启用,不能设为组织默认",
    });
  }

  if (allowed.has("auto_review")) {
    if (!draft.reviewer_enabled) {
      issues.push({
        field: "reviewer",
        message: "开放智能审批前必须启用独立 Reviewer",
      });
    }
    if (
      draft.reviewer_eligible_categories.length === 0 &&
      draft.reviewer_eligible_tools.length === 0
    ) {
      issues.push({
        field: "reviewer",
        message: "开放智能审批前必须配置 Reviewer 可审批的分类或工具",
      });
    }
    if (!options.reviewerRouteHealthy) {
      issues.push({
        field: "reviewer",
        message: "独立 Reviewer 的提示词或模型路由尚未就绪",
      });
    }
  }

  const eligible = new Set(draft.reviewer_eligible_categories);
  const stray = draft.reviewer_overridable_categories.filter(
    (category) => !eligible.has(category),
  );
  if (stray.length > 0) {
    const names = stray.map((item) => TOOL_CATEGORY_LABELS[item]).join("、");
    issues.push({
      field: "hard_rules",
      message: `可覆盖分类必须同时属于 Reviewer 可审批分类:${names}`,
    });
  }

  const known = new Set(options.knownTools);
  const unknown = [
    ...draft.reviewer_eligible_tools,
    ...draft.denied_tools,
    ...draft.user_required_tools,
  ].filter((name) => !known.has(name));
  if (unknown.length > 0) {
    issues.push({
      field: "hard_rules",
      message: `包含未知工具:${[...new Set(unknown)].sort().join(", ")}`,
    });
  }

  const grantIssue = ttlIssue(
    draft.max_grant_ttl_seconds,
    MAX_GRANT_TTL_SECONDS,
    "会话授权有效期",
  );
  if (grantIssue) issues.push({ field: "limits", message: grantIssue });
  const fullAccessIssue = ttlIssue(
    draft.max_full_access_ttl_seconds,
    MAX_FULL_ACCESS_TTL_SECONDS,
    "完全访问有效期",
  );
  if (fullAccessIssue) {
    issues.push({ field: "limits", message: fullAccessIssue });
  }

  return issues;
}

function normalized(draft: PolicyDraft): string {
  // 勾选顺序不算改动,比较前按值排序。
  const sorted = <T extends string>(values: T[]): T[] => [...values].sort();
  return JSON.stringify({
    allowed_profiles: sorted(draft.allowed_profiles),
    default_profile: draft.default_profile,
    reviewer_enabled: draft.reviewer_enabled,
    reviewer_eligible_categories: sorted(draft.reviewer_eligible_categories),
    reviewer_eligible_tools: sorted(draft.reviewer_eligible_tools),
    denied_categories: sorted(draft.denied_categories),
    denied_tools: sorted(draft.denied_tools),
    user_required_categories: sorted(draft.user_required_categories),
    user_required_tools: sorted(draft.user_required_tools),
    reviewer_overridable_categories: sorted(
      draft.reviewer_overridable_categories,
    ),
    max_scope: draft.max_scope,
    max_grant_ttl_seconds: draft.max_grant_ttl_seconds,
    max_full_access_ttl_seconds: draft.max_full_access_ttl_seconds,
    is_enabled: draft.is_enabled,
    shadow_evaluation: draft.shadow_evaluation,
  });
}

export function hasPolicyChanges(
  draft: PolicyDraft,
  policy: OrganizationPolicy,
): boolean {
  return normalized(draft) !== normalized(policyToDraft(policy));
}

/**
 * 硬规则只对"不能无脑自动执行"的工具有意义,53 个工具里只有个位数属于此类。
 * 默认收起其余工具,避免管理员在几十个只读工具里翻找。
 */
export function policyRelevantTools(
  catalog: readonly ToolCatalogItem[],
  includeAutomatic = false,
): ToolCatalogItem[] {
  return catalog.filter(
    (tool) => includeAutomatic || tool.delegation !== "automatic",
  );
}

export function groupToolsByGroup(
  tools: readonly ToolCatalogItem[],
): { group: string; tools: ToolCatalogItem[] }[] {
  const groups = new Map<string, ToolCatalogItem[]>();
  for (const tool of tools) {
    groups.set(tool.group, [...(groups.get(tool.group) ?? []), tool]);
  }
  return [...groups.entries()].map(([group, items]) => ({
    group,
    tools: items,
  }));
}

export function formatTtlLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`;
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

export function toggleValue<T>(values: T[], value: T, on: boolean): T[] {
  const next = values.filter((item) => item !== value);
  return on ? [...next, value] : next;
}
