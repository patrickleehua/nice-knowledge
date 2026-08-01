import { api } from "@/lib/api";
import type {
  ImageAssetIssueKind,
  ImageEnrichmentStatus,
  ImageReviewStatus,
  KbImageAsset,
  KbImageAssetAuditPage,
  KbImageAssetPage,
  KbImageRetryAccepted,
  KbImageBulkReviewRequest,
  KbImageBulkReviewResult,
  KbImageRetryRequest,
  KbImageReviewRequest,
  KnowledgeMediaReference,
} from "@/lib/types";
import {
  authenticatedMediaPath,
  isKnowledgeAssetId,
} from "@/lib/kb-images-utils.mjs";

export { authenticatedMediaPath, isKnowledgeAssetId };

export interface KbImageAssetFilters {
  page?: number;
  pageSize?: number;
  enrichmentStatus?: ImageEnrichmentStatus | null;
  reviewStatus?: ImageReviewStatus | null;
  failureCode?: string;
  documentId?: string;
  issueKind?: ImageAssetIssueKind | null;
}

export function imageAssetInventoryPath(
  kbId: string,
  filters: KbImageAssetFilters = {},
): string {
  const params = new URLSearchParams({
    kb_id: kbId,
    page: String(filters.page ?? 1),
    page_size: String(filters.pageSize ?? 20),
  });
  if (filters.enrichmentStatus)
    params.set("enrichment_status", filters.enrichmentStatus);
  if (filters.reviewStatus)
    params.set("review_status", filters.reviewStatus);
  if (filters.failureCode?.trim())
    params.set("failure_code", filters.failureCode.trim());
  if (filters.documentId?.trim())
    params.set("document_id", filters.documentId.trim());
  if (filters.issueKind) params.set("issue_kind", filters.issueKind);
  return `/kb/image-assets?${params.toString()}`;
}

export const kbImages = {
  list: (kbId: string, filters?: KbImageAssetFilters) =>
    api.get<KbImageAssetPage>(imageAssetInventoryPath(kbId, filters)),
  detail: (assetId: string) =>
    api.get<KbImageAsset>(`/kb/image-assets/${assetId}`),
  audit: (assetId: string, page = 1, pageSize = 50) =>
    api.get<KbImageAssetAuditPage>(
      `/kb/image-assets/${assetId}/audit?page=${page}&page_size=${pageSize}`,
    ),
  review: (assetId: string, body: KbImageReviewRequest) =>
    api.post<KbImageAsset>(`/kb/image-assets/${assetId}/review`, body),
  retry: (assetId: string, body: KbImageRetryRequest) =>
    api.post<KbImageRetryAccepted>(
      `/kb/image-assets/${assetId}/retry`,
      body,
    ),
  /** 批量审核:不传 asset_ids 即对整个筛选结果生效(跨页) */
  bulkReview: (kbId: string, body: KbImageBulkReviewRequest) =>
    api.post<KbImageBulkReviewResult>(
      `/kb/image-assets/bulk-review?kb_id=${encodeURIComponent(kbId)}`,
      body,
    ),
};

export function mediaReferencePath(
  media: KnowledgeMediaReference,
  variant: "thumbnail" | "content",
): string {
  return authenticatedMediaPath(
    media.asset_id,
    variant,
    variant === "thumbnail" ? media.thumbnail_url : media.content_url,
  );
}
