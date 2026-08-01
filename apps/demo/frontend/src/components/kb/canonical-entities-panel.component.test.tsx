import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { CanonicalEntity } from "@/lib/types";
import { CanonicalEntitiesPanel } from "./canonical-entities-panel";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: apiMocks };
});

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";

function entity(
  id: string,
  canonicalName: string,
  overrides: Partial<CanonicalEntity> = {},
): CanonicalEntity {
  return {
    id,
    org_id: "123e4567-e89b-42d3-a456-426614174000",
    kb_id: KB_ID,
    entity_type: "product",
    canonical_name: canonicalName,
    metadata: {},
    support_status: "supported",
    support_status_reason: null,
    support_status_changed_at: "2026-07-28T10:00:00Z",
    support_status_snapshot_id: "snapshot-1",
    unsupported_at: null,
    is_pinned: false,
    pinned_at: null,
    pinned_by: null,
    pin_reason: null,
    merged_into_entity_id: null,
    merged_at: null,
    merged_by: null,
    merge_reason: null,
    created_at: "2026-07-28T09:00:00Z",
    updated_at: "2026-07-28T10:00:00Z",
    aliases: [],
    ...overrides,
  };
}

function renderPanel(entities: CanonicalEntity[]) {
  apiMocks.get.mockImplementation((path: string) => {
    if (path.startsWith("/kb/canonical-entities?")) {
      return Promise.resolve(entities);
    }
    if (
      path.startsWith(`/kb/bases/${KB_ID}/canonical-entities/merge-suggestions`)
    ) {
      return Promise.resolve([]);
    }
    throw new Error(`Unexpected GET ${path}`);
  });
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <CanonicalEntitiesPanel kbId={KB_ID} />
    </QueryClientProvider>,
  );
}

describe("CanonicalEntitiesPanel governance state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("distinguishes supported, unsupported, pinned, and merged registry entities", async () => {
    renderPanel([
      entity("entity-supported", "共享概念实体"),
      entity("entity-unsupported", "失去来源的概念", {
        support_status: "unsupported",
        support_status_reason: "candidate snapshot has no active support",
        support_status_snapshot_id: "snapshot-2",
        unsupported_at: "2026-07-28T10:00:00Z",
      }),
      entity("entity-pinned", "人工维护的概念", {
        is_pinned: true,
        pinned_at: "2026-07-28T10:00:00Z",
        pinned_by: "user-1",
        pin_reason: "仍用于人工规划",
      }),
      entity("entity-merged", "旧概念名称", {
        merged_into_entity_id: "entity-supported",
        merged_at: "2026-07-28T10:00:00Z",
        merged_by: "user-1",
        merge_reason: "与共享概念实体重复",
      }),
    ]);

    expect(await screen.findByText("共享概念实体")).toBeDefined();
    expect(screen.getAllByText("有活动支持")).toHaveLength(2);
    expect(screen.getByText("无活动支持")).toBeDefined();
    expect(
      screen.getByText(
        "无活动支持原因：candidate snapshot has no active support",
      ),
    ).toBeDefined();
    expect(screen.getByText("人工固定")).toBeDefined();
    expect(screen.getByText("固定原因：仍用于人工规划")).toBeDefined();
    expect(screen.getByText("已合并")).toBeDefined();
    expect(
      screen.getByText(
        "已重定向至实体 entity-supported，原因：与共享概念实体重复",
      ),
    ).toBeDefined();
    expect(
      screen.getByRole("button", { name: "编辑 旧概念名称" }),
    ).toHaveProperty("disabled", true);
    expect(
      screen.getByRole("button", { name: "合并 旧概念名称" }),
    ).toHaveProperty("disabled", true);
  });
});
