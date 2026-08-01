"use client";

/**
 * KB 知识图谱可视化(sigma.js WebGL)对外入口。
 * sigma 依赖 WebGL / DOM,必须 next/dynamic + ssr:false 延迟到客户端加载;
 * 工作台接线时直接 `import { KbGraphView } from "@/components/kb/graph/kb-graph-view"` 即可。
 */

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/skeleton";

const KbGraphViewImpl = dynamic(() => import("./graph-view-impl"), {
  ssr: false,
  loading: () => <Skeleton className="h-full min-h-96 w-full rounded-md" />,
});

export function KbGraphView({ kbId }: { kbId: string }) {
  return <KbGraphViewImpl kbId={kbId} />;
}
