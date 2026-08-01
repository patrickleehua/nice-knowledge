/**
 * sigma 图谱配色:沿用 graph-colors.ts 的"运行时读设计 token"语义并扩展。
 * sigma 画布(WebGL)只认真实色值,故从 CSS 自定义属性解析;
 * SSR / 首帧用亮色兜底(与 :root token 同值),挂载后经 MutationObserver 跟随主题重读。
 * 画布内衍生色(document 类型、社区轮换第二圈、弱化态)由 token 混合计算得出,不引入 token 外的硬编码色。
 */

import {
  GRAPH_PALETTE_FALLBACK,
  GRAPH_TYPE_TOKENS,
} from "@/components/kb/graph-colors";

/** 节点类型中文标签(在 graph-canvas 基础上补 route / document) */
export const GRAPH_TYPE_LABELS: Record<string, string> = {
  destination: "目的地",
  hotel: "酒店",
  cost: "成本",
  poi: "景点",
  route_template: "线路",
  route: "线路",
  page: "Wiki 页",
  document: "文档",
};

/** 社区着色的 token 轮换序列(第二圈起向背景混合做浅色变体) */
const COMMUNITY_TOKENS = [
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--teal",
  "--success",
  "--destructive",
  "--chart-5",
];

/** 兜底 token 值,取自 app/globals.css 的 :root(亮色),仅供 SSR / 首帧 */
const TOKEN_FALLBACK: Record<string, string> = {
  "--background": "#F6F7FA",
  "--foreground": "#1A1F2C",
  "--muted": "#EEF2F7",
  "--muted-foreground": "#64748B",
  "--primary": "#1A4D8F",
  "--warning": "#E6A700",
  "--success": "#1E8E5A",
  "--destructive": "#D32F2F",
  "--teal": "#26A69A",
  "--chart-1": "#1A4D8F",
  "--chart-2": "#26A69A",
  "--chart-3": "#E6A700",
  "--chart-4": "#1E8E5A",
  "--chart-5": "#64748B",
};

export interface SigmaGraphPalette {
  /** 节点类型 → 实色 */
  types: Record<string, string>;
  fallbackType: string;
  /** 社区着色轮换色板(16 色,超出取模) */
  communities: string[];
  /** 强关联边(weight 高) */
  edgeStrong: string;
  /** 弱关联边 */
  edgeWeak: string;
  /** LLM 建议边(待确认) */
  edgeSuggested: string;
  /** 悬停时非邻居节点的弱化色(muted token) */
  dimNode: string;
  /** 悬停时无关边的弱化色 */
  dimEdge: string;
  /** 节点 label 颜色 */
  label: string;
  /** 搜索命中 / 选中节点的强调描边色 */
  highlight: string;
  background: string;
}

function clamp255(v: number): number {
  return Math.max(0, Math.min(255, Math.round(v)));
}

function parseHex(color: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return null;
  const v = parseInt(m[1], 16);
  return [(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff];
}

/** 两个 hex 色按 t(0..1)线性混合;解析失败时原样返回 a */
export function mixHex(a: string, b: string, t: number): string {
  const ca = parseHex(a);
  const cb = parseHex(b);
  if (!ca || !cb) return a;
  const c = ca.map((v, i) => clamp255(v + (cb[i] - v) * t));
  return `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** hex → rgba() 字符串(sigma 可解析);解析失败原样返回 */
export function withAlpha(color: string, alpha: number): string {
  const c = parseHex(color);
  if (!c) return color;
  return `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${alpha})`;
}

function buildPalette(read: (token: string, fallback: string) => string): SigmaGraphPalette {
  const readToken = (token: string) =>
    read(token, TOKEN_FALLBACK[token] ?? GRAPH_PALETTE_FALLBACK.fallbackType);

  const background = readToken("--background");
  const foreground = readToken("--foreground");
  const muted = readToken("--muted");
  const mutedForeground = readToken("--muted-foreground");
  const primary = readToken("--primary");
  const warning = readToken("--warning");

  const types: Record<string, string> = {};
  for (const [type, token] of Object.entries(GRAPH_TYPE_TOKENS)) {
    types[type] = read(token, GRAPH_PALETTE_FALLBACK.types[type] ?? mutedForeground);
  }
  // 扩展类型:route 与 route_template 同义;document 由 chart-5 向前景混合衍生(区别于 page)
  types.route = types.route_template;
  types.document = mixHex(types.page ?? mutedForeground, foreground, 0.45);

  const baseCommunity = COMMUNITY_TOKENS.map(readToken);
  const communities = [
    ...baseCommunity,
    ...baseCommunity.map((c) => mixHex(c, background, 0.45)),
  ];

  return {
    types,
    fallbackType: mutedForeground,
    communities,
    edgeStrong: withAlpha(primary, 0.75),
    edgeWeak: withAlpha(mutedForeground, 0.28),
    edgeSuggested: withAlpha(warning, 0.85),
    dimNode: muted,
    dimEdge: withAlpha(mutedForeground, 0.08),
    label: foreground,
    highlight: primary,
    background,
  };
}

/** 亮色兜底(SSR / 首帧),与 :root token 同值 */
export const SIGMA_PALETTE_FALLBACK: SigmaGraphPalette = buildPalette(
  (_token, fallback) => fallback,
);

/** 运行时按当前主题读取图谱配色 */
export function readSigmaPalette(): SigmaGraphPalette {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return SIGMA_PALETTE_FALLBACK;
  }
  const style = getComputedStyle(document.documentElement);
  return buildPalette(
    (token, fallback) => style.getPropertyValue(token).trim() || fallback,
  );
}
