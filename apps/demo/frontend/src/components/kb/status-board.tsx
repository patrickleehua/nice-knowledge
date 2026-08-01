"use client";

import {
  Activity,
  AlertTriangle,
  Clock3,
  Database,
  Rocket,
} from "lucide-react";
import Link from "next/link";
import { ToneBadge, type Tone } from "@/components/shared";
import type {
  KbPrimaryState,
  KbStatusBoardAlertCode,
  KbStatusBoardItem,
  KbStatusBoardResponse,
} from "@/lib/kb-status-board";
import { cn } from "@/lib/utils";

export type KbStatusFilter =
  "all" | "running" | "queued" | "review" | "release" | "attention";

const PRIMARY_STATE_META: Record<
  KbPrimaryState,
  { label: string; tone: Tone }
> = {
  blocked: { label: "推进受阻", tone: "destructive" },
  running: { label: "运行中", tone: "info" },
  queued: { label: "排队中", tone: "info" },
  release_ready: { label: "待发布", tone: "warning" },
  review_required: { label: "待审核", tone: "warning" },
  classification_required: { label: "待分类", tone: "warning" },
  needs_attention: { label: "需处理", tone: "destructive" },
  ready: { label: "已就绪", tone: "success" },
  unpublished: { label: "尚未发布", tone: "muted" },
  empty: { label: "空库", tone: "muted" },
};

const STAGE_LABELS: Record<string, string> = {
  parse: "版面解析",
  parsing: "版面解析",
  layout_parse: "版面解析",
  image: "图片理解",
  image_enrichment: "图片理解",
  image_understanding: "图片理解",
  chunk: "切片与向量化",
  chunking: "切片与向量化",
  chunk_embedding: "切片与向量化",
  extract: "信息抽取",
  extraction: "信息抽取",
  information_extraction: "信息抽取",
  entity_extract: "实体抽取",
  entity_extraction: "实体抽取",
};

const NEXT_ACTION_LABELS: Record<KbPrimaryState, string> = {
  blocked: "自动任务已阻塞，请处理异常后继续",
  running: "后台任务正在推进",
  queued: "资料已入队，等待工作进程",
  release_ready: "新知识版本已就绪，等待发布",
  review_required: "机器采集已完成，等待人工审核",
  classification_required: "资料需要先完成分类",
  needs_attention: "存在需要人工处理的异常",
  ready: "线上知识版本可用",
  unpublished: "采集结果尚未发布到业务端",
  empty: "上传资料后即可开始采集",
};

const OPERATION_KIND_LABELS = {
  withdrawal: "撤回发布",
  reingestion: "重新采集",
  purge: "永久清理",
} as const;

const OPERATION_STAGE_LABELS: Record<string, string> = {
  planned: "等待执行",
  retry_scheduled: "等待重试",
  ingestion: "资料采集中",
  snapshot_rebuild: "重建知识版本",
  snapshot_activation: "发布知识版本",
  object_deletion: "清理文件",
  metadata_deletion: "清理索引",
  verification: "核验清理结果",
};

const ALERT_LABELS: Record<KbStatusBoardAlertCode, string> = {
  ingest_lease_expired: "任务租约已过期",
  ingest_heartbeat_stale: "任务心跳已中断",
  ingest_queue_stalled: "排队等待超时",
  ingest_processing_stalled: "资料处理超时",
  snapshot_build_stalled: "快照构建超时",
  document_failed: "资料处理失败",
  snapshot_failed: "快照构建失败",
  document_operation_failed: "后台操作失败",
};

const FILTERS: Array<{ value: KbStatusFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "running", label: "运行中" },
  { value: "queued", label: "排队中" },
  { value: "review", label: "待审核" },
  { value: "release", label: "待发布" },
  { value: "attention", label: "需处理" },
];

export function matchesKbStatusFilter(
  item: KbStatusBoardItem,
  filter: KbStatusFilter,
): boolean {
  if (filter === "all") return true;
  if (filter === "running") {
    return item.primary_state === "running" || item.document_counts.running > 0;
  }
  if (filter === "queued") {
    return item.primary_state === "queued" || item.document_counts.queued > 0;
  }
  if (filter === "review") {
    return item.primary_state === "review_required" || item.review.total > 0;
  }
  if (filter === "release") {
    return (
      item.primary_state === "release_ready" || item.release.state === "ready"
    );
  }
  return (
    item.primary_state === "blocked" ||
    item.primary_state === "classification_required" ||
    item.primary_state === "needs_attention" ||
    item.alerts.length > 0 ||
    item.document_counts.failed > 0 ||
    item.document_counts.paused > 0
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function KbRuntimeSummary({
  board,
  filter,
  onFilterChange,
  stale,
  loading,
  error,
}: {
  board?: KbStatusBoardResponse;
  filter: KbStatusFilter;
  onFilterChange: (filter: KbStatusFilter) => void;
  stale: boolean;
  loading: boolean;
  error: boolean;
}) {
  return (
    <section
      aria-label="知识库实时状态"
      className="rounded-xl border border-border/70 bg-card p-4 shadow-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-2 font-medium">
          <Activity className="size-4 text-primary" />
          实时状态
        </span>
        <span className="text-xs text-muted-foreground">
          采集、审核与发布状态独立统计
        </span>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
          {stale ? (
            <>
              <AlertTriangle className="size-3.5 text-warning" />
              <span>状态可能已过期</span>
              {board && (
                <span>· 上次更新 {formatTime(board.generated_at)}</span>
              )}
            </>
          ) : board ? (
            <>
              <Clock3 className="size-3.5" />
              更新于 {formatTime(board.generated_at)}
            </>
          ) : loading ? (
            "正在读取状态…"
          ) : error ? (
            "实时状态暂不可用"
          ) : null}
        </span>
      </div>

      {board && (
        <div
          role="group"
          aria-label="实时状态筛选"
          className="mt-3 flex flex-wrap gap-2"
        >
          {FILTERS.map((item) => {
            const count = board.items.filter((status) =>
              matchesKbStatusFilter(status, item.value),
            ).length;
            return (
              <button
                key={item.value}
                type="button"
                aria-pressed={filter === item.value}
                onClick={() => onFilterChange(item.value)}
                className={cn(
                  "rounded-full border px-3 py-1 text-xs transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  filter === item.value
                    ? "border-primary/30 bg-primary/10 font-medium text-primary"
                    : "border-border bg-background text-muted-foreground hover:text-foreground",
                )}
              >
                {item.label} {count}
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

function stageLabel(item: KbStatusBoardItem["stages"][number]): string {
  const label = STAGE_LABELS[item.stage] ?? item.stage;
  const progress =
    item.done !== null && item.total !== null && item.total > 0
      ? ` ${item.done}/${item.total}`
      : "";
  return `${label}${progress} · ${item.document_count} 份资料`;
}

function operationLabel(item: KbStatusBoardItem): string | null {
  const operation = item.operation;
  if (!operation) return null;
  const kind = OPERATION_KIND_LABELS[operation.kind];
  const stage = operation.stage
    ? (OPERATION_STAGE_LABELS[operation.stage] ?? "处理中")
    : operation.status === "pending"
      ? "等待处理"
      : "处理中";
  return `${kind} · ${stage} · ${operation.count} 项`;
}

function releaseMeta(item: KbStatusBoardItem): { label: string; tone: Tone } {
  const { release } = item;
  if (release.state === "ready") {
    return release.active_snapshot_id
      ? { label: "线上可用 · 新版本待发布", tone: "warning" }
      : { label: "待发布", tone: "warning" };
  }
  if (release.state === "building") {
    return { label: "版本构建中", tone: "info" };
  }
  if (release.state === "failed") {
    return release.active_snapshot_id
      ? { label: "线上版本可用 · 最新构建失败", tone: "warning" }
      : { label: "最新构建失败", tone: "destructive" };
  }
  if (release.state === "active" || release.active_snapshot_id) {
    return { label: "已发布", tone: "success" };
  }
  return { label: "尚未发布", tone: "muted" };
}

function CountLink({
  href,
  label,
  count,
  tone = "muted",
  ariaLabel,
}: {
  href: string;
  label: string;
  count: number;
  tone?: Tone;
  ariaLabel: string;
}) {
  if (count <= 0) return null;
  return (
    <Link href={href} aria-label={ariaLabel}>
      <ToneBadge tone={tone} className="transition-opacity hover:opacity-75">
        {label} {count}
      </ToneBadge>
    </Link>
  );
}

export function KbRuntimePanel({
  kbId,
  kbName,
  item,
  stale,
  loading,
}: {
  kbId: string;
  kbName: string;
  item?: KbStatusBoardItem;
  stale: boolean;
  loading: boolean;
}) {
  if (!item) {
    return (
      <div className="flex min-h-24 items-center justify-center rounded-lg border border-dashed border-border/70 bg-muted/20 text-xs text-muted-foreground">
        {loading ? "正在读取实时状态…" : "实时状态暂不可用"}
      </div>
    );
  }

  const counts = item.document_counts;
  const completion =
    counts.total > 0 ? Math.round((counts.ingested / counts.total) * 100) : 0;
  const primary = PRIMARY_STATE_META[item.primary_state];
  const release = releaseMeta(item);
  const factReview = item.review.suggested_facts + item.review.orphaned_facts;
  const visibleAlerts = item.alerts.filter(
    (alert) => alert.code !== "document_failed",
  );
  const visibleStages = item.stages.slice(0, 2);
  const activeOperation = operationLabel(item);

  return (
    <div className="space-y-3 rounded-lg border border-border/70 bg-muted/15 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <ToneBadge tone={primary.tone}>{primary.label}</ToneBadge>
        {stale && <ToneBadge tone="warning">可能过期</ToneBadge>}
        {item.latest_activity_at && (
          <span className="ml-auto text-[11px] text-muted-foreground">
            最近活动 {formatTime(item.latest_activity_at)}
          </span>
        )}
      </div>

      <div>
        <div className="mb-1.5 flex items-center justify-between gap-3 text-xs">
          <span className="font-medium">
            已采集 {counts.ingested}/{counts.total}
          </span>
          <span className="text-muted-foreground">
            剩余 {counts.remaining} · {completion}%
          </span>
        </div>
        <div
          role="progressbar"
          aria-label={`${kbName} 采集完成度`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={completion}
          className="h-1.5 overflow-hidden rounded-full bg-muted"
        >
          <span
            className="block h-full rounded-full bg-primary transition-[width]"
            style={{ width: `${Math.min(100, Math.max(0, completion))}%` }}
          />
        </div>
      </div>

      <div className="min-h-8 text-xs">
        {visibleStages.length > 0 ? (
          <>
            <span className="text-muted-foreground">当前阶段：</span>
            <span className="font-medium">
              {visibleStages.map(stageLabel).join("；")}
              {item.stages.length > visibleStages.length &&
                `；另有 ${item.stages.length - visibleStages.length} 个阶段`}
            </span>
          </>
        ) : activeOperation ? (
          <>
            <span className="text-muted-foreground">后台操作：</span>
            <span className="font-medium">{activeOperation}</span>
          </>
        ) : (
          <span className="text-muted-foreground">
            {NEXT_ACTION_LABELS[item.primary_state]}
          </span>
        )}
      </div>

      <div className="flex min-h-5 flex-wrap items-center gap-1.5">
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=staged`}
          label="待分类"
          count={counts.staged}
          tone="warning"
          ariaLabel={`${kbName} 待分类 ${counts.staged}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=active`}
          label="排队"
          count={counts.queued}
          tone="info"
          ariaLabel={`${kbName} 排队 ${counts.queued}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=active`}
          label="运行"
          count={counts.running}
          tone="info"
          ariaLabel={`${kbName} 运行 ${counts.running}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=awaiting_review`}
          label="资料待审核"
          count={counts.awaiting_review}
          tone="warning"
          ariaLabel={`${kbName} 资料待审核 ${counts.awaiting_review}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=review`}
          label="事实待审核"
          count={factReview}
          tone="warning"
          ariaLabel={`${kbName} 事实待审核 ${factReview}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=images&review=needs_review`}
          label="图片待审核"
          count={item.review.images_needing_review}
          tone="warning"
          ariaLabel={`${kbName} 图片待审核 ${item.review.images_needing_review}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=failed`}
          label="失败"
          count={counts.failed}
          tone="destructive"
          ariaLabel={`${kbName} 失败 ${counts.failed}`}
        />
        <CountLink
          href={`/org/kb/${kbId}?view=sources&status=paused`}
          label="暂停"
          count={counts.paused}
          tone="warning"
          ariaLabel={`${kbName} 暂停 ${counts.paused}`}
        />
        {item.operation && (
          <Link
            href={`/org/kb/${kbId}?view=sources`}
            aria-label={`${kbName} 后台操作 ${item.operation.count}`}
          >
            <ToneBadge
              tone="info"
              className="transition-opacity hover:opacity-75"
            >
              后台操作 {item.operation.count}
            </ToneBadge>
          </Link>
        )}
        {visibleAlerts.map((alert) => (
          <Link
            key={alert.code}
            href={
              alert.code.startsWith("snapshot_")
                ? `/org/kb/${kbId}?view=release`
                : `/org/kb/${kbId}?view=sources`
            }
            aria-label={`${kbName} ${ALERT_LABELS[alert.code]}${alert.count > 1 ? ` ${alert.count}` : ""}`}
          >
            <ToneBadge
              tone={alert.severity === "error" ? "destructive" : "warning"}
              className="transition-opacity hover:opacity-75"
            >
              <AlertTriangle className="size-3" />
              {ALERT_LABELS[alert.code]}
              {alert.count > 1 && ` ${alert.count}`}
            </ToneBadge>
          </Link>
        ))}
        {counts.total === 0 && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Database className="size-3.5" />
            暂无资料
          </span>
        )}
      </div>

      <Link
        href={`/org/kb/${kbId}?view=release`}
        aria-label={`${kbName} 发布状态：${release.label}`}
        className="flex items-center gap-1.5 border-t border-border/60 pt-2 text-xs hover:text-primary"
      >
        <Rocket className="size-3.5" />
        <span className="text-muted-foreground">发布状态</span>
        <ToneBadge tone={release.tone}>{release.label}</ToneBadge>
      </Link>
    </div>
  );
}
