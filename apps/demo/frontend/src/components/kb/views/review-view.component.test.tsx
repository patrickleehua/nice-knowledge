import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { FactClaimQueueItem } from "@/lib/types";
import { ReviewView } from "./review-view";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: apiMocks,
}));

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

const claims: FactClaimQueueItem[] = [
  {
    id: "claim-1",
    kb_id: "kb-1",
    subject_entity_id: null,
    object_entity_id: null,
    subject_type: "place",
    subject_id: "subject-1",
    predicate: "entity_mention",
    value_json: { name: "爱丁堡" },
    raw_payload: { name: "爱丁堡" },
    corrected_payload: null,
    effective_payload: { name: "爱丁堡" },
    valid_from: null,
    valid_to: null,
    confidence: 0.98,
    review_status: "suggested",
    reviewed_by: null,
    review_note: null,
    model_name: "test-model",
    prompt_version: "test-prompt",
    created_at: null,
    updated_at: null,
    evidence: [],
    entity_type: "entity_mention",
  },
  {
    id: "claim-2",
    kb_id: "kb-1",
    subject_entity_id: null,
    object_entity_id: null,
    subject_type: "place",
    subject_id: "subject-2",
    predicate: "entity_mention",
    value_json: { name: "格拉斯哥" },
    raw_payload: { name: "格拉斯哥" },
    corrected_payload: null,
    effective_payload: { name: "格拉斯哥" },
    valid_from: null,
    valid_to: null,
    confidence: 0.96,
    review_status: "suggested",
    reviewed_by: null,
    review_note: null,
    model_name: "test-model",
    prompt_version: "test-prompt",
    created_at: null,
    updated_at: null,
    evidence: [],
    entity_type: "entity_mention",
  },
];

vi.mock("@/components/kb/workbench/kb-data", () => ({
  useFactClaimPages: () => ({
    data: { pages: [claims] },
    isPending: false,
    isError: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
  }),
}));

vi.mock("@/components/kb/workbench/use-virtual-rows", () => ({
  useEndReached: vi.fn(),
}));

function renderReviewView() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReviewView kbId="kb-1" />
    </QueryClientProvider>,
  );
}

describe("ReviewView", () => {
  test("selects and clears every loaded claim from the list header", () => {
    apiMocks.get.mockResolvedValue([]);
    renderReviewView();

    const selectAll = screen.getByRole("checkbox", {
      name: "全选已加载",
    });
    expect(screen.queryByText("已选 2 项")).toBeNull();

    fireEvent.click(selectAll);
    expect(screen.getByText("已选 2 项")).toBeDefined();
    expect(selectAll.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(selectAll);
    expect(screen.queryByText("已选 2 项")).toBeNull();
    expect(selectAll.getAttribute("aria-checked")).toBe("false");
  });

  test("shows a mixed select-all state after selecting one claim", () => {
    apiMocks.get.mockResolvedValue([]);
    renderReviewView();

    fireEvent.keyDown(document.body, { key: "x" });

    const selectAll = screen.getByRole("checkbox", {
      name: "全选已加载",
    });
    expect(screen.getByText("已选 1 项")).toBeDefined();
    expect(selectAll.getAttribute("aria-checked")).toBe("mixed");
  });
});
