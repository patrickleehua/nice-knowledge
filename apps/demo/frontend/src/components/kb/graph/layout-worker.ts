/**
 * ForceAtlas2 布局 Web Worker:主线程传入序列化的节点 / 边 / 迭代参数,
 * 在 worker 内重建 graphology 图并跑同步布局,回传 {nodeId: {x, y}}。
 * 大图(>=220 节点)时避免阻塞主线程;由 layout.ts 负责超时与回退。
 */

import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { LayoutPositions, LayoutRequest } from "./layout";

// tsconfig lib 未含 webworker,此处以最小接口收窄 self
const scope = self as unknown as {
  onmessage: ((event: MessageEvent<LayoutRequest>) => void) | null;
  postMessage: (message: LayoutPositions) => void;
};

scope.onmessage = (event: MessageEvent<LayoutRequest>) => {
  const { nodes, edges, iterations, settings } = event.data;
  const graph = new Graph({ type: "undirected", allowSelfLoops: false });
  for (const node of nodes) {
    graph.addNode(node.id, { x: node.x, y: node.y });
  }
  for (const [src, dst, weight] of edges) {
    if (!graph.hasEdge(src, dst)) graph.addEdge(src, dst, { weight });
  }
  forceAtlas2.assign(graph, { iterations, settings, getEdgeWeight: "weight" });
  const positions: LayoutPositions = {};
  graph.forEachNode((node, attrs) => {
    positions[node] = { x: attrs.x as number, y: attrs.y as number };
  });
  scope.postMessage(positions);
};
