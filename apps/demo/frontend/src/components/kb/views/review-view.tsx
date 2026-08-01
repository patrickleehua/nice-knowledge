"use client";

// review 视图:事实审核工作台。
//
// 相对旧版的改动(P2):
// 1. 逐条审核不再开弹窗——改成「左列表 + 右常驻详情」双栏,选中即看证据,
//    审 200 条不用开关 200 次 Dialog;
// 2. 键盘流:j/k 上下、y 确认、n 拒绝、x 勾选、Esc 取消选择、? 看帮助;
// 3. 表决先乐观出队并给 5 秒「撤销」窗口,到点才真正提交
//    (后端只有 confirm/reject,没有回退接口,所以撤销必须做在提交之前);
// 4. 置信度阈值用带刻度的 Slider,合并原来语义重叠的「选中 / 一键通过」两个按钮;
// 5. 队列切换用 Tabs 组件并进 URL(?queue=),AI 审核不再显示假进度条。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Check,
  FileWarning,
  Keyboard,
  Loader2,
  Quote,
  Sparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  MarkdownPreview,
  type PreviewAnchor,
} from "@/components/kb/markdown-preview";
import { useFactClaimPages } from "@/components/kb/workbench/kb-data";
import { useEndReached } from "@/components/kb/workbench/use-virtual-rows";
import { BulkActionBar, EmptyState, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import { useUrlState } from "@/lib/use-url-state";
import type {
  BatchResult,
  CanonicalEntity,
  FactClaimEvidence,
  FactClaimReview,
} from "@/lib/types";
type ReviewStatus = "suggested" | "orphaned";
type ReviewAction = "confirm" | "reject";

/** 表决落库前的撤销窗口 */
const UNDO_MS = 5000;
const RELATION_PREDICATES = new Set([
  "located_in",
  "near",
  "includes",
  "part_of",
  "serves",
  "supports",
  "derived_from",
  "related",
]);

interface PendingDecision {
  claim: FactClaimReview;
  action: ReviewAction;
  queueStatus: ReviewStatus;
  timer: ReturnType<typeof setTimeout>;
}

function reportBatch(result: BatchResult, verb: string) {
  if (result.done.length) toast.success(`${verb} ${result.done.length} 项`);
  for (const skipped of result.skipped.slice(0, 3)) {
    toast.warning(`跳过: ${skipped.reason}`);
  }
  if (result.skipped.length > 3) {
    toast.warning(`另有 ${result.skipped.length - 3} 项被跳过`);
  }
}

function toggle(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function isComplex(value: unknown): boolean {
  return typeof value === "object" && value !== null;
}

function buildDraft(payload: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [
      key,
      isComplex(value) ? JSON.stringify(value, null, 2) : String(value ?? ""),
    ]),
  );
}

function restorePayload(
  payload: Record<string, unknown>,
  draft: Record<string, string>,
): Record<string, unknown> {
  const restored: Record<string, unknown> = {};
  for (const [key, original] of Object.entries(payload)) {
    const raw = draft[key] ?? "";
    if (isComplex(original)) {
      try {
        restored[key] = JSON.parse(raw) as unknown;
      } catch {
        throw new Error(`字段 ${key} 不是合法 JSON`);
      }
    } else if (typeof original === "number") {
      if (raw.trim() === "") restored[key] = null;
      else if (Number.isNaN(Number(raw)))
        throw new Error(`字段 ${key} 需为数字`);
      else restored[key] = Number(raw);
    } else if (typeof original === "boolean") {
      restored[key] = raw.trim() === "true";
    } else {
      restored[key] = raw === "" && original === null ? null : raw;
    }
  }
  return restored;
}

function evidenceAnchor(
  evidence: FactClaimEvidence | null,
): PreviewAnchor | null {
  if (!evidence || evidence.start_line === null) return null;
  return {
    startLine: evidence.start_line,
    endLine: evidence.end_line ?? evidence.start_line,
  };
}

function evidenceLocation(evidence: FactClaimEvidence): string {
  const parts = [evidence.filename];
  if (evidence.page !== null) parts.push(`第 ${evidence.page} 页`);
  if (evidence.start_line !== null) {
    parts.push(
      evidence.end_line !== null && evidence.end_line !== evidence.start_line
        ? `${evidence.start_line}-${evidence.end_line} 行`
        : `${evidence.start_line} 行`,
    );
  }
  if (evidence.cell_ref) parts.push(evidence.cell_ref);
  return parts.join(" · ");
}

function claimTitle(claim: FactClaimReview): string {
  const payload = claim.effective_payload;
  const value = payload.name ?? payload.title ?? payload.label;
  if (typeof value === "string" && value.trim()) return value;
  // 关系事实没有 name:用「源 → 目标」代替裸谓词,否则队列里全是 located_in
  const source = payload.source_name;
  const target = payload.target_name;
  if (
    typeof source === "string" &&
    source.trim() &&
    typeof target === "string" &&
    target.trim()
  ) {
    return `${source} → ${target}`;
  }
  return claim.predicate;
}

function Confidence({ value }: { value: number | null }) {
  if (value === null)
    return <span className="text-xs text-muted-foreground">--</span>;
  const low = value < 0.7;
  return (
    <span
      className={cn(
        "font-mono text-xs tabular-nums",
        low ? "text-warning" : "text-muted-foreground",
      )}
    >
      {(value * 100).toFixed(0)}%
    </span>
  );
}

function JsonPanel({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown> | null;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <pre className="max-h-28 overflow-auto rounded-md border border-border bg-muted/30 p-2 font-mono text-[11px] whitespace-pre-wrap">
        {value ? JSON.stringify(value, null, 2) : "--"}
      </pre>
    </div>
  );
}

const SHORTCUTS: [string, string][] = [
  ["j / ↓", "下一条"],
  ["k / ↑", "上一条"],
  ["y", "确认当前条"],
  ["n", "拒绝当前条"],
  ["x", "勾选/取消勾选"],
  ["Esc", "清空勾选"],
];

function ShortcutHint() {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger
          aria-label="键盘快捷键"
          className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <Keyboard className="size-4" />
        </TooltipTrigger>
        <TooltipContent className="max-w-56">
          <div className="space-y-0.5">
            {SHORTCUTS.map(([key, desc]) => (
              <div key={key} className="flex justify-between gap-3 text-xs">
                <span className="font-mono">{key}</span>
                <span className="text-muted-foreground">{desc}</span>
              </div>
            ))}
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function ReviewView({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  // 队列进 URL(?queue=),刷新与底部状态条深链都能落到同一队列
  const { get, set } = useUrlState();
  const reviewStatus: ReviewStatus =
    get("queue") === "orphaned" ? "orphaned" : "suggested";

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [threshold, setThreshold] = useState(80);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [evidenceIndex, setEvidenceIndex] = useState(0);
  const [subjectEntityId, setSubjectEntityId] = useState<string | null>(null);
  const [objectEntityId, setObjectEntityId] = useState<string | null>(null);
  const [aiRunning, setAiRunning] = useState(false);

  // 待落库的表决:key = claimId。撤销 = 在计时器到点前清掉。
  const pendingRef = useRef(new Map<string, PendingDecision>());
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());

  const claimsQuery = useFactClaimPages(kbId, reviewStatus);
  const claims = claimsQuery.data?.pages.flatMap((page) => page);

  const invalidateQueue = async (status: ReviewStatus) => {
    await queryClient.resetQueries({
      queryKey: ["kb-fact-claims", kbId, status, "paginated"],
      exact: true,
    });
    await queryClient.invalidateQueries({
      queryKey: ["kb-fact-claims", kbId, status],
      exact: false,
    });
  };

  const review = useMutation({
    mutationFn: ({
      claimId,
      action,
      correctedPayload,
      subjectEntityId: subject,
      objectEntityId: object,
    }: {
      claimId: string;
      action: ReviewAction;
      correctedPayload?: Record<string, unknown>;
      subjectEntityId?: string;
      objectEntityId?: string;
      queueStatus: ReviewStatus;
      silent?: boolean;
    }) =>
      api.post<FactClaimReview>(`/kb/fact-claims/${claimId}/review`, {
        action,
        ...(correctedPayload ? { corrected_payload: correctedPayload } : {}),
        ...(subject ? { subject_entity_id: subject } : {}),
        ...(object ? { object_entity_id: object } : {}),
      }),
    onSuccess: (_, variables) => {
      if (!variables.silent) {
        toast.success(
          variables.action === "confirm" ? "事实已确认" : "事实已拒绝",
        );
      }
      invalidateQueue(variables.queueStatus);
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  /** 立即提交某条待落库表决(计时到点、切队列、离开页面时调用) */
  const flushDecision = (claimId: string) => {
    const decision = pendingRef.current.get(claimId);
    if (!decision) return;
    clearTimeout(decision.timer);
    pendingRef.current.delete(claimId);
    setPendingIds(new Set(pendingRef.current.keys()));
    review.mutate({
      claimId,
      action: decision.action,
      queueStatus: decision.queueStatus,
      silent: true,
    });
  };

  const flushAll = () => {
    for (const claimId of [...pendingRef.current.keys()])
      flushDecision(claimId);
  };

  // 组件卸载(切视图/离开页面)时把还没到点的表决补交,避免用户操作静默丢失
  const flushRef = useRef<() => void>(() => {});
  useEffect(() => {
    flushRef.current = flushAll;
  });
  useEffect(() => () => flushRef.current(), []);

  const decide = (claim: FactClaimReview, action: ReviewAction) => {
    if (pendingRef.current.has(claim.id)) return;
    const timer = setTimeout(() => flushDecision(claim.id), UNDO_MS);
    pendingRef.current.set(claim.id, {
      claim,
      action,
      queueStatus: reviewStatus,
      timer,
    });
    setPendingIds(new Set(pendingRef.current.keys()));
    toast.success(
      `${action === "confirm" ? "已确认" : "已拒绝"}「${claimTitle(claim)}」`,
      {
        duration: UNDO_MS,
        action: {
          label: "撤销",
          onClick: () => {
            const decision = pendingRef.current.get(claim.id);
            if (!decision) {
              toast.info("该表决已提交,无法撤销");
              return;
            }
            clearTimeout(decision.timer);
            pendingRef.current.delete(claim.id);
            setPendingIds(new Set(pendingRef.current.keys()));
          },
        },
      },
    );
  };

  const reviewBatch = useMutation({
    mutationFn: ({
      action,
      ids,
    }: {
      action: ReviewAction;
      ids: string[];
      queueStatus: ReviewStatus;
    }) => {
      const batches = Array.from(
        { length: Math.ceil(ids.length / 200) },
        (_, index) => ids.slice(index * 200, (index + 1) * 200),
      );
      return batches.reduce<Promise<BatchResult>>(
        async (accPromise, batchIds) => {
          const acc = await accPromise;
          const result = await api.post<BatchResult>("/kb/fact-claims/batch", {
            ids: batchIds,
            action,
          });
          return {
            done: [...acc.done, ...result.done],
            skipped: [...acc.skipped, ...result.skipped],
          };
        },
        Promise.resolve({ done: [], skipped: [] }),
      );
    },
    onSuccess: (result, variables) => {
      reportBatch(result, variables.action === "confirm" ? "已确认" : "已拒绝");
      setSelected(new Set());
      invalidateQueue(variables.queueStatus);
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const aiReview = useMutation({
    mutationFn: () => {
      setAiRunning(true);
      return api.post<{
        reviewed: number;
        confirmed: number;
        rejected: number;
        escalated: number;
        failed: number;
      }>("/kb/fact-claims/ai-review", { kb_id: kbId, limit: 500 });
    },
    onSuccess: async (summary) => {
      toast.success(
        `AI 审核完成:确认 ${summary.confirmed} 条,拒绝 ${summary.rejected} 条` +
          (summary.escalated ? `,${summary.escalated} 条需人工复核` : ""),
      );
      if (summary.failed) toast.warning(`${summary.failed} 条判定失败,可重试`);
      if (summary.reviewed === 500) {
        toast.info("本批已达 500 条上限，队列仍有内容时可继续执行下一批");
      }
      setSelected(new Set());
      await invalidateQueue(reviewStatus);
    },
    onError: (error) => toast.error(errMsg(error)),
    onSettled: () => setAiRunning(false),
  });

  // 已表决但尚未落库的条目先从列表移出(乐观出队),撤销后自然回来
  const rows = (claims ?? []).filter((claim) => !pendingIds.has(claim.id));
  const loadedSelectedCount = rows.reduce(
    (count, claim) => count + Number(selected.has(claim.id)),
    0,
  );
  const allLoadedSelected =
    rows.length > 0 && loadedSelectedCount === rows.length;
  const someLoadedSelected = loadedSelectedCount > 0 && !allLoadedSelected;

  const activeIndex = Math.max(
    0,
    rows.findIndex((claim) => claim.id === activeId),
  );
  const active: FactClaimReview | undefined = rows[activeIndex];

  // 队列虚拟渲染:滚到接近底部自动取下一页,不再有「先渲染更多、再去后端要」两套语义
  const listRef = useRef<HTMLDivElement>(null);
  const fetchNextPage = claimsQuery.fetchNextPage;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 44,
    overscan: 8,
  });
  const items = virtualizer.getVirtualItems();
  useEndReached({
    lastIndex: items.length ? items[items.length - 1].index : -1,
    count: rows.length,
    hasMore: claimsQuery.hasNextPage,
    loading: claimsQuery.isFetchingNextPage,
    onEndReached: useCallback(() => void fetchNextPage(), [fetchNextPage]),
  });

  // 切换当前条时重置编辑草稿(渲染期调整状态,避免 effect 级联)
  const [lastActiveId, setLastActiveId] = useState<string | null>(null);
  if (active && lastActiveId !== active.id) {
    setLastActiveId(active.id);
    setDraft(buildDraft(active.effective_payload));
    setEvidenceIndex(0);
    setSubjectEntityId(active.subject_entity_id);
    setObjectEntityId(active.object_entity_id);
  }

  const activeEvidence = active?.evidence[evidenceIndex] ?? null;
  const anchor = evidenceAnchor(activeEvidence);
  const activeIsRelation = RELATION_PREDICATES.has(active?.predicate ?? "");

  const canonicalEntities = useQuery({
    queryKey: ["kb-canonical-entities", kbId, "review", active?.predicate],
    queryFn: () =>
      api.get<CanonicalEntity[]>(
        `/kb/canonical-entities?kb_id=${kbId}${activeIsRelation ? "" : `&entity_type=${encodeURIComponent(active!.predicate)}`}&limit=500`,
      ),
    enabled: active !== undefined,
  });

  const confirmWithDraft = () => {
    if (!active) return;
    let correctedPayload: Record<string, unknown>;
    try {
      correctedPayload = restorePayload(active.effective_payload, draft);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "字段格式有误");
      return;
    }
    const changed =
      JSON.stringify(correctedPayload) !==
      JSON.stringify(active.effective_payload);
    // 带修正的确认直接提交(修正内容不适合放进撤销窗口里悬着)
    review.mutate({
      claimId: active.id,
      action: "confirm",
      correctedPayload: changed ? correctedPayload : undefined,
      subjectEntityId: subjectEntityId ?? undefined,
      objectEntityId: objectEntityId ?? undefined,
      queueStatus: reviewStatus,
    });
  };

  // 置信度 null 的条目不参与阈值批量,保守留给逐条人工
  const aboveThreshold = useMemo(
    () =>
      (claims ?? []).filter(
        (claim) =>
          claim.confidence !== null && claim.confidence * 100 >= threshold,
      ),
    [claims, threshold],
  );

  const switchStatus = (status: ReviewStatus) => {
    flushAll();
    set({ queue: status === "suggested" ? null : status });
    setSelected(new Set());
    setActiveId(null);
  };

  const moveActive = (delta: number) => {
    if (!rows.length) return;
    const next = Math.min(Math.max(activeIndex + delta, 0), rows.length - 1);
    setActiveId(rows[next].id);
  };

  // 键盘流:输入控件获得焦点时一律放行,不劫持打字
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable ||
          target.closest("[role=dialog]"))
      ) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        moveActive(1);
      } else if (key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        moveActive(-1);
      } else if (key === "y" && active && reviewStatus === "suggested") {
        event.preventDefault();
        decide(active, "confirm");
      } else if (key === "n" && active) {
        event.preventDefault();
        decide(active, "reject");
      } else if (key === "x" && active) {
        event.preventDefault();
        setSelected((current) => toggle(current, active.id));
      } else if (event.key === "Escape") {
        setSelected(new Set());
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  // 键盘移动后把当前行滚进视野。虚拟列表里目标行可能还没挂载,
  // 所以交给 virtualizer 按索引滚,而不是查 DOM。
  useEffect(() => {
    if (activeId) virtualizer.scrollToIndex(activeIndex, { align: "auto" });
  }, [activeId, activeIndex, virtualizer]);

  return (
    <div className="flex min-h-full flex-col gap-3">
      {/* 工具条 */}
      <div className="flex flex-wrap items-center gap-2">
        <Tabs
          value={reviewStatus}
          onValueChange={(next) => switchStatus(next as ReviewStatus)}
        >
          <TabsList aria-label="审核队列" className="h-8">
            <TabsTrigger value="suggested" className="px-3 text-xs">
              待确认
            </TabsTrigger>
            <TabsTrigger value="orphaned" className="px-3 text-xs">
              孤立复核
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="text-xs text-muted-foreground tabular-nums">
          已加载 {claims?.length ?? 0} 条
          {claimsQuery.hasNextPage ? "(还有更多)" : ""}
        </span>
        {rows.length > 0 && (
          <div className="flex items-center gap-1.5">
            <Checkbox
              id="review-select-all-loaded"
              checked={allLoadedSelected}
              indeterminate={someLoadedSelected}
              onCheckedChange={(checked) =>
                setSelected(
                  checked ? new Set(rows.map((claim) => claim.id)) : new Set(),
                )
              }
            />
            <Label
              htmlFor="review-select-all-loaded"
              className="cursor-pointer text-xs text-muted-foreground"
            >
              全选已加载
            </Label>
          </div>
        )}
        <ShortcutHint />

        {reviewStatus === "suggested" && (claims?.length ?? 0) > 0 && (
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Label
                htmlFor="confidence-threshold"
                className="shrink-0 text-xs text-muted-foreground"
              >
                置信度 ≥ {threshold}%
              </Label>
              <Slider
                id="confidence-threshold"
                className="w-28"
                min={50}
                max={100}
                step={5}
                ticks={[50, 60, 70, 80, 90, 100]}
                value={threshold}
                onValueChange={(next) => setThreshold(Number(next))}
                aria-label="置信度阈值"
              />
            </div>
            <Button
              size="sm"
              disabled={reviewBatch.isPending || aboveThreshold.length === 0}
              onClick={() =>
                reviewBatch.mutate({
                  action: "confirm",
                  ids: aboveThreshold.map((claim) => claim.id),
                  queueStatus: reviewStatus,
                })
              }
            >
              <Check className="size-3" />
              通过达标的 {aboveThreshold.length} 条
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={aiRunning}
              onClick={() => aiReview.mutate()}
            >
              {aiRunning ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Sparkles className="size-3" />
              )}
              {aiRunning ? "AI 审核中…" : "AI 审核本批"}
            </Button>
          </div>
        )}
      </div>

      {aiRunning && (
        <p
          aria-live="polite"
          className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
        >
          模型正在逐条对照证据判定,本批最多 500
          条。后端按整批返回汇总,完成后队列会自动刷新——期间可以先去别的视图,不必守在这里。
        </p>
      )}

      <BulkActionBar
        count={selected.size}
        onClear={() => setSelected(new Set())}
      >
        <Button
          size="xs"
          variant="outline"
          disabled={reviewBatch.isPending}
          onClick={() =>
            reviewBatch.mutate({
              action: "reject",
              ids: [...selected],
              queueStatus: reviewStatus,
            })
          }
        >
          <X className="size-3" />
          批量拒绝
        </Button>
        {reviewStatus === "suggested" && (
          <Button
            size="xs"
            disabled={reviewBatch.isPending}
            onClick={() =>
              reviewBatch.mutate({
                action: "confirm",
                ids: [...selected],
                queueStatus: reviewStatus,
              })
            }
          >
            <Check className="size-3" />
            批量确认
          </Button>
        )}
      </BulkActionBar>

      {claimsQuery.isPending && (
        <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          正在加载审核队列
        </div>
      )}
      {claimsQuery.isError && (
        <div className="flex items-center justify-center gap-2 py-10 text-sm text-destructive">
          {errMsg(claimsQuery.error)}
          <Button
            size="sm"
            variant="outline"
            onClick={() => claimsQuery.refetch()}
          >
            重试
          </Button>
        </div>
      )}
      {!claimsQuery.isPending && !claimsQuery.isError && rows.length === 0 && (
        <EmptyState
          icon={reviewStatus === "orphaned" ? FileWarning : Check}
          title={
            reviewStatus === "suggested" ? "暂无待确认事实" : "暂无孤立事实"
          }
          description={
            reviewStatus === "suggested"
              ? "新文档摄入并抽取后,待确认的事实会出现在这里。"
              : "文档撤回后失去来源的已确认事实会进入这里复核。"
          }
        />
      )}

      {rows.length > 0 && (
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
          {/* 左:队列列表 */}
          <div
            ref={listRef}
            role="listbox"
            aria-label="待审核事实"
            className="max-h-[60vh] min-h-0 overflow-y-auto lg:max-h-[calc(100dvh-16rem)]"
          >
            <div
              className="relative w-full"
              style={{ height: virtualizer.getTotalSize() }}
            >
              {items.map((item) => {
                const claim = rows[item.index];
                const isActive = claim.id === active?.id;
                return (
                  <div
                    key={claim.id}
                    ref={virtualizer.measureElement}
                    data-index={item.index}
                    className="absolute top-0 left-0 w-full pb-1"
                    style={{ transform: `translateY(${item.start}px)` }}
                  >
                    <div
                      role="option"
                      aria-selected={isActive}
                      tabIndex={-1}
                      onClick={() => setActiveId(claim.id)}
                      className={cn(
                        "flex cursor-pointer flex-wrap items-center gap-2 rounded-md border p-2 text-sm transition-colors",
                        isActive
                          ? "border-primary bg-primary/5"
                          : "border-border hover:border-primary/40",
                      )}
                    >
                      <Checkbox
                        checked={selected.has(claim.id)}
                        aria-label={`选择 ${claimTitle(claim)}`}
                        onClick={(event) => event.stopPropagation()}
                        onCheckedChange={() =>
                          setSelected((current) => toggle(current, claim.id))
                        }
                      />
                      <ToneBadge
                        tone={
                          claim.review_status === "orphaned"
                            ? "warning"
                            : "muted"
                        }
                      >
                        {claim.review_status === "orphaned"
                          ? "孤立"
                          : claim.subject_type}
                      </ToneBadge>
                      <span
                        className="min-w-24 flex-1 truncate"
                        title={claimTitle(claim)}
                      >
                        {claimTitle(claim)}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                        {claim.predicate}
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {claim.evidence.length} 证据
                      </span>
                      <Confidence value={claim.confidence} />
                    </div>
                  </div>
                );
              })}
            </div>
            {claimsQuery.isFetchingNextPage && (
              <p className="flex items-center justify-center gap-2 py-3 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                正在加载更多事实
              </p>
            )}
          </div>

          {/* 右:常驻详情(字段编辑 + 证据原文) */}
          {active && (
            <div className="flex min-h-0 flex-col gap-3 rounded-lg border border-border p-3 lg:max-h-[calc(100dvh-16rem)]">
              <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border pb-2">
                <ToneBadge tone="muted">{active.subject_type}</ToneBadge>
                <span className="font-mono text-xs text-muted-foreground">
                  {active.predicate}
                </span>
                <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">
                    {active.model_name ?? "unknown"}
                  </span>
                  {active.prompt_version && (
                    <span>prompt {active.prompt_version}</span>
                  )}
                  <Confidence value={active.confidence} />
                </span>
              </div>

              {active.reviewed_by?.startsWith("ai:") && active.review_note && (
                <p className="shrink-0 rounded-md border border-warning/30 bg-warning/5 px-2.5 py-1.5 text-xs text-warning">
                  AI 升级人工:{active.review_note}
                </p>
              )}

              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
                <div className="grid grid-cols-2 gap-2">
                  <JsonPanel label="原始抽取" value={active.raw_payload} />
                  <JsonPanel
                    label="审核修正"
                    value={active.corrected_payload}
                  />
                </div>

                <div className="space-y-1">
                  <Label
                    htmlFor="review-subject-entity"
                    className="text-xs text-muted-foreground"
                  >
                    {activeIsRelation ? "关系主体" : "归一实体"}
                  </Label>
                  <Select
                    items={Object.fromEntries(
                      (canonicalEntities.data ?? []).map((entity) => [
                        entity.id,
                        entity.canonical_name,
                      ]),
                    )}
                    value={subjectEntityId}
                    onValueChange={(value) =>
                      setSubjectEntityId(value ? String(value) : null)
                    }
                  >
                    <SelectTrigger
                      id="review-subject-entity"
                      className="w-full"
                    >
                      <SelectValue placeholder="选择同类型实体" />
                    </SelectTrigger>
                    <SelectContent>
                      {(canonicalEntities.data ?? []).map((entity) => (
                        <SelectItem key={entity.id} value={entity.id}>
                          {entity.canonical_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                {activeIsRelation && (
                  <div className="space-y-1">
                    <Label
                      htmlFor="review-object-entity"
                      className="text-xs text-muted-foreground"
                    >
                      关系目标
                    </Label>
                    <Select
                      items={Object.fromEntries(
                        (canonicalEntities.data ?? []).map((entity) => [
                          entity.id,
                          entity.canonical_name,
                        ]),
                      )}
                      value={objectEntityId}
                      onValueChange={(value) =>
                        setObjectEntityId(value ? String(value) : null)
                      }
                    >
                      <SelectTrigger
                        id="review-object-entity"
                        className="w-full"
                      >
                        <SelectValue placeholder="选择关系目标实体" />
                      </SelectTrigger>
                      <SelectContent>
                        {(canonicalEntities.data ?? []).map((entity) => (
                          <SelectItem key={entity.id} value={entity.id}>
                            {entity.canonical_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}

                <div className="space-y-2.5">
                  {Object.entries(active.effective_payload).map(
                    ([key, value]) => (
                      <div key={key} className="space-y-1">
                        <Label
                          htmlFor={`review-field-${key}`}
                          className="font-mono text-xs text-muted-foreground"
                        >
                          {key}
                        </Label>
                        {typeof value === "boolean" ? (
                          <Checkbox
                            id={`review-field-${key}`}
                            checked={(draft[key] ?? "false") === "true"}
                            onCheckedChange={(checked) =>
                              setDraft((current) => ({
                                ...current,
                                [key]: String(checked === true),
                              }))
                            }
                          />
                        ) : isComplex(value) ? (
                          <Textarea
                            id={`review-field-${key}`}
                            value={draft[key] ?? ""}
                            onChange={(event) =>
                              setDraft((current) => ({
                                ...current,
                                [key]: event.target.value,
                              }))
                            }
                            className="min-h-20 font-mono text-xs"
                          />
                        ) : (
                          <Input
                            id={`review-field-${key}`}
                            value={draft[key] ?? ""}
                            onChange={(event) =>
                              setDraft((current) => ({
                                ...current,
                                [key]: event.target.value,
                              }))
                            }
                            className="h-8 text-sm"
                          />
                        )}
                      </div>
                    ),
                  )}
                </div>

                {/* 证据原文 */}
                <div className="overflow-hidden rounded-md border border-border">
                  {active.evidence.length > 0 ? (
                    <>
                      <div className="border-b border-border p-2">
                        <div className="flex gap-1 overflow-x-auto pb-2">
                          {active.evidence.map((evidence, index) => (
                            <button
                              key={evidence.id}
                              type="button"
                              className={cn(
                                "shrink-0 rounded-md border px-2 py-1 text-left text-xs",
                                evidenceIndex === index
                                  ? "border-primary bg-primary/5 text-foreground"
                                  : "border-border text-muted-foreground hover:text-foreground",
                              )}
                              onClick={() => setEvidenceIndex(index)}
                            >
                              {evidenceLocation(evidence)}
                            </button>
                          ))}
                        </div>
                        {activeEvidence && (
                          <div className="flex gap-2 rounded-md bg-muted/40 p-2 text-xs leading-5">
                            <Quote className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                            <span>{activeEvidence.quote_text}</span>
                          </div>
                        )}
                      </div>
                      {activeEvidence && (
                        <MarkdownPreview
                          key={activeEvidence.doc_id}
                          docId={activeEvidence.doc_id}
                          anchor={anchor}
                          className="h-72"
                        />
                      )}
                    </>
                  ) : (
                    <p className="p-6 text-center text-sm text-muted-foreground">
                      这条事实没有附带证据,确认前请谨慎核对
                    </p>
                  )}
                </div>
              </div>

              <div className="flex shrink-0 items-center gap-2 border-t border-border pt-3">
                <Button
                  variant="outline"
                  disabled={review.isPending}
                  onClick={() => decide(active, "reject")}
                >
                  <X className="size-4" />
                  拒绝
                  <kbd className="ml-1 rounded border border-border px-1 text-[10px]">
                    n
                  </kbd>
                </Button>
                {reviewStatus === "suggested" && (
                  <>
                    <Button
                      disabled={review.isPending}
                      onClick={() => decide(active, "confirm")}
                    >
                      <Check className="size-4" />
                      确认
                      <kbd className="ml-1 rounded border border-border px-1 text-[10px]">
                        y
                      </kbd>
                    </Button>
                    <Button
                      variant="outline"
                      disabled={review.isPending}
                      onClick={confirmWithDraft}
                    >
                      带修正确认
                    </Button>
                  </>
                )}
                <span className="ml-auto text-[11px] text-muted-foreground">
                  表决后有 5 秒可撤销
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
