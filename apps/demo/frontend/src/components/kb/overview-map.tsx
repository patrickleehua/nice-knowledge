"use client";

// 知识流水线总览图:资料 → 图片 → 实体 → Wiki → 图谱 → 发布 主链,
// 审核作为治理支线从「资料」分叉、汇入「实体」(虚线表示)。
// 每个节点整体可点,onNavigate 直达对应视图;计数徽章全部复用既有 query
// (queryKey 与 release-bar / 各视图一致,react-query 去重,不新增后端契约)。
// 布局:sm 及以上为 11 列网格(节点列 + 连接线列)双行;sm 以下退化为
// 单列纵向列表,连接线整体隐藏。

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { kbImages } from "@/lib/kb-images";
import { cn } from "@/lib/utils";
import type { GraphInsights, KnowledgeBase } from "@/lib/types";
import { kbView, type KbViewKey } from "@/components/kb/workbench/views";
import {
  KB_DOCUMENTS_SOFT_LIMIT,
  useKbDocuments,
  usePendingExtractions,
} from "@/components/kb/workbench/kb-data";
import { useKbPages } from "@/components/kb/wiki/data";

/** 快照精简形态,与 release-bar 的本地声明保持一致(仅取 status) */
interface SnapshotLite {
  id: string;
  status: "building" | "ready" | "active" | "retired" | "failed";
}

type CaptionTone = "muted" | "warning" | "success" | "primary";

const CAPTION_TONE_CLASS: Record<CaptionTone, string> = {
  muted: "text-muted-foreground",
  warning: "text-warning",
  success: "text-success",
  primary: "text-primary",
};

/**
 * 流水线单节点:图标 + 中文标签 + 计数徽章(可选)+ 状态短语(可选)。
 * 原生 button 保证键盘可达(Enter/Space);aria-label 带上计数语义。
 * 窄屏为横向行(图标在左),sm 及以上为纵向卡(图标在上)。
 */
function PipelineNode({
  view,
  badge,
  ariaExtra,
  warn,
  caption,
  disabled,
  highlight,
  className,
  onNavigate,
}: {
  view: KbViewKey;
  /** 徽章文本;undefined 表示该环节不展示数字 */
  badge?: string;
  /** aria-label 的计数补充语,如「12 份文档」 */
  ariaExtra?: string;
  /** 待处理项 > 0 时以 warning 色提示 */
  warn?: boolean;
  /** 节点下方状态短语(发布节点用) */
  caption?: { text: string; tone: CaptionTone };
  disabled?: boolean;
  /** 空库时突出唯一入口(资料节点) */
  highlight?: boolean;
  className?: string;
  onNavigate: (view: KbViewKey) => void;
}) {
  const meta = kbView(view);
  const Icon = meta.icon;
  return (
    <button
      type="button"
      disabled={disabled}
      aria-label={ariaExtra ? `${meta.label}，${ariaExtra}` : meta.label}
      onClick={() => onNavigate(view)}
      className={cn(
        "flex min-w-0 items-center gap-3 rounded-lg border border-border bg-card px-3 py-2.5 text-left transition-colors",
        "sm:flex-col sm:justify-center sm:gap-1.5 sm:px-2 sm:py-3 sm:text-center",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        disabled
          ? "opacity-40"
          : "hover:border-ring/60 hover:bg-accent/50",
        warn && "border-warning/60",
        highlight && "border-primary/60 bg-primary/5",
        className,
      )}
    >
      <span
        className={cn(
          "flex size-9 shrink-0 items-center justify-center rounded-md bg-accent text-accent-foreground",
          warn && "bg-warning/15 text-warning",
          highlight && "bg-primary/10 text-primary",
        )}
      >
        <Icon className="size-4.5" />
      </span>
      <span className="flex min-w-0 flex-1 items-center gap-2 sm:flex-none sm:flex-col sm:gap-1">
        <span className="text-sm font-medium">{meta.label}</span>
        {badge !== undefined && (
          <span
            className={cn(
              "rounded-full bg-accent px-1.5 py-0.5 text-xs tabular-nums text-muted-foreground",
              warn && "bg-warning/15 font-medium text-warning",
            )}
          >
            {badge}
          </span>
        )}
        {caption && (
          <span className={cn("text-xs", CAPTION_TONE_CLASS[caption.tone])}>
            {caption.text}
          </span>
        )}
      </span>
    </button>
  );
}

/** 主链连接箭头(纯装饰,窄屏隐藏) */
function FlowArrow({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn("hidden items-center justify-center sm:flex", className)}
    >
      <ArrowRight className="size-4 shrink-0 text-muted-foreground/50" />
    </div>
  );
}

/** 治理支线连接线(虚线 SVG,纯装饰,窄屏隐藏)。轴向直线在
 *  preserveAspectRatio="none" 拉伸下不会变形,故可用百分比坐标铺满格子。 */
function BranchLine({
  shape,
  className,
}: {
  /** down = 从上方节点分叉向右;h = 水平段;up = 折向上方节点汇入(带箭头) */
  shape: "down" | "h" | "up";
  className?: string;
}) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      className={cn("hidden h-full min-h-10 w-full text-border sm:block", className)}
    >
      {shape === "down" && (
        <path
          d="M 50 0 V 50 H 100"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeDasharray="4 4"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {shape === "h" && (
        <path
          d="M 0 50 H 100"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeDasharray="4 4"
          vectorEffect="non-scaling-stroke"
        />
      )}
      {shape === "up" && (
        <>
          <path
            d="M 0 50 H 50 V 10"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeDasharray="4 4"
            vectorEffect="non-scaling-stroke"
          />
          {/* 汇入箭头(向上) */}
          <path
            d="M 40 26 L 50 8 L 60 26"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            vectorEffect="non-scaling-stroke"
          />
        </>
      )}
    </svg>
  );
}

export function KbOverviewMap({
  kbId,
  kb,
  onNavigate,
}: {
  kbId: string;
  kb?: KnowledgeBase;
  onNavigate: (view: KbViewKey) => void;
}) {
  const { data: docs } = useKbDocuments(kbId);
  const { data: pendingClaims } = usePendingExtractions(kbId);
  const { data: pages } = useKbPages(kbId);
  const { data: insights } = useQuery({
    queryKey: ["kb-insights", kbId],
    queryFn: () => api.get<GraphInsights>(`/kb/bases/${kbId}/insights`),
  });
  // 与 release-bar 完全同 key 同参,react-query 去重,不产生额外请求
  const { data: imagesPending } = useQuery({
    queryKey: ["kb-image-assets", kbId, "needs-review-count"],
    queryFn: () =>
      kbImages.list(kbId, {
        page: 1,
        pageSize: 1,
        reviewStatus: "needs_review",
      }),
    staleTime: 30_000,
  });
  const { data: snapshots } = useQuery({
    queryKey: ["kb-snapshots", kbId],
    queryFn: () => api.get<SnapshotLite[]>(`/kb/bases/${kbId}/snapshots`),
  });

  // 文档窗口取满(KB_DOCUMENTS_SOFT_LIMIT)时如实标注"+",不谎报总数;
  // 待审事实首屏 limit 200,同理标注"200+"
  const docCount = docs?.length;
  const docBadge =
    docCount === undefined
      ? undefined
      : docCount >= KB_DOCUMENTS_SOFT_LIMIT
        ? `${docCount}+`
        : String(docCount);
  const claimCount = pendingClaims?.length;
  const claimBadge =
    claimCount === undefined
      ? undefined
      : claimCount >= 200
        ? "200+"
        : String(claimCount);
  const imageReviewCount = imagesPending?.total;
  const pageCount = pages?.length;

  // 发布环节状态短语,判定顺序与 release-bar 一致:构建中 > 待激活 > 生效中 > 未发布
  const building = snapshots?.some((s) => s.status === "building") ?? false;
  const readyCount = snapshots?.filter((s) => s.status === "ready").length ?? 0;
  let releaseCaption: { text: string; tone: CaptionTone } = {
    text: "未发布",
    tone: "muted",
  };
  if (building) releaseCaption = { text: "构建中", tone: "primary" };
  else if (readyCount > 0)
    releaseCaption = { text: `${readyCount} 个待激活`, tone: "warning" };
  else if (kb?.active_snapshot_id)
    releaseCaption = { text: "生效中", tone: "success" };

  // 空库:整图除「资料」外置灰不可点,资料节点高亮为唯一入口
  const isEmpty = docs !== undefined && docs.length === 0;
  const restDisabled = isEmpty;

  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2",
        // 桌面:节点列(1fr)与连接线列(1.5rem)交替共 11 列,两行(主链 / 治理支线)
        "sm:grid-cols-[minmax(0,1fr)_1.5rem_minmax(0,1fr)_1.5rem_minmax(0,1fr)_1.5rem_minmax(0,1fr)_1.5rem_minmax(0,1fr)_1.5rem_minmax(0,1fr)] sm:gap-x-0 sm:gap-y-2",
      )}
    >
      {/* ---- 主链(第 1 行):资料 → 图片 → 实体 → Wiki → 图谱 → 发布 ---- */}
      <PipelineNode
        view="sources"
        badge={docBadge}
        ariaExtra={docBadge !== undefined ? `${docBadge} 份文档` : undefined}
        highlight={isEmpty}
        onNavigate={onNavigate}
        className="sm:col-start-1 sm:row-start-1"
      />
      <FlowArrow className="sm:col-start-2 sm:row-start-1" />
      <PipelineNode
        view="images"
        badge={
          imageReviewCount !== undefined && imageReviewCount > 0
            ? String(imageReviewCount)
            : undefined
        }
        ariaExtra={
          imageReviewCount !== undefined && imageReviewCount > 0
            ? `${imageReviewCount} 张待审核`
            : undefined
        }
        warn={(imageReviewCount ?? 0) > 0}
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-3 sm:row-start-1"
      />
      <FlowArrow className="sm:col-start-4 sm:row-start-1" />
      {/* 治理支线节点紧跟在汇入目标(实体)之前,保证窄屏单列阅读顺序合理 */}
      <PipelineNode
        view="review"
        badge={claimBadge}
        ariaExtra={claimBadge !== undefined ? `${claimBadge} 条待审核` : undefined}
        warn={(claimCount ?? 0) > 0}
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-3 sm:row-start-2 sm:mt-0"
      />
      <PipelineNode
        view="entities"
        badge={
          insights?.node_count !== undefined
            ? String(insights.node_count)
            : undefined
        }
        ariaExtra={
          insights?.node_count !== undefined
            ? `${insights.node_count} 个实体节点`
            : undefined
        }
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-5 sm:row-start-1"
      />
      <FlowArrow className="sm:col-start-6 sm:row-start-1" />
      <PipelineNode
        view="wiki"
        badge={pageCount !== undefined ? String(pageCount) : undefined}
        ariaExtra={pageCount !== undefined ? `${pageCount} 个页面` : undefined}
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-7 sm:row-start-1"
      />
      <FlowArrow className="sm:col-start-8 sm:row-start-1" />
      <PipelineNode
        view="graph"
        badge={
          insights?.edge_count !== undefined
            ? String(insights.edge_count)
            : undefined
        }
        ariaExtra={
          insights?.edge_count !== undefined
            ? `${insights.edge_count} 条关系`
            : undefined
        }
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-9 sm:row-start-1"
      />
      <FlowArrow className="sm:col-start-10 sm:row-start-1" />
      <PipelineNode
        view="release"
        caption={releaseCaption}
        ariaExtra={releaseCaption.text}
        disabled={restDisabled}
        onNavigate={onNavigate}
        className="sm:col-start-11 sm:row-start-1"
      />

      {/* ---- 治理支线(第 2 行):资料 ─┐ … 审核 … ┌→ 实体(虚线) ---- */}
      <BranchLine shape="down" className="sm:col-start-1 sm:row-start-2" />
      <BranchLine shape="h" className="sm:col-start-2 sm:row-start-2" />
      <BranchLine shape="h" className="sm:col-start-4 sm:row-start-2" />
      <BranchLine shape="up" className="sm:col-start-5 sm:row-start-2" />
    </div>
  );
}
