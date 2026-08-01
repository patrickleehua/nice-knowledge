import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type {
  EntityType,
  IngestProfile,
  KbImageEnrichmentReadiness,
  KnowledgeBase,
  ProviderModelCatalogItem,
} from "@/lib/types";
import { SettingsView } from "./settings-view";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

const authState = vi.hoisted(() => ({
  role: "operator" as "operator" | "org_admin",
}));

// 保留 ApiError 等真实导出:设置页的「实体类型」Tab 挂着注册表面板,它要用到 ApiError
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

vi.mock("@/lib/auth", () => ({
  useCurrentOrg: () => ({
    id: "123e4567-e89b-42d3-a456-426614174000",
    slug: "qa",
    name: "QA",
    role: authState.role,
  }),
}));

vi.mock("@/components/kb/settings/kb-danger-zone", () => ({
  KbDangerZone: () => <div>知识库危险操作区</div>,
}));

// 设置页三 Tab 的当前分区走 ?tab=;测试里用可控的 params 模拟 URL 状态
const urlState = vi.hoisted(() => ({
  params: new URLSearchParams(),
  set: vi.fn(),
  setAll: vi.fn(),
}));

vi.mock("@/lib/use-url-state", () => ({
  useUrlState: () => ({
    get: (key: string) => urlState.params.get(key),
    getAll: (key: string) => urlState.params.getAll(key),
    set: urlState.set,
    setAll: urlState.setAll,
  }),
}));

const KB_ID = "223e4567-e89b-42d3-a456-426614174001";

const PROFILE: IngestProfile = {
  parser: "docling",
  chunk_strategy: "structure",
  chunk_max_chars: 1200,
  chunk_overlap_chars: 150,
  table_mode: "row",
  parent_child: false,
  caption_images: true,
  caption_provider: null,
  caption_model: null,
  auto_wiki: true,
};

const KB: KnowledgeBase = {
  id: KB_ID,
  org_id: "123e4567-e89b-42d3-a456-426614174000",
  name: "图文资料库",
  kb_type: "mixed",
  description: null,
  ingest_profile: PROFILE,
  active_snapshot_id: null,
  created_at: "2026-07-25T10:00:00Z",
};

const PLATFORM_READY: KbImageEnrichmentReadiness = {
  enabled: true,
  ready: true,
  code: "ready",
  ocr_provider: "docling",
  ocr_model: "rapidocr",
  caption_provider: "openai",
  caption_model: "gpt-4.1",
  selection_source: "platform_default",
  capability_source: "registry",
  registry_revision: "2026-07-25",
  acceptance_policy: "human_review_required",
  config_fingerprint: "a".repeat(64),
};

const CATALOG_ITEM: ProviderModelCatalogItem = {
  provider: "vision-provider",
  model: "vision-model",
  vendor: null,
  capabilities: ["generation", "vision"],
  input_modalities: ["text", "image"],
  capability_source: "manual",
  registry_revision: null,
  provider_metadata: {},
};

const ENTITY_TYPES: EntityType[] = [
  {
    id: "et-builtin-1",
    type_key: "attraction",
    display_name: "概念",
    description: null,
    field_schema: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
    filterable_fields: [],
    card_template: null,
    review_policy: "auto",
    is_builtin: true,
    is_own: false,
  },
  {
    id: "et-own-1",
    type_key: "visa_policy",
    display_name: "报销政策",
    description: "各类报销口径",
    field_schema: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
    filterable_fields: [{ field: "country", type: "text", label: "国家" }],
    card_template: null,
    review_policy: "human",
    is_builtin: false,
    is_own: true,
  },
];

function installApi({
  kb = KB,
  effective = PLATFORM_READY,
  platform = PLATFORM_READY,
  models = [CATALOG_ITEM],
  entityTypes = [],
}: {
  kb?: KnowledgeBase;
  effective?: KbImageEnrichmentReadiness;
  platform?: KbImageEnrichmentReadiness;
  models?: ProviderModelCatalogItem[];
  entityTypes?: EntityType[];
} = {}) {
  apiMocks.get.mockImplementation((path: string) => {
    if (path === "/kb/bases") return Promise.resolve([kb]);
    if (path === "/kb/ingest/profiles/presets") {
      return Promise.resolve({ default: PROFILE, presets: {} });
    }
    if (path === `/kb/image-enrichment/readiness?kb_id=${KB_ID}`) {
      return Promise.resolve(effective);
    }
    if (path === "/kb/image-enrichment/readiness") {
      return Promise.resolve(platform);
    }
    if (path.startsWith("/kb/image-enrichment/models?")) {
      return Promise.resolve(models);
    }
    if (path === `/kb/bases/${KB_ID}/shares`) return Promise.resolve([]);
    if (path === "/kb/entity-types") return Promise.resolve(entityTypes);
    throw new Error(`Unexpected GET ${path}`);
  });
  apiMocks.patch.mockImplementation(
    (_path: string, body: { ingest_profile: IngestProfile | null }) =>
      Promise.resolve({
        ...KB,
        ingest_profile: body.ingest_profile,
      }),
  );
}

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsView kbId={KB_ID} />
    </QueryClientProvider>,
  );
}

/** 模型选择器已抽成独立 Dialog,断言前先打开它 */
async function openModelDialog() {
  fireEvent.click(await screen.findByRole("button", { name: /选择执行模型/ }));
  return screen.findByRole("dialog");
}

// ---- 三 Tab 分区布局(摄入配置 / 分享协作 / 实体类型) --------------------------

describe("SettingsView tab layout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    urlState.params = new URLSearchParams();
    authState.role = "operator";
  });

  test("defaults to the ingest tab and keeps the other panels unmounted", async () => {
    installApi();
    renderSettings();

    expect(await screen.findByRole("tab", { name: "摄入配置" })).toBeDefined();
    expect(screen.getByRole("tab", { name: "分享协作" })).toBeDefined();
    expect(screen.getByRole("tab", { name: "实体类型" })).toBeDefined();

    // 默认落在摄入配置:四个分组标题与 sticky 操作条可见
    expect(await screen.findByText("解析")).toBeDefined();
    expect(screen.getByText("切片")).toBeDefined();
    expect(screen.getByText("表格")).toBeDefined();
    expect(screen.getByText("增强项")).toBeDefined();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDefined();
    expect(screen.getByRole("button", { name: "恢复默认" })).toBeDefined();

    // 分享与注册表面板未挂载,不发多余请求也不渲染内容
    expect(screen.queryByPlaceholderText("输入目标组织标识(slug)")).toBeNull();
    expect(screen.queryByRole("button", { name: "新建类型" })).toBeNull();
  });

  test("writes ?tab= when switching tabs", async () => {
    installApi();
    renderSettings();

    fireEvent.click(await screen.findByRole("tab", { name: "分享协作" }));
    expect(urlState.set).toHaveBeenCalledWith({ tab: "share" });

    fireEvent.click(screen.getByRole("tab", { name: "实体类型" }));
    expect(urlState.set).toHaveBeenCalledWith({ tab: "types" });
  });

  test("shows share management on the share tab while the ingest form stays mounted", async () => {
    urlState.params = new URLSearchParams("tab=share");
    installApi();
    renderSettings();

    expect(
      await screen.findByPlaceholderText("输入目标组织标识(slug)"),
    ).toBeDefined();
    expect(await screen.findByText("尚未分享给任何组织")).toBeDefined();

    // 摄入表单 keepMounted:DOM 仍在(隐藏),未保存编辑与 unsaved-guard 不随切 Tab 丢失
    expect(screen.getByText("摄入切片配置")).toBeDefined();
  });

  test("gives the entity type registry its own full-width tab", async () => {
    urlState.params = new URLSearchParams("tab=types");
    installApi({ entityTypes: ENTITY_TYPES });
    renderSettings();

    expect(await screen.findByText("实体类型配置(2)")).toBeDefined();
    expect(screen.getByRole("button", { name: "新建类型" })).toBeDefined();
    // 内置与自建类型都按行渲染
    expect(await screen.findByText("visa_policy")).toBeDefined();
    expect(screen.getByText("概念")).toBeDefined();
    expect(screen.getByText("自建")).toBeDefined();
    expect(screen.getByText("内置")).toBeDefined();
  });

  test("shows lifecycle danger settings only to an administrator", async () => {
    installApi();
    const operatorView = renderSettings();
    await screen.findByRole("tab", { name: "摄入配置" });
    expect(screen.queryByRole("tab", { name: "危险操作" })).toBeNull();
    operatorView.unmount();

    authState.role = "org_admin";
    renderSettings();
    expect(await screen.findByRole("tab", { name: "危险操作" })).toBeDefined();
  });
});

// ---- 摄入配置 Tab:图片视觉描述模型选择(默认 Tab,行为与重构前一致) ------------

describe("SettingsView image caption model selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    urlState.params = new URLSearchParams();
    authState.role = "operator";
  });

  test("shows the platform source and saves an eligible KB override as a pair", async () => {
    installApi();
    renderSettings();

    // 摘要行:当前生效来源 + provider/model
    expect(await screen.findByText("平台默认")).toBeDefined();
    expect(screen.getByText("openai / gpt-4.1")).toBeDefined();

    await openModelDialog();
    expect((await screen.findAllByText("平台注册表")).length).toBeGreaterThan(
      0,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: /vision-provider \/ vision-model/,
      }),
    );
    expect(await screen.findByText("保存后生效")).toBeDefined();
    expect(screen.getByText("vision-provider / vision-model")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => {
      expect(apiMocks.patch).toHaveBeenCalledWith(`/kb/bases/${KB_ID}`, {
        ingest_profile: expect.objectContaining({
          caption_images: true,
          caption_provider: "vision-provider",
          caption_model: "vision-model",
        }),
      });
    });
  });

  test("explains an unavailable default and prevents enabling without an eligible model", async () => {
    const unavailable: KbImageEnrichmentReadiness = {
      ...PLATFORM_READY,
      enabled: false,
      ready: false,
      code: "caption_provider_credentials_missing",
    };
    installApi({
      effective: unavailable,
      platform: unavailable,
      models: [],
    });
    renderSettings();

    expect(
      await screen.findAllByText(/所选 Provider 缺少可用凭证/),
    ).not.toHaveLength(0);

    await openModelDialog();
    expect(
      await screen.findByText(
        "暂无可用模型。请先在模型提供商页启用模型并明确标注 generation + vision 能力。",
      ),
    ).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));

    const captionSwitch = await screen.findByRole("switch", {
      name: "图片视觉描述",
    });
    expect(captionSwitch.hasAttribute("data-disabled")).toBe(false);
    fireEvent.click(captionSwitch);
    await waitFor(() => {
      expect(captionSwitch.getAttribute("aria-checked")).toBe("false");
      expect(captionSwitch.hasAttribute("data-disabled")).toBe(true);
    });
  });

  test("searches the governed catalog through the server endpoint", async () => {
    installApi();
    renderSettings();

    await openModelDialog();
    const search = await screen.findByRole("textbox", {
      name: "搜索图片描述模型",
    });
    fireEvent.change(search, { target: { value: "vision" } });

    await waitFor(() => {
      expect(apiMocks.get).toHaveBeenCalledWith(
        "/kb/image-enrichment/models?search=vision&limit=50",
      );
    });
  });

  test("preserves an ineligible KB override that is no longer in the catalog", async () => {
    const staleProfile: IngestProfile = {
      ...PROFILE,
      caption_provider: "retired-provider",
      caption_model: "retired-vision-model",
    };
    const staleReadiness: KbImageEnrichmentReadiness = {
      ...PLATFORM_READY,
      ready: false,
      code: "caption_model_ineligible",
      caption_provider: "retired-provider",
      caption_model: "retired-vision-model",
      selection_source: "kb_override",
      capability_source: "provider",
      registry_revision: null,
    };
    installApi({
      kb: { ...KB, ingest_profile: staleProfile },
      effective: staleReadiness,
      models: [CATALOG_ITEM],
    });

    renderSettings();

    // 摘要里如实标注覆盖来源、不可用原因,并保留原有 provider/model 不被静默清除
    expect(await screen.findByText("本库覆盖")).toBeDefined();
    expect(
      screen.getAllByText("retired-provider / retired-vision-model").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText("所选模型未明确同时具备生成与视觉能力"),
    ).toBeDefined();

    await openModelDialog();
    const replacement = await screen.findByRole("button", {
      name: /vision-provider \/ vision-model/,
    });
    expect(replacement.getAttribute("aria-pressed")).toBe("false");
    expect(apiMocks.patch).not.toHaveBeenCalled();
  });
});
