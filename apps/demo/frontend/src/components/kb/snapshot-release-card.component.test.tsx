import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { SnapshotReleaseCard } from "./snapshot-release-card";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/lib/api", () => {
  class MockApiError extends Error {
    constructor(
      public status: number,
      message: string,
      public code?: string,
    ) {
      super(message);
    }
  }

  return {
    ApiError: MockApiError,
    api: apiMocks,
  };
});

const toastMocks = vi.hoisted(() => ({
  error: vi.fn(),
  info: vi.fn(),
  success: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

vi.mock("@/components/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/shared")>();

  return {
    ...actual,
    ConfirmDialog: ({
      trigger,
      description,
      confirmLabel,
      onConfirm,
    }: {
      trigger?: ReactElement;
      description?: ReactNode;
      confirmLabel?: string;
      onConfirm: () => void | Promise<unknown>;
    }) => (
      <div>
        {trigger}
        {description && <p>{description}</p>}
        <button
          type="button"
          onClick={() => {
            void Promise.resolve(onConfirm()).catch(() => undefined);
          }}
        >
          {confirmLabel ?? "确认"}
        </button>
      </div>
    ),
  };
});

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";
const SNAPSHOT_ID = "323e4567-e89b-42d3-a456-426614174002";
const BLOCKED_MESSAGE =
  "该版本创建后知识范围发生过资料撤回或治理变更，不能直接回滚。";

function makeSnapshot(rollbackCapability: {
  allowed: boolean;
  code: string | null;
  message: string | null;
}) {
  return {
    id: SNAPSHOT_ID,
    kb_id: KB_ID,
    revision_set_hash: "a".repeat(64),
    embedding_fingerprint: {
      provider: "openai",
      model: "text-embedding-3-small",
      dim: 1536,
    },
    config_fingerprint: "b".repeat(64),
    revision_manifest: [],
    config_manifest: { consumption_epoch: 1 },
    build_stats: { revision_count: 0 },
    status: "retired",
    ready_at: "2026-07-01T09:00:00Z",
    activated_at: "2026-07-01T09:01:00Z",
    retired_at: "2026-07-02T09:00:00Z",
    failed_at: null,
    error: null,
    created_at: "2026-07-01T09:00:00Z",
    updated_at: "2026-07-02T09:00:00Z",
    rollback_capability: rollbackCapability,
  };
}

function renderReleaseCard(snapshot: ReturnType<typeof makeSnapshot>) {
  apiMocks.get.mockImplementation((path: string) => {
    if (path === "/kb/bases") {
      return Promise.resolve([
        {
          id: KB_ID,
          org_id: "123e4567-e89b-42d3-a456-426614174000",
          name: "产品知识库",
          kb_type: "general",
          description: null,
          ingest_profile: null,
          active_snapshot_id: null,
          created_at: "2026-07-01T08:00:00Z",
        },
      ]);
    }
    if (path === `/kb/bases/${KB_ID}/snapshots`) {
      return Promise.resolve([snapshot]);
    }
    throw new Error(`Unexpected GET ${path}`);
  });

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <SnapshotReleaseCard kbId={KB_ID} />
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  Object.values(toastMocks).forEach((mock) => mock.mockReset());
});

describe("SnapshotReleaseCard rollback eligibility", () => {
  test("explains a blocked historical version without offering rollback", async () => {
    renderReleaseCard(
      makeSnapshot({
        allowed: false,
        code: "SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED",
        message: BLOCKED_MESSAGE,
      }),
    );

    expect(await screen.findByText(BLOCKED_MESSAGE)).toBeDefined();
    expect(screen.getByText("1 个历史版本")).toBeDefined();
    expect(screen.queryByText("1 个版本可回滚")).toBeNull();
    expect(screen.queryByRole("button", { name: "回滚" })).toBeNull();
  });

  test("offers rollback only when the server allows it", async () => {
    renderReleaseCard(
      makeSnapshot({
        allowed: true,
        code: null,
        message: null,
      }),
    );

    expect(await screen.findByRole("button", { name: "回滚" })).toBeDefined();
  });

  test("shows a concurrent business rejection and refreshes eligibility", async () => {
    apiMocks.post.mockRejectedValueOnce(
      new ApiError(409, BLOCKED_MESSAGE, "SNAPSHOT_KNOWLEDGE_BOUNDARY_CHANGED"),
    );
    const { queryClient } = renderReleaseCard(
      makeSnapshot({
        allowed: true,
        code: null,
        message: null,
      }),
    );
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await screen.findByRole("button", { name: "回滚" });
    expect(screen.getByText(/已撤回资料不会被恢复/)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "确认回滚" }));

    await waitFor(() => {
      expect(toastMocks.error).toHaveBeenCalledWith(BLOCKED_MESSAGE);
      expect(invalidateSpy).toHaveBeenCalledWith({
        predicate: expect.any(Function),
      });
    });
  });
});
