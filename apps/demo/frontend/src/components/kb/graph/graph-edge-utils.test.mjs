import assert from "node:assert/strict";
import test from "node:test";
import {
  graphEdgePredicate,
  graphEvidenceLocation,
  graphValidityLabel,
  normalizeGraphEvidence,
} from "./graph-edge-utils.mjs";

test("typed graph predicate wins while legacy link_type remains compatible", () => {
  assert.equal(
    graphEdgePredicate({ predicate: "located_in", link_type: "related" }),
    "located_in",
  );
  assert.equal(graphEdgePredicate({ link_type: "near" }), "near");
  assert.equal(graphEdgePredicate({}), "related");
});

test("graph evidence normalizes singular and list contracts", () => {
  const evidence = { evidence_span_id: "e-1" };
  assert.deepEqual(normalizeGraphEvidence(evidence), [evidence]);
  assert.deepEqual(normalizeGraphEvidence([evidence]), [evidence]);
  assert.deepEqual(normalizeGraphEvidence(null), []);
});

test("graph validity and evidence anchors stay explicit", () => {
  assert.equal(graphValidityLabel("2026-01-01", null), "2026-01-01 - 长期");
  assert.equal(graphValidityLabel(null, null), null);
  assert.equal(
    graphEvidenceLocation({
      source_filename: "巴黎指南.pdf",
      page: 3,
      start_line: 10,
      end_line: 12,
      cell_ref: "B4",
    }),
    "巴黎指南.pdf · 第 3 页 · 10-12 行 · B4",
  );
});
