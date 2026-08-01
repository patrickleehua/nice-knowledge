import {
  focusManager,
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import {
  KB_STATUS_BOARD_QUERY_KEY,
  type KbStatusBoardDocumentCounts,
  type KbStatusBoardItem,
  type KbStatusBoardResponse,
} from "@/lib/kb-status-board";
import type { KnowledgeBase } from "@/lib/types";
import KbListPage from "./page";

const OWNER_ORG_ID = "123e4567-e89b-42d3-a456-426614174000";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

const lifecycleMocks = vi.hoisted(() => ({
  list: vi.fn(),
  restore: vi.fn(),
  board: vi.fn(),
}));

const authState = vi.hoisted(() => ({
  org: {
    id: "123e4567-e89b-42d3-a456-426614174000",
    slug: "owner",
    name: "Owner",
    role: "org_admin",
  },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: apiMocks,
  };
});

vi.mock("@/lib/kb-lifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-lifecycle")>();
  return {
    ...actual,
    kbLifecycleApi: lifecycleMocks,
  };
});

vi.mock("@/lib/auth", () => ({
  useCurrentOrg: () => authState.org,
}));

vi.mock("@/components/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/shared")>();
  return {
    ...actual,
    PageHeader: ({ actions }: { actions?: ReactNode }) => <div>{actions}</div>,
  };
});

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

function makeKb(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: "kb-1",
    org_id: OWNER_ORG_ID,
    name: "活动资料库",
    kb_type: "mixed",
    description: null,
    ingest_profile: null,
    active_snapshot_id: null,
    lifecycle_status: "active",
    created_at: "2026-07-29T10:00:00Z",
    ...overrides,
  };
}

const ACTIVE_BASES = [
  makeKb(),
  makeKb({
    id: "kb-doc",
    name: "文档语料库",
    kb_type: "document",
  }),
  makeKb({
    id: "kb-purged",
    name: "已清理资料库",
    lifecycle_status: "purged",
  }),
];

const ARCHIVED_BASES = [
  makeKb({
    id: "kb-archived",
    name: "自有归档库",
    lifecycle_status: "archived",
  }),
  makeKb({
    id: "kb-shared",
    org_id: "another-org",
    name: "共享归档库",
    lifecycle_status: "archived",
  }),
];

const EMPTY_COUNTS: KbStatusBoardDocumentCounts = {
  total: 0,
  ingested: 0,
  remaining: 0,
  staged: 0,
  queued: 0,
  running: 0,
  awaiting_review: 0,
  completed: 0,
  failed: 0,
  paused: 0,
  canceled: 0,
};

function makeStatus(
  kbId: string,
  overrides: Partial<KbStatusBoardItem> = {},
): KbStatusBoardItem {
  return {
    kb_id: kbId,
    primary_state: "empty",
    alerts: [],
    document_counts: EMPTY_COUNTS,
    stages: [],
    review: {
      suggested_facts: 0,
      orphaned_facts: 0,
      images_needing_review: 0,
      total: 0,
    },
    release: {
      state: "unpublished",
      active_snapshot_id: null,
      candidate_snapshot_id: null,
    },
    operation: null,
    latest_activity_at: null,
    ...overrides,
  };
}

function makeBoard(
  items: KbStatusBoardItem[],
  overrides: Partial<KbStatusBoardResponse> = {},
): KbStatusBoardResponse {
  return {
    generated_at: "2026-07-30T08:00:00Z",
    poll_after_ms: 30_000,
    has_active_work: false,
    items,
    ...overrides,
  };
}

const DEFAULT_STATUS_BOARD = makeBoard([
  makeStatus("kb-1"),
  makeStatus("kb-doc", {
    primary_state: "ready",
    release: {
      state: "active",
      active_snapshot_id: "snapshot-active",
      candidate_snapshot_id: null,
    },
  }),
]);

const RUNNING_STATUS = makeStatus("kb-1", {
  primary_state: "running",
  alerts: [{ code: "document_failed", severity: "error", count: 1 }],
  document_counts: {
    total: 10,
    ingested: 4,
    remaining: 6,
    staged: 1,
    queued: 2,
    running: 2,
    awaiting_review: 2,
    completed: 2,
    failed: 1,
    paused: 1,
    canceled: 0,
  },
  stages: [
    {
      stage: "image_understanding",
      document_count: 2,
      done: 18,
      total: 42,
    },
  ],
  review: {
    suggested_facts: 3,
    orphaned_facts: 1,
    images_needing_review: 2,
    total: 6,
  },
  release: {
    state: "ready",
    active_snapshot_id: "snapshot-active",
    candidate_snapshot_id: "snapshot-next",
  },
  latest_activity_at: "2026-07-30T07:59:58Z",
});

const RUNNING_STATUS_BOARD = makeBoard(
  [
    makeStatus("kb-doc", {
      primary_state: "ready",
      document_counts: {
        ...EMPTY_COUNTS,
        total: 5,
        ingested: 5,
        completed: 5,
      },
      release: {
        state: "active",
        active_snapshot_id: "snapshot-doc",
        candidate_snapshot_id: null,
      },
    }),
    RUNNING_STATUS,
  ],
  { poll_after_ms: 2_000, has_active_work: true },
);

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <KbListPage />
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.get.mockReset();
  apiMocks.post.mockReset();
  apiMocks.get.mockResolvedValue(DEFAULT_STATUS_BOARD);
  authState.org = {
    id: OWNER_ORG_ID,
    slug: "owner",
    name: "Owner",
    role: "org_admin",
  };
  lifecycleMocks.list.mockImplementation((status: "active" | "archived") =>
    Promise.resolve(status === "active" ? ACTIVE_BASES : ARCHIVED_BASES),
  );
  lifecycleMocks.restore.mockResolvedValue(
    makeKb({ id: "kb-archived", name: "自有归档库" }),
  );
  lifecycleMocks.board.mockResolvedValue({
    generated_at: "2026-07-30T00:00:00Z",
    purge_worker_enabled: true,
    retention_days: 30,
    items: [
      {
        kb_id: "kb-archived",
        name: "自有归档库",
        lifecycle_status: "archived",
        archived_at: "2026-07-01T00:00:00Z",
        purged_at: null,
        retention_due_at: "2026-07-31T00:00:00Z",
        purge_due: false,
        latest_operation: null,
      },
    ],
  });
});

describe("KbListPage lifecycle filter", () => {
  test("defaults to active bases and never renders purged audit shells", async () => {
    renderPage();

    expect(await screen.findByText("活动资料库")).toBeDefined();
    expect(screen.queryByText("已清理资料库")).toBeNull();
    expect(lifecycleMocks.list).toHaveBeenCalledWith("active");
  });

  test("shows archived bases and restores only an owned base", async () => {
    renderPage();
    await screen.findByText("活动资料库");

    fireEvent.click(screen.getByRole("button", { name: "已归档" }));

    expect(await screen.findByText("自有归档库")).toBeDefined();
    expect(screen.getByText("共享归档库")).toBeDefined();
    expect(lifecycleMocks.list).toHaveBeenCalledWith("archived");
    const restoreButtons = screen.getAllByRole("button", {
      name: "恢复知识库",
    });
    expect(restoreButtons).toHaveLength(1);
    fireEvent.click(restoreButtons[0]);

    await waitFor(() => {
      expect(lifecycleMocks.restore).toHaveBeenCalledWith("kb-archived");
    });
  });

  test("does not expose restore to a non-admin", async () => {
    authState.org = {
      ...authState.org,
      role: "operator",
    };
    lifecycleMocks.list.mockResolvedValue(ARCHIVED_BASES);
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "已归档" }));
    expect(await screen.findByText("自有归档库")).toBeDefined();
    expect(screen.queryByRole("button", { name: "恢复知识库" })).toBeNull();
  });
});

describe("KbListPage lifecycle hints and hover actions", () => {
  test("reveals quick actions with deep links on cards", async () => {
    renderPage();
    expect(await screen.findByText("活动资料库")).toBeDefined();

    // 悬浮快捷操作(CSS 控制显隐,可访问性上始终在文档流中)
    expect(
      screen
        .getByRole("link", { name: "活动资料库 资料" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=sources");
    expect(
      screen
        .getByRole("link", { name: "活动资料库 设置" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=settings");
  });

  test("skips the board query for operators", async () => {
    authState.org = { ...authState.org, role: "operator" };
    renderPage();
    expect(await screen.findByText("活动资料库")).toBeDefined();
    // 非管理员不拉取看板(board 端点 403)
    expect(lifecycleMocks.board).not.toHaveBeenCalled();
  });

  test("shows retention badge on archived cards and a due banner when due", async () => {
    lifecycleMocks.board.mockResolvedValue({
      generated_at: "2026-07-30T00:00:00Z",
      purge_worker_enabled: true,
      retention_days: 30,
      items: [
        {
          kb_id: "kb-archived",
          name: "自有归档库",
          lifecycle_status: "archived",
          archived_at: "2026-06-01T00:00:00Z",
          purged_at: null,
          retention_due_at: "2026-07-01T00:00:00Z",
          purge_due: true,
          latest_operation: null,
        },
      ],
    });
    renderPage();
    await screen.findByText("活动资料库");

    // 到期横幅(含去处理深链)
    const banner = await screen.findByRole("status");
    expect(banner.textContent).toContain("保留期已到期");
    expect(
      screen.getByRole("link", { name: "自有归档库" }).getAttribute("href"),
    ).toBe("/org/kb/kb-archived?view=settings&tab=danger");

    // 切到已归档筛选,归档卡片带到期角标
    fireEvent.click(screen.getByRole("button", { name: "已归档" }));
    expect(await screen.findByText("保留期已到期")).toBeDefined();
  });
});

describe("KbListPage realtime status board", () => {
  test("shows completion, stages, non-zero counts, release state and actionable deep links", async () => {
    apiMocks.get.mockResolvedValue(RUNNING_STATUS_BOARD);
    renderPage();

    expect(
      (
        await screen.findByRole("progressbar", {
          name: "活动资料库 采集完成度",
        })
      ).getAttribute("aria-valuenow"),
    ).toBe("40");
    expect(screen.getByText("图片理解 18/42 · 2 份资料")).toBeDefined();
    expect(
      screen
        .getByRole("link", { name: "活动资料库 待分类 1" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=sources&status=staged");
    expect(
      screen
        .getByRole("link", { name: "活动资料库 失败 1" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=sources&status=failed");
    expect(
      screen
        .getByRole("link", { name: "活动资料库 事实待审核 4" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=review");
    expect(
      screen
        .getByRole("link", { name: "活动资料库 图片待审核 2" })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=images&review=needs_review");
    expect(
      screen
        .getByRole("link", {
          name: "活动资料库 发布状态：线上可用 · 新版本待发布",
        })
        .getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=release");
  });

  test("filters client-side while preserving the base-list order across status refreshes", async () => {
    apiMocks.get.mockResolvedValue(RUNNING_STATUS_BOARD);
    const { queryClient } = renderPage();
    await screen.findByText("图片理解 18/42 · 2 份资料");

    const cardNames = () =>
      screen
        .getAllByTestId("kb-card")
        .map((card) =>
          card.textContent?.includes("活动资料库")
            ? "活动资料库"
            : "文档语料库",
        );
    expect(cardNames()).toEqual(["活动资料库", "文档语料库"]);

    fireEvent.click(screen.getByRole("button", { name: "运行中 1" }));
    expect(screen.getAllByTestId("kb-card")).toHaveLength(1);
    expect(screen.getByText("活动资料库")).toBeDefined();
    expect(screen.queryByText("文档语料库")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "全部 2" }));
    await act(async () => {
      queryClient.setQueryData<KbStatusBoardResponse>(
        KB_STATUS_BOARD_QUERY_KEY,
        makeBoard([
          makeStatus("kb-doc", {
            primary_state: "running",
            document_counts: { ...EMPTY_COUNTS, total: 1, running: 1 },
          }),
          makeStatus("kb-1", {
            primary_state: "ready",
            document_counts: {
              ...EMPTY_COUNTS,
              total: 10,
              ingested: 10,
              completed: 10,
            },
          }),
        ]),
      );
    });

    expect(cardNames()).toEqual(["活动资料库", "文档语料库"]);
  });

  test("keeps cached card state and marks it stale after a refresh failure", async () => {
    apiMocks.get.mockResolvedValueOnce(RUNNING_STATUS_BOARD);
    const { queryClient } = renderPage();
    await screen.findByText("图片理解 18/42 · 2 份资料");

    apiMocks.get.mockRejectedValueOnce(new Error("temporary unavailable"));
    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: KB_STATUS_BOARD_QUERY_KEY,
      });
    });

    expect(screen.getByText("状态可能已过期")).toBeDefined();
    expect(screen.getByText("图片理解 18/42 · 2 份资料")).toBeDefined();
    expect(
      screen.getByRole("link", { name: "活动资料库 失败 1" }),
    ).toBeDefined();
  });

  test("shows a current snapshot failure as one specific release action", async () => {
    apiMocks.get.mockResolvedValue(
      makeBoard([
        makeStatus("kb-1", {
          primary_state: "needs_attention",
          alerts: [{ code: "snapshot_failed", severity: "warning", count: 1 }],
          release: {
            state: "failed",
            active_snapshot_id: "snapshot-active",
            candidate_snapshot_id: "snapshot-failed",
          },
        }),
        makeStatus("kb-doc"),
      ]),
    );
    renderPage();

    expect(
      (
        await screen.findByRole("link", {
          name: "活动资料库 快照构建失败",
        })
      ).getAttribute("href"),
    ).toBe("/org/kb/kb-1?view=release");
    expect(
      screen.getByRole("link", {
        name: "活动资料库 发布状态：线上版本可用 · 最新构建失败",
      }),
    ).toBeDefined();
    expect(screen.queryByText("告警 1")).toBeNull();
  });

  test("uses the server hint, pauses in background, and refreshes on focus or reconnect", async () => {
    vi.useFakeTimers();
    try {
      apiMocks.get.mockResolvedValue(RUNNING_STATUS_BOARD);
      renderPage();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_999);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(2);

      focusManager.setFocused(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(2);

      await act(async () => {
        focusManager.setFocused(true);
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(3);

      onlineManager.setOnline(false);
      await act(async () => {
        onlineManager.setOnline(true);
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apiMocks.get).toHaveBeenCalledTimes(4);
    } finally {
      focusManager.setFocused(undefined);
      onlineManager.setOnline(true);
      vi.useRealTimers();
    }
  });
});
