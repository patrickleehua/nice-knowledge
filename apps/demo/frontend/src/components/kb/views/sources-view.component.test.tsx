import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type {
  DocumentPurgePreview,
  DocumentWithdrawalImpact,
  KbDocumentOperation,
  SourceDocument,
} from "@/lib/types";
import {
  canPreviewDocumentPurge,
  canReingestWithdrawnDocument,
  canRetryWithdrawalOperation,
  shouldPollDocumentLifecycle,
  SourcesView,
} from "./sources-view";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  postForm: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("@/lib/api", () => {
  class MockApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
    }
  }
  return {
    ApiError: MockApiError,
    api: apiMocks,
  };
});

const urlState = vi.hoisted(() => ({
  params: new URLSearchParams(),
  set: vi.fn(),
}));

vi.mock("@/lib/use-url-state", () => ({
  useUrlState: () => ({
    get: (key: string) => urlState.params.get(key),
    set: urlState.set,
  }),
}));

const authState = vi.hoisted(() => ({
  role: "org_admin",
}));

vi.mock("@/lib/auth", () => ({
  useCurrentOrg: () => ({
    id: "org-1",
    slug: "qa",
    name: "QA",
    role: authState.role,
  }),
}));

vi.mock("@/components/kb/doc-detail", () => ({
  DocDetail: () => <div>文档详情</div>,
}));

vi.mock("@/components/kb/workbench/kb-data", () => ({
  ACTIVE_STATUSES: new Set(["uploaded", "parsing"]),
  PAUSABLE_STATUSES: new Set(),
  RESUMABLE_STATUSES: new Set(),
  RETRYABLE_STATUSES: new Set(["failed", "canceled"]),
  DocStatusBadge: ({ status }: { status: string }) => (
    <span>{status === "completed" ? "已摄入" : status}</span>
  ),
  useDocIngestDurations: () => ({ data: undefined }),
  useDocumentIngestionStatus: () => ({ data: undefined }),
  useKbIngestStats: () => ({ data: undefined }),
}));

vi.mock("@/components/kb/workbench/use-virtual-rows", () => ({
  useEndReached: () => undefined,
}));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        key: index,
        start: index * 52,
        size: 48,
      })),
    getTotalSize: () => count * 52,
    measureElement: () => undefined,
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    info: vi.fn(),
    success: vi.fn(),
    warning: vi.fn(),
  },
}));

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";
const DOC_ID = "123e4567-e89b-42d3-a456-426614174000";

function makeOperation(
  overrides: Partial<KbDocumentOperation> = {},
): KbDocumentOperation {
  return {
    id: "323e4567-e89b-42d3-a456-426614174002",
    document_id: DOC_ID,
    operation_type: "withdrawal",
    status: "completed",
    phase: "settlement",
    requested_revision_id: "423e4567-e89b-42d3-a456-426614174003",
    target_snapshot_id: "523e4567-e89b-42d3-a456-426614174004",
    attempts: 1,
    reason: "qa",
    impact_summary: {},
    error_code: null,
    error_message: null,
    retryable: false,
    created_at: "2026-07-01T10:00:00Z",
    started_at: "2026-07-01T10:00:01Z",
    completed_at: "2026-07-01T10:01:00Z",
    ...overrides,
  };
}

function makeDocument(overrides: Partial<SourceDocument> = {}): SourceDocument {
  return {
    id: DOC_ID,
    kb_id: KB_ID,
    filename: "供应商资料.pdf",
    doc_type: "general",
    status: "completed",
    lifecycle_status: "withdrawn",
    legal_hold_active: false,
    legal_hold_at: null,
    latest_operation: makeOperation(),
    error: null,
    rel_path: null,
    progress: 100,
    progress_stage: null,
    progress_done: 1,
    progress_total: 1,
    expires_at: null,
    markdown_key: "kb/markdown.md",
    parser_name: "docling",
    created_at: "2026-07-01T09:00:00Z",
    ...overrides,
  };
}

const ELIGIBLE_PREVIEW: DocumentPurgePreview = {
  version: "kb-document-purge/v1",
  document_id: DOC_ID,
  plan_hash: "a".repeat(64),
  eligible: true,
  blockers: [],
  delete_counts: {
    objects: 3,
    revisions: 1,
    chunks: 8,
    media: 2,
    evidence: 4,
    fact_claims: 1,
    ingest_runs: 1,
  },
  retain_counts: {
    shared_fact_claims: 2,
    shared_entities: 1,
    shared_relations: 1,
    exclusive_entities_for_gc: 1,
    shared_object_keys: 0,
  },
  retention_deadline: "2026-07-20T10:00:00Z",
};

const BLOCKED_PREVIEW: DocumentPurgePreview = {
  ...ELIGIBLE_PREVIEW,
  plan_hash: "b".repeat(64),
  eligible: false,
  blockers: [
    {
      code: "RETENTION_PERIOD_ACTIVE",
      count: 1,
      retry_at: "2026-08-01T10:00:00Z",
    },
    {
      code: "BUSINESS_ARTIFACT_REFERENCE",
      count: 2,
    },
  ],
};

const WITHDRAWAL_IMPACT: DocumentWithdrawalImpact = {
  document_id: DOC_ID,
  revision_count: 2,
  chunk_count: 8,
  image_count: 3,
  exclusive_fact_count: 4,
  shared_fact_count: 5,
  orphaned_fact_count: 1,
  exclusive_entity_count: 2,
  shared_entity_count: 3,
  exclusive_relation_count: 1,
  shared_relation_count: 2,
};

let documents: SourceDocument[];
let purgePreviews: DocumentPurgePreview[];
let purgePreviewCallCount: number;

function renderSourcesView() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <SourcesView kbId={KB_ID} />
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

async function openDocumentMenu(filename = "供应商资料.pdf") {
  await screen.findByText(filename);
  fireEvent.click(
    screen.getByRole("button", { name: `${filename} 的更多操作` }),
  );
}

async function openPurgePreview() {
  await openDocumentMenu();
  fireEvent.click(await screen.findByText("永久清理"));
  await screen.findByRole("heading", { name: "永久清理文档" });
}

beforeEach(() => {
  authState.role = "org_admin";
  urlState.params = new URLSearchParams();
  urlState.set.mockReset();
  documents = [makeDocument()];
  purgePreviews = [ELIGIBLE_PREVIEW];
  purgePreviewCallCount = 0;
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.get.mockImplementation((path: string) => {
    if (path.startsWith("/kb/documents?")) return Promise.resolve(documents);
    if (path === "/kb/ingest/settings") {
      return Promise.resolve({
        max_concurrency: 2,
        active: 0,
        upload_max_file_bytes: 20 * 1024 * 1024,
        upload_max_batch_files: 20,
      });
    }
    if (path === "/kb/entity-types") {
      return Promise.resolve([
        {
          id: "entity-type-1",
          type_key: "travel_policy",
          display_name: "行程政策",
          description: null,
          field_schema: {
            type: "object",
            properties: { name: { type: "string" } },
            required: ["name"],
          },
          filterable_fields: [],
          card_template: null,
          review_policy: "human",
          is_builtin: false,
          is_own: true,
        },
      ]);
    }
    if (path === `/kb/documents/${DOC_ID}/purge-preview`) {
      const preview =
        purgePreviews[
          Math.min(purgePreviewCallCount, purgePreviews.length - 1)
        ];
      purgePreviewCallCount += 1;
      return Promise.resolve(preview);
    }
    if (path === `/kb/documents/${DOC_ID}/withdrawal-impact`) {
      return Promise.resolve(WITHDRAWAL_IMPACT);
    }
    throw new Error(`Unexpected GET ${path}`);
  });
});

describe("SourcesView staged ingestion workflow", () => {
  test("uploads without a document type and does not enqueue automatically", async () => {
    documents = [];
    apiMocks.postForm.mockResolvedValue(
      makeDocument({
        filename: "线路.docx",
        doc_type: "unclassified",
        status: "staged",
        lifecycle_status: "active",
        latest_operation: null,
      }),
    );
    const { container } = renderSourcesView();
    await screen.findByText("还没有文档");

    const uploadInput = container.querySelector<HTMLInputElement>(
      'input[type="file"][multiple]:not([webkitdirectory])',
    );
    expect(uploadInput).not.toBeNull();
    fireEvent.change(uploadInput!, {
      target: {
        files: [
          new File(["route"], "线路.docx", {
            type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          }),
        ],
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: "开始上传" }));

    await waitFor(() => expect(apiMocks.postForm).toHaveBeenCalledTimes(1));
    const [uploadPath] = apiMocks.postForm.mock.calls[0];
    expect(uploadPath).toContain(`/kb/documents?kb_id=${KB_ID}`);
    expect(uploadPath).not.toContain("doc_type");
    expect(apiMocks.post).not.toHaveBeenCalledWith(
      "/kb/documents/ingestion-queue",
      expect.anything(),
    );
  });

  test("persists batch and row classifications before explicitly enqueueing", async () => {
    const secondId = "123e4567-e89b-42d3-a456-426614174099";
    documents = [
      makeDocument({
        filename: "华东线路.docx",
        doc_type: "unclassified",
        status: "staged",
        lifecycle_status: "active",
        latest_operation: null,
      }),
      makeDocument({
        id: secondId,
        filename: "行程规则.docx",
        doc_type: "unclassified",
        status: "staged",
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    apiMocks.post.mockImplementation(
      (
        path: string,
        body?: { items?: { document_id: string; doc_type?: string }[] },
      ) => {
        if (path === "/kb/documents/classifications") {
          const items = body?.items ?? [];
          for (const item of items) {
            documents = documents.map((document) =>
              document.id === item.document_id
                ? { ...document, doc_type: item.doc_type ?? document.doc_type }
                : document,
            );
          }
          return Promise.resolve({ updated: items, skipped: [] });
        }
        if (path === "/kb/documents/ingestion-queue") {
          return Promise.resolve({
            queued: (body?.items ?? []).map((item) => ({
              document_id: item.document_id,
              doc_type:
                documents.find((document) => document.id === item.document_id)
                  ?.doc_type ?? "unclassified",
              revision_id: `revision-${item.document_id}`,
              run_id: `run-${item.document_id}`,
              run_status: "queued",
              idempotent: false,
            })),
            skipped: [],
          });
        }
        throw new Error(`Unexpected POST ${path}`);
      },
    );
    renderSourcesView();

    await screen.findByText("华东线路.docx");
    fireEvent.click(screen.getByRole("checkbox", { name: "全选待处理文件" }));
    fireEvent.click(screen.getByRole("combobox", { name: "批量文档类型" }));
    const routeTypeOption = await screen.findByRole("option", {
      name: "成熟线路",
    });
    fireEvent.pointerDown(routeTypeOption);
    fireEvent.pointerUp(routeTypeOption);
    fireEvent.click(routeTypeOption);

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        "/kb/documents/classifications",
        {
          items: [
            { document_id: DOC_ID, doc_type: "route_template" },
            { document_id: secondId, doc_type: "route_template" },
          ],
        },
      );
    });

    const secondTypeSelect = await screen.findByRole("combobox", {
      name: "设置 行程规则.docx 的文档类型",
    });
    await waitFor(() =>
      expect(secondTypeSelect).toHaveProperty("disabled", false),
    );
    fireEvent.click(secondTypeSelect);
    const customTypeOption = await screen.findByRole("option", {
      name: "行程政策",
    });
    fireEvent.pointerDown(customTypeOption);
    fireEvent.pointerUp(customTypeOption);
    fireEvent.click(customTypeOption);

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenLastCalledWith(
        "/kb/documents/classifications",
        {
          items: [{ document_id: secondId, doc_type: "travel_policy" }],
        },
      );
    });
    const enqueueButton = screen.getByRole("button", {
      name: "排入解析队列",
    });
    await waitFor(() =>
      expect(enqueueButton).toHaveProperty("disabled", false),
    );
    fireEvent.click(enqueueButton);

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenLastCalledWith(
        "/kb/documents/ingestion-queue",
        {
          items: [{ document_id: DOC_ID }, { document_id: secondId }],
        },
      );
    });
  });

  test("keeps staged actions isolated while showing queued and history sources", async () => {
    documents = [
      makeDocument({
        filename: "待分类.docx",
        doc_type: "unclassified",
        status: "staged",
        lifecycle_status: "active",
        latest_operation: null,
      }),
      makeDocument({
        id: "doc-queued",
        filename: "等待解析.docx",
        status: "uploaded",
        lifecycle_status: "active",
        latest_operation: null,
      }),
      makeDocument({
        id: "doc-history",
        filename: "已完成.docx",
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    renderSourcesView();

    expect(
      await screen.findByRole("button", { name: "上传文件" }),
    ).toBeDefined();
    expect(await screen.findByText("待分类.docx")).toBeDefined();
    expect(screen.getByRole("heading", { name: /待处理/ })).toBeDefined();
    expect(screen.queryByText("解析队列")).toBeNull();
    expect(screen.queryByText("历史记录")).toBeNull();
    expect(screen.getByText("等待解析.docx")).toBeDefined();
    expect(screen.getByText("已完成.docx")).toBeDefined();
    expect(
      screen.queryByRole("checkbox", {
        name: "选择待处理文件 等待解析.docx",
      }),
    ).toBeNull();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "选择待处理文件 待分类.docx",
      }),
    );
    expect(screen.getByRole("button", { name: "排入解析队列" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  test("restores a persisted staged classification after refresh", async () => {
    documents = [
      makeDocument({
        filename: "已分类线路.docx",
        doc_type: "route_template",
        status: "staged",
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    renderSourcesView();

    const typeSelect = await screen.findByRole("combobox", {
      name: "设置 已分类线路.docx 的文档类型",
    });
    expect(typeSelect.textContent).toContain("成熟线路");
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "选择待处理文件 已分类线路.docx",
      }),
    );
    expect(screen.getByRole("button", { name: "排入解析队列" })).toHaveProperty(
      "disabled",
      false,
    );
  });
});

describe("SourcesView route extraction workflow", () => {
  test("queues route reclassification from a completed general document", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    apiMocks.post.mockResolvedValue({
      document_id: DOC_ID,
      revision_id: "revision-1",
      previous_doc_type: "general",
      target_doc_type: "route_template",
      run_id: "run-1",
      status: "queued",
      error: null,
      retryable: false,
      created_at: "2026-07-01T10:00:00Z",
      started_at: null,
      finished_at: null,
    });
    renderSourcesView();

    await openDocumentMenu();
    fireEvent.click(await screen.findByText("抽取为线路知识"));
    expect(
      await screen.findByRole("heading", { name: "抽取为线路知识？" }),
    ).toBeDefined();
    expect(
      screen.getByText(/不会重新上传、改写源文件或改变当前已发布快照/),
    ).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "开始抽取" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/kb/documents/${DOC_ID}/reclassify`,
        { target_doc_type: "route_template" },
      );
    });
  });

  test("opens the eligible document from a diagnostic recovery deep link", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    urlState.params = new URLSearchParams(`reclassify=${DOC_ID}`);
    renderSourcesView();

    expect(
      await screen.findByRole("heading", { name: "抽取为线路知识？" }),
    ).toBeDefined();
    expect(urlState.set).toHaveBeenCalledWith({ reclassify: null });
  });

  test("shows queued and failed typed-extraction state on the document row", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "active",
        latest_operation: null,
        latest_reclassification: {
          run_id: "run-1",
          revision_id: "revision-1",
          previous_doc_type: "general",
          target_doc_type: "route_template",
          status: "queued",
          error: null,
          retryable: false,
          created_at: "2026-07-01T10:00:00Z",
          started_at: null,
          finished_at: null,
        },
      }),
      makeDocument({
        id: "doc-failed",
        filename: "失败线路.docx",
        lifecycle_status: "active",
        latest_operation: null,
        latest_reclassification: {
          run_id: "run-2",
          revision_id: "revision-2",
          previous_doc_type: "general",
          target_doc_type: "route_template",
          status: "failed",
          error: "结构化输出不符合约束",
          retryable: true,
          created_at: "2026-07-01T10:00:00Z",
          started_at: "2026-07-01T10:00:01Z",
          finished_at: "2026-07-01T10:00:02Z",
        },
      }),
    ];
    renderSourcesView();

    expect(await screen.findByText("线路知识抽取已排队")).toBeDefined();
    expect(
      await screen.findByText(/线路知识抽取失败：结构化输出不符合约束/),
    ).toBeDefined();
  });
});

describe("SourcesView document lifecycle", () => {
  test("lets lifecycle state override a stale completed ingestion badge", async () => {
    renderSourcesView();

    expect(await screen.findByText("已撤回")).toBeDefined();
    expect(screen.queryByText("已摄入")).toBeNull();
  });

  test("confirms withdrawn reingestion and refreshes dependent caches", async () => {
    apiMocks.post.mockResolvedValue(
      makeDocument({
        status: "uploaded",
        latest_operation: makeOperation({
          operation_type: "reingestion",
          status: "pending",
          phase: "ingestion",
        }),
      }),
    );
    const { queryClient } = renderSourcesView();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await openDocumentMenu();
    fireEvent.click(await screen.findByText("重新摄入"));

    expect(
      await screen.findByRole("heading", { name: "重新摄入这份文档？" }),
    ).toBeDefined();
    expect(
      screen.getByText(/新修订成功发布前，文档仍保持不可检索/),
    ).toBeDefined();
    expect(apiMocks.post).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认重新摄入" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/kb/documents/${DOC_ID}/reingest`,
      );
    });
    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["kb-docs", KB_ID],
      });
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["kb-doc-revisions", DOC_ID],
      });
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["kb-fact-claims", KB_ID],
      });
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["kb-snapshots", KB_ID],
      });
    });
  });

  test("keeps the reingestion confirmation open and disabled while submitting", async () => {
    apiMocks.post.mockReturnValue(new Promise(() => undefined));
    renderSourcesView();

    await openDocumentMenu();
    fireEvent.click(await screen.findByText("重新摄入"));
    fireEvent.click(screen.getByRole("button", { name: "确认重新摄入" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "确认重新摄入" }),
      ).toHaveProperty("disabled", true);
    });
    expect(
      screen.getByRole("heading", { name: "重新摄入这份文档？" }),
    ).toBeDefined();
  });

  test("uses explicit reingestion instead of row retry for withdrawn failures", async () => {
    documents = [
      makeDocument({
        status: "failed",
        error: "重新摄入解析失败",
      }),
    ];
    renderSourcesView();

    await screen.findByText("供应商资料.pdf");
    expect(
      screen.queryByRole("button", { name: "重试 供应商资料.pdf" }),
    ).toBeNull();
    await openDocumentMenu();
    expect(await screen.findByText("重新摄入")).toBeDefined();
  });

  test("keeps row retry for active failed documents", async () => {
    documents = [
      makeDocument({
        status: "failed",
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    renderSourcesView();

    expect(
      await screen.findByRole("button", { name: "重试 供应商资料.pdf" }),
    ).toBeDefined();
    await openDocumentMenu();
    expect(screen.queryByText("重新摄入")).toBeNull();
  });

  test("renders and polls persisted reingestion publication state", async () => {
    const reingesting = makeDocument({
      lifecycle_status: "reingestion_pending",
      latest_operation: makeOperation({
        operation_type: "reingestion",
        status: "processing",
        phase: "snapshot_activation",
      }),
    });
    documents = [reingesting];
    renderSourcesView();

    expect(await screen.findByText("重新摄入中")).toBeDefined();
    expect(screen.getByText("正在激活知识快照")).toBeDefined();
    expect(screen.queryByText("已摄入")).toBeNull();
    expect(shouldPollDocumentLifecycle(reingesting)).toBe(true);
  });

  test("keeps live ingestion state visible while lifecycle is fail-closed", async () => {
    documents = [
      makeDocument({
        status: "parsing",
        lifecycle_status: "reingestion_pending",
        latest_operation: makeOperation({
          operation_type: "reingestion",
          status: "processing",
          phase: "ingestion",
        }),
      }),
    ];
    renderSourcesView();

    expect(await screen.findByText("parsing")).toBeDefined();
    expect(screen.getByText("重新摄入中")).toBeDefined();
    expect(screen.getByText("正在重新摄入")).toBeDefined();
  });

  test("labels a failed reingestion operation without calling it withdrawal", async () => {
    documents = [
      makeDocument({
        status: "failed",
        latest_operation: makeOperation({
          operation_type: "reingestion",
          status: "failed",
          phase: "ingestion",
          error_message: "parser unavailable",
          retryable: true,
        }),
      }),
    ];
    renderSourcesView();

    expect(
      await screen.findByText("重新摄入失败：parser unavailable"),
    ).toBeDefined();
    expect(screen.queryByText(/撤回发布失败/)).toBeNull();
  });

  test("shows withdrawal impact before confirmation", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "active",
        latest_operation: null,
      }),
    ];
    renderSourcesView();

    await openDocumentMenu();
    fireEvent.click(await screen.findByText("撤回文档"));

    expect(await screen.findByText("涉及修订 2")).toBeDefined();
    expect(screen.getByText("将隔离切片 8")).toBeDefined();
    expect(screen.getByText("将隔离图片 3")).toBeDefined();
    expect(screen.getByText("待复核事实 1")).toBeDefined();
    expect(apiMocks.delete).not.toHaveBeenCalled();
  });

  test("renders purge blockers and never submits an ineligible plan", async () => {
    purgePreviews = [BLOCKED_PREVIEW];
    renderSourcesView();

    await openPurgePreview();

    expect(await screen.findByText("当前不可永久清理")).toBeDefined();
    expect(screen.getByText(/仍在审计保留期内：1 项/)).toBeDefined();
    expect(screen.getByText(/业务产物仍在引用：2 项/)).toBeDefined();
    expect(screen.getByText("共享实体：1")).toBeDefined();
    expect(screen.getByText("共享关系：1")).toBeDefined();
    expect(screen.getByRole("button", { name: "确认永久清理" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(apiMocks.post).not.toHaveBeenCalled();
  });

  test("force purge skips bypassable blockers and sends force=true", async () => {
    // BLOCKED_PREVIEW 的两类拦截(保留期 + 业务产物引用)都在可跳过清单内
    purgePreviews = [BLOCKED_PREVIEW];
    apiMocks.post.mockResolvedValue(
      makeOperation({
        operation_type: "purge",
        status: "pending",
        phase: "planned",
      }),
    );
    renderSourcesView();
    await openPurgePreview();

    expect(await screen.findByText("当前不可永久清理")).toBeDefined();
    // 勾选跳过后理由与不可逆确认区才出现,提交按钮改为「强制永久清理」
    fireEvent.click(
      screen.getByRole("checkbox", { name: /跳过以上 2 类拦截立即清理/ }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "永久清理理由" }), {
      target: { value: "业务确认可删" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: /我确认该操作不可逆/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "强制永久清理" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/kb/documents/${DOC_ID}/purge`,
        {
          expected_plan_hash: BLOCKED_PREVIEW.plan_hash,
          reason: "业务确认可删",
          confirm_irreversible: true,
          force: true,
        },
      );
    });
  });

  test("submits an eligible purge with the exact confirmation body", async () => {
    apiMocks.post.mockResolvedValue(
      makeOperation({
        operation_type: "purge",
        status: "pending",
        phase: "planned",
      }),
    );
    renderSourcesView();
    await openPurgePreview();

    fireEvent.change(screen.getByRole("textbox", { name: "永久清理理由" }), {
      target: { value: "  合规保留期结束  " },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /我确认该操作不可逆/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认永久清理" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/kb/documents/${DOC_ID}/purge`,
        {
          expected_plan_hash: ELIGIBLE_PREVIEW.plan_hash,
          reason: "合规保留期结束",
          confirm_irreversible: true,
          // 常规清理不强制:预检已 eligible,无需跳过任何拦截
          force: false,
        },
      );
    });
  });

  test("re-previews after a failed purge and never uses withdrawal retry", async () => {
    purgePreviews = [
      ELIGIBLE_PREVIEW,
      {
        ...BLOCKED_PREVIEW,
        plan_hash: "c".repeat(64),
      },
    ];
    apiMocks.post.mockRejectedValueOnce(
      new ApiError(500, "worker unavailable"),
    );
    renderSourcesView();
    await openPurgePreview();

    fireEvent.change(screen.getByRole("textbox", { name: "永久清理理由" }), {
      target: { value: "合规清理" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /我确认该操作不可逆/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认永久清理" }));

    await waitFor(() => expect(purgePreviewCallCount).toBe(2));
    expect(await screen.findByText("当前不可永久清理")).toBeDefined();
    expect(
      apiMocks.post.mock.calls.some(([path]) =>
        String(path).includes("/document-operations/"),
      ),
    ).toBe(false);
  });

  test("requires a fresh confirmation and uses the new plan hash after failure", async () => {
    const refreshedPreview = {
      ...ELIGIBLE_PREVIEW,
      plan_hash: "d".repeat(64),
    };
    purgePreviews = [ELIGIBLE_PREVIEW, refreshedPreview];
    apiMocks.post
      .mockRejectedValueOnce(new ApiError(500, "worker unavailable"))
      .mockResolvedValueOnce(
        makeOperation({
          operation_type: "purge",
          status: "pending",
          phase: "planned",
        }),
      );
    renderSourcesView();
    await openPurgePreview();

    fireEvent.change(screen.getByRole("textbox", { name: "永久清理理由" }), {
      target: { value: "合规清理" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /我确认该操作不可逆/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认永久清理" }));

    await waitFor(() => expect(purgePreviewCallCount).toBe(2));
    expect(
      screen
        .getByRole("checkbox", {
          name: /我确认该操作不可逆/,
        })
        .getAttribute("aria-checked"),
    ).toBe("false");

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /我确认该操作不可逆/,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认永久清理" }));

    await waitFor(() => expect(apiMocks.post).toHaveBeenCalledTimes(2));
    expect(apiMocks.post).toHaveBeenLastCalledWith(
      `/kb/documents/${DOC_ID}/purge`,
      {
        expected_plan_hash: refreshedPreview.plan_hash,
        reason: "合规清理",
        confirm_irreversible: true,
        force: false,
      },
    );
  });

  test("renders persisted processing state", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "withdrawal_pending",
        latest_operation: makeOperation({
          status: "processing",
          phase: "snapshot_rebuild",
        }),
      }),
    ];
    renderSourcesView();

    expect(await screen.findByText("正在重建知识快照")).toBeDefined();
  });

  test("retries only a retryable failed withdrawal", async () => {
    const failedWithdrawal = makeOperation({
      status: "failed",
      phase: "snapshot_build",
      error_message: "snapshot build failed",
      retryable: true,
    });
    documents = [
      makeDocument({
        lifecycle_status: "withdrawal_pending",
        latest_operation: failedWithdrawal,
      }),
    ];
    apiMocks.post.mockResolvedValue({
      ...failedWithdrawal,
      status: "pending",
    });
    renderSourcesView();

    expect(await screen.findByText(/撤回发布失败/)).toBeDefined();
    await openDocumentMenu();
    fireEvent.click(await screen.findByText("重试撤回发布"));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/kb/document-operations/${failedWithdrawal.id}/retry`,
      );
    });
  });

  test("renders dead-letter distinctly and hides a non-retryable retry", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "withdrawal_pending",
        latest_operation: makeOperation({
          status: "dead_letter",
          phase: "dead_letter",
          error_message: "snapshot publication exhausted retries",
          retryable: false,
        }),
      }),
    ];
    renderSourcesView();

    expect(await screen.findByText(/撤回发布已进入死信/)).toBeDefined();
    await openDocumentMenu();
    await screen.findByText("查看图片");
    expect(screen.queryByText("重试撤回发布")).toBeNull();
  });

  test("does not offer permanent purge to a non-admin", async () => {
    authState.role = "operator";
    renderSourcesView();

    await openDocumentMenu();
    expect(screen.queryByText("永久清理")).toBeNull();
  });

  test("keeps every destructive operation disabled after a document is purged", async () => {
    documents = [
      makeDocument({
        lifecycle_status: "purged",
        latest_operation: makeOperation({
          operation_type: "purge",
          status: "completed",
        }),
      }),
    ];
    renderSourcesView();
    await openDocumentMenu();

    const purgedItems = await screen.findAllByRole("menuitem", {
      name: "已永久清理",
    });
    expect(purgedItems).toHaveLength(2);
    expect(
      purgedItems.every(
        (item) =>
          item.getAttribute("aria-disabled") === "true" ||
          item.hasAttribute("disabled"),
      ),
    ).toBe(true);
    expect(apiMocks.get).not.toHaveBeenCalledWith(
      `/kb/documents/${DOC_ID}/purge-preview`,
    );
  });

  test("routes a failed purge through a new preview instead of operation retry", async () => {
    documents = [
      makeDocument({
        latest_operation: makeOperation({
          operation_type: "purge",
          status: "failed",
          phase: "verification",
          retryable: true,
        }),
      }),
    ];
    renderSourcesView();
    await openDocumentMenu();

    fireEvent.click(await screen.findByText("重新预检并永久清理"));
    await screen.findByRole("heading", { name: "永久清理文档" });
    expect(apiMocks.get).toHaveBeenCalledWith(
      `/kb/documents/${DOC_ID}/purge-preview`,
    );
    expect(apiMocks.post).not.toHaveBeenCalledWith(
      expect.stringContaining("/document-operations/"),
    );
  });
});

describe("document lifecycle helpers", () => {
  test("polls resumable operations and gates retries and purge entry", () => {
    const pending = makeDocument({
      lifecycle_status: "withdrawal_pending",
      latest_operation: makeOperation({
        status: "processing",
        phase: "snapshot_rebuild",
      }),
    });
    const retryable = makeDocument({
      lifecycle_status: "withdrawal_pending",
      latest_operation: makeOperation({
        status: "dead_letter",
        retryable: true,
      }),
    });
    const purged = makeDocument({ lifecycle_status: "purged" });

    expect(shouldPollDocumentLifecycle(pending)).toBe(true);
    expect(shouldPollDocumentLifecycle(retryable)).toBe(false);
    expect(canRetryWithdrawalOperation(retryable)).toBe(true);
    expect(canPreviewDocumentPurge(makeDocument())).toBe(true);
    expect(canPreviewDocumentPurge(purged)).toBe(false);
    expect(canReingestWithdrawnDocument(makeDocument())).toBe(true);
    expect(
      canReingestWithdrawnDocument(
        makeDocument({
          lifecycle_status: "active",
          latest_operation: null,
        }),
      ),
    ).toBe(false);
    expect(
      canReingestWithdrawnDocument(
        makeDocument({
          lifecycle_status: "withdrawn",
          status: "parsing",
        }),
      ),
    ).toBe(false);
  });
});
