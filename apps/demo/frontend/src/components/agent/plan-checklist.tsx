"use client";

import {
  AlertCircle,
  Check,
  ChevronRight,
  Circle,
  ListTodo,
  Loader2,
  MinusCircle,
  Play,
} from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { PlanStep } from "@/lib/chat";
import { cn } from "@/lib/utils";

const STEP_ICONS = {
  pending: Circle,
  in_progress: Loader2,
  done: Check,
  skipped: MinusCircle,
  failed: AlertCircle,
} as const;

/** Agent 计划属于对话上下文，只读展示，不承担后台调度。 */
export function PlanChecklist({
  steps,
  onContinueStep,
  continueDisabled = false,
}: {
  steps: PlanStep[];
  onContinueStep?: (step: PlanStep) => void;
  continueDisabled?: boolean;
}) {
  const done = steps.filter((step) => step.status === "done").length;
  const failed = steps.filter((step) => step.status === "failed");
  const active = steps.some((step) => step.status === "in_progress");
  const current =
    steps.find((step) => step.status === "in_progress") ??
    failed[0] ??
    steps.find((step) => step.status === "pending") ??
    steps.at(-1);
  const [open, setOpen] = useState(false);
  if (!steps.length) return null;
  const progress = Math.round((done / steps.length) * 100);
  return (
    <details
      className={cn(
        "group overflow-hidden rounded-2xl bg-muted/35 ring-1 ring-inset ring-border/45",
        failed.length > 0 && "ring-destructive/35",
      )}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2.5 px-3.5 text-sm">
        <span
          className={cn(
            "flex size-6 items-center justify-center text-muted-foreground",
            failed.length > 0 && "text-destructive",
          )}
        >
          <ListTodo className="size-3.5" />
        </span>
        <span className="shrink-0 font-medium">执行计划</span>
        <span
          className={cn(
            "min-w-0 truncate text-xs text-muted-foreground",
            failed.length > 0 && "text-destructive",
          )}
        >
          {failed.length > 0
            ? `${failed.length} 步待处理 · ${current?.title ?? ""}`
            : active
              ? `正在：${current?.title ?? ""}`
              : done === steps.length
                ? "已完成"
                : `下一步：${current?.title ?? ""}`}
        </span>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          {done}/{steps.length}
        </span>
        <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" />
      </summary>
      <div className="max-h-64 overflow-y-auto border-t border-border/45 px-4 py-3">
        <div className="mb-3 h-1 overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-full rounded-full transition-[width]",
              failed.length > 0 ? "bg-destructive/70" : "bg-foreground/70",
            )}
            style={{ width: `${progress}%` }}
          />
        </div>
        <ol className="space-y-2">
          {steps.map((step) => {
            const Icon = STEP_ICONS[step.status] ?? Circle;
            return (
              <li key={step.id} className="flex items-start gap-2 text-xs">
                <Icon
                  className={cn(
                    "mt-0.5 size-3.5 shrink-0",
                    step.status === "pending" && "text-muted-foreground/60",
                    step.status === "in_progress" &&
                      "animate-spin text-primary",
                    step.status === "done" && "text-emerald-600",
                    step.status === "skipped" && "text-muted-foreground",
                    step.status === "failed" && "text-destructive",
                  )}
                />
                <span
                  className={cn(
                    "leading-5",
                    step.status === "done" && "text-muted-foreground",
                    step.status === "skipped" &&
                      "text-muted-foreground line-through",
                    step.status === "failed" && "text-foreground",
                  )}
                >
                  {step.title}
                  {step.note && (
                    <span
                      className={cn(
                        "ml-1 text-muted-foreground",
                        step.status === "failed" && "text-destructive",
                      )}
                    >
                      — {step.note}
                    </span>
                  )}
                </span>
                {step.status === "failed" && onContinueStep && (
                  <Button
                    size="xs"
                    variant="outline"
                    className="ml-auto shrink-0"
                    disabled={continueDisabled}
                    onClick={() => onContinueStep(step)}
                  >
                    <Play />
                    继续处理
                  </Button>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </details>
  );
}
