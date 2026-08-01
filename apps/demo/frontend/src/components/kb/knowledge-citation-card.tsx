"use client";

import { ChevronDown, ImageIcon, Quote } from "lucide-react";
import { KnowledgeMedia } from "@/components/kb/knowledge-media";
import type {
  KnowledgeMediaReference,
  SearchCitation,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function citationLocation(citation: SearchCitation): string {
  const values: string[] = [];
  if (citation.page != null) values.push(`第 ${citation.page} 页`);
  if (citation.kind === "image_source" && citation.slide != null)
    values.push(`第 ${citation.slide} 张幻灯片`);
  if (
    citation.kind !== "image_source" &&
    citation.start_line != null
  ) {
    values.push(
      citation.end_line != null && citation.end_line !== citation.start_line
        ? `行 ${citation.start_line}-${citation.end_line}`
        : `行 ${citation.start_line}`,
    );
  }
  if (citation.kind !== "image_source" && citation.cell_ref)
    values.push(`单元格 ${citation.cell_ref}`);
  return values.join(" · ");
}

export function KnowledgeCitationCard({
  media,
  sourceFilename,
  description,
  className,
  compact = false,
}: {
  media: KnowledgeMediaReference;
  sourceFilename?: string | null;
  description?: string | null;
  className?: string;
  compact?: boolean;
}) {
  const source = sourceFilename?.trim() || "知识文档";
  const location = citationLocation(media.citation);
  const caption = description?.trim() || media.alt_text;

  return (
    <article
      className={cn(
        "grid gap-3 rounded-xl border border-border bg-card p-3",
        compact ? "grid-cols-[5.5rem_minmax(0,1fr)]" : "sm:grid-cols-[9rem_minmax(0,1fr)]",
        className,
      )}
    >
      <KnowledgeMedia
        assetId={media.asset_id}
        alt={media.alt_text}
        width={media.width}
        height={media.height}
        thumbnailUrl={media.thumbnail_url}
        contentUrl={media.content_url}
        sourceLabel={[source, location].filter(Boolean).join(" · ")}
        className="self-start"
      />
      <div className="min-w-0 space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <ImageIcon className="size-3.5 text-primary" />
          来源图片
        </div>
        <p className={cn("text-sm leading-5", compact && "line-clamp-3")}>
          {caption}
        </p>
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          <Quote className="size-3.5 text-success" />
          <span className="font-medium text-foreground/80">{source}</span>
          {location && <span>· {location}</span>}
        </div>
        <details className="group text-[11px] text-muted-foreground">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded px-1 py-0.5 hover:bg-muted">
            高级详情
            <ChevronDown className="size-3 transition-transform group-open:rotate-180" />
          </summary>
          <dl className="mt-1 grid gap-x-2 gap-y-1 rounded-md bg-muted/40 p-2 sm:grid-cols-[auto_minmax(0,1fr)]">
            <dt>资产</dt>
            <dd className="break-all font-mono">{media.asset_id}</dd>
            <dt>修订</dt>
            <dd className="break-all font-mono">
              {media.citation.revision_id}
            </dd>
            <dt>来源校验</dt>
            <dd className="break-all font-mono">
              {media.citation.source_sha256}
            </dd>
          </dl>
        </details>
      </div>
    </article>
  );
}
