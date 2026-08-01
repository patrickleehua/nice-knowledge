import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { KbImageAsset, KbImageAssetAuditPage } from "@/lib/types";
import { ImagesView } from "./images-view";

const apiMocks = vi.hoisted(() => ({
  list: vi.fn(),
  detail: vi.fn(),
  audit: vi.fn(),
  review: vi.fn(),
  retry: vi.fn(),
}));

vi.mock("@/lib/kb-images", () => ({
  kbImages: apiMocks,
}));

// 筛选与分页已改为 URL 状态;组件测试不挂 app router,这里替身掉读写层。
const urlState = vi.hoisted(() => ({
  params: new URLSearchParams(),
  set: vi.fn(),
  setAll: vi.fn(),
}));

vi.mock("@/lib/use-url-state", () => ({
  useUrlState: () => ({
    get: (key: string) => urlState.params.get(key),
    getAll: (key: string) => urlState.params.getAll(key),
    set: urlState.set,
    setAll: urlState.setAll,
  }),
}));

vi.mock("@/components/kb/workbench/kb-data", () => ({
  useKbDocuments: () => ({
    data: [{ id: "document-1", filename: "皇宫开放时间图文手册.pdf" }],
    isPending: false,
    isError: false,
  }),
}));

vi.mock("@/components/kb/knowledge-media", () => ({
  KnowledgeMedia: ({ alt }: { alt: string }) => (
    <div data-testid="authenticated-knowledge-media">{alt}</div>
  ),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}));

const ASSET: KbImageAsset = {
  asset_id: "123e4567-e89b-42d3-a456-426614174000",
  kb_id: "223e4567-e89b-42d3-a456-426614174001",
  source_document_id: "document-1",
  source_filename: "皇宫开放时间图文手册.pdf",
  revision_id: "323e4567-e89b-42d3-a456-426614174002",
  revision_number: 1,
  revision_sha256: "a".repeat(64),
  ingest_run_id: null,
  source_occurrence: "picture:1",
  parser_name: "fixture",
  parser_item_ref: "picture:1",
  page: 3,
  slide: null,
  bbox: { left: 0.1, top: 0.2, right: 0.8, bottom: 0.9 },
  reading_order: 1,
  surrounding_text: "皇宫开放时间与入口指引。",
  image_sha256: "b".repeat(64),
  size_bytes: 8192,
  content_type: "image/png",
  width: 960,
  height: 540,
  thumbnail_sha256: "c".repeat(64),
  thumbnail_content_type: "image/png",
  thumbnail_size_bytes: 2048,
  thumbnail_width: 480,
  thumbnail_height: 270,
  content_url: `/api/v1/kb/image-assets/123e4567-e89b-42d3-a456-426614174000/content`,
  thumbnail_url: `/api/v1/kb/image-assets/123e4567-e89b-42d3-a456-426614174000/thumbnail`,
  ocr_text: "PALACE OPEN 09:00 - 18:00",
  caption: "蓝色信息牌展示皇宫开放时间。",
  ocr_provider: "fixture",
  ocr_model: "qa-ocr",
  caption_provider: "fixture",
  caption_model: "qa-caption",
  extraction_fingerprint: "extract-v1",
  ocr_fingerprint: "ocr-v1",
  caption_fingerprint: "caption-v1",
  config_fingerprint: "config-v1",
  enrichment_status: "succeeded",
  review_status: "needs_review",
  failure_code: null,
  reviewed_by: null,
  reviewed_at: null,
  review_source: null,
  review_note: null,
  lock_version: 1,
  usage: {
    snapshot_count: 0,
    image_chunk_count: 1,
    evidence_span_count: 0,
    business_artifact_count: 0,
    business_artifact_support: "unsupported",
  },
  created_at: "2026-07-24T10:00:00Z",
  updated_at: "2026-07-24T10:00:00Z",
};

const AUDIT: KbImageAssetAuditPage = {
  items: [
    {
      event_version: 1,
      action: "enrichment_completed",
      actor_kind: "provider",
      actor_user_id: null,
      reason: "qa fixture",
      before_state: null,
      after_state: {
        caption: ASSET.caption,
        ocr_text: ASSET.ocr_text,
        enrichment_status: "succeeded",
        review_status: "needs_review",
        failure_code: null,
        review_source: "provider",
        reviewed_by: null,
        lock_version: 1,
        image_sha256: ASSET.image_sha256,
        config_fingerprint: ASSET.config_fingerprint,
        result_fingerprint: "result-v1",
      },
      created_at: "2026-07-24T10:01:00Z",
    },
  ],
  total: 1,
  page: 1,
  page_size: 50,
};

function renderImagesView() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ImagesView kbId={ASSET.kb_id} />
    </QueryClientProvider>,
  );
}

describe("ImagesView", () => {
  test("covers inventory, detail, audit, and governed review submission", async () => {
    apiMocks.list.mockResolvedValue({
      items: [ASSET],
      total: 1,
      page: 1,
      page_size: 18,
    });
    apiMocks.detail.mockResolvedValue(ASSET);
    apiMocks.audit.mockResolvedValue(AUDIT);
    apiMocks.review.mockResolvedValue({
      ...ASSET,
      review_status: "accepted",
      lock_version: 2,
    });

    renderImagesView();

    expect(await screen.findByText(ASSET.source_filename)).toBeDefined();
    expect(screen.getAllByText(ASSET.caption as string)).toHaveLength(2);
    expect(screen.getByText("下游引用 1")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "审核" }));
    expect(
      await screen.findByRole("textbox", { name: "图片描述" }),
    ).toHaveProperty("value", ASSET.caption);
    expect(screen.getByRole("textbox", { name: "OCR 文字" })).toHaveProperty(
      "value",
      ASSET.ocr_text,
    );
    expect(await screen.findByText("qa fixture")).toBeDefined();

    const reason =
      screen.getByPlaceholderText("记录接受、修正、排除或重试原因");
    fireEvent.change(reason, { target: { value: "人工核对图片与来源一致" } });
    fireEvent.click(screen.getByRole("button", { name: "接受" }));

    await waitFor(() => {
      expect(apiMocks.review).toHaveBeenCalledWith(ASSET.asset_id, {
        action: "accept",
        expected_lock_version: 1,
        reason: "人工核对图片与来源一致",
      });
    });
  });

  test("shows bounded permission failure without exposing API detail", async () => {
    apiMocks.list.mockResolvedValue({
      items: [ASSET],
      total: 1,
      page: 1,
      page_size: 18,
    });
    apiMocks.detail.mockResolvedValue(ASSET);
    apiMocks.audit.mockRejectedValue(
      new ApiError(403, "private policy detail must not render"),
    );

    renderImagesView();
    fireEvent.click(await screen.findByRole("button", { name: "审核" }));

    expect(
      await screen.findByText("当前角色无权查看审核记录或执行审核操作。"),
    ).toBeDefined();
    expect(document.body.textContent).not.toContain("private policy detail");
  });
});
