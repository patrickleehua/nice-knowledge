import assert from "node:assert/strict";
import test from "node:test";
import {
  citationIdentityRows,
  citationLocation,
  formatSearchScore,
  primarySearchScore,
  sourceLineAnchor,
} from "./hit-metadata-utils.mjs";

test("source span identity does not invent fact or evidence ids", () => {
  assert.deepEqual(
    citationIdentityRows({
      kind: "source_span",
      source_doc_id: "source-1",
      revision_id: "revision-1",
      chunk_id: "chunk-1",
    }),
    [
      ["来源", "source-1"],
      ["修订", "revision-1"],
      ["片段", "chunk-1"],
    ],
  );
});

test("fact evidence identity includes governed ids before source ids", () => {
  assert.deepEqual(
    citationIdentityRows({
      kind: "fact_evidence",
      fact_claim_id: "fact-1",
      evidence_span_id: "evidence-1",
      source_doc_id: "source-1",
      revision_id: "revision-1",
      chunk_id: null,
    }),
    [
      ["事实", "fact-1"],
      ["证据", "evidence-1"],
      ["来源", "source-1"],
      ["修订", "revision-1"],
      ["片段", null],
    ],
  );
});

test("rerank is the final score when present", () => {
  const score = primarySearchScore({
    native: { dense: 0.876 },
    rrf: 0.0325,
    rerank: 0.7312,
  });

  assert.deepEqual(score, { label: "重排", value: 0.7312 });
  assert.equal(formatSearchScore(score), "0.731");
});

test("RRF remains visible when reranking was not applied", () => {
  const score = primarySearchScore({
    native: { sparse: 0.423 },
    rrf: 0.0325,
    rerank: null,
  });

  assert.deepEqual(score, { label: "RRF", value: 0.0325 });
  assert.equal(formatSearchScore(score), "0.0325");
});

test("citation anchors preserve page, line range, and table cell", () => {
  assert.deepEqual(
    citationLocation({
      page: 3,
      start_line: 18,
      end_line: 21,
      cell_ref: "B7",
    }),
    ["第 3 页", "行 18-21", "单元格 B7"],
  );
});

test("source line anchor prefers the complete citation range", () => {
  assert.deepEqual(
    sourceLineAnchor(
      { start_line: 18, end_line: 21 },
      { start_line: 90, end_line: 99 },
    ),
    { start: 18, end: 21 },
  );
});

test("source line anchor falls back to chunk metadata", () => {
  assert.deepEqual(
    sourceLineAnchor(
      { start_line: null, end_line: null },
      { start_line: 90, end_line: 99 },
    ),
    { start: 90, end: 99 },
  );
});

test("source line anchor stays empty when neither source is anchored", () => {
  assert.deepEqual(
    sourceLineAnchor(
      { start_line: null, end_line: null },
      { heading_path: "酒店 / 价格" },
    ),
    { start: null, end: null },
  );
});
