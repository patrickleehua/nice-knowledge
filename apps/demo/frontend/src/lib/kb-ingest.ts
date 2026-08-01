// 摄入耗时批量接口。成本纪律:文档列表禁止按行发 N 个 ingestion-status 请求,
// 终态行的"耗时 X"由这里一次请求整页供给。

import { api } from "@/lib/api";
import type { DocIngestDurations } from "@/lib/types";

/**
 * 一次 GET 承载的 doc_ids 上限。再多 query string 就要过万字节,
 * 会撞上服务端请求行长度限制;超过时改为不传 doc_ids(按 kb 全量,
 * 后端以最近上传的 500 份为界截断,与列表软上限对齐)。
 */
export const DOC_IDS_URL_MAX = 100;

/** GET /kb/documents/ingest-durations:不传 docIds 即按 kb 全量(上限 500) */
export function fetchDocIngestDurations(
  kbId: string,
  docIds?: string[],
): Promise<DocIngestDurations> {
  const params = new URLSearchParams({ kb_id: kbId });
  for (const id of docIds ?? []) params.append("doc_ids", id);
  return api.get<DocIngestDurations>(
    `/kb/documents/ingest-durations?${params.toString()}`,
  );
}
