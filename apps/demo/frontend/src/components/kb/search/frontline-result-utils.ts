import type { SearchCitation, SearchHit } from "@/lib/types";

// `hit.kind` 是自由字符串:内置只有 chunk(文档切片)与 page(wiki 页),
// 其余取值就是实体类型 key(nicekit/kb/search.py:1307)。查不到的一律回显
// 原值,不要归到"资料"这种含糊兜底里——用户需要知道命中的到底是什么类型。
const BUILTIN_KIND_LABELS: Record<string, string> = {
  page: "知识页",
  chunk: "文档",
};

const SUMMARY_PART_LIMIT = 4;

// 摘要跳过:标识类、已单独渲染的标题/描述、检索内部标记
const SUMMARY_SKIP_KEYS = new Set([
  "id",
  "name",
  "title",
  "canonical_name",
  "heading_path",
  "entity_type_key",
  "snapshot_id",
  "must_include_hit",
  "stale",
  "description",
  "notes",
  "content",
]);

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function hitKindLabel(kind: string): string {
  return BUILTIN_KIND_LABELS[kind] ?? kind;
}

export function frontlineHitTitle(hit: SearchHit): string {
  const named =
    text(hit.data.name) ??
    text(hit.data.title) ??
    text(hit.data.canonical_name) ??
    text(hit.data.heading_path);
  if (named) return named;
  const content = text(hit.data.content);
  return content ? content.slice(0, 72) : hit.source || "未命名资料";
}

export function frontlineHitSummary(hit: SearchHit): string {
  if (hit.kind === "chunk") {
    return (
      text(hit.data.content)?.replace(/\s+/g, " ").slice(0, 260) ??
      "该来源没有可展示的摘要。"
    );
  }

  // 通用实体:SDK 不认识宿主的字段名,按 attributes 顺序取前几项 `键: 值`。
  // 描述性长文本(description/notes/content)优先直出,读起来比键值对好。
  const parts: string[] = [];
  const add = (value: string | null) => {
    if (value && !parts.includes(value)) parts.push(value);
  };
  add(text(hit.data.description));
  add(text(hit.data.notes));

  for (const [key, value] of Object.entries(hit.data)) {
    if (parts.length >= SUMMARY_PART_LIMIT) break;
    if (SUMMARY_SKIP_KEYS.has(key)) continue;
    const scalar =
      text(value) ?? (number(value) !== null ? String(value) : null);
    if (scalar === null) continue;
    add(`${key}: ${scalar.length > 40 ? `${scalar.slice(0, 40)}…` : scalar}`);
  }
  add(text(hit.data.content)?.replace(/\s+/g, " ").slice(0, 220) ?? null);

  return parts.slice(0, SUMMARY_PART_LIMIT).join(" · ") ||
    "打开来源查看完整信息。";
}

export function citationLocation(citation: SearchCitation | null): string {
  if (!citation) return "";
  const parts: string[] = [];
  if (typeof citation.page === "number") parts.push(`第 ${citation.page} 页`);
  if ("slide" in citation && typeof citation.slide === "number") {
    parts.push(`第 ${citation.slide} 张幻灯片`);
  }
  if ("start_line" in citation && typeof citation.start_line === "number") {
    parts.push(
      typeof citation.end_line === "number" &&
        citation.end_line !== citation.start_line
        ? `第 ${citation.start_line}-${citation.end_line} 行`
        : `第 ${citation.start_line} 行`,
    );
  }
  if ("cell_ref" in citation && citation.cell_ref) {
    parts.push(`单元格 ${citation.cell_ref}`);
  }
  return parts.join(" · ");
}

export function sourceLayerLabel(layer: SearchHit["layer"]): string {
  if (layer === "tenant") return "本组织资料";
  if (layer === "shared") return "共享资料";
  return "平台资料";
}

export function linkifyAnswerCitations(
  answer: string,
  refs: readonly number[],
): string {
  const available = new Set(refs);
  return answer.replace(/\[(\d{1,3})\]/g, (match, raw: string) => {
    const ref = Number(raw);
    return available.has(ref) ? `[[${ref}]](#source-${ref})` : match;
  });
}
