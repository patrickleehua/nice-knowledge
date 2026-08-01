"use client";

import { CircleCheck, Target, X } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  SESSION_GOAL_STATUS_LABELS,
  type SessionGoalIn,
  type SessionGoalOut,
} from "@/lib/chat";
import { cn } from "@/lib/utils";

const STATUS_STYLES: Record<SessionGoalOut["status"], string> = {
  active: "bg-primary/10 text-primary",
  completed: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  budget_limited: "bg-amber-500/12 text-amber-700 dark:text-amber-400",
  cancelled: "bg-muted text-muted-foreground",
};

/**
 * 会话目标横幅:目标 active 时 Agent 空闲会自动起新 run 继续推进,
 * 因此进度(token 预算 / 自动续跑轮次)必须一直可见——用户要能随时看出
 * "它还会替我跑多久",以及一键叫停。
 */
export function GoalBanner({
  goal,
  disabled = false,
  busy = false,
  onSubmit,
  onCancel,
  onComplete,
}: {
  goal: SessionGoalOut | null;
  disabled?: boolean;
  busy?: boolean;
  onSubmit: (body: SessionGoalIn) => void | Promise<void>;
  onCancel: () => void | Promise<void>;
  onComplete: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const openEditor = () => {
    setDraft(goal?.objective ?? "");
    setEditing(true);
  };

  if (editing)
    return (
      <div className="mx-auto w-full max-w-[50rem] px-4 pt-2 sm:px-6">
        <div className="space-y-2 rounded-xl border border-border/80 bg-white p-3 dark:bg-[#232321]">
          <p className="text-xs text-muted-foreground">
            设定会话目标后，Agent 会在空闲时自动继续推进，直到目标完成或预算用尽。
          </p>
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={2}
            autoFocus
            placeholder="例如：整理这批资料并产出一份带引用的对比结论"
          />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setEditing(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!draft.trim() || busy}
              onClick={async () => {
                await onSubmit({ objective: draft.trim() });
                setEditing(false);
              }}
            >
              完成
            </Button>
          </div>
        </div>
      </div>
    );

  if (!goal || goal.status === "cancelled")
    return (
      <div className="mx-auto w-full max-w-[50rem] px-4 pt-2 sm:px-6">
        <button
          type="button"
          disabled={disabled}
          onClick={openEditor}
          className="flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Target className="size-3.5" />
          设置会话目标
        </button>
      </div>
    );

  const budget = Math.max(goal.token_budget, 1);
  const percent = Math.min(Math.round((goal.tokens_used / budget) * 100), 100);
  const open = goal.status === "active" || goal.status === "budget_limited";

  return (
    <div className="mx-auto w-full max-w-[50rem] px-4 pt-2 sm:px-6">
      <div className="rounded-xl border border-border/80 bg-white px-3.5 py-2.5 dark:bg-[#232321]">
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 shrink-0 text-muted-foreground">
            {goal.status === "completed" ? (
              <CircleCheck className="size-4 text-emerald-600 dark:text-emerald-400" />
            ) : (
              <Target className="size-4" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[11px] font-medium",
                  STATUS_STYLES[goal.status],
                )}
              >
                {SESSION_GOAL_STATUS_LABELS[goal.status]}
              </span>
              <p className="min-w-0 flex-1 truncate text-sm leading-6">
                {goal.objective}
              </p>
            </div>
            {goal.summary && (
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {goal.summary}
              </p>
            )}
            <div className="mt-1.5 flex items-center gap-2">
              <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-black/[0.07] dark:bg-white/[0.09]">
                <div
                  className={cn(
                    "h-full rounded-full transition-[width]",
                    goal.status === "budget_limited"
                      ? "bg-amber-500"
                      : "bg-primary",
                  )}
                  style={{ width: `${percent}%` }}
                />
              </div>
              <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">
                {goal.tokens_used.toLocaleString()} /{" "}
                {goal.token_budget.toLocaleString()} token · 自动续跑{" "}
                {goal.auto_runs}/{goal.max_auto_runs}
              </span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={disabled}
              onClick={openEditor}
            >
              {open ? "修改" : "重设"}
            </Button>
            {open && (
              <>
                {/* 目标达成但模型没自觉调 update_goal 时,用户自己收尾;
                    与模型声明完成落到同一种终态,都不再自动续跑 */}
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={disabled || busy}
                  onClick={() => void onComplete()}
                >
                  标记完成
                </Button>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label="取消会话目标"
                  title="取消目标"
                  disabled={disabled || busy}
                  onClick={() => void onCancel()}
                >
                  <X className="size-3.5" />
                </Button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
