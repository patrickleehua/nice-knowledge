// 检索命中 → 工作台深链的统一构造(检索页卡片与 ⌘K 命令面板共用,避免两处各写一份)。
// 深链契约:/org/kb/{kb_id}?view=sources&doc={source_doc_id}&start=&end=

import { sourceLineAnchor } from "@/components/kb/search/hit-metadata-utils.mjs";
import type { SearchHit } from "@/lib/types";

export interface SourceTarget {
  href: string;
  hasLineAnchor: boolean;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function sourceTarget(hit: SearchHit): SourceTarget | null {
  if (hit.citation === null) return null;
  const doc = hit.citation.source_doc_id || str(hit.data.source_doc_id);
  if (!doc) return null;
  const params = new URLSearchParams({ view: "sources", doc });
  const { start, end } =
    hit.citation.kind === "image_source"
      ? { start: null, end: null }
      : sourceLineAnchor(hit.citation, hit.data);
  if (start !== null) params.set("start", String(start));
  if (end !== null) params.set("end", String(end));
  return {
    href: `/org/kb/${hit.kb_id}?${params.toString()}`,
    hasLineAnchor: start !== null,
  };
}

/** 命令面板行标题:实体取名称,片段取标题路径,兜底取正文首段。 */
export function hitTitle(hit: SearchHit): string {
  const named =
    str(hit.data.name) ?? str(hit.data.title) ?? str(hit.data.canonical_name);
  if (named) return named;
  const heading = str(hit.data.heading_path);
  if (heading) return heading;
  const content = str(hit.data.content);
  if (content) return content.slice(0, 80).replace(/\s+/g, " ");
  return hit.source || "未命名结果";
}

/** 命令面板行副标题:来源文件名 + 页/行定位。 */
export function hitSubtitle(hit: SearchHit): string {
  const parts: string[] = [];
  const filename =
    str(hit.data.source_filename) ?? str(hit.data.filename) ?? null;
  if (filename) parts.push(filename);
  if (hit.citation && hit.citation.kind !== "image_source") {
    const { start, end } = sourceLineAnchor(hit.citation, hit.data);
    if (typeof start === "number") {
      parts.push(
        typeof end === "number" && end !== start
          ? `${start}-${end} 行`
          : `${start} 行`,
      );
    }
  }
  return parts.join(" · ");
}
