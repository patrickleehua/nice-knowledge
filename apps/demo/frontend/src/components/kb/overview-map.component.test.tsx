import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { KnowledgeBase } from "@/lib/types";
import { KbOverviewMap } from "./overview-map";

const KB_ID = "123e4567-e89b-42d3-a456-426614174000";

// insights / snapshots 走 api.get,按 URL 路由返回
const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, get: apiMocks.get },
  };
});

const kbImagesMocks = vi.hoisted(() => ({
  list: vi.fn(),
}));

vi.mock("@/lib/kb-images", () => ({
  kbImages: kbImagesMocks,
}));

// 文档 / 待审事实 / wiki 页三个共享 hook 直接替身,免去轮询与网络层
const hookData = vi.hoisted(() => ({
  docs: undefined as unknown[] | undefined,
  pending: undefined as unknown[] | undefined,
  pages: undefined as unknown[] | undefined,
}));

vi.mock("@/components/kb/workbench/kb-data", () => ({
  KB_DOCUMENTS_SOFT_LIMIT: 500,
  useKbDocuments: () => ({ data: hookData.docs }),
  usePendingExtractions: () => ({ data: hookData.pending }),
}));

vi.mock("@/components/kb/wiki/data", () => ({
  useKbPages: () => ({ data: hookData.pages }),
}));

function makeKb(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: KB_ID,
    org_id: "223e4567-e89b-42d3-a456-426614174001",
    name: "东京地接资料库",
    kb_type: "mixed",
    description: null,
    ingest_profile: null,
    active_snapshot_id: null,
    created_at: "2026-07-29T10:00:00Z",
    ...overrides,
  };
}

function renderMap(props: { kb?: KnowledgeBase; onNavigate?: () => void } = {}) {
  const onNavigate = props.onNavigate ?? vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <KbOverviewMap
        kbId={KB_ID}
        kb={props.kb ?? makeKb()}
        onNavigate={onNavigate}
      />
    </QueryClientProvider>,
  );
  return { onNavigate };
}

beforeEach(() => {
  vi.clearAllMocks();
  hookData.docs = [
    { id: "doc-1", status: "completed" },
    { id: "doc-2", status: "completed" },
    { id: "doc-3", status: "parsing" },
  ];
  hookData.pending = [{ id: "claim-1" }, { id: "claim-2" }];
  hookData.pages = [{ id: "page-1" }, { id: "page-2" }, { id: "page-3" }];
  apiMocks.get.mockImplementation((url: string) => {
    if (url.includes("/insights")) {
      return Promise.resolve({
        node_count: 42,
        edge_count: 17,
        isolated_count: 0,
        isolated_nodes: [],
        avg_degree: 1.2,
        communities: [],
        sparse_community_count: 0,
      });
    }
    if (url.includes("/snapshots")) {
      return Promise.resolve([{ id: "snap-1", status: "ready" }]);
    }
    return Promise.reject(new Error(`unexpected url: ${url}`));
  });
  kbImagesMocks.list.mockResolvedValue({
    items: [],
    total: 5,
    page: 1,
    page_size: 1,
  });
});

describe("KbOverviewMap", () => {
  test("渲染流水线全部 7 个节点并带上计数徽章", async () => {
    renderMap();

    // 同步计数(替身 hook 直出)
    expect(screen.getByRole("button", { name: "资料，3 份文档" })).toBeDefined();
    expect(
      screen.getByRole("button", { name: "审核，2 条待审核" }),
    ).toBeDefined();
    expect(screen.getByRole("button", { name: "Wiki，3 个页面" })).toBeDefined();

    // 异步计数(insights / 待审图片 / 快照)
    expect(
      await screen.findByRole("button", { name: "实体，42 个实体节点" }),
    ).toBeDefined();
    expect(
      await screen.findByRole("button", { name: "图谱，17 条关系" }),
    ).toBeDefined();
    expect(
      await screen.findByRole("button", { name: "图片，5 张待审核" }),
    ).toBeDefined();
    expect(
      await screen.findByRole("button", { name: "发布，1 个待激活" }),
    ).toBeDefined();
  });

  test("点击节点触发 onNavigate 且视图参数正确", async () => {
    const { onNavigate } = renderMap();

    fireEvent.click(screen.getByRole("button", { name: "资料，3 份文档" }));
    expect(onNavigate).toHaveBeenLastCalledWith("sources");

    fireEvent.click(screen.getByRole("button", { name: "审核，2 条待审核" }));
    expect(onNavigate).toHaveBeenLastCalledWith("review");

    fireEvent.click(
      await screen.findByRole("button", { name: "发布，1 个待激活" }),
    );
    expect(onNavigate).toHaveBeenLastCalledWith("release");
    expect(onNavigate).toHaveBeenCalledTimes(3);
  });

  test("待审核 > 0 时审核节点以 warning 呈现,清零后恢复", () => {
    renderMap();
    expect(
      screen.getByRole("button", { name: "审核，2 条待审核" }).className,
    ).toContain("border-warning");

    hookData.pending = [];
    renderMap();
    expect(
      screen.getByRole("button", { name: "审核，0 条待审核" }).className,
    ).not.toContain("border-warning");
  });

  test("空库时仅「资料」可点并高亮,其余节点禁用", () => {
    hookData.docs = [];
    hookData.pending = [];
    hookData.pages = [];
    const { onNavigate } = renderMap();

    const sources = screen.getByRole("button", { name: "资料，0 份文档" });
    expect((sources as HTMLButtonElement).disabled).toBe(false);
    expect(sources.className).toContain("border-primary");

    const wiki = screen.getByRole("button", { name: "Wiki，0 个页面" });
    expect((wiki as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(wiki);
    expect(onNavigate).not.toHaveBeenCalled();

    fireEvent.click(sources);
    expect(onNavigate).toHaveBeenCalledWith("sources");
  });
});
