/**
 * @typedef {{
 *   native: Record<string, number>,
 *   rrf: number,
 *   rerank: number | null
 * }} SearchScores
 */

/**
 * @typedef {{ label: "重排" | "RRF", value: number }} PrimarySearchScore
 */

/**
 * @typedef {{
 *   page?: number | null,
 *   slide?: number | null,
 *   start_line?: number | null,
 *   end_line?: number | null,
 *   cell_ref?: string | null
 * }} CitationLocation
 */

/**
 * @param {SearchScores} scores
 * @returns {PrimarySearchScore}
 */
export function primarySearchScore(scores) {
  return scores.rerank === null
    ? { label: "RRF", value: scores.rrf }
    : { label: "重排", value: scores.rerank };
}

/**
 * @param {PrimarySearchScore} score
 */
export function formatSearchScore(score) {
  return score.value.toFixed(score.label === "RRF" ? 4 : 3);
}

/**
 * @param {CitationLocation} citation
 */
export function citationLocation(citation) {
  const location = [];
  if (typeof citation.page === "number") location.push(`第 ${citation.page} 页`);
  if (typeof citation.slide === "number")
    location.push(`第 ${citation.slide} 张幻灯片`);
  if (typeof citation.start_line === "number") {
    location.push(
      typeof citation.end_line === "number" &&
        citation.end_line !== citation.start_line
        ? `行 ${citation.start_line}-${citation.end_line}`
        : `行 ${citation.start_line}`,
    );
  }
  if (citation.cell_ref) location.push(`单元格 ${citation.cell_ref}`);
  return location;
}

/**
 * Choose one coherent line range. A citation range wins as a unit; chunk
 * metadata is used only when the citation has no start line.
 *
 * @param {{ start_line?: number | null, end_line?: number | null }} citation
 * @param {Record<string, unknown>} data
 */
export function sourceLineAnchor(citation, data) {
  if (typeof citation.start_line === "number") {
    return { start: citation.start_line, end: citation.end_line };
  }
  const start = typeof data.start_line === "number" ? data.start_line : null;
  const end = typeof data.end_line === "number" ? data.end_line : null;
  return { start, end: start === null ? null : end };
}

/**
 * @param {{
 *   kind: "source_span" | "fact_evidence" | "image_source",
 *   source_doc_id: string,
 *   revision_id: string,
 *   chunk_id?: string | null,
 *   fact_claim_id?: string,
 *   evidence_span_id?: string,
 *   asset_id?: string,
 *   image_sha256?: string
 * }} citation
 */
export function citationIdentityRows(citation) {
  const sourceRows = [
    ["来源", citation.source_doc_id],
    ["修订", citation.revision_id],
    ["片段", citation.chunk_id],
  ];
  if (citation.kind === "image_source") {
    return [
      ["图片资产", citation.asset_id],
      ["图片校验", citation.image_sha256],
      ["来源", citation.source_doc_id],
      ["修订", citation.revision_id],
    ];
  }
  return citation.kind === "fact_evidence"
    ? [
        ["事实", citation.fact_claim_id],
        ["证据", citation.evidence_span_id],
        ...sourceRows,
      ]
    : sourceRows;
}
