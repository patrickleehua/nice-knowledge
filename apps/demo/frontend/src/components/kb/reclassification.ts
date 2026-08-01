// 文档"二次抽取重分类"的可用性判定与状态文案。
//
// SDK 化改造(MIGRATION-PLAN §5.8):TF 版本写死 `route_template`(线路知识)
// 一种目标类型。后端 `POST /kb/documents/{id}/reclassify` 的 `target_doc_type`
// 是开放字符串(内置 DocType 或任意已注册的实体类型 key,见
// `nicekit/api/v1/kb.py::_resolve_doc_type`),因此这里改为按目标类型参数化。
//
// 语义没变:复用最新修订**已暂存的解析产物**重跑抽取阶段,不重新上传、不改写
// 源文件、不动当前已发布快照;新事实仍需审核并发布新快照才生效。

import type { SourceDocument } from "@/lib/types";

/**
 * 该文档现在能否发起二次抽取。
 *
 * - 仅 active 生命周期 + 已到终态(completed / awaiting_review)的文档可以;
 * - 上一次重分类失败且 retryable 时允许重试(同一目标类型);
 * - 否则只有"只切了 chunk 没做结构化抽取"的 general 文档值得重抽。
 */
export function canReclassifyDocument(document: SourceDocument) {
  if (
    document.lifecycle_status !== "active" ||
    (document.status !== "completed" && document.status !== "awaiting_review")
  ) {
    return false;
  }
  const latest = document.latest_reclassification;
  if (latest?.status === "failed" && latest.retryable) return true;
  return document.doc_type === "general";
}

/** 已失败且可重试时的目标类型(重试要沿用上次的目标,不能悄悄换)。 */
export function retryTargetDocType(
  document: SourceDocument,
): string | null {
  const latest = document.latest_reclassification;
  return latest?.status === "failed" && latest.retryable
    ? latest.target_doc_type
    : null;
}

/**
 * 最近一次重分类的状态文案。`typeLabel` 由调用方按类型注册表解析
 * (拿不到就回落 type_key 原值)。
 */
export function reclassificationStatus(
  document: SourceDocument,
  typeLabel: (typeKey: string) => string = (key) => key,
): { text: string; failed: boolean } | null {
  const operation = document.latest_reclassification;
  if (!operation) return null;
  const label = typeLabel(operation.target_doc_type);
  if (operation.status === "queued") {
    return { text: `已排队重新抽取为「${label}」`, failed: false };
  }
  if (operation.status === "running") {
    return { text: `正在重新抽取为「${label}」`, failed: false };
  }
  if (operation.status === "failed") {
    return {
      text: `重新抽取为「${label}」失败${operation.error ? `：${operation.error}` : ""}`,
      failed: true,
    };
  }
  if (operation.status === "succeeded" || operation.status === "staged") {
    return {
      text: `已抽取为「${label}」，需审核并发布新快照后生效`,
      failed: false,
    };
  }
  return { text: `重新抽取为「${label}」已取消`, failed: false };
}

export function shouldPollReclassification(document: SourceDocument) {
  const status = document.latest_reclassification?.status;
  return status === "queued" || status === "running";
}
