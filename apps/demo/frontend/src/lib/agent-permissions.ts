import type {
  PermissionDecision,
  PermissionProfile,
  PendingConfirmation,
  ReviewerOverride,
  ToolCategory,
} from "@/lib/chat";

export type PermissionScope = "session" | "project" | "organization";

export interface PermissionProfileOption {
  id: PermissionProfile;
  label: string;
  description: string;
  allowed: boolean;
  restriction: string | null;
}

export interface OrganizationPermissionConstraints {
  policy_id: string | null;
  policy_version: number;
  is_enabled: boolean;
  shadow_evaluation: boolean;
  max_scope: PermissionScope;
  max_grant_ttl_seconds: number;
  max_full_access_ttl_seconds: number;
  reviewer_enabled: boolean;
  reviewer_eligible_categories: ToolCategory[];
  reviewer_eligible_tools: string[];
  denied_categories: ToolCategory[];
  denied_tools: string[];
  user_required_categories: ToolCategory[];
  user_required_tools: string[];
}

export interface PermissionGrant {
  id: string;
  session_id: string | null;
  project_id: string | null;
  tool_name: string | null;
  category: ToolCategory | null;
  scope: PermissionScope;
  resource_type: string | null;
  resource_id: string | null;
  policy_version: number;
  created_at: string | null;
  expires_at: string;
  revoked_at: string | null;
}

export interface SessionPermissionState {
  session_id: string;
  revision: number;
  profile: PermissionProfile;
  scope: PermissionScope;
  expires_at: string | null;
  active_run: boolean;
  project_id: string | null;
  custom_rules: Partial<Record<ToolCategory, PermissionDecision>>;
  policy_snapshot: Record<string, unknown>;
  profile_options: PermissionProfileOption[];
  organization: OrganizationPermissionConstraints;
  grants: PermissionGrant[];
  pending_decision: PendingConfirmation | null;
  reviewer_overrides: ReviewerOverride[];
}

export interface SessionPermissionUpdate {
  expected_revision: number;
  expected_policy_version: number;
  profile: PermissionProfile;
  scope: PermissionScope;
  custom_rules?: Partial<Record<ToolCategory, PermissionDecision>>;
  expires_in_seconds?: number;
  acknowledge_full_access?: boolean;
}

export type DeferredSessionPermissionUpdate = Omit<
  SessionPermissionUpdate,
  "expected_revision" | "expected_policy_version"
>;

export const PERMISSION_PROFILE_FALLBACK = {
  request_approval: {
    label: "请求审批",
    description: "写入、联网和付费操作由你确认",
  },
  auto_review: {
    label: "智能审批",
    description: "常规操作自动执行，敏感操作由独立 Reviewer 判断",
  },
  full_access: {
    label: "完全访问（业务范围内）",
    description: "在当前业务授权、范围和有效期内免询问执行",
  },
  custom: {
    label: "自定义",
    description: "按业务操作类别设置审批方式",
  },
} satisfies Record<
  PermissionProfile,
  { label: string; description: string }
>;

export const PERMISSION_PROFILE_ORDER: PermissionProfile[] = [
  "request_approval",
  "auto_review",
  "full_access",
  "custom",
];

export const CUSTOM_PERMISSION_GROUPS = [
  {
    id: "data_writes",
    label: "数据写入",
    description: "创建或修改旅行计划、需求、行程与报价草稿",
    categories: ["local_data"],
  },
  {
    id: "network_paid",
    label: "联网与付费操作",
    description: "外部检索、OTA 比价与图片生成",
    categories: ["network", "external_cost"],
  },
  {
    id: "financial",
    label: "财务变更",
    description: "成本、报价与商业金额相关操作",
    categories: ["financial"],
  },
  {
    id: "destructive",
    label: "破坏性操作",
    description: "删除、撤销或可能造成数据损失的操作",
    categories: ["destructive"],
  },
  {
    id: "workflow",
    label: "流程状态",
    description: "提交审核、回退确认与旅行计划状态推进",
    categories: ["workflow"],
  },
  {
    id: "exports",
    label: "导出与交付",
    description: "生成或导出可交付文件与数据",
    categories: ["export"],
  },
] as const satisfies readonly {
  id: string;
  label: string;
  description: string;
  categories: readonly ToolCategory[];
}[];

export const CUSTOM_DECISION_OPTIONS: {
  value: PermissionDecision;
  label: string;
}[] = [
  { value: "allow", label: "自动执行" },
  { value: "auto_review", label: "智能审批" },
  { value: "ask_user", label: "请求审批" },
  { value: "deny", label: "禁止" },
];

export function effectiveGroupDecision(
  rules: Partial<Record<ToolCategory, PermissionDecision>>,
  categories: readonly ToolCategory[],
): PermissionDecision {
  const values = categories.map((category) => rules[category]);
  const first = values[0];
  return first && values.every((value) => value === first)
    ? first
    : "ask_user";
}

export function resolvedPermissionProfileOptions(
  state: SessionPermissionState,
): PermissionProfileOption[] {
  const options = new Map(
    state.profile_options.map((option) => [option.id, option]),
  );
  return PERMISSION_PROFILE_ORDER.map(
    (profile) =>
      options.get(profile) ?? {
        id: profile,
        ...PERMISSION_PROFILE_FALLBACK[profile],
        allowed: false,
        restriction: "组织未开放该模式",
      },
  );
}

export function completeCustomPermissionRules(
  rules: Partial<Record<ToolCategory, PermissionDecision>>,
): Partial<Record<ToolCategory, PermissionDecision>> {
  const complete = { ...rules };
  for (const group of CUSTOM_PERMISSION_GROUPS) {
    const decision = effectiveGroupDecision(rules, group.categories);
    for (const category of group.categories) complete[category] = decision;
  }
  return complete;
}

export function permissionControlUnavailable(
  state: SessionPermissionState | undefined,
  flags: { loading?: boolean; pending?: boolean },
): boolean {
  return !state || !!flags.loading || !!flags.pending;
}

export function deferPermissionUpdate(
  update: SessionPermissionUpdate,
): DeferredSessionPermissionUpdate {
  return {
    profile: update.profile,
    scope: update.scope,
    ...(update.custom_rules === undefined
      ? {}
      : { custom_rules: update.custom_rules }),
    ...(update.expires_in_seconds === undefined
      ? {}
      : { expires_in_seconds: update.expires_in_seconds }),
    ...(update.acknowledge_full_access === undefined
      ? {}
      : { acknowledge_full_access: update.acknowledge_full_access }),
  };
}

export function rebasePermissionUpdate(
  state: SessionPermissionState,
  update: DeferredSessionPermissionUpdate,
): SessionPermissionUpdate {
  return {
    ...permissionUpdateBase(state),
    ...update,
  };
}

export function buildFullAccessPermissionUpdate(
  state: SessionPermissionState,
  scope: PermissionScope,
  expiresInSeconds: number,
): SessionPermissionUpdate | null {
  const scopeAllowed =
    scope === "session" ||
    (scope === "project" &&
      !!state.project_id &&
      state.organization.max_scope !== "session");
  if (
    !scopeAllowed ||
    !Number.isInteger(expiresInSeconds) ||
    expiresInSeconds <= 0 ||
    expiresInSeconds > state.organization.max_full_access_ttl_seconds
  )
    return null;
  return {
    ...permissionUpdateBase(state),
    profile: "full_access",
    scope,
    expires_in_seconds: expiresInSeconds,
    acknowledge_full_access: true,
  };
}

export function canRevokePermissionGrant(
  state: SessionPermissionState,
  grantId: string,
  busy = false,
): boolean {
  return (
    !busy &&
    !state.active_run &&
    state.grants.some(
      (grant) => grant.id === grantId && grant.revoked_at === null,
    )
  );
}

export function permissionUpdateBase(
  state: SessionPermissionState,
): Pick<
  SessionPermissionUpdate,
  "expected_revision" | "expected_policy_version"
> {
  return {
    expected_revision: state.revision,
    expected_policy_version: state.organization.policy_version,
  };
}

export function formatPermissionExpiry(value: string | null): string | null {
  if (!value) return null;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}
