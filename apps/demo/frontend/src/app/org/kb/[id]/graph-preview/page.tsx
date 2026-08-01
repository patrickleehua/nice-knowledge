"use client";

/**
 * sigma 图谱组件的独立演示路由(开发冒烟用):全屏渲染 KbGraphView。
 * 合并时由主控把 KbGraphView 接线到知识库工作台的 graph 视图,本页可保留作调试入口。
 */

import { useParams } from "next/navigation";
import { KbGraphView } from "@/components/kb/graph/kb-graph-view";
import { PageHeader } from "@/components/shared";

export default function GraphPreviewPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <>
      <PageHeader title="知识图谱预览" />
      <div className="h-[calc(100vh-6.5rem)] min-h-96">
        <KbGraphView kbId={id} />
      </div>
    </>
  );
}
