import { describe, expect, it } from "vitest";
import {
  citationLocation,
  frontlineHitSummary,
  frontlineHitTitle,
  linkifyAnswerCitations,
} from "@/components/kb/search/frontline-result-utils";
import type { SearchHit } from "@/lib/types";

const hit: SearchHit = {
  kind: "hotel",
  layer: "tenant",
  kb_id: "kb-1",
  source: "hotel/1",
  confidence: 0.9,
  data: {
    name: "Hotel Paris",
    city: "巴黎",
    star: "4星",
    price_ref: 220,
    currency: "EUR",
    scores: { native: {}, rrf: 0.1, rerank: null },
  },
  citation: {
    kind: "source_span",
    revision_id: "rev-1",
    source_doc_id: "doc-1",
    source_sha256: "hash",
    quote_text: "酒店参考价",
    chunk_id: "chunk-1",
    page: 2,
    start_line: 4,
    end_line: 6,
    cell_ref: null,
  },
};

describe("frontline result presentation", () => {
  it("uses business-facing title and summary fields", () => {
    expect(frontlineHitTitle(hit)).toBe("Hotel Paris");
    expect(frontlineHitSummary(hit)).toContain("巴黎");
    expect(frontlineHitSummary(hit)).toContain("参考 220 EUR");
  });

  it("formats a human-readable source location", () => {
    expect(citationLocation(hit.citation)).toBe("第 2 页 · 第 4-6 行");
  });

  it("links only citations that exist in the response", () => {
    expect(linkifyAnswerCitations("结论 [1]，未知 [3]", [1, 2])).toBe(
      "结论 [[1]](#source-1)，未知 [3]",
    );
  });
});
