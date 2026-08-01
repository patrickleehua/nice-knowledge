"use client";

// graph 视图:sigma.js 知识图谱工作区(画布/搜索/过滤/社区着色/洞察与 LLM 建议边
// 全部在 KbGraphView 内,query key 与旧版一致)。需要父级确定高度。

import { KbGraphView } from "@/components/kb/graph/kb-graph-view";

export function GraphView({ kbId }: { kbId: string }) {
  return (
    <div className="h-full min-h-96">
      <KbGraphView kbId={kbId} />
    </div>
  );
}
