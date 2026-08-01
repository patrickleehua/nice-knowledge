"use client";

/**
 * sigma.js 图谱视图主体(仅客户端,经 kb-graph-view.tsx 的 next/dynamic ssr:false 加载)。
 * 职责:拉取 graph / insights → graphology 构图 → ForceAtlas2 布局(大图走 worker)
 * → sigma 渲染 + 悬停高亮 / 点选详情 / 搜索聚焦 / 过滤 / 类型与社区双着色;
 * 并处理主题切换重建 settings、工作台拖拽面板期间卸载重挂(防 WebGL 崩溃)。
 */

import { SigmaContainer, useSigma } from "@react-sigma/core";
import "@react-sigma/core/lib/style.css";
import { useQuery } from "@tanstack/react-query";
import louvain from "graphology-communities-louvain";
import { Share2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Sigma } from "sigma";
import { drawDiscNodeLabel } from "sigma/rendering";
import type { Settings } from "sigma/settings";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";
import { EmptyState, Spinner } from "@/components/shared";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { GraphData, GraphInsights } from "@/lib/types";
import {
  buildKbGraph,
  hashGraphData,
  type KbEdgeAttributes,
  type KbGraph,
  type KbNodeAttributes,
} from "./graph-build";
import {
  readSigmaPalette,
  SIGMA_PALETTE_FALLBACK,
  withAlpha,
  type SigmaGraphPalette,
} from "./graph-palette";
import { applyLayout, LAYOUT_WORKER_THRESHOLD } from "./layout";
import {
  DEFAULT_GRAPH_FILTERS,
  GraphLegend,
  GraphToolbar,
  InsightsSheet,
  NodeDetailPanel,
  typeLabel,
  ZoomControls,
  type CommunityEntry,
  type GraphColorMode,
  type GraphFiltersState,
  type SelectedNodeInfo,
  type TypeEntry,
} from "./panels";

/** /kb/bases/{id}/related 响应项(四信号拆分) */
interface RelatedHit {
  node: { id: string; type: string; name: string };
  score: number;
  signals: Record<string, number>;
}

/** 配色随设计 token 走:首帧亮色兜底,挂载后读实际值并跟随暗色切换(同 graph-colors 的做法) */
function useSigmaPalette(): SigmaGraphPalette {
  const [palette, setPalette] = useState<SigmaGraphPalette>(
    SIGMA_PALETTE_FALLBACK,
  );
  useEffect(() => {
    const update = () => setPalette(readSigmaPalette());
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    });
    return () => observer.disconnect();
  }, []);
  return palette;
}

/** 确定性伪随机(louvain 用),同数据社区划分稳定 */
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 把 SigmaContainer 内的 sigma 实例提升到父组件 state(实例随 graph/settings 变化重建) */
function SigmaBridge({ onReady }: { onReady: (sigma: Sigma | null) => void }) {
  const sigma = useSigma();
  useEffect(() => {
    onReady(sigma as unknown as Sigma);
    return () => onReady(null);
  }, [sigma, onReady]);
  return null;
}

export default function KbGraphViewImpl({ kbId }: { kbId: string }) {
  const palette = useSigmaPalette();

  const { data, isLoading } = useQuery({
    queryKey: ["kb-graph", kbId],
    queryFn: () => api.get<GraphData>(`/kb/bases/${kbId}/graph`),
  });
  const { data: insights } = useQuery({
    queryKey: ["kb-insights", kbId],
    queryFn: () => api.get<GraphInsights>(`/kb/bases/${kbId}/insights`),
  });

  // ---- 构图与布局 ------------------------------------------------------------
  const { graph, hash } = useMemo(() => {
    const g = buildKbGraph(data ?? { nodes: [], edges: [] });
    return { graph: g, hash: data ? hashGraphData(data) : "" };
  }, [data]);

  const [layoutInfo, setLayoutInfo] = useState<{
    hash: string;
    usedWorker: boolean;
  } | null>(null);
  useEffect(() => {
    if (!graph.order || !hash) return;
    let cancelled = false;
    void applyLayout(graph, hash).then((result) => {
      if (!cancelled) setLayoutInfo({ hash, usedWorker: result.usedWorker });
    });
    return () => {
      cancelled = true;
    };
  }, [graph, hash]);
  const layoutPending = graph.order > 0 && layoutInfo?.hash !== hash;

  // ---- 社区划分(insights 不含逐节点归属,以边 weight 为权本地 Louvain) --------
  const communityMap = useMemo(() => {
    const map = new Map<string, number>();
    if (!graph.order) return map;
    if (!graph.size) {
      graph.forEachNode((node) => map.set(node, 0));
      return map;
    }
    const raw = louvain(graph, {
      getEdgeWeight: "weight",
      rng: mulberry32(42),
    });
    // 按社区规模重排编号:最大社区固定取 0 号色,着色稳定
    const sizes = new Map<number, number>();
    for (const community of Object.values(raw)) {
      sizes.set(community, (sizes.get(community) ?? 0) + 1);
    }
    const rank = new Map(
      [...sizes.entries()].sort((a, b) => b[1] - a[1]).map(([c], i) => [c, i]),
    );
    for (const [node, community] of Object.entries(raw)) {
      map.set(node, rank.get(community) ?? 0);
    }
    return map;
  }, [graph]);

  // ---- 视图状态 --------------------------------------------------------------
  const [colorMode, setColorMode] = useState<GraphColorMode>("type");
  const [filters, setFilters] = useState<GraphFiltersState>(
    DEFAULT_GRAPH_FILTERS,
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [insightsOpen, setInsightsOpen] = useState(false);
  const [sigma, setSigma] = useState<Sigma | null>(null);
  const [suspended, setSuspended] = useState(false);
  const [mountKey, setMountKey] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);

  // ---- 节点着色(图属性原地更新,graphology 事件驱动 sigma 重绘) --------------
  useEffect(() => {
    graph.updateEachNodeAttributes((node, attrs) => ({
      ...attrs,
      color:
        colorMode === "community"
          ? palette.communities[
              (communityMap.get(node) ?? 0) % palette.communities.length
            ]
          : (palette.types[attrs.entityType] ?? palette.fallbackType),
    }));
    graph.updateEachEdgeAttributes((_edge, attrs) => ({
      ...attrs,
      color:
        attrs.status === "suggested"
          ? palette.edgeSuggested
          : attrs.normWeight >= 0.55
            ? palette.edgeStrong
            : palette.edgeWeak,
    }));
  }, [graph, palette, colorMode, communityMap]);

  // ---- 派生集合:过滤隐藏 / 搜索命中 / 悬停邻域 --------------------------------
  const hiddenNodes = useMemo(() => {
    const disabled = new Set(filters.disabledTypes);
    const hidden = new Set<string>();
    graph.forEachNode((node, attrs) => {
      if (
        disabled.has(attrs.entityType) ||
        attrs.degree < filters.minDegree ||
        (filters.hideIsolated && attrs.degree === 0)
      ) {
        hidden.add(node);
      }
    });
    return hidden;
  }, [graph, filters]);

  const searchMatches = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return null;
    const matches = new Set<string>();
    graph.forEachNode((node, attrs) => {
      if (attrs.label.toLowerCase().includes(q)) matches.add(node);
    });
    return matches;
  }, [graph, searchQuery]);

  const neighborhood = useMemo(() => {
    if (!hovered || !graph.hasNode(hovered)) return null;
    return new Set([hovered, ...graph.neighbors(hovered)]);
  }, [graph, hovered]);

  // ---- sigma settings(主题切换 → palette 变化 → 实例携新 settings 重建) ------
  const settings = useMemo<Partial<Settings>>(
    () => ({
      allowInvalidContainer: true,
      renderLabels: true,
      labelFont: "system-ui, -apple-system, sans-serif",
      labelSize: 11,
      labelWeight: "500",
      labelColor: { color: palette.label },
      labelRenderedSizeThreshold: 7,
      defaultNodeColor: palette.fallbackType,
      defaultEdgeColor: palette.edgeWeak,
      zIndex: true,
      minCameraRatio: 0.03,
      maxCameraRatio: 20,
      stagePadding: 48,
      // 悬停 label 底板用主题背景色(sigma 默认硬编码白色,暗色下刺眼)
      defaultDrawNodeHover: (context, drawData, drawSettings) => {
        const size = drawSettings.labelSize;
        context.font = `${drawSettings.labelWeight} ${size}px ${drawSettings.labelFont}`;
        context.fillStyle = palette.background;
        context.shadowOffsetX = 0;
        context.shadowOffsetY = 0;
        context.shadowBlur = 8;
        context.shadowColor = withAlpha(palette.label, 0.3);
        const padding = 2;
        if (typeof drawData.label === "string") {
          const textWidth = context.measureText(drawData.label).width;
          const boxWidth = Math.round(textWidth + 5);
          const boxHeight = Math.round(size + 2 * padding);
          const radius = Math.max(drawData.size, size / 2) + padding;
          const angleRadian = Math.asin(boxHeight / 2 / radius);
          const xDelta = Math.sqrt(
            Math.abs(radius ** 2 - (boxHeight / 2) ** 2),
          );
          context.beginPath();
          context.moveTo(drawData.x + xDelta, drawData.y + boxHeight / 2);
          context.lineTo(
            drawData.x + radius + boxWidth,
            drawData.y + boxHeight / 2,
          );
          context.lineTo(
            drawData.x + radius + boxWidth,
            drawData.y - boxHeight / 2,
          );
          context.lineTo(drawData.x + xDelta, drawData.y - boxHeight / 2);
          context.arc(
            drawData.x,
            drawData.y,
            radius,
            angleRadian,
            -angleRadian,
          );
          context.closePath();
          context.fill();
        } else {
          context.beginPath();
          context.arc(
            drawData.x,
            drawData.y,
            drawData.size + padding,
            0,
            Math.PI * 2,
          );
          context.closePath();
          context.fill();
        }
        context.shadowOffsetX = 0;
        context.shadowOffsetY = 0;
        context.shadowBlur = 0;
        drawDiscNodeLabel(context, drawData, drawSettings);
      },
    }),
    [palette],
  );

  // ---- 事件:悬停高亮 / 点选详情 / 画布空白关闭 --------------------------------
  useEffect(() => {
    if (!sigma) return;
    const enterNode = ({ node }: { node: string }) => setHovered(node);
    const leaveNode = () => setHovered(null);
    const clickNode = ({ node }: { node: string }) => setSelected(node);
    const clickStage = () => setSelected(null);
    sigma.on("enterNode", enterNode);
    sigma.on("leaveNode", leaveNode);
    sigma.on("clickNode", clickNode);
    sigma.on("clickStage", clickStage);
    return () => {
      sigma.off("enterNode", enterNode);
      sigma.off("leaveNode", leaveNode);
      sigma.off("clickNode", clickNode);
      sigma.off("clickStage", clickStage);
    };
  }, [sigma]);

  // ---- reducer:过滤隐藏 → 悬停弱化非邻居 → 搜索命中高亮 -----------------------
  useEffect(() => {
    if (!sigma) return;
    sigma.setSettings({
      nodeReducer: (node, attrs) => {
        const baseSize = (attrs.size as number | undefined) ?? 4;
        const res: Partial<NodeDisplayData> = { ...attrs };
        if (hiddenNodes.has(node)) {
          res.hidden = true;
          return res;
        }
        if (neighborhood && !neighborhood.has(node)) {
          res.color = palette.dimNode;
          res.size = baseSize * 0.75;
          res.label = "";
          res.zIndex = 0;
          return res;
        }
        if (searchMatches && !searchMatches.has(node)) {
          res.color = palette.dimNode;
          res.label = "";
        }
        if (neighborhood?.has(node)) {
          res.zIndex = 2;
          if (node === hovered) res.size = baseSize * 1.15;
        }
        if (searchMatches?.has(node) || node === selected) {
          res.highlighted = true;
          res.zIndex = 2;
        }
        return res;
      },
      edgeReducer: (edge, attrs) => {
        const baseSize = (attrs.size as number | undefined) ?? 1;
        const res: Partial<EdgeDisplayData> = { ...attrs };
        const [src, dst] = sigma.getGraph().extremities(edge);
        if (hiddenNodes.has(src) || hiddenNodes.has(dst)) {
          res.hidden = true;
          return res;
        }
        if (neighborhood) {
          if (src === hovered || dst === hovered) {
            res.size = baseSize * 1.6;
            res.color = palette.edgeStrong;
            res.zIndex = 1;
          } else {
            res.color = palette.dimEdge;
          }
        }
        if (
          searchMatches &&
          !(searchMatches.has(src) && searchMatches.has(dst))
        ) {
          res.color = palette.dimEdge;
        }
        return res;
      },
    });
    sigma.refresh();
  }, [
    sigma,
    hiddenNodes,
    neighborhood,
    searchMatches,
    selected,
    hovered,
    palette,
  ]);

  // ---- 镜头 ------------------------------------------------------------------
  const focusNode = useCallback(
    (nodeId: string) => {
      if (!sigma || !sigma.getGraph().hasNode(nodeId)) return;
      const displayData = sigma.getNodeDisplayData(nodeId);
      if (!displayData) return;
      const camera = sigma.getCamera();
      void camera.animate(
        {
          x: displayData.x,
          y: displayData.y,
          ratio: Math.min(camera.ratio, 0.35),
        },
        { duration: 450 },
      );
      setSelected(nodeId);
    },
    [sigma],
  );

  // 搜索命中后镜头聚焦首个匹配(350ms 防抖,连续输入不来回飞)
  useEffect(() => {
    if (!sigma || !searchMatches?.size) return;
    const first = searchMatches.values().next().value;
    if (!first) return;
    const timer = setTimeout(() => {
      const displayData = sigma.getNodeDisplayData(first);
      if (!displayData) return;
      const camera = sigma.getCamera();
      void camera.animate(
        {
          x: displayData.x,
          y: displayData.y,
          ratio: Math.min(camera.ratio, 0.35),
        },
        { duration: 450 },
      );
    }, 350);
    return () => clearTimeout(timer);
  }, [sigma, searchMatches]);

  // ---- 防崩溃:工作台拖拽面板 / 容器尺寸剧变期间卸载 sigma,结束后重挂 ----------
  useEffect(() => {
    const body = document.body;
    let panelResizing = false;
    let resizeTimer: number | null = null;
    let firstObserve = true;

    const suspend = () => setSuspended(true);
    const resume = () => {
      setSuspended(false);
      setMountKey((k) => k + 1);
    };

    const mutationObserver = new MutationObserver(() => {
      const active =
        body.dataset.panelResizing !== undefined &&
        body.dataset.panelResizing !== "false";
      if (active && !panelResizing) {
        panelResizing = true;
        suspend();
      } else if (!active && panelResizing) {
        panelResizing = false;
        resume();
      }
    });
    mutationObserver.observe(body, {
      attributes: true,
      attributeFilter: ["data-panel-resizing"],
    });

    const resizeObserver = new ResizeObserver(() => {
      if (firstObserve) {
        firstObserve = false;
        return;
      }
      if (panelResizing) return; // 面板拖拽由 dataset 流程接管
      suspend();
      if (resizeTimer !== null) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(resume, 280);
    });
    if (wrapRef.current) resizeObserver.observe(wrapRef.current);

    return () => {
      mutationObserver.disconnect();
      resizeObserver.disconnect();
      if (resizeTimer !== null) window.clearTimeout(resizeTimer);
    };
  }, []);

  // ---- 详情面板数据 ------------------------------------------------------------
  const { data: relatedHits } = useQuery({
    queryKey: ["kb-related", kbId, selected],
    enabled: selected !== null && selected.includes(":"),
    queryFn: () => {
      const [type, ...rest] = selected!.split(":");
      return api.get<RelatedHit[]>(
        `/kb/bases/${kbId}/related/${type}/${rest.join(":")}?top_k=12`,
      );
    },
  });

  const selectedInfo = useMemo<SelectedNodeInfo | null>(() => {
    if (!selected || !graph.hasNode(selected)) return null;
    const attrs = graph.getNodeAttributes(selected);
    const signalsByNode = new Map(
      (relatedHits ?? []).map((hit) => [hit.node.id, hit.signals]),
    );
    const related: SelectedNodeInfo["related"] = [];
    graph.forEachEdge(selected, (edge, edgeAttrs, src, dst) => {
      const other = src === selected ? dst : src;
      const otherAttrs = graph.getNodeAttributes(other);
      related.push({
        edgeId: edge,
        id: other,
        label: otherAttrs.label,
        entityType: otherAttrs.entityType,
        weight: edgeAttrs.weight,
        linkType: edgeAttrs.linkType,
        predicate: edgeAttrs.predicate,
        direction: edgeAttrs.direction,
        outgoing: edgeAttrs.source === selected,
        validFrom: edgeAttrs.validFrom,
        validTo: edgeAttrs.validTo,
        evidence: edgeAttrs.evidence,
        status: edgeAttrs.status,
        signals: signalsByNode.get(other),
      });
    });
    related.sort((a, b) => b.weight - a.weight);
    return {
      id: selected,
      label: attrs.label,
      entityType: attrs.entityType,
      degree: attrs.degree,
      community: communityMap.get(selected) ?? null,
      related: related.slice(0, 12),
    };
  }, [graph, selected, communityMap, relatedHits]);

  // ---- 图例 / 过滤所需的聚合 ---------------------------------------------------
  const typeEntries = useMemo<TypeEntry[]>(() => {
    const counts = new Map<string, number>();
    graph.forEachNode((_node, attrs) => {
      counts.set(attrs.entityType, (counts.get(attrs.entityType) ?? 0) + 1);
    });
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([type, count]) => ({
        type,
        label: typeLabel(type),
        color: palette.types[type] ?? palette.fallbackType,
        count,
      }));
  }, [graph, palette]);

  const communityEntries = useMemo<CommunityEntry[]>(() => {
    const sizes = new Map<number, number>();
    for (const rankId of communityMap.values()) {
      sizes.set(rankId, (sizes.get(rankId) ?? 0) + 1);
    }
    return [...sizes.entries()]
      .sort((a, b) => a[0] - b[0])
      .slice(0, 12)
      .map(([rank, size]) => ({
        rank,
        size,
        color: palette.communities[rank % palette.communities.length],
      }));
  }, [communityMap, palette]);

  const maxDegree = useMemo(() => {
    let max = 0;
    graph.forEachNode((_node, attrs) => {
      if (attrs.degree > max) max = attrs.degree;
    });
    return max;
  }, [graph]);

  // ---- 渲染 --------------------------------------------------------------------
  if (isLoading) {
    return <Skeleton className="h-full min-h-96 w-full rounded-md" />;
  }
  if (!graph.order) {
    return (
      <div className="flex h-full min-h-96 items-center justify-center rounded-md border border-border bg-muted/30">
        <EmptyState
          icon={Share2}
          title="图谱为空"
          description="确认入库一些实体后,节点与关联边会出现在这里。"
        />
      </div>
    );
  }

  return (
    <div
      ref={wrapRef}
      className="relative h-full min-h-96 w-full overflow-hidden rounded-md border border-border bg-muted/30"
    >
      {suspended ? (
        <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
          <Spinner size={4} />
          调整布局中,松开后恢复图谱
        </div>
      ) : (
        <SigmaContainer<KbNodeAttributes, KbEdgeAttributes>
          key={mountKey}
          graph={graph as KbGraph}
          settings={
            settings as Partial<Settings<KbNodeAttributes, KbEdgeAttributes>>
          }
          className="!bg-transparent"
          style={{ height: "100%", width: "100%" }}
        >
          <SigmaBridge onReady={setSigma} />
        </SigmaContainer>
      )}

      <GraphToolbar
        typeEntries={typeEntries}
        maxDegree={maxDegree}
        filters={filters}
        onFiltersChange={setFilters}
        colorMode={colorMode}
        onColorModeChange={setColorMode}
        matchCount={searchMatches ? searchMatches.size : null}
        onSearch={setSearchQuery}
        onOpenInsights={() => setInsightsOpen(true)}
      />
      <ZoomControls
        shifted={selectedInfo !== null}
        onZoomIn={() => sigma?.getCamera().animatedZoom({ duration: 200 })}
        onZoomOut={() => sigma?.getCamera().animatedUnzoom({ duration: 200 })}
        onReset={() => void sigma?.getCamera().animatedReset({ duration: 300 })}
      />
      <GraphLegend
        mode={colorMode}
        typeEntries={typeEntries}
        communityEntries={communityEntries}
      />
      {layoutPending && graph.order >= LAYOUT_WORKER_THRESHOLD && (
        <div className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded-md border border-border bg-background/95 px-2.5 py-1 text-xs text-muted-foreground shadow-sm">
          <Spinner size={3.5} />
          正在后台计算布局({graph.order} 节点)
        </div>
      )}
      {selectedInfo && (
        <NodeDetailPanel
          info={selectedInfo}
          palette={palette}
          onClose={() => setSelected(null)}
          onFocusNode={focusNode}
        />
      )}
      <InsightsSheet
        open={insightsOpen}
        onOpenChange={setInsightsOpen}
        insights={insights}
        onFocusNode={focusNode}
      />
    </div>
  );
}
