import type { SearchCitation, SearchHit } from "@/lib/types";

const KIND_LABELS: Record<string, string> = {
  destination: "目的地",
  poi: "景点",
  hotel: "酒店",
  cost: "费用",
  route_template: "线路",
  route: "线路",
  page: "知识页",
  chunk: "文档",
};

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function hitKindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? "资料";
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

  const parts: string[] = [];
  const add = (value: string | null) => {
    if (value && !parts.includes(value)) parts.push(value);
  };
  add(text(hit.data.city));
  add(text(hit.data.country));
  add(text(hit.data.star));
  add(text(hit.data.poi_type));
  add(text(hit.data.category));

  const price =
    number(hit.data.price_ref) ??
    number(hit.data.unit_price) ??
    number(hit.data.unit_cost);
  if (price !== null) {
    const currency = text(hit.data.currency);
    const unit = text(hit.data.unit);
    add(
      `参考 ${price}${currency ? ` ${currency}` : ""}${unit ? ` / ${unit}` : ""}`,
    );
  }

  const days = number(hit.data.days);
  if (days !== null) add(`${days} 天`);
  const minutes = number(hit.data.visit_minutes);
  if (minutes !== null) add(`建议游览 ${minutes} 分钟`);
  add(text(hit.data.ticket_info));
  add(text(hit.data.description));
  add(text(hit.data.notes));
  add(text(hit.data.content)?.replace(/\s+/g, " ").slice(0, 220) ?? null);

  return parts.slice(0, 4).join(" · ") || "打开来源查看完整信息。";
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
