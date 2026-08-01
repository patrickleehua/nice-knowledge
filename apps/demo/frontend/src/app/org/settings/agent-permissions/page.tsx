"use client";

// 组织 Agent 权限策略配置。策略是不可变版本:每次保存创建新版本并携带 expected_version,
// 版本过期返回 409,此时只提示重载而不覆盖别人的改动(见 docs/operations/agent-permission-policies.md)。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, RotateCcw, Save, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, ErrorState, PageHeader } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ApiError, api } from "@/lib/api";
import {
  PERMISSION_PROFILE_FALLBACK,
  PERMISSION_PROFILE_ORDER,
  type PermissionScope,
} from "@/lib/agent-permissions";
import type { PermissionProfile, ToolCategory } from "@/lib/chat";
import {
  DELEGATION_LABELS,
  MAX_FULL_ACCESS_TTL_SECONDS,
  MAX_GRANT_TTL_SECONDS,
  MIN_TTL_SECONDS,
  PERMISSION_SCOPE_LABELS,
  TOOL_CATEGORY_LABELS,
  TOOL_CATEGORY_ORDER,
  type OrganizationPolicy,
  type PolicyDraft,
  type PolicyIssueField,
  type ToolCatalogItem,
  draftToPayload,
  formatTtlLabel,
  groupToolsByGroup,
  hasPolicyChanges,
  policyRelevantTools,
  policyToDraft,
  toggleValue,
  validatePolicyDraft,
} from "@/lib/org-agent-permissions";
import { errMsg, fmtDateTime } from "@/lib/utils";
import { useUnsavedGuard } from "@/lib/unsaved-guard";

interface PermissionAuditEntry {
  id: string;
  user_id: string | null;
  action: string;
  entity_type: string;
  entity_id: string | null;
  detail: Record<string, unknown>;
  created_at: string | null;
}

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "agent.permission.organization_policy_changed": "策略变更",
  "agent.permission.policy_rolled_back": "策略回滚",
};

const SCOPE_OPTIONS: PermissionScope[] = ["session", "project", "organization"];

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="border-b border-border px-5 py-3.5">
        <h2 className="text-sm font-medium">{title}</h2>
        {description && (
          <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        )}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

function Issues({
  issues,
  field,
}: {
  issues: { field: PolicyIssueField; message: string }[];
  field: PolicyIssueField;
}) {
  const matched = issues.filter((issue) => issue.field === field);
  if (matched.length === 0) return null;
  return (
    <ul className="mt-3 space-y-1 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
      {matched.map((issue) => (
        <li key={issue.message}>{issue.message}</li>
      ))}
    </ul>
  );
}

function CategoryPicker({
  selected,
  onToggle,
  disabled,
}: {
  selected: ToolCategory[];
  onToggle: (category: ToolCategory, on: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
      {TOOL_CATEGORY_ORDER.map((category) => (
        <label
          key={category}
          className="flex cursor-pointer items-center gap-2 text-sm"
        >
          <Checkbox
            checked={selected.includes(category)}
            disabled={disabled}
            onCheckedChange={(checked) => onToggle(category, Boolean(checked))}
          />
          <span className="truncate">{TOOL_CATEGORY_LABELS[category]}</span>
        </label>
      ))}
    </div>
  );
}

function ToolPicker({
  catalog,
  selected,
  onToggle,
  emptyHint,
}: {
  catalog: ToolCatalogItem[];
  selected: string[];
  onToggle: (name: string, on: boolean) => void;
  emptyHint: string;
}) {
  const [showAll, setShowAll] = useState(false);
  // 已选中的自动执行类工具必须始终可见,否则用户无法取消勾选。
  const visible = policyRelevantTools(catalog, showAll).concat(
    showAll
      ? []
      : catalog.filter(
          (tool) =>
            tool.delegation === "automatic" && selected.includes(tool.name),
        ),
  );

  return (
    <div className="space-y-3">
      <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
        <Checkbox
          checked={showAll}
          onCheckedChange={(checked) => setShowAll(Boolean(checked))}
        />
        显示全部工具（默认只列出不能无人值守执行的）
      </label>
      {visible.length === 0 ? (
        <p className="text-xs text-muted-foreground">{emptyHint}</p>
      ) : (
        groupToolsByGroup(visible).map((entry) => (
          <div key={entry.group}>
            <div className="mb-1 text-xs text-muted-foreground">
              {entry.group}
            </div>
            <div className="grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
              {entry.tools.map((tool) => (
                <label
                  key={tool.name}
                  className="flex cursor-pointer items-center gap-2 text-sm"
                  title={tool.description}
                >
                  <Checkbox
                    checked={selected.includes(tool.name)}
                    onCheckedChange={(checked) =>
                      onToggle(tool.name, Boolean(checked))
                    }
                  />
                  <span className="truncate">{tool.label}</span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                    {DELEGATION_LABELS[tool.delegation]}
                  </span>
                </label>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function TtlField({
  label,
  hint,
  value,
  maximum,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  maximum: number;
  onChange: (seconds: number) => void;
}) {
  // 秒是后端契约,但管理员按小时思考;不整除的历史值退回按秒编辑,避免四舍五入改坏数据。
  const editableInHours = value % 3600 === 0;
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
      <div className="min-w-0 flex-1">
        <div className="text-sm">{label}</div>
        <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>
      </div>
      <div className="flex w-56 shrink-0 items-center gap-2">
        <Input
          type="number"
          min={editableInHours ? 1 : MIN_TTL_SECONDS}
          max={editableInHours ? maximum / 3600 : maximum}
          value={editableInHours ? value / 3600 : value}
          onChange={(event) => {
            const raw = Number(event.target.value);
            if (!Number.isFinite(raw)) return;
            onChange(editableInHours ? Math.round(raw) * 3600 : Math.round(raw));
          }}
        />
        <span className="shrink-0 text-xs text-muted-foreground">
          {editableInHours ? "小时" : "秒"}
        </span>
      </div>
      <div className="w-20 shrink-0 text-right text-xs text-muted-foreground">
        上限 {formatTtlLabel(maximum)}
      </div>
    </div>
  );
}

function PolicyEditor({
  policy,
  catalog,
}: {
  policy: OrganizationPolicy;
  catalog: ToolCatalogItem[];
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<PolicyDraft>(() => policyToDraft(policy));
  const dirty = hasPolicyChanges(draft, policy);
  const issues = validatePolicyDraft(draft, {
    reviewerRouteHealthy: policy.reviewer_route_healthy,
    knownTools: catalog.map((tool) => tool.name),
  });
  useUnsavedGuard(dirty, "Agent 权限策略");

  const patch = (next: Partial<PolicyDraft>) =>
    setDraft((prev) => ({ ...prev, ...next }));

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["org-agent-policy"] });

  const save = useMutation({
    mutationFn: () =>
      api.put<OrganizationPolicy>(
        "/org/agent-permissions/policy",
        draftToPayload(draft, policy),
      ),
    onSuccess: (next) => {
      toast.success(`已保存为第 ${next.version} 版`);
      queryClient.setQueryData(["org-agent-policy"], next);
      queryClient.invalidateQueries({ queryKey: ["org-agent-audit"] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast.error("策略已被他人更新，请重新载入后再改", {
          action: { label: "重新载入", onClick: () => refresh() },
        });
        return;
      }
      toast.error(errMsg(error, "保存失败"));
    },
  });

  const rollback = useMutation({
    mutationFn: () =>
      api.post<OrganizationPolicy>("/org/agent-permissions/policy/rollback", {
        expected_version: policy.version,
      }),
    onSuccess: (next) => {
      toast.success(`已降级为请求审批（第 ${next.version} 版）`);
      queryClient.setQueryData(["org-agent-policy"], next);
      queryClient.invalidateQueries({ queryKey: ["org-agent-audit"] });
    },
    onError: (error) => toast.error(errMsg(error, "回滚失败")),
  });

  const busy = save.isPending || rollback.isPending;
  const autoReviewBlocked = !policy.reviewer_route_healthy;

  return (
    <>
      <PageHeader
        title="Agent 权限"
        description={`当前第 ${policy.version} 版 · ${policy.is_enabled ? "已启用" : "已停用"}${
          policy.shadow_evaluation ? " · 影子评估中" : ""
        }`}
        actions={
          <div className="flex items-center gap-2">
            <ConfirmDialog
              trigger={
                <Button variant="outline" size="sm" disabled={busy}>
                  <RotateCcw className="size-3.5" />
                  紧急降级
                </Button>
              }
              title="降级为请求审批？"
              description="将新建一个版本：只保留请求审批、关闭 Reviewer 并停用策略。已在运行的回合使用旧快照，不受影响。"
              destructive
              confirmLabel="确认降级"
              onConfirm={() => rollback.mutateAsync()}
            />
            <Button
              size="sm"
              disabled={!dirty || issues.length > 0 || busy}
              onClick={() => save.mutate()}
            >
              <Save className="size-3.5" />
              保存
            </Button>
          </div>
        }
      />

      <Section
        title="可用模式"
        description="决定会话里的权限选择器能选哪几项；请求审批必须常开"
      >
        <div className="space-y-3">
          {PERMISSION_PROFILE_ORDER.map((profile: PermissionProfile) => {
            const locked = profile === "request_approval";
            const blocked = profile === "auto_review" && autoReviewBlocked;
            return (
              <div key={profile} className="flex items-start gap-3">
                <Switch
                  className="mt-0.5"
                  checked={draft.allowed_profiles.includes(profile)}
                  disabled={locked || blocked}
                  aria-label={PERMISSION_PROFILE_FALLBACK[profile].label}
                  onCheckedChange={(checked) =>
                    patch({
                      allowed_profiles: toggleValue(
                        draft.allowed_profiles,
                        profile,
                        Boolean(checked),
                      ),
                    })
                  }
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-sm">
                    {PERMISSION_PROFILE_FALLBACK[profile].label}
                    {profile === "full_access" && (
                      <span className="inline-flex items-center gap-1 rounded bg-orange-500/15 px-1.5 py-0.5 text-xs text-orange-600 dark:text-orange-400">
                        <ShieldAlert className="size-3" />
                        高风险
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {locked
                      ? "必须常开：任何模式下都要保留人工确认的兜底"
                      : blocked
                        ? "独立 Reviewer 的提示词或模型路由尚未就绪，暂不能开放"
                        : PERMISSION_PROFILE_FALLBACK[profile].description}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
        <Issues issues={issues} field="allowed_profiles" />
      </Section>

      <Section
        title="默认与上限"
        description="新会话的起始模式，以及任何会话都不能突破的范围与有效期"
      >
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <div className="min-w-0 flex-1">
              <div className="text-sm">默认模式</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                完全访问只能由用户在会话内显式启用，不能设为默认
              </div>
            </div>
            <div className="w-56 shrink-0">
              <Select
                value={draft.default_profile}
                onValueChange={(value) =>
                  patch({ default_profile: value as PermissionProfile })
                }
              >
                <SelectTrigger className="w-full">
                  {/* base-ui 的 Value 默认回显原始 value,要拿中文标签得给渲染函数 */}
                  <SelectValue>
                    {(value: PermissionProfile) =>
                      PERMISSION_PROFILE_FALLBACK[value]?.label ?? value
                    }
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {draft.allowed_profiles
                    .filter((profile) => profile !== "full_access")
                    .map((profile) => (
                      <SelectItem key={profile} value={profile}>
                        {PERMISSION_PROFILE_FALLBACK[profile].label}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>
            <div className="w-20 shrink-0" />
          </div>

          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <div className="min-w-0 flex-1">
              <div className="text-sm">最大授权范围</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                单次授权最宽能覆盖到哪一层
              </div>
            </div>
            <div className="w-56 shrink-0">
              <Select
                value={draft.max_scope}
                onValueChange={(value) =>
                  patch({ max_scope: value as PermissionScope })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue>
                    {(value: PermissionScope) =>
                      PERMISSION_SCOPE_LABELS[value] ?? value
                    }
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {SCOPE_OPTIONS.map((scope) => (
                    <SelectItem key={scope} value={scope}>
                      {PERMISSION_SCOPE_LABELS[scope]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="w-20 shrink-0" />
          </div>

          <TtlField
            label="会话授权有效期上限"
            hint="同一工具的免询问授权最长能保留多久"
            value={draft.max_grant_ttl_seconds}
            maximum={MAX_GRANT_TTL_SECONDS}
            onChange={(seconds) => patch({ max_grant_ttl_seconds: seconds })}
          />
          <TtlField
            label="完全访问有效期上限"
            hint="用户启用完全访问后最长能持续多久"
            value={draft.max_full_access_ttl_seconds}
            maximum={MAX_FULL_ACCESS_TTL_SECONDS}
            onChange={(seconds) =>
              patch({ max_full_access_ttl_seconds: seconds })
            }
          />

          <div className="flex items-start gap-3 border-t border-border pt-4">
            <Switch
              className="mt-0.5"
              checked={draft.is_enabled}
              aria-label="启用策略"
              onCheckedChange={(checked) =>
                patch({ is_enabled: Boolean(checked) })
              }
            />
            <div className="min-w-0 flex-1">
              <div className="text-sm">启用本策略</div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                关闭后全部会话回落到请求审批
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <Switch
              className="mt-0.5"
              checked={draft.shadow_evaluation}
              aria-label="影子评估"
              onCheckedChange={(checked) =>
                patch({ shadow_evaluation: Boolean(checked) })
              }
            />
            <div className="min-w-0 flex-1">
              <div className="text-sm">影子评估</div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                只记录新策略会怎么判，实际仍走原确认门槛；放开新模式前先跑一段
              </p>
            </div>
          </div>
        </div>
        <Issues issues={issues} field="default_profile" />
        <Issues issues={issues} field="limits" />
      </Section>

      <Section
        title="独立 Reviewer"
        description="智能审批由独立模型复核敏感操作；未配置可审批范围时不能开放该模式"
      >
        <div className="space-y-4">
          <div className="flex items-start gap-3">
            <Switch
              className="mt-0.5"
              checked={draft.reviewer_enabled}
              disabled={autoReviewBlocked}
              aria-label="启用独立 Reviewer"
              onCheckedChange={(checked) =>
                patch({ reviewer_enabled: Boolean(checked) })
              }
            />
            <div className="min-w-0 flex-1">
              <div className="text-sm">启用独立 Reviewer</div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {autoReviewBlocked
                  ? "提示词或模型路由未就绪，请先在平台管理端配置 agent.approval_review"
                  : "Reviewer 无工具、不能改动作，只能批准、拒绝或升级给用户"}
              </p>
            </div>
          </div>

          <div>
            <div className="mb-2 text-sm">可由 Reviewer 审批的分类</div>
            <CategoryPicker
              selected={draft.reviewer_eligible_categories}
              disabled={!draft.reviewer_enabled}
              onToggle={(category, on) =>
                patch({
                  reviewer_eligible_categories: toggleValue(
                    draft.reviewer_eligible_categories,
                    category,
                    on,
                  ),
                })
              }
            />
          </div>

          <div>
            <div className="mb-2 text-sm">可由 Reviewer 审批的具体工具</div>
            <ToolPicker
              catalog={catalog}
              selected={draft.reviewer_eligible_tools}
              emptyHint="没有需要审批的工具"
              onToggle={(name, on) =>
                patch({
                  reviewer_eligible_tools: toggleValue(
                    draft.reviewer_eligible_tools,
                    name,
                    on,
                  ),
                })
              }
            />
          </div>
        </div>
        <Issues issues={issues} field="reviewer" />
      </Section>

      <Section
        title="硬规则"
        description="优先级高于任何会话选择：禁止的永远不执行，必须确认的永远问用户"
      >
        <div className="space-y-5">
          <div>
            <div className="mb-2 text-sm">禁止 Agent 执行的分类</div>
            <CategoryPicker
              selected={draft.denied_categories}
              onToggle={(category, on) =>
                patch({
                  denied_categories: toggleValue(
                    draft.denied_categories,
                    category,
                    on,
                  ),
                })
              }
            />
          </div>
          <div>
            <div className="mb-2 text-sm">禁止 Agent 执行的工具</div>
            <ToolPicker
              catalog={catalog}
              selected={draft.denied_tools}
              emptyHint="没有可禁用的工具"
              onToggle={(name, on) =>
                patch({
                  denied_tools: toggleValue(draft.denied_tools, name, on),
                })
              }
            />
          </div>
          <div className="border-t border-border pt-4">
            <div className="mb-2 text-sm">每次必须由用户确认的分类</div>
            <CategoryPicker
              selected={draft.user_required_categories}
              onToggle={(category, on) =>
                patch({
                  user_required_categories: toggleValue(
                    draft.user_required_categories,
                    category,
                    on,
                  ),
                })
              }
            />
          </div>
          <div>
            <div className="mb-2 text-sm">每次必须由用户确认的工具</div>
            <ToolPicker
              catalog={catalog}
              selected={draft.user_required_tools}
              emptyHint="没有可选的工具"
              onToggle={(name, on) =>
                patch({
                  user_required_tools: toggleValue(
                    draft.user_required_tools,
                    name,
                    on,
                  ),
                })
              }
            />
          </div>
          <div className="border-t border-border pt-4">
            <div className="mb-1 text-sm">Reviewer 拒绝后允许用户覆盖的分类</div>
            <p className="mb-2 text-xs text-muted-foreground">
              必须是上面「可由 Reviewer 审批的分类」的子集
            </p>
            <CategoryPicker
              selected={draft.reviewer_overridable_categories}
              onToggle={(category, on) =>
                patch({
                  reviewer_overridable_categories: toggleValue(
                    draft.reviewer_overridable_categories,
                    category,
                    on,
                  ),
                })
              }
            />
          </div>
        </div>
        <Issues issues={issues} field="hard_rules" />
      </Section>
    </>
  );
}

function AuditSection() {
  const audit = useQuery({
    queryKey: ["org-agent-audit"],
    queryFn: () =>
      api.get<PermissionAuditEntry[]>(
        "/org/agent-permissions/audit?action=agent.permission.organization_policy_changed&limit=20",
      ),
  });

  return (
    <Section title="策略变更记录" description="最近 20 条组织策略调整">
      {audit.isPending ? (
        <Skeleton className="h-20" />
      ) : audit.error ? (
        <p className="text-xs text-muted-foreground">
          {errMsg(audit.error, "变更记录加载失败")}
        </p>
      ) : audit.data && audit.data.length > 0 ? (
        <ul className="space-y-2">
          {audit.data.map((entry) => (
            <li
              key={entry.id}
              className="flex items-center gap-3 text-xs text-muted-foreground"
            >
              <History className="size-3.5 shrink-0" />
              <span className="shrink-0 text-foreground">
                {AUDIT_ACTION_LABELS[entry.action] ?? entry.action}
              </span>
              <span className="truncate">{entry.entity_id ?? ""}</span>
              <span className="ml-auto shrink-0">
                {fmtDateTime(entry.created_at)}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">暂无变更记录</p>
      )}
    </Section>
  );
}

export default function OrgAgentPermissionsPage() {
  const policyQuery = useQuery({
    queryKey: ["org-agent-policy"],
    queryFn: () =>
      api.get<OrganizationPolicy>("/org/agent-permissions/policy"),
  });
  const catalogQuery = useQuery({
    queryKey: ["org-agent-tool-catalog"],
    queryFn: () =>
      api.get<ToolCatalogItem[]>("/org/agent-permissions/tool-catalog"),
  });

  const error = policyQuery.error ?? catalogQuery.error;
  const ready = policyQuery.data && catalogQuery.data;

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      {error ? (
        <>
          <PageHeader title="Agent 权限" />
          <ErrorState
            error={error}
            onRetry={() => {
              policyQuery.refetch();
              catalogQuery.refetch();
            }}
          />
        </>
      ) : !ready ? (
        <>
          <PageHeader title="Agent 权限" />
          <Skeleton className="h-48 rounded-lg" />
          <Skeleton className="h-64 rounded-lg" />
        </>
      ) : (
        <>
          {/* 版本变了就重挂载,草稿随之重置,不必用 effect 同步 state */}
          <PolicyEditor
            key={policyQuery.data.version}
            policy={policyQuery.data}
            catalog={catalogQuery.data}
          />
          <AuditSection />
        </>
      )}
    </div>
  );
}
