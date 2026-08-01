"use client";

import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FileImage,
  History,
  ImageOff,
  Loader2,
  RefreshCcw,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { KnowledgeMedia } from "@/components/kb/knowledge-media";
import { useKbDocuments } from "@/components/kb/workbench/kb-data";
import { BulkActionBar, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import { kbImages } from "@/lib/kb-images";
import { cn, errMsg } from "@/lib/utils";
import { useUrlState } from "@/lib/use-url-state";
import type {
  ImageAssetIssueKind,
  ImageEnrichmentStatus,
  ImageReviewAction,
  ImageReviewStatus,
  KbImageAsset,
  KbImageAssetAuditEvent,
  NormalizedBBox,
} from "@/lib/types";

interface AssetGroup {
  documentId: string;
  filename: string;
  assets: KbImageAsset[];
}

/**
 * 按来源文档分组。图片资产平铺时一份研报的上百张插图和其它文档混在一起,
 * 既看不出哪份文档的图有问题,也没法整份处理。分组内保持后端返回的顺序
 * (doc_id, source_occurrence),即页面出现顺序。
 */
function groupAssetsByDocument(assets: KbImageAsset[]): AssetGroup[] {
  const groups = new Map<string, AssetGroup>();
  for (const asset of assets) {
    const existing = groups.get(asset.source_document_id);
    if (existing) {
      existing.assets.push(asset);
      continue;
    }
    groups.set(asset.source_document_id, {
      documentId: asset.source_document_id,
      filename: asset.source_filename,
      assets: [asset],
    });
  }
  return [...groups.values()];
}

const PAGE_SIZE = 18;

/**
 * 来源文档下拉的规模上限。几百份文档摊在一个下拉里既翻不动也认不出,
 * 超过这个量就不再渲染完整下拉,改为引导用户从资料页那份文档的
 * 「查看图片」进来(即 ?view=images&doc=)——那里有搜索、分组和状态,
 * 比在这里重造一个可搜索选择器更省事也更准。
 */
const DOCUMENT_FILTER_LIMIT = 20;

const ENRICHMENT_LABELS: Record<ImageEnrichmentStatus, string> = {
  pending: "等待处理",
  processing: "处理中",
  succeeded: "已完成",
  failed: "处理失败",
};
const REVIEW_LABELS: Record<ImageReviewStatus, string> = {
  needs_review: "待审核",
  accepted: "已接受",
  excluded: "已排除",
  rejected: "已拒绝",
};
const FAILURE_LABELS: Record<string, string> = {
  caption_provider_unconfigured: "图片描述服务未配置",
  caption_provider_unavailable: "图片描述服务不可用",
  caption_provider_credentials_missing: "图片描述服务凭证缺失",
  ocr_provider_unavailable: "OCR 服务不可用",
  image_too_large: "图片超过大小限制",
  invalid_dimensions: "图片尺寸不受支持",
  unsupported_format: "图片格式不受支持",
  malformed_image: "图片内容损坏",
  missing_object: "原图对象缺失",
  image_asset_unavailable: "图片对象当前不可用",
};
const ISSUE_LABELS: Record<ImageAssetIssueKind, string> = {
  unresolved: "未解决",
  provider_failed: "服务失败",
  missing_object: "对象缺失",
  oversized: "尺寸超限",
  unsupported: "格式不支持",
  duplicate: "重复图片",
};

function locationLabel(asset: KbImageAsset): string {
  if (asset.page != null) return `第 ${asset.page} 页`;
  if (asset.slide != null) return `第 ${asset.slide} 张幻灯片`;
  return "未提供页码或幻灯片位置";
}

function StateBadges({ asset }: { asset: KbImageAsset }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      <ToneBadge
        tone={
          asset.enrichment_status === "failed"
            ? "destructive"
            : asset.enrichment_status === "succeeded"
              ? "success"
              : "muted"
        }
      >
        {ENRICHMENT_LABELS[asset.enrichment_status]}
      </ToneBadge>
      <ToneBadge
        tone={
          asset.review_status === "accepted"
            ? "success"
            : asset.review_status === "needs_review"
              ? "warning"
              : "muted"
        }
      >
        {REVIEW_LABELS[asset.review_status]}
      </ToneBadge>
      {asset.review_source === "human" ? (
        <ToneBadge tone="primary">
          <ShieldCheck className="size-3" />
          人工审核
        </ToneBadge>
      ) : asset.caption_provider ? (
        <ToneBadge tone="muted">
          <Sparkles className="size-3" />
          AI 生成
        </ToneBadge>
      ) : null}
    </div>
  );
}

function BboxSourceContext({
  bbox,
  context,
}: {
  bbox: NormalizedBBox | null;
  context: string | null;
}) {
  return (
    <section className="space-y-2" aria-label="原文位置">
      <h3 className="text-xs font-medium">原文上下文与位置</h3>
      <div className="grid gap-3 sm:grid-cols-[8rem_minmax(0,1fr)]">
        <div className="relative aspect-[3/4] rounded-md border border-border bg-muted/40">
          {bbox ? (
            <span
              className="absolute border-2 border-primary bg-primary/10"
              style={{
                left: `${bbox.left * 100}%`,
                top: `${bbox.top * 100}%`,
                width: `${(bbox.right - bbox.left) * 100}%`,
                height: `${(bbox.bottom - bbox.top) * 100}%`,
              }}
              aria-label={`图片位置：左 ${Math.round(bbox.left * 100)}%，上 ${Math.round(bbox.top * 100)}%，右 ${Math.round(bbox.right * 100)}%，下 ${Math.round(bbox.bottom * 100)}%`}
            />
          ) : (
            <span className="absolute inset-0 flex items-center justify-center px-2 text-center text-[11px] text-muted-foreground">
              源格式未提供可靠 bbox
            </span>
          )}
        </div>
        <blockquote className="min-h-28 rounded-md border-l-2 border-border bg-muted/25 p-3 text-xs leading-5 whitespace-pre-wrap">
          {context || "源格式未提供邻近原文，系统不会猜测上下文。"}
        </blockquote>
      </div>
    </section>
  );
}

function AuditEvent({ event }: { event: KbImageAssetAuditEvent }) {
  const actor =
    event.actor_kind === "human"
      ? "人工"
      : event.actor_kind === "policy"
        ? "策略"
        : event.actor_kind === "provider"
          ? "服务"
          : "系统";
  return (
    <li className="relative space-y-1 border-l border-border pb-4 pl-4 text-xs last:pb-0">
      <span className="absolute top-1 -left-1 size-2 rounded-full bg-primary" />
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{event.action}</span>
        <ToneBadge tone={event.actor_kind === "human" ? "primary" : "muted"}>
          {actor}
        </ToneBadge>
        <span className="text-muted-foreground">
          v{event.event_version} ·{" "}
          {new Date(event.created_at).toLocaleString("zh-CN")}
        </span>
      </div>
      {event.reason && <p className="text-muted-foreground">{event.reason}</p>}
      {(event.before_state?.review_status ||
        event.after_state?.review_status) && (
        <p className="text-muted-foreground">
          状态 {event.before_state?.review_status ?? "—"} →{" "}
          {event.after_state?.review_status ?? "—"}
        </p>
      )}
      {event.before_state?.caption !== event.after_state?.caption &&
        event.after_state?.caption && (
          <p className="line-clamp-2">描述更新为：{event.after_state.caption}</p>
        )}
    </li>
  );
}

function ImageReviewDialog({
  assetId,
  open,
  onOpenChange,
  onPrev,
  onNext,
  position,
}: {
  assetId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 按当前页顺序切换上/下一张,审完不必关掉弹窗再点 */
  onPrev?: () => void;
  onNext?: () => void;
  position?: { current: number; total: number };
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState({
    assetId: "",
    caption: "",
    ocrText: "",
    reason: "",
  });

  const detail = useQuery({
    queryKey: ["kb-image-asset", assetId],
    queryFn: () => kbImages.detail(assetId as string),
    enabled: open && Boolean(assetId),
  });
  const audit = useQuery({
    queryKey: ["kb-image-audit", assetId],
    queryFn: () => kbImages.audit(assetId as string),
    enabled: open && Boolean(assetId),
    retry: (count, error) =>
      !(error instanceof ApiError && error.status === 403) && count < 2,
  });
  const asset = detail.data;
  const caption =
    draft.assetId === asset?.asset_id
      ? draft.caption
      : (asset?.caption ?? "");
  const ocrText =
    draft.assetId === asset?.asset_id
      ? draft.ocrText
      : (asset?.ocr_text ?? "");
  const reason =
    draft.assetId === asset?.asset_id ? draft.reason : "";
  const updateDraft = (
    field: "caption" | "ocrText" | "reason",
    value: string,
  ) => {
    if (!asset) return;
    setDraft((current) => ({
      assetId: asset.asset_id,
      caption:
        current.assetId === asset.asset_id
          ? current.caption
          : (asset.caption ?? ""),
      ocrText:
        current.assetId === asset.asset_id
          ? current.ocrText
          : (asset.ocr_text ?? ""),
      reason: current.assetId === asset.asset_id ? current.reason : "",
      [field]: value,
    }));
  };

  const invalidate = async (updated?: KbImageAsset) => {
    if (updated) {
      queryClient.setQueryData(["kb-image-asset", updated.asset_id], updated);
    }
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["kb-image-assets"] }),
      queryClient.invalidateQueries({
        queryKey: ["kb-image-audit", assetId],
      }),
    ]);
  };
  const handleMutationError = (error: unknown) => {
    if (error instanceof ApiError && error.status === 409) {
      toast.warning("该图片已被其他审核操作更新，已刷新最新状态");
      void detail.refetch();
      void audit.refetch();
      return;
    }
    toast.error(errMsg(error));
  };
  const review = useMutation({
    mutationFn: ({
      action,
      edited,
    }: {
      action: ImageReviewAction;
      edited?: boolean;
    }) =>
      kbImages.review(assetId as string, {
        action,
        expected_lock_version: asset?.lock_version ?? 0,
        reason: reason.trim(),
        ...(edited
          ? {
              caption: caption.trim() || null,
              ocr_text: ocrText.trim() || null,
            }
          : {}),
      }),
    onSuccess: (updated) => {
      toast.success("图片审核状态已更新");
      setDraft({
        assetId: updated.asset_id,
        caption: updated.caption ?? "",
        ocrText: updated.ocr_text ?? "",
        reason: "",
      });
      void invalidate(updated);
    },
    onError: handleMutationError,
  });
  const retry = useMutation({
    mutationFn: () =>
      kbImages.retry(assetId as string, {
        expected_lock_version: asset?.lock_version ?? 0,
        reason: reason.trim(),
      }),
    onSuccess: () => {
      toast.success("已重新派发图片摄入任务");
      void invalidate();
      void detail.refetch();
    },
    onError: handleMutationError,
  });
  const busy = review.isPending || retry.isPending;
  const reasonReady = Boolean(reason.trim());

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[94vh] w-[calc(100vw-2rem)] overflow-y-auto sm:max-w-[min(94vw,1440px)]">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            图片审核
            {asset && <StateBadges asset={asset} />}
            {(onPrev || onNext) && (
              <span className="ml-auto flex items-center gap-0.5 pr-8">
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  aria-label="上一张"
                  title="上一张"
                  disabled={!onPrev}
                  onClick={onPrev}
                >
                  <ChevronLeft className="size-4" />
                </Button>
                {position && (
                  <span className="text-xs font-normal text-muted-foreground tabular-nums">
                    {position.current}/{position.total}
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  aria-label="下一张"
                  title="下一张"
                  disabled={!onNext}
                  onClick={onNext}
                >
                  <ChevronRight className="size-4" />
                </Button>
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            检查原图、来源定位、OCR 与描述后再决定是否进入后续快照。
          </DialogDescription>
        </DialogHeader>

        {detail.isPending && (
          <div className="grid gap-4 md:grid-cols-2">
            <Skeleton className="aspect-video" />
            <Skeleton className="h-80" />
          </div>
        )}
        {detail.isError && (
          <div className="flex min-h-48 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <ImageOff className="size-6" />
            {errMsg(detail.error)}
            <Button variant="outline" size="sm" onClick={() => detail.refetch()}>
              重试
            </Button>
          </div>
        )}
        {asset && (
          <div className="grid min-h-0 gap-5 xl:grid-cols-[minmax(18rem,2fr)_minmax(24rem,3fr)]">
            <div className="space-y-4">
              <KnowledgeMedia
                assetId={asset.asset_id}
                alt={asset.caption || `来自 ${asset.source_filename} 的图片`}
                width={asset.width}
                height={asset.height}
                thumbnailUrl={asset.thumbnail_url}
                contentUrl={asset.content_url}
                sourceLabel={`${asset.source_filename} · ${locationLabel(asset)}`}
              />
              <div className="space-y-1 text-xs text-muted-foreground">
                <p className="font-medium text-foreground">
                  {asset.source_filename}
                </p>
                <p>
                  修订 v{asset.revision_number} · {locationLabel(asset)} ·{" "}
                  {asset.parser_name}
                </p>
                <p>
                  {asset.width ?? "—"} × {asset.height ?? "—"} ·{" "}
                  {(asset.size_bytes / 1024).toFixed(1)} KiB
                </p>
              </div>
              <BboxSourceContext
                bbox={asset.bbox}
                context={asset.surrounding_text}
              />
            </div>

            <div className="space-y-4">
              {asset.failure_code && (
                <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                  失败原因：
                  {FAILURE_LABELS[asset.failure_code] ?? asset.failure_code}
                </div>
              )}
              <div className="grid gap-3 md:grid-cols-2">
                <label className="space-y-1.5 text-xs font-medium">
                  <span className="flex items-center gap-1.5">
                    <Sparkles className="size-3.5" />
                    图片描述
                    {asset.caption_provider && (
                      <ToneBadge tone="muted">AI 生成</ToneBadge>
                    )}
                  </span>
                  <Textarea
                    aria-label="图片描述"
                    value={caption}
                    onChange={(event) =>
                      updateDraft("caption", event.target.value)
                    }
                    className="min-h-40 text-sm"
                  />
                  <span className="block font-normal text-muted-foreground">
                    {asset.caption_provider ?? "未提供 provider"} ·{" "}
                    {asset.caption_model ?? "未提供 model"}
                  </span>
                </label>
                <label className="space-y-1.5 text-xs font-medium">
                  <span>OCR 文字</span>
                  <Textarea
                    aria-label="OCR 文字"
                    value={ocrText}
                    onChange={(event) =>
                      updateDraft("ocrText", event.target.value)
                    }
                    className="min-h-40 text-sm"
                  />
                  <span className="block font-normal text-muted-foreground">
                    {asset.ocr_provider ?? "未提供 provider"} ·{" "}
                    {asset.ocr_model ?? "未提供 model"}
                  </span>
                </label>
              </div>
              <label className="space-y-1.5 text-xs font-medium">
                <span>审核说明（必填）</span>
                <Input
                  value={reason}
                  onChange={(event) =>
                    updateDraft("reason", event.target.value)
                  }
                  placeholder="记录接受、修正、排除或重试原因"
                />
              </label>
              {/* 主行动(接受/修正并接受)在前,否决类其次,重新处理单独一档 */}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  disabled={!reasonReady || busy}
                  onClick={() => review.mutate({ action: "accept" })}
                >
                  <Check />
                  接受
                </Button>
                <Button
                  variant="outline"
                  disabled={
                    !reasonReady ||
                    busy ||
                    (!caption.trim() && !ocrText.trim())
                  }
                  onClick={() =>
                    review.mutate({ action: "edit_and_accept", edited: true })
                  }
                >
                  <ShieldCheck />
                  修正并接受
                </Button>
                <span aria-hidden className="mx-1 h-5 w-px bg-border" />
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!reasonReady || busy}
                  onClick={() => review.mutate({ action: "exclude" })}
                >
                  <ImageOff />
                  排除装饰图
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-destructive"
                  disabled={!reasonReady || busy}
                  onClick={() => review.mutate({ action: "reject" })}
                >
                  <X />
                  拒绝
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  disabled={
                    !reasonReady ||
                    busy ||
                    !(
                      asset.enrichment_status === "failed" ||
                      asset.review_status === "rejected"
                    )
                  }
                  onClick={() => retry.mutate()}
                >
                  <RefreshCcw />
                  重新处理
                </Button>
              </div>

              <section className="space-y-2 border-t border-border pt-4">
                <h3 className="flex items-center gap-1.5 text-xs font-medium">
                  <History className="size-3.5" />
                  不可变审核记录
                </h3>
                {audit.isPending && (
                  <p className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="size-3.5 animate-spin" />
                    正在加载审核记录
                  </p>
                )}
                {audit.isError && (
                  <p className="text-xs text-muted-foreground">
                    {audit.error instanceof ApiError &&
                    audit.error.status === 403
                      ? "当前角色无权查看审核记录或执行审核操作。"
                      : errMsg(audit.error)}
                  </p>
                )}
                {audit.data?.items.length === 0 && (
                  <p className="text-xs text-muted-foreground">暂无审核记录</p>
                )}
                {audit.data && (
                  <ol>
                    {audit.data.items.map((event) => (
                      <AuditEvent
                        key={`${event.event_version}-${event.created_at}`}
                        event={event}
                      />
                    ))}
                  </ol>
                )}
              </section>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function AssetCard({
  asset,
  selected,
  onToggleSelect,
  onReview,
}: {
  asset: KbImageAsset;
  selected: boolean;
  onToggleSelect: () => void;
  onReview: () => void;
}) {
  const usageTotal =
    asset.usage.snapshot_count +
    asset.usage.image_chunk_count +
    asset.usage.evidence_span_count +
    asset.usage.business_artifact_count;
  return (
    <Card
      className={cn(
        "relative overflow-hidden py-0 transition-colors",
        selected && "ring-2 ring-primary",
      )}
    >
      {/* 多选勾选框浮在缩略图左上角,不与整卡点击冲突 */}
      <span className="absolute top-2 left-2 z-10 rounded-md bg-background/90 p-1 shadow-sm">
        <Checkbox
          checked={selected}
          aria-label={`选择 ${asset.source_filename} 的图片`}
          onCheckedChange={onToggleSelect}
        />
      </span>
      <KnowledgeMedia
        assetId={asset.asset_id}
        alt={asset.caption || `来自 ${asset.source_filename} 的图片`}
        width={asset.thumbnail_width ?? asset.width}
        height={asset.thumbnail_height ?? asset.height}
        thumbnailUrl={asset.thumbnail_url}
        contentUrl={asset.content_url}
        sourceLabel={`${asset.source_filename} · ${locationLabel(asset)}`}
        className="rounded-none border-0 border-b"
      />
      <CardContent className="space-y-3 p-3">
        <StateBadges asset={asset} />
        <div className="space-y-1">
          {/* 文件名由所属分组的表头承担,卡片位置让给更有辨识度的页内位置 */}
          <button
            type="button"
            onClick={onReview}
            className="block w-full truncate text-left text-sm font-medium transition-colors hover:text-primary hover:underline"
            title={`${asset.source_filename} · ${locationLabel(asset)}(点击审核)`}
          >
            {locationLabel(asset)}
          </button>
          <p className="text-xs text-muted-foreground">
            修订 v{asset.revision_number}
          </p>
        </div>
        {asset.failure_code ? (
          <p className="line-clamp-2 text-xs text-destructive">
            {FAILURE_LABELS[asset.failure_code] ?? asset.failure_code}
          </p>
        ) : (
          <p className="line-clamp-2 min-h-8 text-xs leading-4 text-muted-foreground">
            {asset.caption || asset.ocr_text || "尚无图片描述或 OCR 文字"}
          </p>
        )}
        <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
          <span>下游引用 {usageTotal}</span>
          {/* 不用整卡覆盖层:缩略图本身要保留点开大图的交互 */}
          <Button size="sm" variant="outline" onClick={onReview}>
            审核
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// 筛选与分页一律进 query string:刷新不丢、可分享、可被底部发布状态条深链命中
// (?review=needs_review 等)。每个下拉都带「全部」项,可单独清除该维度——
// 旧版只有一个总的「清除筛选」,选了处理状态后无法单独取消。
const ALL = "__all__";

/** 带「全部」项的筛选下拉:选中「全部」即清除该维度,不必整体清空。 */
function FilterSelect({
  label,
  allLabel,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  allLabel: string;
  value: string | null;
  options: { value: string; label: string }[];
  disabled?: boolean;
  onChange: (next: string | null) => void;
}) {
  const items = [{ value: ALL, label: allLabel }, ...options];
  return (
    <Select
      items={items}
      value={value ?? ALL}
      disabled={disabled}
      onValueChange={(next) => {
        const picked = String(next);
        onChange(picked === ALL ? null : picked);
      }}
    >
      <SelectTrigger className="w-full" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function ImagesView({ kbId }: { kbId: string }) {
  const { get, set } = useUrlState();
  const page = Math.max(1, Number(get("p") ?? 1) || 1);
  const enrichmentStatus = (get("enrichment") ??
    null) as ImageEnrichmentStatus | null;
  const reviewStatus = (get("review") ?? null) as ImageReviewStatus | null;
  const issueKind = (get("issue") ?? null) as ImageAssetIssueKind | null;
  const failureCode = get("failure") ?? "";
  const documentId = get("doc");
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // 默认全部展开:折叠是用户的主动收纳动作,不该一进页面就把内容藏起来
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(
    () => new Set(),
  );
  const [bulkTarget, setBulkTarget] = useState<AssetGroup | null>(null);
  const [bulkReason, setBulkReason] = useState("");
  const [batchAction, setBatchAction] = useState<ImageReviewAction | null>(
    null,
  );
  const [batchReason, setBatchReason] = useState("");
  const documents = useKbDocuments(kbId);
  const queryClient = useQueryClient();

  const setPage = (next: number) => set({ p: next === 1 ? null : next });
  /** 改任一筛选都把分页复位到第 1 页 */
  const setFilter = (key: string, value: string | null) =>
    set({ [key]: value }, { reset: ["p"] });
  const hasFilters = Boolean(
    documentId || enrichmentStatus || reviewStatus || issueKind || failureCode,
  );
  const documentOptions =
    documents.data?.map((document) => ({
      value: document.id,
      label: document.filename,
    })) ?? [];

  const inventory = useQuery({
    queryKey: [
      "kb-image-assets",
      kbId,
      page,
      enrichmentStatus,
      reviewStatus,
      issueKind,
      failureCode.trim(),
      documentId,
    ],
    queryFn: () =>
      kbImages.list(kbId, {
        page,
        pageSize: PAGE_SIZE,
        enrichmentStatus,
        reviewStatus,
        issueKind,
        failureCode,
        documentId: documentId ?? undefined,
      }),
  });
  const totalPages = Math.max(
    1,
    Math.ceil((inventory.data?.total ?? 0) / PAGE_SIZE),
  );
  const items = inventory.data?.items ?? [];
  const assetGroups = groupAssetsByDocument(items);
  // 文档名优先取文档列表(带筛选也能显示),取不到再退到本页任一资产的来源名
  const activeDocumentName = documentId
    ? (documents.data?.find((document) => document.id === documentId)
        ?.filename ??
      items.find((asset) => asset.source_document_id === documentId)
        ?.source_filename ??
      null)
    : null;

  // 审核弹窗内的上/下一张:按当前页的顺序走,审完不必关掉再点下一张
  const activeIndex = items.findIndex(
    (asset) => asset.asset_id === selectedAssetId,
  );

  /**
   * 批量审核:后端按资产逐个校验 lock_version 并写不可变审核记录,
   * 没有批量端点,这里串行提交并如实汇报成功/冲突数量,不谎报"全部成功"。
   * 审核说明是治理要求(reason 必填),批量时填一次、复用到每一条。
   */
  /**
   * 整份通过:走后端批量端点,按 document_id + needs_review 跨页生效,
   * 不受当前分页限制。富化未成功或内容为空的图会被后端跳过并计入 skipped。
   */
  const bulkReview = useMutation({
    mutationFn: ({
      documentId,
      reason,
    }: {
      documentId: string;
      reason: string;
    }) =>
      kbImages.bulkReview(kbId, {
        action: "accept",
        reason,
        document_id: documentId,
        review_status: "needs_review",
      }),
    onSuccess: async (result) => {
      if (result.updated) toast.success(`已通过 ${result.updated} 张图片`);
      if (result.skipped) {
        toast.warning(
          `${result.skipped} 张需要人工确认（富化未完成或无描述），已保留`,
        );
      }
      if (result.truncated) {
        toast.warning(`本次仅处理了前 ${result.matched} 张，请再执行一次`);
      }
      if (!result.updated && !result.skipped) toast.info("没有待审核的图片");
      setBulkTarget(null);
      setBulkReason("");
      setSelectedIds(new Set());
      await queryClient.invalidateQueries({ queryKey: ["kb-image-assets"] });
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const batchReview = useMutation({
    mutationFn: async ({
      action,
      reason,
      assets,
    }: {
      action: ImageReviewAction;
      reason: string;
      assets: KbImageAsset[];
    }) => {
      let succeeded = 0;
      const conflicts: string[] = [];
      const failures: string[] = [];
      for (const asset of assets) {
        try {
          await kbImages.review(asset.asset_id, {
            action,
            expected_lock_version: asset.lock_version,
            reason,
          });
          succeeded += 1;
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) {
            conflicts.push(asset.source_filename);
          } else {
            failures.push(asset.source_filename);
          }
        }
      }
      return { succeeded, conflicts, failures };
    },
    onSuccess: async ({ succeeded, conflicts, failures }) => {
      if (succeeded) toast.success(`已处理 ${succeeded} 张图片`);
      if (conflicts.length) {
        toast.warning(`${conflicts.length} 张已被其他操作更新，请刷新后重试`);
      }
      if (failures.length) toast.error(`${failures.length} 张处理失败`);
      setSelectedIds(new Set());
      setBatchAction(null);
      setBatchReason("");
      await queryClient.invalidateQueries({ queryKey: ["kb-image-assets"] });
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const allSelected =
    items.length > 0 && items.every((asset) => selectedIds.has(asset.asset_id));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <FileImage className="size-4" />
            图片资产
          </h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            维护文档图片的来源、OCR、描述、审核状态和下游使用情况。
          </p>
        </div>
        <span className="ml-auto text-xs text-muted-foreground">
          {inventory.data?.total ?? "…"} 张
        </span>
      </div>

      {/* 从资料页某份文档进来时,明确告诉用户"现在只看这一份",并给一键退出 */}
      {documentId && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/40 bg-primary/5 px-3 py-2">
          <FileImage className="size-4 shrink-0 text-primary" />
          <span className="min-w-0 flex-1 truncate text-sm">
            仅显示《{activeDocumentName ?? "所选文档"}》的图片
          </span>
          <Button
            size="xs"
            variant="outline"
            onClick={() => setFilter("doc", null)}
          >
            查看全部图片
          </Button>
        </div>
      )}

      <div className="grid gap-2 rounded-lg border border-border bg-card p-3 sm:grid-cols-2 xl:grid-cols-5">
        {documentOptions.length > DOCUMENT_FILTER_LIMIT ? (
          <p className="flex items-center rounded-md border border-dashed border-border px-2.5 py-1.5 text-xs text-muted-foreground">
            {/* 不报具体份数:文档列表本身有软上限,报出来会是个截断后的假总数 */}
            文档较多,请从「资料」页某份文档的「查看图片」进入
          </p>
        ) : (
          <FilterSelect
            label="来源文档"
            allLabel={documents.isError ? "文档列表不可用" : "全部来源文档"}
            value={documentId}
            options={documentOptions}
            disabled={documents.isPending || documents.isError}
            onChange={(next) => setFilter("doc", next)}
          />
        )}
        <FilterSelect
          label="处理状态"
          allLabel="全部处理状态"
          value={enrichmentStatus}
          options={Object.entries(ENRICHMENT_LABELS).map(([value, label]) => ({
            value,
            label,
          }))}
          onChange={(next) => setFilter("enrichment", next)}
        />
        <FilterSelect
          label="审核状态"
          allLabel="全部审核状态"
          value={reviewStatus}
          options={Object.entries(REVIEW_LABELS).map(([value, label]) => ({
            value,
            label,
          }))}
          onChange={(next) => setFilter("review", next)}
        />
        <FilterSelect
          label="问题类型"
          allLabel="全部问题类型"
          value={issueKind}
          options={Object.entries(ISSUE_LABELS).map(([value, label]) => ({
            value,
            label,
          }))}
          onChange={(next) => setFilter("issue", next)}
        />
        <Input
          aria-label="失败代码筛选"
          value={failureCode}
          onChange={(event) =>
            setFilter(
              "failure",
              event.target.value.replace(/[^a-z0-9_]/g, "") || null,
            )
          }
          placeholder="失败代码（可选）"
        />
        {hasFilters && (
          <Button
            variant="ghost"
            size="sm"
            className="sm:col-span-2 xl:col-span-5 xl:justify-self-end"
            onClick={() =>
              set({
                doc: null,
                enrichment: null,
                review: null,
                issue: null,
                failure: null,
                p: null,
              })
            }
          >
            清除全部筛选
          </Button>
        )}
      </div>

      {inventory.isPending && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="aspect-[4/3]" />
          ))}
        </div>
      )}
      {inventory.isError && (
        <div className="flex min-h-56 flex-col items-center justify-center gap-2 rounded-lg border border-border text-sm text-muted-foreground">
          <ImageOff className="size-6" />
          {errMsg(inventory.error)}
          <Button variant="outline" size="sm" onClick={() => inventory.refetch()}>
            重试
          </Button>
        </div>
      )}
      {inventory.data?.items.length === 0 && (
        <div className="flex min-h-56 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border text-sm text-muted-foreground">
          <FileImage className="size-7" />
          当前筛选下没有图片资产
        </div>
      )}
      {items.length > 0 && (
        <>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Checkbox
              checked={allSelected}
              aria-label="全选本页图片"
              onCheckedChange={(checked) =>
                setSelectedIds(
                  checked === true
                    ? new Set(items.map((asset) => asset.asset_id))
                    : new Set(),
                )
              }
            />
            全选本页({items.length} 张)
          </div>

          <BulkActionBar
            count={selectedIds.size}
            onClear={() => setSelectedIds(new Set())}
          >
            <Button
              size="xs"
              disabled={batchReview.isPending}
              onClick={() => setBatchAction("accept")}
            >
              <Check className="size-3" />
              批量接受
            </Button>
            <Button
              size="xs"
              variant="outline"
              disabled={batchReview.isPending}
              onClick={() => setBatchAction("exclude")}
            >
              <ImageOff className="size-3" />
              批量排除装饰图
            </Button>
            <Button
              size="xs"
              variant="outline"
              disabled={batchReview.isPending}
              onClick={() => setBatchAction("reject")}
            >
              <X className="size-3" />
              批量拒绝
            </Button>
          </BulkActionBar>

          {/* 按来源文档分组:整份研报的插图收在一个可折叠区块里,可整组通过 */}
          <div className="space-y-3">
            {assetGroups.map((group) => {
              const collapsed = collapsedGroups.has(group.documentId);
              const groupIds = group.assets.map((asset) => asset.asset_id);
              const groupAllSelected = groupIds.every((id) =>
                selectedIds.has(id),
              );
              return (
                <section
                  key={group.documentId}
                  className="rounded-lg border border-border"
                >
                  <header className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-3 py-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-6"
                      aria-label={
                        collapsed
                          ? `展开 ${group.filename}`
                          : `折叠 ${group.filename}`
                      }
                      aria-expanded={!collapsed}
                      onClick={() =>
                        setCollapsedGroups((current) => {
                          const next = new Set(current);
                          if (next.has(group.documentId))
                            next.delete(group.documentId);
                          else next.add(group.documentId);
                          return next;
                        })
                      }
                    >
                      {collapsed ? (
                        <ChevronRight className="size-4" />
                      ) : (
                        <ChevronDown className="size-4" />
                      )}
                    </Button>
                    <Checkbox
                      checked={groupAllSelected}
                      aria-label={`选择 ${group.filename} 的全部图片`}
                      onCheckedChange={(checked) =>
                        setSelectedIds((current) => {
                          const next = new Set(current);
                          for (const id of groupIds) {
                            if (checked === true) next.add(id);
                            else next.delete(id);
                          }
                          return next;
                        })
                      }
                    />
                    <FileImage className="size-4 shrink-0 text-muted-foreground" />
                    <span
                      className="min-w-0 flex-1 truncate text-sm font-medium"
                      title={group.filename}
                    >
                      {group.filename}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      本页 {group.assets.length} 张
                    </span>
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={bulkReview.isPending}
                      onClick={() => setBulkTarget(group)}
                    >
                      <Check className="size-3" />
                      整份通过
                    </Button>
                  </header>
                  {!collapsed && (
                    <div className="grid gap-4 p-3 sm:grid-cols-2 xl:grid-cols-3">
                      {group.assets.map((asset) => (
                        <AssetCard
                          key={asset.asset_id}
                          asset={asset}
                          selected={selectedIds.has(asset.asset_id)}
                          onToggleSelect={() =>
                            setSelectedIds((current) => {
                              const next = new Set(current);
                              if (next.has(asset.asset_id))
                                next.delete(asset.asset_id);
                              else next.add(asset.asset_id);
                              return next;
                            })
                          }
                          onReview={() => setSelectedAssetId(asset.asset_id)}
                        />
                      ))}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        </>
      )}

      {inventory.data && inventory.data.total > PAGE_SIZE && (
        <nav
          aria-label="图片资产分页"
          className="flex items-center justify-center gap-2"
        >
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage(Math.max(1, page - 1))}
          >
            <ChevronLeft />
            上一页
          </Button>
          <span className="text-xs text-muted-foreground tabular-nums">
            第 {page} / {totalPages} 页 · 共 {inventory.data.total} 张
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage(Math.min(totalPages, page + 1))}
          >
            下一页
            <ChevronRight />
          </Button>
        </nav>
      )}

      {/* 批量审核说明:reason 是后端治理要求(必填),批量时填一次复用到每一条 */}
      <Dialog
        open={batchAction !== null}
        onOpenChange={(next) => {
          if (!next && !batchReview.isPending) {
            setBatchAction(null);
            setBatchReason("");
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {batchAction === "accept"
                ? "批量接受"
                : batchAction === "exclude"
                  ? "批量排除装饰图"
                  : "批量拒绝"}
            </DialogTitle>
            <DialogDescription>
              将对选中的 {selectedIds.size} 张图片逐条写入不可变审核记录。
            </DialogDescription>
          </DialogHeader>
          <label className="block text-xs font-medium">
            审核说明（必填，将记入每条审核记录）
            <Input
              className="mt-1"
              value={batchReason}
              onChange={(event) => setBatchReason(event.target.value)}
              placeholder="如：页眉装饰图，统一排除"
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={batchReview.isPending}
              onClick={() => {
                setBatchAction(null);
                setBatchReason("");
              }}
            >
              取消
            </Button>
            <Button
              disabled={!batchReason.trim() || batchReview.isPending}
              onClick={() =>
                batchAction &&
                batchReview.mutate({
                  action: batchAction,
                  reason: batchReason.trim(),
                  assets: items.filter((asset) =>
                    selectedIds.has(asset.asset_id),
                  ),
                })
              }
            >
              {batchReview.isPending && (
                <Loader2 className="size-4 animate-spin" />
              )}
              确认处理 {selectedIds.size} 张
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 整份通过:同样要求审核说明,后端逐条写入不可变审核记录 */}
      <Dialog
        open={bulkTarget !== null}
        onOpenChange={(next) => {
          if (!next && !bulkReview.isPending) {
            setBulkTarget(null);
            setBulkReason("");
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>整份通过</DialogTitle>
            <DialogDescription>
              将通过《{bulkTarget?.filename}》全部待审核的图片（不止当前页）。
              富化未完成或没有描述的会被保留在人工队列。
            </DialogDescription>
          </DialogHeader>
          <label className="block text-xs font-medium">
            审核说明（必填，将记入每条审核记录）
            <Input
              className="mt-1"
              value={bulkReason}
              onChange={(event) => setBulkReason(event.target.value)}
              placeholder="如：正文插图已抽检，整份通过"
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              disabled={bulkReview.isPending}
              onClick={() => {
                setBulkTarget(null);
                setBulkReason("");
              }}
            >
              取消
            </Button>
            <Button
              disabled={!bulkReason.trim() || bulkReview.isPending}
              onClick={() =>
                bulkTarget &&
                bulkReview.mutate({
                  documentId: bulkTarget.documentId,
                  reason: bulkReason.trim(),
                })
              }
            >
              {bulkReview.isPending && (
                <Loader2 className="size-4 animate-spin" />
              )}
              确认通过
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <ImageReviewDialog
        assetId={selectedAssetId}
        open={selectedAssetId !== null}
        onOpenChange={(next) => {
          if (!next) setSelectedAssetId(null);
        }}
        onPrev={
          activeIndex > 0
            ? () => setSelectedAssetId(items[activeIndex - 1].asset_id)
            : undefined
        }
        onNext={
          activeIndex >= 0 && activeIndex < items.length - 1
            ? () => setSelectedAssetId(items[activeIndex + 1].asset_id)
            : undefined
        }
        position={
          activeIndex >= 0
            ? { current: activeIndex + 1, total: items.length }
            : undefined
        }
      />
    </div>
  );
}
