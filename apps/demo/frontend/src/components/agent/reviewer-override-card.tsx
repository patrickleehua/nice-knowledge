"use client";

import { Clock3, ShieldAlert } from "lucide-react";
import { toolLabel, type ReviewerOverride } from "@/lib/chat";

function targetLabel(override: ReviewerOverride): string {
  if (override.scope.resource_type && override.scope.resource_id)
    return `${override.scope.resource_type} · ${override.scope.resource_id.slice(0, 8)}`;
  if (override.scope.target_project_id)
    return `旅行计划 · ${override.scope.target_project_id.slice(0, 8)}`;
  return override.scope.mode === "general" ? "通用会话" : "当前旅行计划";
}

function expiryLabel(value: string): string | null {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

/** Fallback for a durable override restored without its original live trace. */
export function ReviewerOverrideCard({
  override,
  busy,
  onOverride,
}: {
  override: ReviewerOverride;
  busy: boolean;
  onOverride?: (override: ReviewerOverride) => void;
}) {
  const expires = expiryLabel(override.expires_at);
  return (
    <section
      aria-label="Reviewer 一次性覆盖"
      className="overflow-hidden rounded-2xl bg-amber-500/[0.055] ring-1 ring-inset ring-amber-500/25"
    >
      <div className="flex items-start gap-3 p-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-amber-500/12 text-amber-700 dark:text-amber-300">
          <ShieldAlert className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">Reviewer 未批准此项操作</p>
          <p className="mt-1 text-sm leading-6">
            {override.label || toolLabel(override.name)}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {targetLabel(override)}
            {expires ? ` · 覆盖选项于 ${expires} 到期` : ""}
          </p>
          {override.reviewer_rationale && (
            <p className="mt-2 text-xs leading-5 text-amber-950/75 dark:text-amber-100/75">
              {override.reviewer_rationale}
            </p>
          )}
          {override.reviewer_risk_flags.length > 0 && (
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              风险标记：{override.reviewer_risk_flags.join("、")}
            </p>
          )}
        </div>
      </div>
      <div className="flex flex-col gap-2 border-t border-amber-500/15 bg-background/25 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Clock3 className="size-3.5" />
          只覆盖这一次准确操作，不创建长期授权
        </p>
        <button
          type="button"
          disabled={busy || !onOverride}
          onClick={() => onOverride?.(override)}
          className="rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white outline-none hover:bg-amber-700 focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          仅此次仍要执行
        </button>
      </div>
    </section>
  );
}
