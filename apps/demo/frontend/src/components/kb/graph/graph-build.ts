/**
 * GraphData(后端 /kb/bases/{id}/graph 响应)→ graphology 无向图。
 * - 节点大小 = 关联数(degree)平方根缩放;
 * - 边粗细 = weight 归一化(四信号关联度);
 * - 初始坐标为确定性圆环分布(FA2 布局前也能渲染,且同数据两次构建结果一致);
 * - 颜色不在此处落定,由视图层按"类型 / 社区"模式 + 当前主题写入属性。
 */

import Graph from "graphology";
import type { GraphData, GraphEdgeEvidence } from "@/lib/types";
import {
  graphEdgePredicate,
  normalizeGraphEvidence,
} from "./graph-edge-utils.mjs";

export interface KbNodeAttributes {
  x: number;
  y: number;
  size: number;
  label: string;
  color: string;
  /** 业务实体类型(避开 sigma 保留的 type 字段) */
  entityType: string;
  degree: number;
  [key: string]: unknown;
}

export interface KbEdgeAttributes {
  size: number;
  color: string;
  weight: number;
  /** weight / maxWeight,0..1 */
  normWeight: number;
  linkType: string;
  predicate: string;
  direction: string;
  source: string;
  target: string;
  validFrom: string | null;
  validTo: string | null;
  evidence: GraphEdgeEvidence[];
  status: string;
  [key: string]: unknown;
}

export type KbGraph = Graph<KbNodeAttributes, KbEdgeAttributes>;

/** 数据指纹(FNV-1a):同数据命中布局缓存,不重算 ForceAtlas2 */
export function hashGraphData(data: GraphData): string {
  let h = 0x811c9dc5;
  const mix = (s: string) => {
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
  };
  mix(`${data.nodes.length}|${data.edges.length}|`);
  for (const n of data.nodes) mix(`${n.id};`);
  for (const e of data.edges) {
    mix(`${e.src}>${e.dst}:${graphEdgePredicate(e)}:${e.weight};`);
  }
  return (h >>> 0).toString(36);
}

/** GraphData → graphology 多重图(保留同端点的不同谓词，过滤悬空端点与自环) */
export function buildKbGraph(data: GraphData): KbGraph {
  const graph = new Graph<KbNodeAttributes, KbEdgeAttributes>({
    type: "undirected",
    allowSelfLoops: false,
    multi: true,
  });

  const n = Math.max(data.nodes.length, 1);
  const radius = 30 * Math.sqrt(n);
  data.nodes.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / n;
    graph.addNode(node.id, {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      size: 4,
      label: node.name,
      color: "",
      entityType: node.type,
      degree: 0,
    });
  });

  let maxWeight = 0;
  for (const edge of data.edges) {
    if (edge.src === edge.dst) continue;
    if (!graph.hasNode(edge.src) || !graph.hasNode(edge.dst)) continue;
    graph.addEdge(edge.src, edge.dst, {
      size: 1,
      color: "",
      weight: edge.weight,
      normWeight: 0,
      linkType: edge.link_type ?? graphEdgePredicate(edge),
      predicate: graphEdgePredicate(edge),
      direction: edge.direction ?? "undirected",
      source: edge.src,
      target: edge.dst,
      validFrom: edge.valid_from ?? null,
      validTo: edge.valid_to ?? null,
      evidence: normalizeGraphEvidence(edge.evidence),
      status: edge.status,
    });
    if (edge.weight > maxWeight) maxWeight = edge.weight;
  }

  graph.updateEachNodeAttributes((node, attrs) => {
    const degree = graph.degree(node);
    return {
      ...attrs,
      degree,
      // 关联数平方根缩放:0 度 4px,100 度约 17px
      size: 4 + Math.sqrt(degree) * 1.3,
    };
  });
  graph.updateEachEdgeAttributes((_edge, attrs) => {
    const norm = maxWeight > 0 ? attrs.weight / maxWeight : 0;
    return { ...attrs, normWeight: norm, size: 0.8 + norm * 2.4 };
  });

  return graph;
}
