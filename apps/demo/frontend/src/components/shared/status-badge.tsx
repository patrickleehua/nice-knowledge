/**
 * tone 徽章单一真源(色板迁移自 components/projects/badges.tsx 的 TONE_STYLES)。
 * "info" 与 stage-b 的历史值 "primary" 等价(均为 bg-primary/10 text-primary),
 * 保证 Wave 2 从 StatusMeta 机械替换。
 *
 * @example
 * ```tsx
 * <ToneBadge tone="success">已确认</ToneBadge>
 * <ToneBadge tone="info">运行中</ToneBadge>
 *
 * // 状态机元数据直渲(lib/stage-b 的 PROJECT_STATUS / RUN_STATUS 等)
 * <StatusBadge meta={PROJECT_STATUS[project.status] ?? { label: project.status, tone: "muted" }} />
 * ```
 */

import type { StatusMeta } from "@/lib/utils";
import { cn } from "@/lib/utils";

/** "primary" 为 stage-b StatusMeta 的历史别名,渲染与 "info" 一致 */
export type Tone =
  | "success"
  | "warning"
  | "destructive"
  | "muted"
  | "info"
  | "teal"
  | "primary";

const TONE_STYLES: Record<Tone, string> = {
  muted: "bg-muted text-muted-foreground",
  info: "bg-primary/10 text-primary",
  primary: "bg-primary/10 text-primary",
  warning: "bg-warning/15 text-warning",
  success: "bg-success/15 text-success",
  destructive: "bg-destructive/15 text-destructive",
  teal: "bg-teal/10 text-teal",
};

export function ToneBadge({
  tone,
  children,
  className,
}: {
  tone: Tone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE_STYLES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** 直接渲染 lib/stage-b 的 StatusMeta({ label, tone }) */
export function StatusBadge({
  meta,
  className,
}: {
  meta: StatusMeta;
  className?: string;
}) {
  return (
    <ToneBadge tone={meta.tone} className={className}>
      {meta.label}
    </ToneBadge>
  );
}
