import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type {
  LlmProviderDto,
  ModelRouteDto,
  ModelRouteTaskDto,
} from "@/lib/types";
import AdminModelsPage from "./page";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

vi.mock("@/components/shared", async () => {
  const actual = await vi.importActual<typeof import("@/components/shared")>(
    "@/components/shared",
  );
  return {
    ...actual,
    PageHeader: ({ actions }: { actions?: ReactNode }) => <div>{actions}</div>,
  };
});

function provider(
  name: string,
  protocol: LlmProviderDto["protocol"],
  model: string,
  capabilities: NonNullable<
    LlmProviderDto["model_metadata"]
  >[string]["capabilities"],
): LlmProviderDto {
  return {
    id: `${name}-id`,
    name,
    protocol,
    api_key: "secret",
    base_url: `https://${name}.test/v1`,
    models: [model],
    model_metadata: {
      [model]: {
        capabilities,
        input_modalities: capabilities.includes("vision")
          ? ["text", "image"]
          : ["text"],
        capability_source: "manual",
        registry_revision: null,
        provider_metadata: {},
      },
    },
    enabled: true,
    is_builtin: false,
    created_at: null,
    updated_at: null,
  };
}

const PROVIDERS = [
  provider("embed-openai", "openai", "embed-model", ["embedding"]),
  provider("chat-openai", "openai", "chat-model", ["generation"]),
  provider("embed-anthropic", "anthropic", "anthropic-model", ["embedding"]),
  provider("vision-openai", "openai", "vision-model", ["generation", "vision"]),
  provider("chat-anthropic", "anthropic", "claude-model", ["generation"]),
];

const CREATED_ROUTE: ModelRouteDto = {
  id: "route-id",
  org_id: null,
  task: "kb.embedding",
  primary_provider: "embed-openai",
  primary_model: "embed-model",
  fallback_chain: [],
  max_tokens: 1,
  timeout_seconds: 30,
  is_active: true,
  created_at: null,
};

const CUSTOM_ROUTE: ModelRouteDto = {
  id: "custom-route-id",
  org_id: null,
  task: "agent.approval_review",
  primary_provider: "chat-openai",
  primary_model: "chat-model",
  fallback_chain: [{ provider: "chat-anthropic", model: "claude-model" }],
  max_tokens: 600,
  timeout_seconds: 30,
  is_active: true,
  created_at: null,
};

const TASK_CATALOG: ModelRouteTaskDto[] = [
  {
    task: "llm.default",
    label: "对话模型",
    description: "普通生成任务的统一基线",
    category: "system",
    is_system: true,
  },
  {
    task: "agent.approval_review",
    label: "Agent 操作审批复核",
    description: "复核高风险工具动作",
    category: "agent",
    is_system: false,
  },
  {
    task: "pipeline.report.outline",
    label: "文档摘要生成",
    description: "生成多个摘要候选",
    category: "pipeline",
    is_system: false,
  },
  {
    task: "pipeline.report.detail",
    label: "摘要终版润色",
    description: "润色选定摘要",
    category: "pipeline",
    is_system: false,
  },
];

/** 板块卡:按标题定位到那一张 section。 */
function board(label: string): HTMLElement {
  return screen
    .getByRole("heading", { name: label })
    .closest("section") as HTMLElement;
}

/** 板块按钮在 providers 未加载时是禁用的，先等能力过滤结果落地。 */
async function waitForProviders() {
  await waitFor(() => {
    expect(
      within(board("嵌入模型"))
        .getByRole("button", { name: "配置" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });
}

function selectOption(option: HTMLElement) {
  fireEvent.pointerDown(option, { pointerType: "mouse" });
  fireEvent.click(option);
}

function renderPage(
  routes: ModelRouteDto[] = [],
  tasks: ModelRouteTaskDto[] = TASK_CATALOG,
) {
  apiMocks.get.mockImplementation((url: string) => {
    if (url === "/admin/providers") return Promise.resolve(PROVIDERS);
    if (url === "/admin/models") return Promise.resolve(routes);
    if (url === "/admin/model-route-tasks") return Promise.resolve(tasks);
    return Promise.resolve([]);
  });
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminModelsPage />
    </QueryClientProvider>,
  );
}

describe("AdminModelsPage system capability boards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.post.mockResolvedValue(CREATED_ROUTE);
    apiMocks.patch.mockResolvedValue(CREATED_ROUTE);
    apiMocks.delete.mockResolvedValue(undefined);
  });

  test("renders one board per system capability slot", async () => {
    renderPage();

    for (const label of ["对话模型", "视觉模型", "嵌入模型", "重排序模型"]) {
      expect(await screen.findByRole("heading", { name: label })).toBeDefined();
    }
  });

  test("an unconfigured slot is flagged; a configured one shows its chain", async () => {
    renderPage([CREATED_ROUTE]);

    // 等路由查询落地,否则板块还停在"未配置"
    await screen.findByText("已启用");
    // 徽章把 provider 与 model 拆成了两个文本节点,按整段 textContent 匹配
    expect(board("嵌入模型").textContent).toContain("embed-openai/embed-model");
    // 视觉板块没有路由行 → 必须显式提示未配置,而不是静默看起来正常
    expect(within(board("视觉模型")).getByText("未配置")).toBeDefined();
  });

  test("eligible provider counts encode the per-slot protocol restriction", async () => {
    renderPage();
    await waitForProviders();

    // 嵌入只走 OpenAI 兼容线:embed-anthropic 标了 embedding 也不计入
    expect(
      within(board("嵌入模型")).getByText("1 个提供商有可用模型"),
    ).toBeDefined();
    // 对话不限协议:chat-openai + vision-openai + chat-anthropic
    expect(
      within(board("对话模型")).getByText("3 个提供商有可用模型"),
    ).toBeDefined();
    // 重排没有任何标注模型 → 按钮必须禁用,而不是打开一个选不出模型的表单
    const rerank = within(board("重排序模型")).getByRole("button", {
      name: "配置",
    });
    expect(rerank.hasAttribute("disabled")).toBe(true);
  });

  test("creates an embedding route with capability-matched OpenAI models", async () => {
    renderPage();

    await waitForProviders();
    fireEvent.click(
      within(board("嵌入模型")).getByRole("button", { name: "配置" }),
    );

    const dialog = screen.getByRole("dialog");
    const model = within(dialog).getByRole("combobox", {
      name: "主模型选择",
    });
    expect(within(dialog).getByText("kb.embedding")).toBeDefined();
    expect(model.textContent).toContain("embed-model");
    expect(within(model).getByText("嵌入")).toBeDefined();
    expect(within(dialog).getByText("embed-openai")).toBeDefined();
    // 嵌入只走 OpenAI 兼容线,anthropic 实例即使标注了 embedding 也不能选
    expect(within(dialog).queryByText("embed-anthropic")).toBeNull();
    // 嵌入不消耗生成 token
    expect(within(dialog).queryByLabelText("max_tokens")).toBeNull();
    expect(
      within(dialog).queryByRole("combobox", { name: "作用域" }),
    ).toBeNull();

    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith("/admin/models", {
        task: "kb.embedding",
        org_id: null,
        primary_provider: "embed-openai",
        primary_model: "embed-model",
        fallback_chain: [],
        max_tokens: 1,
        timeout_seconds: 30,
      });
    });
  });

  test("the vision slot demands generation AND vision, not either one", async () => {
    renderPage();

    await waitForProviders();
    fireEvent.click(
      within(board("视觉模型")).getByRole("button", { name: "配置" }),
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("kb.image.caption")).toBeDefined();
    const model = within(dialog).getByRole("combobox", {
      name: "主模型选择",
    });
    expect(model.textContent).toContain("vision-model");
    expect(within(model).getByText("生成")).toBeDefined();
    expect(within(model).getByText("视觉")).toBeDefined();
    // 只有 generation 的实例不满足这个板块
    expect(within(dialog).queryByText("chat-openai")).toBeNull();
    // 视觉是对话式调用,需要 max_tokens
    expect(within(dialog).getByLabelText("max_tokens")).toBeDefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith("/admin/models", {
        task: "kb.image.caption",
        org_id: null,
        primary_provider: "vision-openai",
        primary_model: "vision-model",
        fallback_chain: [],
        max_tokens: 2048,
        timeout_seconds: 120,
      });
    });
  });

  test("the chat slot saves the platform fallback route with its own defaults", async () => {
    renderPage();
    await waitForProviders();
    fireEvent.click(
      within(board("对话模型")).getByRole("button", { name: "配置" }),
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("llm.default")).toBeDefined();
    expect(within(dialog).getByLabelText("max_tokens")).toBeDefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith("/admin/models", {
        task: "llm.default",
        org_id: null,
        primary_provider: "chat-openai",
        primary_model: "chat-model",
        fallback_chain: [],
        max_tokens: 8192,
        timeout_seconds: 120,
      });
    });
  });

  test("separates system baselines from task-specific overrides", async () => {
    renderPage([CREATED_ROUTE, CUSTOM_ROUTE]);

    expect(await screen.findByText("系统能力基线")).toBeDefined();
    expect(screen.getByText("任务专属路由")).toBeDefined();
    expect(
      screen.getByText(/没有专属路由的普通任务自动继承上方“对话模型”/),
    ).toBeDefined();
    expect(await screen.findByText("Agent 操作审批复核")).toBeDefined();
    // 系统任务只出现在上方卡片，不再以内部 task key 重复进表格。
    expect(screen.queryByText("kb.embedding")).toBeNull();
    expect(screen.getByText("agent.approval_review")).toBeDefined();
  });

  test("searches and selects multiple tasks, then uses the batch endpoint", async () => {
    renderPage();
    await waitForProviders();
    fireEvent.click(screen.getByRole("button", { name: "添加任务专属路由" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/专属路由会覆盖“对话模型”/)).toBeDefined();
    expect(within(dialog).queryByLabelText("任务标识")).toBeNull();

    fireEvent.change(within(dialog).getByLabelText("搜索任务"), {
      target: { value: "摘要" },
    });
    expect(within(dialog).getByText("文档摘要生成")).toBeDefined();
    expect(within(dialog).getByText("摘要终版润色")).toBeDefined();
    expect(within(dialog).queryByText("Agent 操作审批复核")).toBeNull();

    fireEvent.click(
      within(dialog).getByRole("checkbox", {
        name: "全选当前结果",
      }),
    );
    fireEvent.click(
      within(dialog).getByRole("button", { name: "保存 2 条路由" }),
    );

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith("/admin/models/batch", {
        tasks: ["pipeline.report.outline", "pipeline.report.detail"],
        org_id: null,
        primary_provider: "chat-openai",
        primary_model: "chat-model",
        fallback_chain: [],
        max_tokens: 8192,
        timeout_seconds: 120,
      });
    });
  });

  test("shows model capabilities in the catalog selector without free-text input", async () => {
    renderPage();
    await waitForProviders();
    fireEvent.click(
      within(board("视觉模型")).getByRole("button", { name: "配置" }),
    );

    const dialog = screen.getByRole("dialog");
    const modelSelect = within(dialog).getByRole("combobox", {
      name: "主模型选择",
    });
    expect(within(dialog).queryByPlaceholderText(/模型名/)).toBeNull();

    fireEvent.click(modelSelect);
    const listbox = await screen.findByRole("listbox");
    const option = within(listbox)
      .getByText("vision-model")
      .closest('[role="option"]') as HTMLElement;
    expect(within(option).getByText("生成")).toBeDefined();
    expect(within(option).getByText("视觉")).toBeDefined();
  });

  test("switching providers selects the first eligible catalog model", async () => {
    renderPage();
    await waitForProviders();
    fireEvent.click(screen.getByRole("button", { name: "添加任务专属路由" }));

    const dialog = screen.getByRole("dialog");
    const providerSelect = within(dialog).getByRole("combobox", {
      name: "主模型提供商",
    });
    fireEvent.click(providerSelect);
    selectOption(await screen.findByRole("option", { name: "chat-anthropic" }));

    await waitFor(() => {
      expect(
        within(dialog).getByRole("combobox", { name: "主模型选择" })
          .textContent,
      ).toContain("claude-model");
    });
  });

  test("requires a stale route model to be reselected from the catalog", async () => {
    renderPage([
      {
        ...CUSTOM_ROUTE,
        primary_model: "retired-model",
        fallback_chain: [],
      },
    ]);
    await waitForProviders();
    await screen.findByText("Agent 操作审批复核");
    fireEvent.click(screen.getByRole("button", { name: "编辑路由" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("alert").textContent).toContain(
      "原模型 retired-model 已不在当前可用目录中",
    );
    expect(
      within(dialog)
        .getByRole("button", { name: "保存" })
        .hasAttribute("disabled"),
    ).toBe(true);

    fireEvent.click(
      within(dialog).getByRole("combobox", { name: "主模型选择" }),
    );
    selectOption(await screen.findByRole("option", { name: /chat-model/ }));

    await waitFor(() => {
      expect(within(dialog).queryByRole("alert")).toBeNull();
      expect(
        within(dialog)
          .getByRole("button", { name: "保存" })
          .hasAttribute("disabled"),
      ).toBe(false);
    });
  });

  test("marks routes configured in the selected scope as unavailable", async () => {
    renderPage([CUSTOM_ROUTE]);
    await waitForProviders();
    fireEvent.click(screen.getByRole("button", { name: "添加任务专属路由" }));

    const dialog = screen.getByRole("dialog");
    const configured = await within(dialog).findByRole("checkbox", {
      name: /Agent 操作审批复核/,
    });
    expect(configured.getAttribute("aria-disabled")).toBe("true");
    expect(within(dialog).getByText("已配置")).toBeDefined();
    expect(within(dialog).queryByText("普通生成任务的统一基线")).toBeNull();
  });

  test("moves the primary model into the fallback chain and saves the new order", async () => {
    renderPage([CUSTOM_ROUTE]);
    await screen.findByText("Agent 操作审批复核");
    fireEvent.click(screen.getByRole("button", { name: "编辑路由" }));

    const dialog = screen.getByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "下移 chat-openai/chat-model",
      }),
    );

    expect(within(dialog).getByText("主模型")).toBeDefined();
    expect(within(dialog).getByText("降级 1")).toBeDefined();
    expect(
      within(dialog).getByRole("button", {
        name: "上移 chat-openai/chat-model",
      }),
    ).toBeDefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => {
      expect(apiMocks.patch).toHaveBeenCalledWith(
        "/admin/models/custom-route-id",
        {
          primary_provider: "chat-anthropic",
          primary_model: "claude-model",
          fallback_chain: [{ provider: "chat-openai", model: "chat-model" }],
          max_tokens: 600,
          timeout_seconds: 30,
        },
      );
    });
  });
});
