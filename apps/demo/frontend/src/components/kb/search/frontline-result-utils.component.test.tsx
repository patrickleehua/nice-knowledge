import { describe, expect, it } from "vitest";
import {
  citationLocation,
  frontlineHitSummary,
  frontlineHitTitle,
  hitKindLabel,
  linkifyAnswerCitations,
} from "@/components/kb/search/frontline-result-utils";
import type { SearchHit } from "@/lib/types";

// kind 就是 entity_type_key(自由字符串),data 是宿主自定的 attributes
const hit: SearchHit = {
  kind: "policy",
  layer: "tenant",
  kb_id: "kb-1",
  source: "entity/1",
  confidence: 0.9,
  data: {
    name: "差旅报销标准",
    department: "财务",
    effective_from: "2026-01-01",
    scores: { native: {}, rrf: 0.1, rerank: null },
  },
  citation: {
    kind: "source_span",
    revision_id: "rev-1",
    source_doc_id: "doc-1",
    source_sha256: "hash",
    quote_text: "报销标准",
    chunk_id: "chunk-1",
    page: 2,
    start_line: 4,
    end_line: 6,
    cell_ref: null,
  },
};

describe("frontline result presentation", () => {
  it("takes the title from name and summarizes arbitrary attributes", () => {
    expect(frontlineHitTitle(hit)).toBe("差旅报销标准");
    const summary = frontlineHitSummary(hit);
    expect(summary).toContain("department: 财务");
    expect(summary).toContain("effective_from: 2026-01-01");
    // 标题字段不重复进摘要
    expect(summary).not.toContain("name:");
  });

  it("prefers descriptive text over key/value pairs when present", () => {
    const described: SearchHit = {
      ...hit,
      data: { ...hit.data, description: "每人每日住宿上限 600 元" },
    };
    expect(frontlineHitSummary(described)).toContain("每人每日住宿上限 600 元");
  });

  it("echoes unknown kinds instead of collapsing them to a vague label", () => {
    expect(hitKindLabel("chunk")).toBe("文档");
    expect(hitKindLabel("page")).toBe("知识页");
    expect(hitKindLabel("policy")).toBe("policy");
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
