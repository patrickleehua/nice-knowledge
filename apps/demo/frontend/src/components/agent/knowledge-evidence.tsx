"use client";

import { FileSearch, ScanSearch } from "lucide-react";
import Link from "next/link";
import { KnowledgeCitationCard } from "@/components/kb/knowledge-citation-card";
import type {
  KnowledgeMediaReference,
  SearchCitation,
} from "@/lib/types";
import { isRecord, readRecordArray, readString } from "./tool-presentation";

interface EvidenceHit {
  kb_id?: string;
  data: Record<string, unknown>;
  citation: SearchCitation | null;
  media_refs: KnowledgeMediaReference[];
}

// 检索结果的分桶键由后端的实体类型注册表决定(宿主注册什么类型就有什么桶),
// 因此不写死清单:凡是值为数组的键都当作一个桶收集。TF 曾在这里硬编码五个
// 行业分组(destinations/pois/hotels/...),SDK 化后那五张专表已不存在。

function isCitation(value: unknown): value is SearchCitation {
  return (
    isRecord(value) &&
    ["source_span", "fact_evidence", "image_source"].includes(
      String(value.kind),
    ) &&
    typeof value.source_doc_id === "string" &&
    typeof value.revision_id === "string"
  );
}

function hitFrom(value: unknown): EvidenceHit | null {
  if (!isRecord(value)) return null;
  const data = isRecord(value.data) ? value.data : {};
  return {
    kb_id: readString(value, "kb_id"),
    data,
    citation: isCitation(value.citation) ? value.citation : null,
    media_refs: readRecordArray(value.media_refs)
      .filter(
        (media) =>
        typeof media.asset_id === "string" &&
        typeof media.alt_text === "string" &&
        isCitation(media.citation),
      )
      .map((media) => media as unknown as KnowledgeMediaReference),
  };
}

function evidenceHits(output: Record<string, unknown>): EvidenceHit[] {
  const directHits = readRecordArray(output.hits);
  const values: unknown[] = [...directHits];
  if (!directHits.length && isRecord(output.retrieval)) {
    for (const bucket of Object.values(output.retrieval)) {
      values.push(...readRecordArray(bucket));
    }
  }
  return values
    .map(hitFrom)
    .filter((value): value is EvidenceHit => value !== null)
    .slice(0, 8);
}

export function AgentKnowledgeEvidence({
  output,
}: {
  output: Record<string, unknown>;
}) {
  const hits = evidenceHits(output);
  if (!hits.length) {
    return (
      <p className="text-xs text-muted-foreground">
        本次没有可展示的已授权知识证据。
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {hits.flatMap((hit, index) => {
        const sourceName =
          readString(hit.data, "source_filename") ??
          readString(hit.data, "filename") ??
          "知识文档";
        const cards = hit.media_refs.map((media) => (
          <KnowledgeCitationCard
            key={`${index}-${media.asset_id}`}
            media={media}
            sourceFilename={sourceName}
            compact
          />
        ));
        if (!hit.citation || hit.citation.kind === "image_source") return cards;
        const href = hit.kb_id
          ? `/org/kb/${hit.kb_id}?${new URLSearchParams({
              view: "sources",
              doc: hit.citation.source_doc_id,
              ...(hit.citation.start_line != null
                ? { start: String(hit.citation.start_line) }
                : {}),
            }).toString()}`
          : null;
        cards.unshift(
          <div
            key={`${index}-text`}
            className="rounded-lg border border-border bg-card p-3 text-xs"
          >
            <div className="flex items-center gap-1.5 font-medium">
              <FileSearch className="size-3.5 text-success" />
              {href ? (
                <Link href={href} className="text-primary hover:underline">
                  {sourceName}
                </Link>
              ) : (
                sourceName
              )}
            </div>
            <p className="mt-1.5 line-clamp-3 leading-5 text-muted-foreground">
              {hit.citation.quote_text || "已核验原文证据"}
            </p>
          </div>,
        );
        return cards;
      })}
    </div>
  );
}

export function AgentImageInspection({
  output,
}: {
  output: Record<string, unknown>;
}) {
  const inspection = isRecord(output.inspection) ? output.inspection : null;
  return (
    <div className="rounded-lg border border-border bg-card p-3 text-xs">
      <p className="flex items-center gap-1.5 font-medium">
        <ScanSearch className="size-3.5 text-primary" />
        已核验知识来源原图
      </p>
      <p className="mt-1 text-muted-foreground">
        {readString(output, "source_name") ?? "知识文档"}
        {typeof output.page === "number" ? ` · 第 ${output.page} 页` : ""}
        {typeof output.slide === "number"
          ? ` · 第 ${output.slide} 张幻灯片`
          : ""}
      </p>
      {inspection && (
        <div className="mt-2 space-y-1">
          {readString(inspection, "description") && (
            <p>{readString(inspection, "description")}</p>
          )}
          {readString(inspection, "visible_text") && (
            <p className="text-muted-foreground">
              可见文字：{readString(inspection, "visible_text")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
