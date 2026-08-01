"use client";

// 工作台常驻底栏:把「摄入 → 审核 → 快照发布 → 生效」这条主链在任何视图下都摆到眼前。
// 每个计数段都是可点的深链(带上对应筛选),右端是发布主 CTA。
// 全部 query key 与各视图复用,react-query 去重,不额外增加请求压力。

import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  ClipboardCheck,
  Images,
  Loader2,
  PauseCircle,
  Rocket,
} from "lucide-react";
import { api } from "@/lib/api";
import { kbImages } from "@/lib/kb-images";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { KnowledgeBase } from "@/lib/types";
import type { KbViewKey } from "./views";
import {
  ACTIVE_STATUSES,
  KB_DOCUMENTS_SOFT_LIMIT,
  useKbDocuments,
  usePendingExtractions,
} from "./kb-data";

interface SnapshotLite {
  id: string;
  status: "building" | "ready" | "active" | "retired" | "failed";
}

function Segment({
  icon: Icon,
  label,
  value,
  tone = "muted",
  onClick,
}: {
  icon: typeof Rocket;
  label: string;
  value: string;
  tone?: "muted" | "warning" | "primary" | "success" | "destructive";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors hover:bg-accent"
    >
      <Icon
        className={cn(
          "size-3.5",
          tone === "warning" && "text-warning",
          tone === "primary" && "text-primary",
          tone === "success" && "text-success",
          tone === "destructive" && "text-destructive",
          tone === "muted" && "text-muted-foreground",
        )}
      />
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          "font-medium tabular-nums",
          tone === "warning" && "text-warning",
          tone === "destructive" && "text-destructive",
        )}
      >
        {value}
      </span>
    </button>
  );
}

export function ReleaseBar({
  kbId,
  kb,
  onNavigate,
}: {
  kbId: string;
  kb?: KnowledgeBase;
  onNavigate: (view: KbViewKey, params?: Record<string, string>) => void;
}) {
  const { data: docs } = useKbDocuments(kbId);
  const { data: pendingClaims } = usePendingExtractions(kbId);

  const imagesPending = useQuery({
    queryKey: ["kb-image-assets", kbId, "needs-review-count"],
    queryFn: () =>
      kbImages.list(kbId, {
        page: 1,
        pageSize: 1,
        reviewStatus: "needs_review",
      }),
    staleTime: 30_000,
  });

  const snapshots = useQuery({
    queryKey: ["kb-snapshots", kbId],
    queryFn: () => api.get<SnapshotLite[]>(`/kb/bases/${kbId}/snapshots`),
    refetchInterval: (query) =>
      query.state.data?.some((snapshot) => snapshot.status === "building")
        ? 3000
        : false,
  });

  const ingesting =
    docs?.filter((doc) => ACTIVE_STATUSES.has(doc.status)).length ?? 0;
  const failed = docs?.filter((doc) => doc.status === "failed").length ?? 0;
  // 暂停既不算活跃也不算失败,不单列一段的话这些文档会从底栏彻底消失,
  // 用户只会看到「摄入中 0」,完全不知道还有一批停在半路
  const paused = docs?.filter((doc) => doc.status === "paused").length ?? 0;
  const claimCount = pendingClaims?.length ?? 0;
  // usePendingExtractions 按 limit 200 取首屏,达上限时如实标注"200+",不谎报总数
  const claimLabel = claimCount >= 200 ? "200+" : String(claimCount);
  // 三个文档派生计数都只统计了最近 KB_DOCUMENTS_SOFT_LIMIT 份文档,
  // 窗口取满时任意一个都可能少算,统一加"+"而不是只给「摄入中」加
  const docsTruncated = (docs?.length ?? 0) >= KB_DOCUMENTS_SOFT_LIMIT;
  const docLabel = (count: number) =>
    docsTruncated ? `${count}+` : String(count);
  const imageCount = imagesPending.data?.total ?? 0;

  const building = snapshots.data?.some((s) => s.status === "building");
  const ready = snapshots.data?.filter((s) => s.status === "ready").length ?? 0;

  let releaseIcon = Rocket;
  let releaseText = "尚未发布";
  let releaseTone: "muted" | "warning" | "success" | "primary" = "muted";
  if (building) {
    releaseIcon = Loader2;
    releaseText = "快照构建中";
    releaseTone = "primary";
  } else if (ready > 0) {
    releaseIcon = AlertCircle;
    releaseText = `${ready} 个快照待激活`;
    releaseTone = "warning";
  } else if (kb?.active_snapshot_id) {
    releaseIcon = CheckCircle2;
    releaseText = `快照 ${kb.active_snapshot_id.slice(0, 8)} 生效中`;
    releaseTone = "success";
  }
  const ReleaseIcon = releaseIcon;

  return (
    <div className="flex h-10 shrink-0 items-center gap-1 overflow-x-auto border-t border-border bg-sidebar px-2">
      <Segment
        icon={Loader2}
        label="摄入中"
        value={docLabel(ingesting)}
        tone={ingesting > 0 ? "primary" : "muted"}
        onClick={() => onNavigate("sources", { status: "active" })}
      />
      {paused > 0 && (
        <Segment
          icon={PauseCircle}
          label="已暂停"
          value={docLabel(paused)}
          tone="warning"
          onClick={() => onNavigate("sources", { status: "paused" })}
        />
      )}
      {failed > 0 && (
        <Segment
          icon={AlertCircle}
          label="摄入失败"
          value={docLabel(failed)}
          tone="destructive"
          onClick={() => onNavigate("sources", { status: "failed" })}
        />
      )}
      <Segment
        icon={ClipboardCheck}
        label="待审事实"
        value={claimLabel}
        tone={claimCount > 0 ? "warning" : "muted"}
        onClick={() => onNavigate("review", { queue: "suggested" })}
      />
      <Segment
        icon={Images}
        label="待审图片"
        value={String(imageCount)}
        tone={imageCount > 0 ? "warning" : "muted"}
        onClick={() => onNavigate("images", { review: "needs_review" })}
      />

      <div className="ml-auto flex shrink-0 items-center gap-2 pl-2">
        <span className="flex items-center gap-1.5 text-xs">
          <ReleaseIcon
            className={cn(
              "size-3.5",
              releaseTone === "primary" && "animate-spin text-primary",
              releaseTone === "warning" && "text-warning",
              releaseTone === "success" && "text-success",
              releaseTone === "muted" && "text-muted-foreground",
            )}
          />
          <span className="text-muted-foreground">{releaseText}</span>
        </span>
        <Button
          size="xs"
          variant="outline"
          onClick={() => onNavigate("release")}
        >
          <Rocket className="size-3" />
          发布管理
        </Button>
      </div>
    </div>
  );
}
