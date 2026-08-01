/**
 * ForceAtlas2 布局:inferSettings 起步 + 项目规格覆盖,迭代次数按节点数分档;
 * - 结果按数据哈希做模块级缓存(同数据不重算,含 worker 路径);
 * - 节点数 >= 220 时优先走 Web Worker(layout-worker.ts),失败 / 超时回退主线程同步计算。
 */

import forceAtlas2, {
  type ForceAtlas2Settings,
} from "graphology-layout-forceatlas2";
import type { KbGraph } from "./graph-build";

export type LayoutPositions = Record<string, { x: number; y: number }>;

export interface LayoutRequest {
  nodes: { id: string; x: number; y: number }[];
  edges: [string, string, number][];
  iterations: number;
  settings: ForceAtlas2Settings;
}

/** 触发 worker 布局的节点数阈值 */
export const LAYOUT_WORKER_THRESHOLD = 220;
const WORKER_TIMEOUT_MS = 20_000;
/** 布局基础间距系数(乘入 scalingRatio) */
const SPACING = 4;

const layoutCache = new Map<string, LayoutPositions>();

/** 迭代次数分档:小图收敛更充分,大图控制耗时 */
export function layoutIterations(order: number): number {
  if (order <= 60) return 140;
  if (order <= 150) return 100;
  if (order <= 400) return 60;
  return 28;
}

export function buildLayoutSettings(graph: KbGraph): ForceAtlas2Settings {
  const n = graph.order;
  return {
    ...forceAtlas2.inferSettings(graph),
    gravity: 1,
    strongGravityMode: true,
    barnesHutOptimize: n > 50,
    scalingRatio: SPACING * (n > 400 ? 3 : 2),
  };
}

function collectPositions(graph: KbGraph): LayoutPositions {
  const positions: LayoutPositions = {};
  graph.forEachNode((node, attrs) => {
    positions[node] = { x: attrs.x, y: attrs.y };
  });
  return positions;
}

function layoutOnMainThread(graph: KbGraph): LayoutPositions {
  forceAtlas2.assign(graph, {
    iterations: layoutIterations(graph.order),
    settings: buildLayoutSettings(graph),
    getEdgeWeight: "weight",
  });
  return collectPositions(graph);
}

function layoutInWorker(graph: KbGraph): Promise<LayoutPositions> {
  return new Promise((resolve, reject) => {
    let worker: Worker;
    try {
      worker = new Worker(new URL("./layout-worker.ts", import.meta.url));
    } catch (err) {
      reject(err);
      return;
    }
    const timer = setTimeout(() => {
      worker.terminate();
      reject(new Error("layout worker timeout"));
    }, WORKER_TIMEOUT_MS);
    const done = (fn: () => void) => {
      clearTimeout(timer);
      worker.terminate();
      fn();
    };
    worker.onmessage = (event: MessageEvent<LayoutPositions>) => {
      done(() => resolve(event.data));
    };
    worker.onerror = (event) => {
      done(() => reject(event.error ?? new Error(event.message || "layout worker error")));
    };
    const request: LayoutRequest = {
      nodes: graph.mapNodes((node, attrs) => ({ id: node, x: attrs.x, y: attrs.y })),
      edges: graph.mapEdges((_edge, attrs, src, dst) => [src, dst, attrs.weight]),
      iterations: layoutIterations(graph.order),
      settings: buildLayoutSettings(graph),
    };
    worker.postMessage(request);
  });
}

export interface LayoutResult {
  positions: LayoutPositions;
  /** 本次是否经 Web Worker 计算(缓存命中时为 false) */
  usedWorker: boolean;
  cached: boolean;
}

/**
 * 计算(或读缓存)布局坐标并写回图属性。
 * 注意:传入的 graph 会被原地修改(x/y)。
 */
export async function applyLayout(graph: KbGraph, hash: string): Promise<LayoutResult> {
  const cachedPositions = layoutCache.get(hash);
  if (cachedPositions) {
    assignPositions(graph, cachedPositions);
    return { positions: cachedPositions, usedWorker: false, cached: true };
  }
  if (graph.order === 0) return { positions: {}, usedWorker: false, cached: false };

  let positions: LayoutPositions;
  let usedWorker = false;
  if (graph.order >= LAYOUT_WORKER_THRESHOLD && typeof Worker !== "undefined") {
    try {
      positions = await layoutInWorker(graph);
      assignPositions(graph, positions);
      usedWorker = true;
    } catch {
      // Worker 不可用(打包器不支持 / 运行异常 / 超时)→ 回退主线程同步计算
      positions = layoutOnMainThread(graph);
    }
  } else {
    positions = layoutOnMainThread(graph);
  }
  layoutCache.set(hash, positions);
  return { positions, usedWorker, cached: false };
}

function assignPositions(graph: KbGraph, positions: LayoutPositions): void {
  graph.updateEachNodeAttributes((node, attrs) => {
    const pos = positions[node];
    return pos ? { ...attrs, x: pos.x, y: pos.y } : attrs;
  });
}
