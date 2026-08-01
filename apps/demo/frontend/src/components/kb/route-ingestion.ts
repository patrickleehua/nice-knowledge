import type { RouteKnowledgeDiagnostics, SourceDocument } from "@/lib/types";

export function canRequestRouteReclassification(document: SourceDocument) {
  if (
    document.lifecycle_status !== "active" ||
    (document.status !== "completed" && document.status !== "awaiting_review")
  ) {
    return false;
  }
  const latest = document.latest_reclassification;
  if (
    latest?.target_doc_type === "route_template" &&
    latest.status === "failed" &&
    latest.retryable
  ) {
    return true;
  }
  return document.doc_type === "general";
}

export function routeReclassificationStatus(
  document: SourceDocument,
): { text: string; failed: boolean } | null {
  const operation = document.latest_reclassification;
  if (!operation || operation.target_doc_type !== "route_template") return null;
  if (operation.status === "queued") {
    return { text: "线路知识抽取已排队", failed: false };
  }
  if (operation.status === "running") {
    return { text: "正在抽取线路知识", failed: false };
  }
  if (operation.status === "failed") {
    return {
      text: `线路知识抽取失败${operation.error ? `：${operation.error}` : ""}`,
      failed: true,
    };
  }
  if (operation.status === "succeeded" || operation.status === "staged") {
    return {
      text: "线路知识已抽取，需审核并发布新快照后生效",
      failed: false,
    };
  }
  return { text: "线路知识抽取已取消", failed: false };
}

export function shouldPollReclassification(document: SourceDocument) {
  const status = document.latest_reclassification?.status;
  return status === "queued" || status === "running";
}

export type RouteDiagnosticPresentation = {
  title: string;
  description: string;
  actionLabel: string | null;
  destination: "sources" | "review" | "release" | null;
};

export function routeDiagnosticPresentation(
  diagnostics: RouteKnowledgeDiagnostics,
): RouteDiagnosticPresentation {
  switch (diagnostics.reason_code) {
    case "classification_required":
      return {
        title: "线路文件已上传，等待分类",
        description:
          "原文件已经安全保存，但尚未解析。请到待处理文件中指定划分 / 抽取类型，再手动排入解析队列。",
        actionLabel: "去分类并排队",
        destination: "sources",
      };
    case "no_sources":
      return {
        title: "还没有线路来源",
        description:
          "先上传线路日程，再到“待处理文件”明确指定为成熟线路抽取并手动排队。",
        actionLabel: "上传线路文档",
        destination: "sources",
      };
    case "wrong_extraction_type":
      return {
        title: "文档已入库，但没有按线路抽取",
        description:
          "当前文档只生成了检索切片。可复用原解析产物重新抽取，源文件、历史事实和已发布快照都不会被改写。",
        actionLabel: "去重新抽取",
        destination: "sources",
      };
    case "extraction_in_progress":
      return {
        title: "线路知识正在抽取",
        description: "抽取完成后仍需按实体类型配置完成审核，并发布新快照。",
        actionLabel: "查看处理状态",
        destination: "sources",
      };
    case "review_required":
      return {
        title: "线路知识等待审核",
        description: `当前有 ${diagnostics.route_claim_counts.suggested} 条待审核线路事实，审核通过后才能进入候选快照。`,
        actionLabel: "去审核",
        destination: "review",
      };
    case "snapshot_build_required":
      return {
        title: "审核已完成，尚未构建新快照",
        description: `已有 ${diagnostics.route_claim_counts.confirmed} 条已确认线路事实；构建快照不会自动替换当前已发布版本。`,
        actionLabel: "去构建快照",
        destination: "release",
      };
    case "snapshot_activation_required":
      return {
        title: "候选快照等待发布",
        description: `候选快照含 ${diagnostics.ready_route_count} 条线路知识；发布后才会替换本库当前可见版本。`,
        actionLabel: "去发布快照",
        destination: "release",
      };
    case "no_route_claims":
      return {
        title: "尚未抽取到线路知识",
        description:
          "已按线路类型处理，但没有形成线路事实。请检查文档内容与抽取失败信息，再决定重试或上传其他资料。",
        actionLabel: "查看来源文档",
        destination: "sources",
      };
    case "published":
      return {
        title: "当前快照没有可展示的线路知识",
        description: "请刷新诊断；已发布线路数量与当前投影列表暂不一致。",
        actionLabel: null,
        destination: null,
      };
  }
}
