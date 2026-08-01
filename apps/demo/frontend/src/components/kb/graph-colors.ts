/**
 * 图谱画布配色:从 CSS 自定义属性(设计 token)运行时解析出真实色值字符串。
 * SVG 的 fill/stroke 需要真实颜色,不能直接用 var();故用 getComputedStyle 读取,
 * 并在暗色切换时经 MutationObserver 重读(见 graph-canvas 的 useGraphPalette)。
 * SSR 与首帧一律用亮色兜底(与 :root token 同值),避免水合不一致;挂载后再校正。
 *
 * SDK 化改造(MIGRATION-PLAN §5.8):节点类型是**自由字符串**(entity_type_key),
 * 不可能预先给每个类型配一个颜色。因此内置类型(page/document)固定取色,
 * 其余类型按 type_key 做稳定哈希落到色板轮换上 —— 同一个类型每次都得到同一种
 * 颜色,新类型也不会渲染成"全都灰"。
 */

/** 内置节点类型 → 设计 token */
export const GRAPH_TYPE_TOKENS: Record<string, string> = {
  page: "--chart-5",
  document: "--muted-foreground",
};

/** 自由类型的色板轮换(按 type_key 稳定哈希取模) */
export const GRAPH_TYPE_TOKEN_CYCLE = [
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--teal",
] as const;

const TOKEN_CYCLE_FALLBACK: Record<string, string> = {
  "--chart-1": "#1A4D8F",
  "--chart-2": "#26A69A",
  "--chart-3": "#E6A700",
  "--chart-4": "#1E8E5A",
  "--teal": "#26A69A",
};

/** djb2 稳定哈希:同一个 type_key 永远落到同一个色位(不依赖出现顺序)。 */
function stableIndex(key: string, size: number): number {
  let hash = 5381;
  for (let i = 0; i < key.length; i += 1) {
    hash = ((hash << 5) + hash + key.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % size;
}

/** 该节点类型应使用的设计 token。 */
export function graphTypeToken(type: string): string {
  return (
    GRAPH_TYPE_TOKENS[type] ??
    GRAPH_TYPE_TOKEN_CYCLE[stableIndex(type, GRAPH_TYPE_TOKEN_CYCLE.length)]
  );
}

export interface GraphPalette {
  types: Record<string, string>;
  /** 轮换色板(token → 当前主题色值),供自由类型按哈希取色 */
  cycle: Record<string, string>;
  fallbackType: string;
  linkSuggested: string;
  linkNormal: string;
  nodeStrokeActive: string;
  nodeStrokeIdle: string;
}

/** 亮色兜底,取自 app/globals.css 的 :root(SSR / 首帧用,保证初始渲染正确) */
export const GRAPH_PALETTE_FALLBACK: GraphPalette = {
  types: {
    page: "#64748B",
    document: "#64748B",
  },
  cycle: TOKEN_CYCLE_FALLBACK,
  fallbackType: "#64748B",
  linkSuggested: "#E6A700",
  linkNormal: "#64748B",
  nodeStrokeActive: "#1A1F2C",
  nodeStrokeIdle: "#F6F7FA",
};

/** 运行时读取当前主题下的图谱配色;非浏览器环境回退亮色兜底 */
/** 任意节点类型的色值(内置类型直取,其余按稳定哈希轮换取色)。 */
export function graphTypeColor(palette: GraphPalette, type: string): string {
  const known = palette.types[type];
  if (known) return known;
  const token = graphTypeToken(type);
  return palette.cycle[token] ?? palette.fallbackType;
}

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
  const cycle: Record<string, string> = {};
  for (const token of GRAPH_TYPE_TOKEN_CYCLE) {
    cycle[token] = read(token, TOKEN_CYCLE_FALLBACK[token]);
  }
  return {
    types,
    cycle,
    fallbackType: read("--muted-foreground", GRAPH_PALETTE_FALLBACK.fallbackType),
    linkSuggested: read("--warning", GRAPH_PALETTE_FALLBACK.linkSuggested),
    linkNormal: read("--muted-foreground", GRAPH_PALETTE_FALLBACK.linkNormal),
    nodeStrokeActive: read("--foreground", GRAPH_PALETTE_FALLBACK.nodeStrokeActive),
    nodeStrokeIdle: read("--background", GRAPH_PALETTE_FALLBACK.nodeStrokeIdle),
  };
}
