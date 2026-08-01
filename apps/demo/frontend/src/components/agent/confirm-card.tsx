"use client";

import {
  Check,
  ChevronRight,
  Clock3,
  LockKeyhole,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  buildBundleConfirmationAction,
  legacyConfirmationAction,
  type DraftApprovalDecision,
} from "@/lib/approval-decisions";
import {
  isApprovalBundle,
  toolLabel,
  type ApprovalBundle,
  type ApprovalBundleItem,
  type ConfirmationAction,
  type LegacyPendingConfirmation,
  type PendingConfirmation,
  type ToolCategory,
  type ToolRisk,
} from "@/lib/chat";
import { cn } from "@/lib/utils";

const riskCopy: Record<ToolRisk, string> = {
  routine: "常规",
  sensitive: "敏感",
  critical: "高风险",
};

const categoryCopy: Record<ToolCategory, string> = {
  local_data: "业务数据",
  network: "联网",
  external_cost: "付费调用",
  financial: "财务",
  destructive: "破坏性",
  workflow: "流程状态",
  export: "导出",
};

const reasonCopy: Record<string, string> = {
  profile_request_approval: "当前模式要求确认此类操作",
  compatibility_confirmation: "兼容模式保留原确认边界",
  organization_user_required: "组织策略要求由用户确认",
  tool_user_required: "该业务动作必须由用户确认",
  general_scope_mutation: "通用会话修改既有业务作用域需要确认",
  reviewer_not_eligible: "该操作不在智能审批范围内",
  reviewer_unavailable: "独立 Reviewer 当前不可用",
  reviewer_malformed: "独立 Reviewer 未返回有效决定",
  reviewer_escalated: "独立 Reviewer 建议由你决定",
  profile_critical: "关键业务操作需要确认",
  custom_missing_rule: "自定义模式尚未配置此类别",
  custom_rule: "自定义规则要求确认",
};

function targetLabel(item: ApprovalBundleItem) {
  if (item.scope?.resource_type && item.scope.resource_id)
    return `${item.scope.resource_type} · ${item.scope.resource_id.slice(0, 8)}`;
  if (item.scope?.target_scope_id)
    return `作用域 · ${item.scope.target_scope_id.slice(0, 8)}`;
  return item.scope?.mode === "general" ? "通用会话" : "当前作用域";
}

function RiskBadge({ risk }: { risk: ToolRisk }) {
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset",
        risk === "critical"
          ? "bg-red-500/8 text-red-700 ring-red-500/20 dark:text-red-300"
          : risk === "sensitive"
            ? "bg-amber-500/8 text-amber-700 ring-amber-500/20 dark:text-amber-300"
            : "bg-foreground/[0.04] text-muted-foreground ring-border/60",
      )}
    >
      {riskCopy[risk]}
    </span>
  );
}

function LegacyConfirmation({
  pending,
  active,
  busy,
  onDecide,
}: {
  pending: LegacyPendingConfirmation;
  active: boolean;
  busy: boolean;
  onDecide?: (decision: ConfirmationAction) => void;
}) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-2xl bg-muted/30 ring-1 ring-inset",
        active ? "ring-amber-500/30" : "ring-border/50",
      )}
    >
      <div className="flex items-start gap-3 p-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-300">
          <ShieldAlert className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">
            {active ? "需要你的确认" : "确认已处理"}
          </p>
          <p className="mt-1 text-sm leading-6">{pending.summary}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {toolLabel(pending.name)}
          </p>
        </div>
      </div>
      {active && onDecide && (
        <div className="flex justify-end gap-2 border-t border-border/45 px-4 py-3">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onDecide(legacyConfirmationAction(pending, false))}
          >
            <X className="size-3.5" />
            拒绝
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => onDecide(legacyConfirmationAction(pending, true))}
          >
            <Check className="size-3.5" />
            仅此次允许
          </Button>
        </div>
      )}
      <details className="group border-t border-border/45">
        <summary className="flex cursor-pointer list-none items-center gap-1.5 px-4 py-2 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50">
          高级参数
          <ChevronRight className="ml-auto size-3.5 transition-transform group-open:rotate-90" />
        </summary>
        <pre className="max-h-48 overflow-auto border-t border-border/45 bg-background/45 p-3 font-mono text-[11px] leading-5 whitespace-pre-wrap">
          {JSON.stringify(pending.input, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function BundleItemRow({
  item,
  active,
  disabled,
  decision,
  onChange,
}: {
  item: ApprovalBundleItem;
  active: boolean;
  disabled: boolean;
  decision?: DraftApprovalDecision;
  onChange: (decision: DraftApprovalDecision) => void;
}) {
  const pending = item.status === "pending";
  return (
    <li className="border-b border-border/45 px-4 py-3.5 last:border-b-0">
      <div className="flex items-start gap-3">
        <span
          className={cn(
            "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg",
            item.reviewer_decision
              ? "bg-violet-500/9 text-violet-700 dark:text-violet-300"
              : "bg-foreground/[0.05] text-muted-foreground",
          )}
        >
          {item.reviewer_decision ? (
            <Sparkles className="size-3.5" />
          ) : (
            <ShieldAlert className="size-3.5" />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="mr-1 text-sm font-medium">
              {item.label || toolLabel(item.name)}
            </p>
            <RiskBadge risk={item.risk} />
            {item.categories.slice(0, 2).map((category) => (
              <span
                key={category}
                className="rounded-full bg-foreground/[0.04] px-2 py-0.5 text-[10px] text-muted-foreground"
              >
                {categoryCopy[category]}
              </span>
            ))}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {targetLabel(item)} · {reasonCopy[item.reason_code] ?? item.reason_code}
          </p>
          {item.reviewer_rationale && (
            <p className="mt-2 rounded-lg bg-violet-500/6 px-2.5 py-2 text-xs leading-5 text-violet-950 dark:text-violet-100">
              Reviewer：{item.reviewer_rationale}
            </p>
          )}
          {active && pending && (
            <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label="审批决定">
              <button
                type="button"
                disabled={disabled}
                onClick={() => onChange({ decision: "deny", scope: "once" })}
                className={cn(
                  "rounded-lg px-2.5 py-1.5 text-xs font-medium ring-1 ring-inset outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
                  decision?.decision === "deny"
                    ? "bg-red-500/10 text-red-700 ring-red-500/25 dark:text-red-300"
                    : "text-muted-foreground ring-border/70 hover:bg-foreground/[0.04] hover:text-foreground",
                )}
              >
                拒绝
              </button>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onChange({ decision: "approve", scope: "once" })}
                className={cn(
                  "rounded-lg px-2.5 py-1.5 text-xs font-medium ring-1 ring-inset outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
                  decision?.decision === "approve" && decision.scope === "once"
                    ? "bg-foreground text-background ring-foreground"
                    : "text-muted-foreground ring-border/70 hover:bg-foreground/[0.04] hover:text-foreground",
                )}
              >
                仅此次
              </button>
              {item.eligible_scopes.includes("session_tool") && (
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() =>
                    onChange({ decision: "approve", scope: "session_tool" })
                  }
                  className={cn(
                    "rounded-lg px-2.5 py-1.5 text-xs font-medium ring-1 ring-inset outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
                    decision?.decision === "approve" &&
                      decision.scope === "session_tool"
                      ? "bg-foreground text-background ring-foreground"
                      : "text-muted-foreground ring-border/70 hover:bg-foreground/[0.04] hover:text-foreground",
                  )}
                >
                  本会话允许
                </button>
              )}
            </div>
          )}
          {decision?.decision === "deny" && active && pending && (
            <input
              value={decision.note ?? ""}
              maxLength={500}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...decision, note: event.target.value })
              }
              placeholder="可选：告诉 Agent 为什么不执行"
              aria-label="拒绝说明"
              className="mt-2 h-8 w-full rounded-lg border border-input bg-background px-2.5 text-xs outline-none placeholder:text-muted-foreground/70 focus-visible:ring-2 focus-visible:ring-ring/50"
            />
          )}
          {!pending && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
              {item.status === "denied" ? (
                <X className="size-3.5 text-red-600" />
              ) : (
                <Check className="size-3.5" />
              )}
              {item.status === "denied"
                ? "已拒绝"
                : item.decision_scope === "session_tool"
                  ? "已允许，并记住到本会话"
                  : item.status === "allowed"
                    ? "策略已允许，等待本组决定后执行"
                    : "已允许此次执行"}
            </p>
          )}
          <details className="group mt-2">
            <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-[11px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50">
              高级参数
              <ChevronRight className="size-3 transition-transform group-open:rotate-90" />
            </summary>
            <pre className="mt-1.5 max-h-40 overflow-auto rounded-lg bg-background/60 p-2.5 font-mono text-[10px] leading-4 whitespace-pre-wrap ring-1 ring-inset ring-border/45">
              {JSON.stringify(item.input, null, 2)}
            </pre>
          </details>
        </div>
      </div>
    </li>
  );
}

function BundleConfirmation({
  bundle,
  active,
  busy,
  onDecide,
}: {
  bundle: ApprovalBundle;
  active: boolean;
  busy: boolean;
  onDecide?: (decision: ConfirmationAction) => void;
}) {
  const [decisions, setDecisions] = useState<
    Record<string, DraftApprovalDecision>
  >({});
  const pendingItems = useMemo(
    () => bundle.items.filter((item) => item.status === "pending"),
    [bundle.items],
  );
  const unresolved = pendingItems.filter((item) => !decisions[item.tool_call_id]);
  const critical = bundle.items.some((item) => item.risk === "critical");

  function submit() {
    if (!onDecide || unresolved.length) return;
    const action = buildBundleConfirmationAction(bundle, decisions);
    if (action) onDecide(action);
  }

  return (
    <section
      aria-label="Agent 操作审批"
      className={cn(
        "overflow-hidden rounded-2xl bg-muted/25 ring-1 ring-inset",
        active
          ? critical
            ? "ring-red-500/25"
            : "ring-amber-500/30"
          : "ring-border/50",
      )}
    >
      <div className="flex items-start gap-3 px-4 py-3.5">
        <span
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-xl",
            critical
              ? "bg-red-500/9 text-red-700 dark:text-red-300"
              : "bg-amber-500/10 text-amber-700 dark:text-amber-300",
          )}
        >
          <ShieldAlert className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">
            {active
              ? `${pendingItems.length} 项操作等待决定`
              : "本组审批已处理"}
          </p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            同一模型步骤中的操作会先全部决定，再按原顺序执行；拒绝项不会执行。
          </p>
        </div>
        {active && (
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Clock3 className="size-3" />
            等待中
          </span>
        )}
      </div>
      <ol className="border-t border-border/45">
        {bundle.items.map((item) => (
          <BundleItemRow
            key={item.tool_call_id}
            item={item}
            active={active}
            disabled={busy}
            decision={decisions[item.tool_call_id]}
            onChange={(decision) =>
              setDecisions((current) => ({
                ...current,
                [item.tool_call_id]: decision,
              }))
            }
          />
        ))}
      </ol>
      {active && onDecide && (
        <div className="flex flex-col gap-2 border-t border-border/45 bg-background/35 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[11px] text-muted-foreground" aria-live="polite">
            {unresolved.length
              ? `还需决定 ${unresolved.length} 项`
              : "所有待处理操作已选择"}
          </p>
          <Button size="sm" disabled={busy || unresolved.length > 0} onClick={submit}>
            {busy ? <LockKeyhole className="size-3.5" /> : <Check className="size-3.5" />}
            提交决定
          </Button>
        </div>
      )}
    </section>
  );
}

/** Render authoritative pending state; no synthetic risk or reviewer copy. */
export function ConfirmCard({
  pending,
  active,
  busy = false,
  onDecide,
}: {
  pending: PendingConfirmation;
  active: boolean;
  busy?: boolean;
  onDecide?: (decision: ConfirmationAction) => void;
}) {
  return isApprovalBundle(pending) ? (
    <BundleConfirmation
      key={pending.bundle_id}
      bundle={pending}
      active={active}
      busy={busy}
      onDecide={onDecide}
    />
  ) : (
    <LegacyConfirmation
      pending={pending}
      active={active}
      busy={busy}
      onDecide={onDecide}
    />
  );
}
