/**
 * 图谱画布配色:从 CSS 自定义属性(设计 token)运行时解析出真实色值字符串。
 * SVG 的 fill/stroke 需要真实颜色,不能直接用 var();故用 getComputedStyle 读取,
 * 并在暗色切换时经 MutationObserver 重读(见 graph-canvas 的 useGraphPalette)。
 * SSR 与首帧一律用亮色兜底(与 :root token 同值),避免水合不一致;挂载后再校正。
 */

/** 节点类型 → 设计 token(色板外的紫色 route_template 归入 --teal 辅色) */
export const GRAPH_TYPE_TOKENS: Record<string, string> = {
  destination: "--chart-1",
  hotel: "--chart-2",
  cost: "--chart-3",
  poi: "--chart-4",
  page: "--chart-5",
  route_template: "--teal",
};

export interface GraphPalette {
  types: Record<string, string>;
  fallbackType: string;
  linkSuggested: string;
  linkNormal: string;
  nodeStrokeActive: string;
  nodeStrokeIdle: string;
}

/** 亮色兜底,取自 app/globals.css 的 :root(SSR / 首帧用,保证初始渲染正确) */
export const GRAPH_PALETTE_FALLBACK: GraphPalette = {
  types: {
    destination: "#1A4D8F",
    hotel: "#26A69A",
    cost: "#E6A700",
    poi: "#1E8E5A",
    page: "#64748B",
    route_template: "#26A69A",
  },
  fallbackType: "#64748B",
  linkSuggested: "#E6A700",
  linkNormal: "#64748B",
  nodeStrokeActive: "#1A1F2C",
  nodeStrokeIdle: "#F6F7FA",
};

/** 运行时读取当前主题下的图谱配色;非浏览器环境回退亮色兜底 */
export function readGraphPalette(): GraphPalette {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return GRAPH_PALETTE_FALLBACK;
  }
  const style = getComputedStyle(document.documentElement);
  const read = (token: string, fallback: string) =>
    style.getPropertyValue(token).trim() || fallback;

  const types: Record<string, string> = {};
  for (const [type, token] of Object.entries(GRAPH_TYPE_TOKENS)) {
    types[type] = read(token, GRAPH_PALETTE_FALLBACK.types[type]);
  }
  return {
    types,
    fallbackType: read("--muted-foreground", GRAPH_PALETTE_FALLBACK.fallbackType),
    linkSuggested: read("--warning", GRAPH_PALETTE_FALLBACK.linkSuggested),
    linkNormal: read("--muted-foreground", GRAPH_PALETTE_FALLBACK.linkNormal),
    nodeStrokeActive: read("--foreground", GRAPH_PALETTE_FALLBACK.nodeStrokeActive),
    nodeStrokeIdle: read("--background", GRAPH_PALETTE_FALLBACK.nodeStrokeIdle),
  };
}
