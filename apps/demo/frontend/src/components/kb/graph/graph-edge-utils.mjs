/** @param {{ predicate?: string, link_type?: string }} edge */
export function graphEdgePredicate(edge) {
  return edge.predicate || edge.link_type || "related";
}

/** @template T @param {T[] | T | null | undefined} evidence @returns {T[]} */
export function normalizeGraphEvidence(evidence) {
  if (Array.isArray(evidence)) return evidence;
  return evidence ? [evidence] : [];
}

/** @param {string | null} from @param {string | null} to */
export function graphValidityLabel(from, to) {
  if (!from && !to) return null;
  return `${from || "不限"} - ${to || "长期"}`;
}

/**
 * @param {{ source_filename?: string | null, page?: number | null, start_line?: number | null,
 * end_line?: number | null, cell_ref?: string | null }} evidence
 */
export function graphEvidenceLocation(evidence) {
  const parts = [];
  if (evidence.source_filename) parts.push(evidence.source_filename);
  if (evidence.page != null) parts.push(`第 ${evidence.page} 页`);
  if (evidence.start_line != null) {
    parts.push(
      evidence.end_line != null && evidence.end_line !== evidence.start_line
        ? `${evidence.start_line}-${evidence.end_line} 行`
        : `${evidence.start_line} 行`,
    );
  }
  if (evidence.cell_ref) parts.push(evidence.cell_ref);
  return parts.join(" · ") || "证据引用";
}
