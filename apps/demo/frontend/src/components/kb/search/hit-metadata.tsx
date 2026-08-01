import {
  ChartNoAxesCombined,
  ChevronDown,
  CircleSlash2,
  Quote,
} from "lucide-react";
import {
  citationIdentityRows,
  citationLocation,
  formatSearchScore,
  primarySearchScore,
} from "./hit-metadata-utils.mjs";
import type { SearchCitation, SearchHit } from "@/lib/types";

const NATIVE_SCORE_LABELS: Record<string, string> = {
  structured: "结构化",
  sparse: "全文",
  must_include: "必含",
  dense: "语义",
};

export function SearchScoreDetails({ hit }: { hit: SearchHit }) {
  const scores = hit.data.scores;
  const primary = primarySearchScore(scores);
  const nativeScores = Object.entries(scores.native).sort(([left], [right]) =>
    left.localeCompare(right),
  );

  return (
    <details className="group min-w-0 text-xs text-muted-foreground">
      <summary className="flex cursor-pointer list-none items-center gap-1 whitespace-nowrap select-none hover:text-foreground">
        <ChartNoAxesCombined className="size-3.5" />
        <span>{primary.label}</span>
        <span className="font-mono tabular-nums">
          {formatSearchScore(primary)}
        </span>
        <ChevronDown className="size-3 transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-1 flex max-w-full flex-wrap gap-x-3 gap-y-1 pl-4">
        {nativeScores.map(([channel, value]) => (
          <span key={channel} className="whitespace-nowrap">
            {NATIVE_SCORE_LABELS[channel] ?? channel}{" "}
            <span className="font-mono tabular-nums">{value.toFixed(3)}</span>
          </span>
        ))}
        <span className="whitespace-nowrap">
          RRF{" "}
          <span className="font-mono tabular-nums">
            {scores.rrf.toFixed(4)}
          </span>
        </span>
        {scores.rerank !== null && (
          <span className="whitespace-nowrap">
            重排{" "}
            <span className="font-mono tabular-nums">
              {scores.rerank.toFixed(3)}
            </span>
          </span>
        )}
      </div>
    </details>
  );
}

function CitationIds({ citation }: { citation: SearchCitation }) {
  const ids = citationIdentityRows(citation);

  return (
    <dl className="grid gap-x-3 gap-y-1 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
      {ids.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-all font-mono text-foreground/80">
            {value ?? "-"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function SearchCitationDetails({
  citation,
}: {
  citation: SearchCitation | null;
}) {
  if (citation === null) {
    return (
      <div className="flex flex-wrap items-center gap-1.5 border-t border-border/70 pt-2 text-xs text-muted-foreground">
        <CircleSlash2 className="size-3.5 shrink-0" />
        <span className="font-medium text-foreground/80">参考信息</span>
        <span>未关联可核验来源</span>
      </div>
    );
  }

  const location = citationLocation(citation);
  return (
    <details className="group border-t border-border/70 pt-2 text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 select-none">
        <Quote className="size-3.5 shrink-0 text-success" />
        <span className="font-medium">
          {citation.kind === "fact_evidence"
            ? "已确认事实证据"
            : citation.kind === "image_source"
              ? "图片来源"
              : "原文来源"}
        </span>
        {location.length > 0 && (
          <span className="min-w-0 truncate text-muted-foreground">
            {location.join(" · ")}
          </span>
        )}
        <ChevronDown className="ml-auto size-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-2 min-w-0 space-y-2 pl-5">
        {citation.quote_text && (
          <blockquote className="max-h-40 overflow-y-auto border-l-2 border-border pl-2 leading-relaxed whitespace-pre-wrap text-foreground/80 break-words">
            {citation.quote_text}
          </blockquote>
        )}
        <CitationIds citation={citation} />
      </div>
    </details>
  );
}
