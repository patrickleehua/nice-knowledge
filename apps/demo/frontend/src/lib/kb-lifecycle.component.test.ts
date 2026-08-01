import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { kbLifecycleApi, kbLifecycleErrorMessage } from "@/lib/kb-lifecycle";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  postWithHeaders: vi.fn(),
  deleteWithHeaders: vi.fn(),
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

beforeEach(() => {
  vi.clearAllMocks();
  Object.values(apiMocks).forEach((mock) => mock.mockResolvedValue(undefined));
});

describe("kbLifecycleApi", () => {
  test("uses separate active and archived list contracts", async () => {
    await kbLifecycleApi.list("active");
    await kbLifecycleApi.list("archived");

    expect(apiMocks.get).toHaveBeenNthCalledWith(1, "/kb/bases");
    expect(apiMocks.get).toHaveBeenNthCalledWith(
      2,
      "/kb/bases?lifecycle_status=archived",
    );
  });

  test("sends archive and restore as distinct requests", async () => {
    const body = {
      expected_plan_hash: "a".repeat(64),
      reason: "retire source",
      acknowledge_external_unlink: true,
    };
    await kbLifecycleApi.archive("kb-1", body);
    await kbLifecycleApi.restore("kb-1");

    expect(apiMocks.post).toHaveBeenNthCalledWith(
      1,
      "/kb/bases/kb-1/archive",
      body,
    );
    expect(apiMocks.post).toHaveBeenNthCalledWith(2, "/kb/bases/kb-1/restore");
  });

  test("uses If-Match only for synchronous empty deletion", async () => {
    await kbLifecycleApi.deleteEmpty("kb-1", "b".repeat(64));

    expect(apiMocks.deleteWithHeaders).toHaveBeenCalledWith("/kb/bases/kb-1", {
      "If-Match": `"${"b".repeat(64)}"`,
    });
  });

  test("uses Idempotency-Key and an explicit purge body", async () => {
    const body = {
      expected_plan_hash: "c".repeat(64),
      confirmation_name: "供应商知识库",
      reason: "retention complete",
    };
    await kbLifecycleApi.purge("kb-1", body, "idempotency-1");

    expect(apiMocks.postWithHeaders).toHaveBeenCalledWith(
      "/kb/bases/kb-1/purge",
      body,
      { "Idempotency-Key": "idempotency-1" },
    );
  });
});

describe("kbLifecycleErrorMessage", () => {
  test("maps stable lifecycle codes and preserves unknown server messages", () => {
    expect(
      kbLifecycleErrorMessage(
        new ApiError(409, "stale", "deletion_plan_stale"),
      ),
    ).toBe("删除影响已经变化，请重新预检后再次确认");
    expect(
      kbLifecycleErrorMessage(new ApiError(500, "inventory timeout", "other")),
    ).toBe("inventory timeout");
  });
});
