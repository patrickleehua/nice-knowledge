import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type {
  LlmProviderDto,
  ProviderModelCatalogItem,
} from "@/lib/types";
import AdminProvidersPage from "./page";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

const PROVIDER: LlmProviderDto = {
  id: "123e4567-e89b-42d3-a456-426614174000",
  name: "catalog-gateway",
  protocol: "openai",
  api_key: "secret",
  base_url: "https://gateway.example/v1",
  models: ["gpt-4.1", "provider-model", "opaque-model"],
  model_metadata: {
    "gpt-4.1": {
      capabilities: ["generation", "vision"],
      input_modalities: ["text", "image"],
      capability_source: "registry",
      registry_revision: "2026-07-25",
      provider_metadata: {},
    },
    "provider-model": {
      capabilities: ["generation"],
      input_modalities: ["text"],
      capability_source: "provider",
      registry_revision: null,
      provider_metadata: { owned_by: "gateway" },
    },
    "opaque-model": {
      capabilities: [],
      input_modalities: [],
      capability_source: "unclassified",
      registry_revision: "2026-07-25",
      provider_metadata: {},
    },
  },
  enabled: true,
  is_builtin: false,
  created_at: "2026-07-25T10:00:00Z",
  updated_at: "2026-07-25T10:00:00Z",
};

const MANUAL_ENTRY: ProviderModelCatalogItem = {
  provider: PROVIDER.name,
  model: "opaque-model",
  vendor: null,
  capabilities: ["generation", "vision"],
  input_modalities: ["text", "image"],
  capability_source: "manual",
  registry_revision: null,
  provider_metadata: {},
};

const DISCOVERED_MODELS: ProviderModelCatalogItem[] = [
  {
    provider: PROVIDER.name,
    model: "new-opaque-model",
    vendor: null,
    capabilities: [],
    input_modalities: [],
    capability_source: "unclassified",
    registry_revision: "2026-07-25.4",
    provider_metadata: {},
  },
  {
    provider: PROVIDER.name,
    model: "text-embedding-3-large",
    vendor: "openai",
    capabilities: ["embedding"],
    input_modalities: ["text"],
    capability_source: "registry",
    registry_revision: "2026-07-25.4",
    provider_metadata: { owned_by: "openai" },
  },
  {
    provider: PROVIDER.name,
    model: "claude-sonnet-4-5",
    vendor: "anthropic",
    capabilities: ["generation", "vision", "function_call", "reasoning"],
    input_modalities: ["text", "image"],
    capability_source: "registry",
    registry_revision: "2026-07-25.4",
    provider_metadata: {},
  },
];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminProvidersPage />
    </QueryClientProvider>,
  );
}

describe("AdminProvidersPage model capabilities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.get.mockImplementation((url: string) =>
      url.endsWith("/discover-models")
        ? Promise.resolve(DISCOVERED_MODELS)
        : Promise.resolve([PROVIDER]),
    );
    apiMocks.post.mockResolvedValue({
      ...PROVIDER,
      models: [...PROVIDER.models, "new-opaque-model"],
      model_metadata: {
        ...PROVIDER.model_metadata,
        "new-opaque-model": {
          capabilities: [],
          input_modalities: [],
          capability_source: "unclassified",
          registry_revision: "2026-07-25.3",
          provider_metadata: {},
        },
      },
    });
    apiMocks.patch.mockResolvedValue(PROVIDER);
    apiMocks.put.mockResolvedValue(MANUAL_ENTRY);
    apiMocks.delete.mockResolvedValue(undefined);
  });

  test("shows capability provenance and saves a manual multi-capability override", async () => {
    renderPage();

    expect(await screen.findByText("平台注册表")).toBeDefined();
    expect(screen.getByText("Provider 声明")).toBeDefined();
    expect(screen.getAllByText("未分类").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/网关导入后有 1 个模型无法可信识别能力/),
    ).toBeDefined();
    expect(screen.getAllByText(/注册表 2026-07-25/).length).toBeGreaterThan(0);

    // 模型类型是 Cherry Studio 式的 chip 开关(role=switch),不是 checkbox
    const generation = screen.getByRole("switch", {
      name: "opaque-model 生成能力",
    });
    const vision = screen.getByRole("switch", {
      name: "opaque-model 视觉能力",
    });
    fireEvent.click(generation);
    fireEvent.click(vision);

    const card = screen.getByText("opaque-model").closest(".rounded-lg");
    expect(card).not.toBeNull();
    fireEvent.click(
      within(card as HTMLElement).getByRole("button", {
        name: "保存能力",
      }),
    );

    await waitFor(() => {
      expect(apiMocks.put).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/model-capabilities`,
        {
          model: "opaque-model",
          capabilities: ["generation", "vision"],
        },
      );
    });
  });

  test("model type chips cover the Cherry Studio vocabulary plus generation", async () => {
    renderPage();

    const card = (await screen.findByText("opaque-model")).closest(
      ".rounded-lg",
    ) as HTMLElement;
    const labels = within(card)
      .getAllByRole("switch")
      .map((node) => node.textContent);

    expect(labels).toEqual([
      "生成",
      "视觉",
      "联网",
      "推理",
      "工具",
      "重排",
      "嵌入",
    ]);
  });

  test("a chip toggles off on a second click and saves in canonical order", async () => {
    renderPage();

    await screen.findByText("opaque-model");
    const embedding = screen.getByRole("switch", {
      name: "opaque-model 嵌入能力",
    });
    const webSearch = screen.getByRole("switch", {
      name: "opaque-model 联网能力",
    });

    fireEvent.click(embedding);
    expect(embedding.getAttribute("aria-checked")).toBe("true");
    // 再点一次关掉 —— chip 是开关,不是只增不减
    fireEvent.click(embedding);
    expect(embedding.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(webSearch);
    fireEvent.click(
      screen.getByRole("switch", { name: "opaque-model 生成能力" }),
    );

    const card = screen.getByText("opaque-model").closest(
      ".rounded-lg",
    ) as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "保存能力" }));

    await waitFor(() => {
      // 勾选顺序是 联网 → 生成,提交必须按 chip 的规范顺序归一
      expect(apiMocks.put).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/model-capabilities`,
        { model: "opaque-model", capabilities: ["generation", "web_search"] },
      );
    });
  });

  test("reset clears a manual override back to automatic detection", async () => {
    apiMocks.get.mockImplementation((url: string) =>
      url.endsWith("/discover-models")
        ? Promise.resolve(DISCOVERED_MODELS)
        : Promise.resolve([
            {
              ...PROVIDER,
              models: ["manual-model"],
              model_metadata: {
                "manual-model": {
                  capabilities: ["generation", "vision"],
                  input_modalities: ["text", "image"],
                  capability_source: "manual",
                  registry_revision: null,
                  provider_metadata: {},
                },
              },
            },
          ]),
    );

    renderPage();

    await screen.findByText("manual-model");
    fireEvent.click(
      screen.getByRole("button", { name: "重置 manual-model 为自动识别" }),
    );

    await waitFor(() => {
      expect(apiMocks.put).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/model-capabilities`,
        { model: "manual-model", mode: "auto" },
      );
    });
  });

  test("reset is hidden unless the model carries a manual override", async () => {
    renderPage();

    // registry 来源没有人工标注可清除,重置入口不应可达
    const card = (await screen.findByText("gpt-4.1")).closest(
      ".rounded-lg",
    ) as HTMLElement;
    const reset = within(card).getByRole("button", {
      name: "重置 gpt-4.1 为自动识别",
      hidden: true,
    });

    expect(reset.className).toContain("invisible");
    expect(reset.getAttribute("tabindex")).toBe("-1");
  });

  test("discovers without importing and only submits the selected models", async () => {
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "从网关导入" }),
    );

    expect(
      await screen.findByRole("heading", {
        name: new RegExp(`从 ${PROVIDER.name} 选择模型`),
      }),
    ).toBeDefined();
    expect(apiMocks.post).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("checkbox", { name: /^选择 new-opaque-model/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "导入所选模型" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/import-models`,
        { models: ["new-opaque-model"] },
      );
      expect(toastMocks.warning).toHaveBeenCalledWith(
        "1 个模型未能识别能力，请完成人工标注后再用于业务",
      );
    });
  });

  test("groups discovered models by vendor and imports a whole group at once", async () => {
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "从网关导入" }),
    );
    await screen.findByRole("heading", {
      name: new RegExp(`从 ${PROVIDER.name} 选择模型`),
    });

    // 未识别厂商固定排在最后，其余按显示名排序
    const headers = screen
      .getAllByText(/^(Anthropic|OpenAI|未识别厂商)$/)
      .map((node) => node.textContent);
    expect(headers).toEqual(["Anthropic", "OpenAI", "未识别厂商"]);

    const group = screen.getByText("Anthropic").closest("div") as HTMLElement;
    fireEvent.click(within(group).getByRole("button", { name: "选中整组" }));
    fireEvent.click(screen.getByRole("button", { name: "导入所选模型" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/import-models`,
        { models: ["claude-sonnet-4-5"] },
      );
    });
  });

  test("collapsing a vendor group hides its models without dropping the selection", async () => {
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "从网关导入" }),
    );
    await screen.findByRole("heading", {
      name: new RegExp(`从 ${PROVIDER.name} 选择模型`),
    });

    fireEvent.click(
      screen.getByRole("checkbox", { name: /^选择 claude-sonnet-4-5/ }),
    );
    fireEvent.click(screen.getByRole("button", { expanded: true, name: /Anthropic/ }));

    expect(screen.queryByText("claude-sonnet-4-5")).toBeNull();
    expect(screen.getByText(/已选择 1 个/)).toBeDefined();
  });

  test("instance connectivity check reports the catalog size", async () => {
    apiMocks.post.mockImplementation((url: string) =>
      url.endsWith("/test-connection")
        ? Promise.resolve({
            provider: PROVIDER.name,
            model: null,
            scope: "instance",
            ok: true,
            latency_ms: 142.5,
            error_code: null,
            model_count: 37,
            probed_capability: null,
          })
        : Promise.resolve(PROVIDER),
    );

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "测试连通性" }),
    );

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/test-connection`,
        {},
      );
      expect(screen.getByText(/连通 · 143ms · 目录 37 个/)).toBeDefined();
    });
  });

  test("a failed model check surfaces the sanitized code as actionable text", async () => {
    apiMocks.post.mockImplementation((url: string) =>
      url.endsWith("/test-model")
        ? Promise.resolve({
            provider: PROVIDER.name,
            model: "gpt-4.1",
            scope: "model",
            ok: false,
            latency_ms: 88,
            // Backend never returns a raw upstream body — only this code shape.
            error_code: "http_error;status=404",
            model_count: null,
            probed_capability: "generation",
          })
        : Promise.resolve(PROVIDER),
    );

    renderPage();

    const card = (await screen.findByText("gpt-4.1")).closest(
      ".rounded-lg",
    ) as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: /测试/ }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith(
        `/admin/providers/${PROVIDER.id}/test-model`,
        { model: "gpt-4.1" },
      );
      expect(
        within(card).getByText(/上游返回错误（HTTP 404）/),
      ).toBeDefined();
    });
  });

  test("an unclassified model cannot be tested because its call shape is unknown", async () => {
    renderPage();

    const card = (await screen.findByText("opaque-model")).closest(
      ".rounded-lg",
    ) as HTMLElement;
    const button = within(card).getByRole("button", { name: /测试/ });

    expect(button.hasAttribute("disabled")).toBe(true);
  });

  test("treats legacy provider responses without model metadata as unclassified", async () => {
    apiMocks.get.mockResolvedValue([
      {
        ...PROVIDER,
        models: ["legacy-vision-model"],
        model_metadata: undefined,
      },
    ]);

    renderPage();

    const modelName = await screen.findByText("legacy-vision-model");
    const card = modelName.closest(".rounded-lg");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("未分类")).toBeDefined();
    expect(within(card as HTMLElement).getByText(/输入模态：未声明/)).toBeDefined();
    expect(
      within(card as HTMLElement).getByText(
        /完成人工标注前不会进入能力敏感的模型选择器/,
      ),
    ).toBeDefined();
    const visionToggle = within(card as HTMLElement).getByRole("switch", {
      name: "legacy-vision-model 视觉能力",
    });
    expect(visionToggle.getAttribute("aria-checked")).toBe("false");
  });
});
