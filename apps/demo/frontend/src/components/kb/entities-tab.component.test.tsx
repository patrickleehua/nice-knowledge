import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { RouteKnowledgeDiagnostics, RouteEntry } from "@/lib/types";
import { EntitiesTab } from "./entities-tab";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

const urlState = vi.hoisted(() => ({
  params: new URLSearchParams("kind=route"),
  set: vi.fn(),
}));

vi.mock("@/lib/use-url-state", () => ({
  useUrlState: () => ({
    get: (key: string) => urlState.params.get(key),
    set: urlState.set,
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";

function makeDiagnostics(
  overrides: Partial<RouteKnowledgeDiagnostics> = {},
): RouteKnowledgeDiagnostics {
  return {
    kb_id: KB_ID,
    source_counts: {
      total: 1,
      active: 1,
      terminal: 1,
      by_doc_type: { general: 1 },
      by_status: { completed: 1 },
    },
    eligible_general_document_ids: ["doc-1"],
    route_claim_counts: {
      total: 0,
      suggested: 0,
      confirmed: 0,
      rejected: 0,
      orphaned: 0,
    },
    active_snapshot_id: null,
    ready_snapshot_id: null,
    active_route_count: 0,
    ready_route_count: 0,
    business_asset_count: 0,
    reason_code: "wrong_extraction_type",
    next_action: "reclassify",
    ...overrides,
  };
}

let diagnosticsPayload: RouteKnowledgeDiagnostics;
let routes: RouteEntry[];

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <EntitiesTab kbId={KB_ID} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  urlState.params = new URLSearchParams("kind=route");
  urlState.set.mockReset();
  diagnosticsPayload = makeDiagnostics();
  routes = [];
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.get.mockImplementation((path: string) => {
    if (path.startsWith("/kb/routes?")) return Promise.resolve(routes);
    if (path.startsWith("/kb/destinations?")) return Promise.resolve([]);
    if (path.startsWith("/kb/hotels?")) return Promise.resolve([]);
    if (path.startsWith("/kb/costs?")) return Promise.resolve([]);
    if (path.startsWith("/kb/pois?")) return Promise.resolve([]);
    if (path === "/kb/entity-types") return Promise.resolve([]);
    if (path === `/kb/bases/${KB_ID}/route-diagnostics`) {
      return Promise.resolve(diagnosticsPayload);
    }
    throw new Error(`Unexpected GET ${path}`);
  });
});

describe("EntitiesTab route knowledge diagnostics", () => {
  test("explains a retrieval-only mismatch and navigates to that document's reclassification", async () => {
    renderTab();

    expect(
      await screen.findByText("文档已入库，但没有按线路抽取"),
    ).toBeDefined();
    expect(screen.getByText(/不会被改写/)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "去重新抽取" }));

    expect(urlState.set).toHaveBeenCalledWith(
      {
        view: "sources",
        tab: null,
        dest: null,
        kind: null,
        reclassify: "doc-1",
      },
      { reset: ["q", "status", "sort", "doc", "start", "end"] },
    );
  });

  test("sends a ready snapshot to release management instead of suggesting another upload", async () => {
    diagnosticsPayload = makeDiagnostics({
      ready_snapshot_id: "snapshot-ready",
      ready_route_count: 2,
      reason_code: "snapshot_activation_required",
      next_action: "activate_snapshot",
    });
    renderTab();

    expect(await screen.findByText("候选快照等待发布")).toBeDefined();
    expect(screen.getByText(/2 条线路知识/)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "去发布快照" }));

    expect(urlState.set).toHaveBeenCalledWith(
      expect.objectContaining({
        view: "release",
        reclassify: null,
      }),
      expect.any(Object),
    );
    expect(screen.queryByText("上传线路文档")).toBeNull();
  });

  test("uses lifecycle-specific terminology for published knowledge", async () => {
    routes = [
      {
        id: "route-1",
        kb_id: KB_ID,
        name: "西班牙 8 日",
        days: 8,
        structure: {},
        created_at: "2026-07-01T10:00:00Z",
      },
    ];
    renderTab();

    expect(await screen.findByText("西班牙 8 日")).toBeDefined();
    expect(screen.getAllByText("线路知识").length).toBeGreaterThan(0);
    expect(screen.getByText(/提升为经营线路资产后/)).toBeDefined();
    expect(apiMocks.get).not.toHaveBeenCalledWith(
      `/kb/bases/${KB_ID}/route-diagnostics`,
    );
  });
});
