// 相对 .mjs 导入:本文件被 node 原生测试直接加载,解析不了 "@/" 别名
import { RENDER_KIND_LABELS } from "../../lib/render-kind-labels.mjs";

export type ToolStatus = "running" | "waiting" | "ok" | "failed";

export type BusinessResultKind =
  "project" | "demand" | "retrieval" | "itinerary" | "quote" | "document";

export type UnknownRecord = Record<string, unknown>;

const BUSINESS_KINDS: Record<string, BusinessResultKind> = {
  project_create: "project",
  demand_parse: "demand",
  kb_retrieve: "retrieval",
  itinerary_generate: "itinerary",
  quote_generate: "quote",
  render_generate: "document",
};

const AUTO_EXPAND = new Set([
  "project_create",
  "demand_parse",
  "itinerary_generate",
  "quote_generate",
  "render_generate",
]);

// chat 与旅行计划页共用同一份 kind 文案(消除双字典漂移;权威数据源是 /render-kinds)
const DOCUMENT_LABELS: Record<string, string> = RENDER_KIND_LABELS;

export function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function readRecord(
  value: unknown,
  key?: string,
): UnknownRecord | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return isRecord(candidate) ? candidate : undefined;
}

export function readString(value: unknown, key?: string): string | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return typeof candidate === "string" && candidate.trim()
    ? candidate
    : undefined;
}

export function readNumber(value: unknown, key?: string): number | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return typeof candidate === "number" && Number.isFinite(candidate)
    ? candidate
    : undefined;
}

export function readStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function readRecordArray(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function countRecordValues(value: unknown): number {
  if (!isRecord(value)) return 0;
  return Object.values(value).reduce(
    (total: number, item) =>
      total + (typeof item === "number" && item > 0 ? item : 0),
    0,
  );
}

export function businessResultKind(
  name: string,
  output: unknown,
): BusinessResultKind | null {
  return isRecord(output) ? (BUSINESS_KINDS[name] ?? null) : null;
}

export function shouldExpandTool(
  name: string,
  status: ToolStatus,
  output: unknown,
): boolean {
  if (status === "failed") return true;
  return status === "ok" && isRecord(output) && AUTO_EXPAND.has(name);
}

function formatMoney(value: number): string {
  return `¥${Math.round(value).toLocaleString("zh-CN")}`;
}

function validationIssueCount(value: unknown): number {
  if (!isRecord(value)) return 0;
  return ["blocking_issues", "need_human_confirm", "warnings"].reduce(
    (total, key) => total + (Array.isArray(value[key]) ? value[key].length : 0),
    0,
  );
}

export function toolResultSummary(name: string, output: unknown): string {
  if (!isRecord(output)) {
    if (typeof output === "string" && output.trim()) return output.trim();
    return "已完成";
  }

  // 子 agent 委派:摘要给"派了谁 + 跑了几轮",结论正文在展开区里
  if (name === "Agent") {
    const agent = readString(output, "agent");
    const who = agent ? `${agent} · ` : "";
    if (readString(output, "error")) return `${who}未完成`;
    const turns = readNumber(output, "turns");
    return turns === undefined ? `${who}已完成` : `${who}${turns} 轮完成`;
  }

  if (name === "project_create") {
    const title = readString(output, "title");
    return title ? `已创建「${title}」` : "旅行计划已创建";
  }

  if (name === "demand_parse") {
    const parsed = readRecord(output, "parsed");
    const destinations = readStringArray(parsed?.destinations);
    const missing = readStringArray(output.missing_fields).length;
    const scope = destinations.length
      ? destinations.slice(0, 2).join("、")
      : "需求";
    return missing ? `${scope} · ${missing} 项待补充` : `${scope} · 信息完整`;
  }

  if (name === "kb_retrieve") {
    const count = countRecordValues(output.counts);
    if (output.empty === true || count === 0) return "暂无匹配资料";
    return `${count} 条可用资料`;
  }

  if (name === "kb_image_inspect") {
    return readString(readRecord(output, "inspection"), "description")
      ? "已核验来源原图"
      : "图片核验完成";
  }

  if (name === "business_media_select") {
    const count = Array.isArray(output.selected_kb_media)
      ? output.selected_kb_media.length
      : 0;
    return count ? `已选择 ${count} 张知识图片` : "已清除知识图片";
  }

  if (name === "itinerary_generate") {
    const options = readRecord(output, "options");
    const count =
      readNumber(output, "option_count") ??
      readRecordArray(options?.options).length;
    return count ? `已生成 ${count} 套方案` : "行程方案已生成";
  }

  if (name === "quote_generate") {
    const totals = readRecord(output, "totals");
    const perPerson = readNumber(totals, "per_person");
    const itemCount = readNumber(output, "item_count");
    const segments = [
      perPerson === undefined ? null : `${formatMoney(perPerson)}/人`,
      itemCount === undefined ? null : `${itemCount} 项成本`,
    ].filter(Boolean);
    return segments.length ? segments.join(" · ") : "报价草稿已生成";
  }

  if (name === "render_generate") {
    const kind = readString(output, "kind");
    const status = readString(output, "status");
    const label = kind ? (DOCUMENT_LABELS[kind] ?? kind) : "文档";
    return status === "success" || status === "downloaded"
      ? `${label}已生成`
      : `${label} · ${status ?? "任务已创建"}`;
  }

  if (name === "web_search") {
    const results = readRecordArray(output.results);
    if (!results.length) return "未找到相关网页";
    const official = results.filter(
      (item) => readString(item, "source_tier") === "official",
    ).length;
    const summary = official
      ? `${results.length} 条来源 · ${official} 官方`
      : `${results.length} 条来源`;
    return output.cached === true ? `缓存 · ${summary}` : summary;
  }

  if (name === "web_fetch") {
    const pages = readRecordArray(output.pages);
    if (!pages.length) return "未读取到网页";
    const succeeded = pages.filter(
      (page) => readString(page, "status") === "ok",
    ).length;
    if (!succeeded) return "网页读取失败";
    const failed = pages.length - succeeded;
    return failed
      ? `已读取 ${succeeded} 个网页 · ${failed} 个失败`
      : `已读取 ${succeeded} 个网页`;
  }

  if (name === "image_generate") {
    const count = Array.isArray(output.images) ? output.images.length : 0;
    if (count) return `${count} 张图片`;
    return output.status === "unavailable" || readString(output, "error")
      ? "图片生成失败"
      : "未生成图片";
  }

  const items = Array.isArray(output.items) ? output.items.length : undefined;
  if (items !== undefined) return `${items} 项结果`;
  const total = readNumber(output, "total");
  if (total !== undefined) return `${total} 项结果`;
  const issues = validationIssueCount(output.validation);
  if (issues > 0) return `完成 · ${issues} 项需关注`;
  return readString(output, "status") ?? "已完成";
}

export { DOCUMENT_LABELS };
