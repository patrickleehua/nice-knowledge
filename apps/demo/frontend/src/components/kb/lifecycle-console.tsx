"use client";

// 知识库生命周期提醒横幅(/org/kb 列表页用):
// - WorkerDisabledBanner:永久清理 worker 未启用的全局警示;
// - PurgeDueBanner:归档库保留期到期提醒(数据来自 board 的 purge_due 行)。
// 操作队列/孤儿巡检的独立 UI 已按产品决策移除(后端端点保留);
// 单库的归档/恢复/永久清理在各库设置页危险操作区执行。

import { TriangleAlert } from "lucide-react";
import Link from "next/link";
import type { KnowledgeBaseBoardItem } from "@/lib/kb-lifecycle";

// ---- 时间格式化(组件为 "use client" 且数据来自 query,不参与 SSR,无 hydration 风险) ----

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 仅日期:MM-DD */
export function formatMonthDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** 单库危险操作区深链(归档/恢复/永久清理都在各库设置页执行) */
export function dangerZoneHref(kbId: string): string {
  return `/org/kb/${kbId}?view=settings&tab=danger`;
}

// ---- worker 未启用警示条 ----

export function WorkerDisabledBanner({
  pendingPurges,
}: {
  pendingPurges: number;
}) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-sm text-destructive"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" />
      <span>
        永久清理 worker 未启用(KB_LIFECYCLE_PURGE_WORKER_ENABLED),已提交的清理会一直停留在
        pending,不会执行
        {pendingPurges > 0 && (
          <>
            ;当前已有{" "}
            <span className="font-semibold tabular-nums">{pendingPurges}</span>{" "}
            个库清理操作在等待执行
          </>
        )}
      </span>
    </div>
  );
}

// ---- 保留期到期提醒(列表页顶部横幅) ----

export function PurgeDueBanner({
  items,
}: {
  /** board 中 purge_due=true 的行 */
  items: KnowledgeBaseBoardItem[];
}) {
  if (items.length === 0) return null;
  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-warning/50 bg-warning/10 px-3 py-2.5 text-sm"
    >
      <TriangleAlert className="size-4 shrink-0 text-warning" />
      <span>
        有 <span className="font-semibold tabular-nums">{items.length}</span>{" "}
        个归档知识库保留期已到期,可审阅是否永久清理:
      </span>
      <span className="flex flex-wrap gap-x-3 gap-y-1">
        {items.map((item) => (
          <Link
            key={item.kb_id}
            href={dangerZoneHref(item.kb_id)}
            className="font-medium text-primary hover:underline"
          >
            {item.name}
          </Link>
        ))}
      </span>
    </div>
  );
}
