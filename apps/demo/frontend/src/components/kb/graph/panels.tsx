"use client";

/**
 * sigma 图谱的外围面板(不依赖 sigma context,经回调与视图层交互):
 * 顶栏(搜索 / 过滤 / 着色切换 / 洞察入口)、Zoom 控制、图例、节点详情、洞察抽屉。
 */

import {
  ChevronDown,
  ChevronUp,
  Lightbulb,
  Locate,
  Maximize2,
  Minus,
  Palette,
  Plus,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { GraphEdgeEvidence, GraphInsights } from "@/lib/types";
import {
  graphEvidenceLocation,
  graphValidityLabel,
} from "./graph-edge-utils.mjs";
import { GRAPH_TYPE_LABELS, type SigmaGraphPalette } from "./graph-palette";

export type GraphColorMode = "type" | "community";

export interface GraphFiltersState {
  hideIsolated: boolean;
  /** 被取消勾选的类型 */
  disabledTypes: string[];
  minDegree: number;
}

export const DEFAULT_GRAPH_FILTERS: GraphFiltersState = {
  hideIsolated: false,
  disabledTypes: [],
  minDegree: 0,
};

export interface TypeEntry {
  type: string;
  label: string;
  color: string;
  count: number;
}

export interface CommunityEntry {
  rank: number;
  color: string;
  size: number;
}

export function typeLabel(type: string): string {
  return GRAPH_TYPE_LABELS[type] ?? type;
}

// ---- 顶栏:搜索 + 过滤 + 着色切换 + 洞察 ------------------------------------

export function GraphToolbar({
  typeEntries,
  maxDegree,
  filters,
  onFiltersChange,
  colorMode,
  onColorModeChange,
  matchCount,
  onSearch,
  onOpenInsights,
}: {
  typeEntries: TypeEntry[];
  maxDegree: number;
  filters: GraphFiltersState;
  onFiltersChange: (next: GraphFiltersState) => void;
  colorMode: GraphColorMode;
  onColorModeChange: (mode: GraphColorMode) => void;
  /** 当前搜索命中数;null 表示无搜索 */
  matchCount: number | null;
  onSearch: (query: string) => void;
  onOpenInsights: () => void;
}) {
  const [input, setInput] = useState("");
  const composing = useRef(false);
  const [filterOpen, setFilterOpen] = useState(false);

  return (
    <div className="pointer-events-none absolute inset-x-3 top-3 z-10 flex flex-wrap items-start gap-2">
      <div className="pointer-events-auto relative w-60">
        <Search className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={input}
          placeholder="搜索节点名称"
          className="bg-background pl-8 shadow-sm"
          onChange={(e) => {
            setInput(e.target.value);
            // IME composition 保护:拼音组词期间不触发搜索
            if (!composing.current) onSearch(e.target.value);
          }}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={(e) => {
            composing.current = false;
            onSearch(e.currentTarget.value);
          }}
        />
        {input && (
          <button
            type="button"
            aria-label="清空搜索"
            className="absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            onClick={() => {
              setInput("");
              onSearch("");
            }}
          >
            <X className="size-3.5" />
          </button>
        )}
        {matchCount !== null && (
          <div className="absolute top-full mt-1 rounded-md border border-border bg-background px-2 py-0.5 text-xs text-muted-foreground shadow-sm">
            {matchCount > 0
              ? `命中 ${matchCount} 个节点,已聚焦首个`
              : "无匹配节点"}
          </div>
        )}
      </div>

      <div className="pointer-events-auto relative">
        <Button
          size="sm"
          variant="outline"
          className="bg-background shadow-sm"
          aria-expanded={filterOpen}
          onClick={() => setFilterOpen((v) => !v)}
        >
          <SlidersHorizontal className="size-3.5" />
          过滤
        </Button>
        {filterOpen && (
          <>
            <div
              className="fixed inset-0 z-10"
              onClick={() => setFilterOpen(false)}
            />
            <FilterPanel
              typeEntries={typeEntries}
              maxDegree={maxDegree}
              filters={filters}
              onFiltersChange={onFiltersChange}
            />
          </>
        )}
      </div>

      <div className="pointer-events-auto flex items-center gap-0.5 rounded-lg border border-border bg-background p-0.5 shadow-sm">
        <Palette className="mx-1 size-3.5 text-muted-foreground" />
        <Button
          size="xs"
          variant={colorMode === "type" ? "secondary" : "ghost"}
          onClick={() => onColorModeChange("type")}
        >
          按类型
        </Button>
        <Button
          size="xs"
          variant={colorMode === "community" ? "secondary" : "ghost"}
          onClick={() => onColorModeChange("community")}
        >
          按社区
        </Button>
      </div>

      <div className="pointer-events-auto ml-auto">
        <Button
          size="sm"
          variant="outline"
          className="bg-background shadow-sm"
          onClick={onOpenInsights}
        >
          <Lightbulb className="size-3.5" />
          洞察与建议
        </Button>
      </div>
    </div>
  );
}

function FilterPanel({
  typeEntries,
  maxDegree,
  filters,
  onFiltersChange,
}: {
  typeEntries: TypeEntry[];
  maxDegree: number;
  filters: GraphFiltersState;
  onFiltersChange: (next: GraphFiltersState) => void;
}) {
  // 滑块 180ms debounce:拖动流畅,过滤在停顿后生效
  const [degreeDraft, setDegreeDraft] = useState(filters.minDegree);
  useEffect(() => {
    if (degreeDraft === filters.minDegree) return;
    const timer = setTimeout(() => {
      onFiltersChange({ ...filters, minDegree: degreeDraft });
    }, 180);
    return () => clearTimeout(timer);
  }, [degreeDraft, filters, onFiltersChange]);

  const toggleType = (type: string, checked: boolean) => {
    const disabled = new Set(filters.disabledTypes);
    if (checked) disabled.delete(type);
    else disabled.add(type);
    onFiltersChange({ ...filters, disabledTypes: [...disabled] });
  };

  return (
    <div className="absolute top-full left-0 z-20 mt-1 w-64 space-y-3 rounded-md border border-border bg-popover p-3 text-sm shadow-md">
      <div className="space-y-1.5">
        <div className="text-xs font-medium text-muted-foreground">
          节点类型
        </div>
        {typeEntries.map((t) => (
          <Label
            key={t.type}
            className="flex cursor-pointer items-center gap-2 font-normal"
          >
            <Checkbox
              checked={!filters.disabledTypes.includes(t.type)}
              onCheckedChange={(checked) =>
                toggleType(t.type, checked === true)
              }
            />
            <span
              className="inline-block size-2.5 rounded-full"
              style={{ background: t.color }}
            />
            <span className="flex-1">{t.label}</span>
            <span className="text-xs text-muted-foreground">{t.count}</span>
          </Label>
        ))}
      </div>
      <Label className="flex cursor-pointer items-center gap-2 font-normal">
        <Checkbox
          checked={filters.hideIsolated}
          onCheckedChange={(checked) =>
            onFiltersChange({ ...filters, hideIsolated: checked === true })
          }
        />
        隐藏孤立节点
      </Label>
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>最小关联数</span>
          <span className="font-mono tabular-nums">{degreeDraft}</span>
        </div>
        <input
          type="range"
          min={0}
          max={Math.max(maxDegree, 1)}
          step={1}
          value={degreeDraft}
          onChange={(e) => setDegreeDraft(Number(e.target.value))}
          className="w-full accent-primary"
          aria-label="最小关联数"
        />
      </div>
    </div>
  );
}

// ---- Zoom 控制(右上) ------------------------------------------------------

export function ZoomControls({
  onZoomIn,
  onZoomOut,
  onReset,
  shifted = false,
}: {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
  /** 详情面板打开时左移,避免被面板(w-72,右贴边)遮住 */
  shifted?: boolean;
}) {
  return (
    <div
      className={cn(
        "absolute top-16 z-10 flex flex-col overflow-hidden rounded-lg border border-border bg-background shadow-sm transition-[right]",
        shifted ? "right-[19.5rem]" : "right-3",
      )}
    >
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="放大"
        onClick={onZoomIn}
      >
        <Plus className="size-3.5" />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="缩小"
        onClick={onZoomOut}
      >
        <Minus className="size-3.5" />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="重置视野"
        onClick={onReset}
      >
        <Maximize2 className="size-3.5" />
      </Button>
    </div>
  );
}

// ---- 图例(左下,可折叠) ----------------------------------------------------

export function GraphLegend({
  mode,
  typeEntries,
  communityEntries,
}: {
  mode: GraphColorMode;
  typeEntries: TypeEntry[];
  communityEntries: CommunityEntry[];
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="absolute bottom-3 left-3 z-10 max-w-56 rounded-md border border-border bg-background/95 text-xs shadow-sm">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-4 px-2.5 py-1.5 font-medium"
        onClick={() => setOpen((v) => !v)}
      >
        <span>图例 · {mode === "type" ? "类型" : "社区"}</span>
        {open ? (
          <ChevronDown className="size-3.5" />
        ) : (
          <ChevronUp className="size-3.5" />
        )}
      </button>
      {open && (
        <div className="max-h-48 space-y-1 overflow-y-auto px-2.5 pb-2">
          {mode === "type"
            ? typeEntries.map((t) => (
                <div key={t.type} className="flex items-center gap-1.5">
                  <span
                    className="inline-block size-2.5 shrink-0 rounded-full"
                    style={{ background: t.color }}
                  />
                  <span className="flex-1 truncate">{t.label}</span>
                  <span className="text-muted-foreground">{t.count}</span>
                </div>
              ))
            : communityEntries.map((c) => (
                <div key={c.rank} className="flex items-center gap-1.5">
                  <span
                    className="inline-block size-2.5 shrink-0 rounded-full"
                    style={{ background: c.color }}
                  />
                  <span className="flex-1">社区 {c.rank + 1}</span>
                  <span className="text-muted-foreground">{c.size} 节点</span>
                </div>
              ))}
        </div>
      )}
    </div>
  );
}

// ---- 节点详情(右侧滑出) ----------------------------------------------------

export interface RelatedItem {
  edgeId: string;
  id: string;
  label: string;
  entityType: string;
  weight: number;
  linkType: string;
  predicate: string;
  direction: string;
  outgoing: boolean;
  validFrom: string | null;
  validTo: string | null;
  evidence: GraphEdgeEvidence[];
  status: string;
  signals?: Record<string, number>;
}

export interface SelectedNodeInfo {
  id: string;
  label: string;
  entityType: string;
  degree: number;
  community: number | null;
  related: RelatedItem[];
}

export function NodeDetailPanel({
  info,
  palette,
  onClose,
  onFocusNode,
}: {
  info: SelectedNodeInfo;
  palette: SigmaGraphPalette;
  onClose: () => void;
  onFocusNode: (nodeId: string) => void;
}) {
  return (
    <div className="absolute top-16 right-3 bottom-3 z-10 flex w-72 flex-col overflow-hidden rounded-md border border-border bg-background/95 shadow-md">
      <div className="flex items-start justify-between gap-2 border-b border-border p-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block size-2.5 shrink-0 rounded-full"
              style={{
                background:
                  palette.types[info.entityType] ?? palette.fallbackType,
              }}
            />
            <span className="truncate text-sm font-medium">{info.label}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>{typeLabel(info.entityType)}</span>
            <span>关联数 {info.degree}</span>
            {info.community !== null && <span>社区 {info.community + 1}</span>}
          </div>
        </div>
        <Button
          size="icon-xs"
          variant="ghost"
          aria-label="关闭详情"
          onClick={onClose}
        >
          <X className="size-3.5" />
        </Button>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        <div className="text-xs font-medium text-muted-foreground">
          Top 关联节点(按边 weight)
        </div>
        {!info.related.length && (
          <p className="text-xs text-muted-foreground">该节点暂无关联边</p>
        )}
        {info.related.map((r) => (
          <button
            key={r.edgeId}
            type="button"
            className="block w-full rounded-md border border-border p-2 text-left text-xs transition-colors hover:bg-muted/50"
            onClick={() => onFocusNode(r.id)}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="flex min-w-0 items-center gap-1.5">
                <span
                  className="inline-block size-2 shrink-0 rounded-full"
                  style={{
                    background:
                      palette.types[r.entityType] ?? palette.fallbackType,
                  }}
                />
                <span className="truncate">{r.label}</span>
              </span>
              <span className="shrink-0 font-mono text-muted-foreground tabular-nums">
                {r.weight.toFixed(1)}
              </span>
            </div>
            <div className="mt-0.5 flex flex-wrap gap-x-2 text-muted-foreground">
              <span>
                {r.outgoing ? "→" : "←"} {r.predicate || r.linkType}
              </span>
              {r.direction !== "undirected" && <span>{r.direction}</span>}
              {r.status === "suggested" && (
                <span className="text-warning">待确认</span>
              )}
            </div>
            {graphValidityLabel(r.validFrom, r.validTo) && (
              <div className="mt-0.5 text-muted-foreground">
                有效期 {graphValidityLabel(r.validFrom, r.validTo)}
              </div>
            )}
            {r.evidence.length > 0 && (
              <div className="mt-1 space-y-1 border-t border-border/70 pt-1 text-muted-foreground">
                {r.evidence.slice(0, 2).map((evidence, index) => (
                  <div key={`${evidence.evidence_span_id ?? index}`}>
                    {graphEvidenceLocation(evidence)}
                    {evidence.quote_text && (
                      <span title={evidence.quote_text}>
                        {" "}
                        · {evidence.quote_text.slice(0, 160)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
            {r.signals && (
              <div className="mt-0.5 text-muted-foreground">
                {Object.entries(r.signals)
                  .filter(([, v]) => v > 0)
                  .map(([k, v]) => `${k} ${v.toFixed(1)}`)
                  .join(" · ")}
              </div>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---- 洞察抽屉:统计 / 社区 / 孤立节点 / 关系治理说明 ---------------------------

export function InsightsSheet({
  open,
  onOpenChange,
  insights,
  onFocusNode,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  insights: GraphInsights | undefined;
  onFocusNode: (nodeId: string) => void;
}) {
  const focus = (nodeId: string) => {
    onOpenChange(false);
    onFocusNode(nodeId);
  };

  const stats = [
    { label: "节点", value: insights?.node_count },
    { label: "关系边", value: insights?.edge_count },
    { label: "平均度", value: insights?.avg_degree },
    { label: "孤立节点", value: insights?.isolated_count, warn: true },
    { label: "社区数", value: insights?.communities.length },
    { label: "稀疏社区", value: insights?.sparse_community_count, warn: true },
  ];

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full gap-0 overflow-y-auto sm:max-w-md"
      >
        <SheetHeader>
          <SheetTitle>图谱洞察</SheetTitle>
          <SheetDescription>
            当前生效知识快照的关系结构、社区分布与证据治理状态。
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-6 p-4 pt-0">
          <div className="grid grid-cols-3 gap-2">
            {stats.map((s) => (
              <div
                key={s.label}
                className="rounded-md border border-border p-2"
              >
                <div className="text-xs text-muted-foreground">{s.label}</div>
                <div
                  className={cn(
                    "text-lg font-semibold tabular-nums",
                    s.warn &&
                      typeof s.value === "number" &&
                      s.value > 0 &&
                      "text-warning",
                  )}
                >
                  {s.value ?? "-"}
                </div>
              </div>
            ))}
          </div>

          <section className="space-y-2">
            <h3 className="text-xs font-medium text-muted-foreground">
              社区分布
            </h3>
            {!insights?.communities.length && (
              <p className="text-xs text-muted-foreground">
                暂无社区(图为空或全部孤立)
              </p>
            )}
            {insights?.communities.map((c, i) => (
              <div
                key={i}
                className="rounded-md border border-border p-2 text-sm"
              >
                <div className="flex items-center justify-between">
                  <span>
                    社区 {i + 1} · {c.size} 节点 · 密度 {c.density}
                  </span>
                  {c.sparse && <ToneBadge tone="warning">稀疏</ToneBadge>}
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {c.core_nodes.map((n) => (
                    <button
                      key={n.id}
                      type="button"
                      className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
                      onClick={() => focus(n.id)}
                    >
                      <Locate className="size-3" />
                      {n.name}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-medium text-muted-foreground">
              孤立节点(未连接任何知识)
            </h3>
            {!insights?.isolated_nodes.length && (
              <p className="text-xs text-muted-foreground">没有孤立节点</p>
            )}
            <div className="flex flex-wrap gap-1">
              {insights?.isolated_nodes.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
                  onClick={() => focus(n.id)}
                >
                  <Locate className="size-3" />
                  {n.name}
                </button>
              ))}
            </div>
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-medium text-muted-foreground">
              关系如何生效
            </h3>
            <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
              <p>
                图谱页面不再直接生成或确认关系。每条关系必须先作为带
                EvidenceSpan 原文定位的 FactClaim 进入审核流。
              </p>
              <p>
                人工确认后，关系仍只属于待发布事实；构建并激活新的知识快照后，
                系统才会把它投影为当前图谱中的类型化关系边。
              </p>
            </div>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}
