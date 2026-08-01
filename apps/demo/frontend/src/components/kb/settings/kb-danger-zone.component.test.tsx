import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type {
  KnowledgeBaseDeletionPreview,
  KnowledgeBaseLifecycleOperation,
} from "@/lib/kb-lifecycle";
import { KbDangerZone } from "./kb-danger-zone";

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";
const OWNER_ORG_ID = "123e4567-e89b-42d3-a456-426614174000";

const lifecycleMocks = vi.hoisted(() => ({
  preview: vi.fn(),
  archive: vi.fn(),
  restore: vi.fn(),
  deleteEmpty: vi.fn(),
  purge: vi.fn(),
  operation: vi.fn(),
  retryOperation: vi.fn(),
}));

const authState = vi.hoisted(() => ({
  org: {
    id: "123e4567-e89b-42d3-a456-426614174000",
    slug: "owner",
    name: "Owner",
    role: "org_admin",
  } as {
    id: string;
    slug: string;
    name: string;
    role: "org_admin" | "operator";
  } | null,
}));

const routerMocks = vi.hoisted(() => ({
  push: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

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

vi.mock("next/navigation", () => ({
  useRouter: () => routerMocks,
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

function makeOperation(
  overrides: Partial<KnowledgeBaseLifecycleOperation> = {},
): KnowledgeBaseLifecycleOperation {
  return {
    id: "operation-1",
    kb_id: KB_ID,
    operation_type: "purge",
    status: "processing",
    phase: "delete_objects",
    attempt_count: 1,
    impact_summary: { objects: 8 },
    last_error_code: null,
    error_message: null,
    retryable: false,
    created_at: "2026-07-29T10:00:00Z",
    started_at: "2026-07-29T10:01:00Z",
    completed_at: null,
    ...overrides,
  };
}

function makePreview(
  overrides: Partial<KnowledgeBaseDeletionPreview> = {},
): KnowledgeBaseDeletionPreview {
  return {
    version: "kb-deletion/v1",
    kb_id: KB_ID,
    kb_name: "供应商知识库",
    owner_org_id: OWNER_ORG_ID,
    lifecycle_status: "active",
    consumption_epoch: 4,
    complete: true,
    allowed_actions: ["archive"],
    impact_counts: {
      documents: 3,
      entities: 7,
      projects: 1,
    },
    project_references: [
      {
        id: "project-1",
        name: "华东团队线路",
        scope_mode: "explicit",
      },
    ],
    blockers: [],
    plan_hash: "a".repeat(64),
    expires_at: "2026-07-29T11:00:00Z",
    latest_operation: null,
    ...overrides,
  };
}

function renderDangerZone() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <KbDangerZone kbId={KB_ID} />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.org = {
    id: OWNER_ORG_ID,
    slug: "owner",
    name: "Owner",
    role: "org_admin",
  };
  lifecycleMocks.preview.mockResolvedValue(makePreview());
  lifecycleMocks.archive.mockResolvedValue({});
  lifecycleMocks.restore.mockResolvedValue({});
  lifecycleMocks.deleteEmpty.mockResolvedValue(undefined);
  lifecycleMocks.purge.mockResolvedValue(makeOperation({ status: "pending" }));
  lifecycleMocks.operation.mockResolvedValue(makeOperation());
  lifecycleMocks.retryOperation.mockResolvedValue(
    makeOperation({ status: "pending" }),
  );
});

describe("KbDangerZone permissions and impact", () => {
  test("does not load or render lifecycle controls for a non-admin", () => {
    authState.org = {
      id: OWNER_ORG_ID,
      slug: "owner",
      name: "Owner",
      role: "operator",
    };
    renderDangerZone();

    expect(screen.queryByText("危险操作")).toBeNull();
    expect(lifecycleMocks.preview).not.toHaveBeenCalled();
  });

  test("shows no executable action for a shared knowledge base", async () => {
    lifecycleMocks.preview.mockResolvedValue(
      makePreview({ owner_org_id: "another-org" }),
    );
    renderDangerZone();

    expect(
      await screen.findByText(
        "共享或平台只读知识库不能由当前组织归档、删除或永久清理。",
      ),
    ).toBeDefined();
    expect(screen.queryByRole("button", { name: "归档知识库" })).toBeNull();
    expect(screen.queryByRole("button", { name: "永久清理" })).toBeNull();
  });

  test("renders impact counts, blockers, identifiers and resolution hints", async () => {
    lifecycleMocks.preview.mockResolvedValue(
      makePreview({
        allowed_actions: [],
        blockers: [
          {
            code: "BUSINESS_ARTIFACT_REFERENCE",
            count: 2,
            identifiers: ["行程 A", "报价 B"],
            resolution_hint: "先按业务产物生命周期解除引用",
          },
        ],
      }),
    );
    renderDangerZone();

    expect(await screen.findByText("文档")).toBeDefined();
    expect(screen.getByText("当前旅行计划")).toBeDefined();
    expect(screen.getByText("业务产物仍在引用（2）")).toBeDefined();
    expect(screen.getByText("行程 A、报价 B")).toBeDefined();
    expect(
      screen.getByText("解决提示：先按业务产物生命周期解除引用"),
    ).toBeDefined();
  });
});

describe("KbDangerZone confirmations and operations", () => {
  test("requires exact name, reason and project unlink confirmation before archive", async () => {
    renderDangerZone();

    fireEvent.click(await screen.findByRole("button", { name: "归档知识库" }));
    const submit = screen.getByRole("button", { name: "确认归档" });
    expect(submit).toHaveProperty("disabled", true);

    fireEvent.change(screen.getByRole("textbox", { name: "操作理由" }), {
      target: { value: "停止旧资料消费" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "知识库名称确认" }), {
      target: { value: "供应商知识" },
    });
    expect(submit).toHaveProperty("disabled", true);

    fireEvent.change(screen.getByRole("textbox", { name: "知识库名称确认" }), {
      target: { value: "供应商知识库" },
    });
    expect(submit).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("checkbox", { name: /确认解除旅行计划关联/ }));
    expect(submit).toHaveProperty("disabled", false);

    fireEvent.click(submit);
    await waitFor(() => {
      expect(lifecycleMocks.archive).toHaveBeenCalledWith(KB_ID, {
        expected_plan_hash: "a".repeat(64),
        reason: "停止旧资料消费",
        confirm_project_unlinks: true,
      });
    });
  });

  test("keeps empty deletion distinct and refreshes a stale plan", async () => {
    lifecycleMocks.preview.mockResolvedValue(
      makePreview({
        allowed_actions: ["empty_delete"],
        impact_counts: {},
        project_references: [],
      }),
    );
    lifecycleMocks.deleteEmpty.mockRejectedValueOnce(
      new ApiError(409, "stale", "deletion_plan_stale"),
    );
    renderDangerZone();

    fireEvent.click(
      await screen.findByRole("button", { name: "删除空知识库" }),
    );
    expect(
      screen.getByText(/仅删除从未使用且没有任何资料或引用/),
    ).toBeDefined();
    fireEvent.change(screen.getByRole("textbox", { name: "知识库名称确认" }), {
      target: { value: "供应商知识库" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认删除空知识库" }));

    await waitFor(() => {
      expect(lifecycleMocks.deleteEmpty).toHaveBeenCalledWith(
        KB_ID,
        "a".repeat(64),
      );
      expect(toastMocks.error).toHaveBeenCalledWith(
        "删除影响已经变化，请重新预检后再次确认",
      );
      expect(lifecycleMocks.preview.mock.calls.length).toBeGreaterThan(1);
    });
    expect(lifecycleMocks.archive).not.toHaveBeenCalled();
  });

  test("restores purge progress from preview and retries a persisted failure", async () => {
    const failed = makeOperation({
      status: "failed",
      phase: "cleanup_metadata",
      attempt_count: 3,
      last_error_code: "metadata_cleanup_failed",
      error_message: "dependency unavailable",
      retryable: true,
    });
    lifecycleMocks.preview.mockResolvedValue(
      makePreview({
        lifecycle_status: "purge_pending",
        allowed_actions: [],
        latest_operation: failed,
      }),
    );
    lifecycleMocks.operation.mockResolvedValue(failed);
    renderDangerZone();

    expect(await screen.findByText("执行失败")).toBeDefined();
    expect(screen.getByText("当前阶段：清理依赖元数据")).toBeDefined();
    expect(
      screen.getByText("metadata_cleanup_failed：dependency unavailable"),
    ).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "重试永久清理" }));

    await waitFor(() => {
      expect(lifecycleMocks.retryOperation).toHaveBeenCalledWith(failed.id);
    });
  });
});
