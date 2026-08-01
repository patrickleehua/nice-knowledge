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
import type { CapabilitySlotDto, OperationsDiagnosticsDto } from "@/lib/types";
import AdminDiagnosticsPage from "./page";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMocks }));
vi.mock("sonner", () => ({ toast: toastMocks }));

vi.mock("@/components/shared", async () => {
  const actual = await vi.importActual<typeof import("@/components/shared")>(
    "@/components/shared",
  );
  return {
    ...actual,
    PageHeader: ({ actions }: { actions?: ReactNode }) => <div>{actions}</div>,
  };
});

function slot(overrides: Partial<CapabilitySlotDto>): CapabilitySlotDto {
  return {
    task: "llm.default",
    required_capabilities: ["generation"],
    configured: true,
    is_active: true,
    primary_provider: "gateway",
    primary_model: "gpt-4o",
    fallback_hops: 0,
    resolved_provider: "gateway",
    resolved_model: "gpt-4o",
    ready: true,
    ...overrides,
  };
}

function diagnostics(
  overrides: Partial<OperationsDiagnosticsDto> = {},
): OperationsDiagnosticsDto {
  return {
    generated_at: "2026-07-25T06:00:00Z",
    runtime: {
      dispatch_mode: "inline",
      service_version: "release-42",
      heartbeat_expiry_seconds: 60,
    },
    capability_slots: [
      slot({}),
      slot({
        task: "kb.image.caption",
        required_capabilities: ["generation", "vision"],
      }),
      slot({ task: "kb.embedding", required_capabilities: ["embedding"] }),
      slot({ task: "kb.search.rerank", required_capabilities: ["rerank"] }),
    ],
    heartbeats: [],
    providers: [],
    skills: { status: "healthy", total: 3, invalid: [] },
    mcp_servers: [],
    agents: [],
    schedules: { system: [], custom: [] },
    organizations: [],
    ...overrides,
  };
}

function renderPage(payload: OperationsDiagnosticsDto = diagnostics()) {
  apiMocks.get.mockResolvedValue(payload);
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminDiagnosticsPage />
    </QueryClientProvider>,
  );
}

function card(label: string): HTMLElement {
  return screen
    .getByRole("heading", { name: label })
    .closest("section") as HTMLElement;
}

async function openPanel(label: string): Promise<HTMLElement> {
  fireEvent.click(
    await screen.findByRole("button", { name: `查看${label}详情` }),
  );
  return screen.findByRole("dialog");
}

async function closePanel(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
}

describe("AdminDiagnosticsPage", () => {
  beforeEach(() => vi.clearAllMocks());

  test("all four capability slots render with their required capabilities", async () => {
    renderPage();

    expect(screen.queryByRole("heading", { name: "对话模型" })).toBeNull();
    await openPanel("模型能力");
    expect(within(card("视觉模型")).getByText("生成")).toBeDefined();
    expect(within(card("视觉模型")).getByText("视觉")).toBeDefined();
    expect(within(card("嵌入模型")).getByText("嵌入")).toBeDefined();
    expect(within(card("重排序模型")).getByText("重排")).toBeDefined();
  });

  test("a route row that no longer resolves reads as 路由失效, not 未配置", async () => {
    // 行还在也启用着,但能力校验不过 —— 这是最容易被当成"配好了"的状态
    renderPage(
      diagnostics({
        capability_slots: [
          slot({
            task: "kb.embedding",
            required_capabilities: ["embedding"],
            configured: true,
            is_active: true,
            ready: false,
            primary_model: "bge-m3",
            resolved_provider: null,
            resolved_model: null,
          }),
        ],
      }),
    );

    await openPanel("模型能力");
    expect(within(card("嵌入模型")).getByText("路由失效")).toBeDefined();
    expect(within(card("嵌入模型")).queryByText("未配置")).toBeNull();
    await closePanel();
    fireEvent.click(screen.getByRole("button", { name: "查看问题清单" }));
    await screen.findByRole("dialog");
    expect(screen.getByText("嵌入模型不可用")).toBeDefined();
  });

  test("an unconfigured slot reads as 未配置", async () => {
    renderPage(
      diagnostics({
        capability_slots: [
          slot({
            task: "kb.image.caption",
            required_capabilities: ["generation", "vision"],
            configured: false,
            is_active: false,
            ready: false,
            primary_provider: null,
            primary_model: null,
            resolved_provider: null,
            resolved_model: null,
          }),
        ],
      }),
    );

    await openPanel("模型能力");
    expect(within(card("视觉模型")).getByText("未配置")).toBeDefined();
  });

  test("a slot serving from its fallback says so instead of looking healthy", async () => {
    renderPage(
      diagnostics({
        capability_slots: [
          slot({
            primary_model: "gpt-4o",
            resolved_model: "backup-model",
            resolved_provider: "backup",
            fallback_hops: 1,
          }),
        ],
      }),
    );

    await openPanel("模型能力");
    expect(within(card("对话模型")).getByText("就绪")).toBeDefined();
    expect(
      within(card("对话模型")).getByText(/主模型不可用，已降级/),
    ).toBeDefined();
  });

  test("no warning banner when every slot is ready", async () => {
    renderPage();

    await screen.findByRole("button", { name: "查看模型能力详情" });
    expect(screen.queryByText(/项系统能力不可用/)).toBeNull();
  });

  test("立即检测 posts, swaps in the fresh snapshot and warns on failures", async () => {
    renderPage();
    await screen.findByRole("button", { name: "查看模型能力详情" });

    apiMocks.post.mockResolvedValue(
      diagnostics({
        providers: [
          {
            capability: "rerank",
            provider: "gateway",
            model: "bge-reranker-v2-m3",
            configuration_ready: true,
            status: "unavailable",
            last_probe_at: "2026-07-25T06:05:00Z",
            last_success_at: null,
            last_failure_at: "2026-07-25T06:05:00Z",
            probe_age_seconds: 12,
            latency_ms: 903.4,
            consecutive_failures: 3,
            error_code: "rerank_http_404",
          },
        ],
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "全面检测" }));

    await waitFor(() => {
      expect(apiMocks.post).toHaveBeenCalledWith("/admin/operations/probe", {});
      expect(toastMocks.warning).toHaveBeenCalledWith("1 项外部能力检测未通过");
    });
    // 探测结果直接写回缓存,不用等下一次 GET
    await openPanel("模型能力");
    expect(screen.getByText(/rerank_http_404 · 连续失败 3 次/)).toBeDefined();
    expect(screen.getByText("903ms")).toBeDefined();
  });

  test("a clean probe run reports success", async () => {
    renderPage();
    await screen.findByRole("button", { name: "查看模型能力详情" });

    apiMocks.post.mockResolvedValue(
      diagnostics({
        providers: [
          {
            capability: "embedding",
            provider: "gateway",
            model: "bge-m3",
            configuration_ready: true,
            status: "healthy",
            last_probe_at: "2026-07-25T06:05:00Z",
            last_success_at: "2026-07-25T06:05:00Z",
            last_failure_at: null,
            probe_age_seconds: 3,
            latency_ms: 120,
            consecutive_failures: 0,
            error_code: null,
          },
        ],
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "全面检测" }));

    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith(
        "模型能力与 MCP 检测通过",
      );
    });
  });

  test("heartbeat uses the backend role contract and does not flag optional processes", async () => {
    renderPage(
      diagnostics({
        providers: [
          {
            capability: "embedding",
            provider: "gateway",
            model: "bge-m3",
            configuration_ready: true,
            status: "healthy",
            last_probe_at: "2026-07-25T06:05:00Z",
            last_success_at: "2026-07-25T06:05:00Z",
            last_failure_at: null,
            probe_age_seconds: 3,
            latency_ms: 120,
            consecutive_failures: 0,
            error_code: null,
          },
        ],
        heartbeats: [
          {
            role: "api",
            required: true,
            status: "healthy",
            instance: "api-1",
            version: "release-42",
            recorded_at: "2026-07-25T06:05:00Z",
            age_seconds: 10,
          },
          {
            role: "worker",
            required: false,
            status: "missing",
            instance: null,
            version: null,
            recorded_at: null,
            age_seconds: null,
          },
          {
            role: "beat",
            required: false,
            status: "missing",
            instance: null,
            version: null,
            recorded_at: null,
            age_seconds: null,
          },
        ],
      }),
    );

    await openPanel("运行进程");
    expect(screen.getByText("API 服务")).toBeDefined();
    expect(screen.getByText("api-1 · release-42")).toBeDefined();
    expect(screen.getAllByText("当前模式不需要")).toHaveLength(2);
    expect(screen.queryByText(/Worker未正常上报/)).toBeNull();
  });

  test("skills, MCP, agents, schedules and KB pipelines are included", async () => {
    renderPage(
      diagnostics({
        providers: [
          {
            capability: "embedding",
            provider: "gateway",
            model: "bge-m3",
            configuration_ready: true,
            status: "healthy",
            last_probe_at: "2026-07-25T06:05:00Z",
            last_success_at: "2026-07-25T06:05:00Z",
            last_failure_at: null,
            probe_age_seconds: 3,
            latency_ms: 120,
            consecutive_failures: 0,
            error_code: null,
          },
        ],
        skills: {
          status: "degraded",
          total: 2,
          invalid: [{ slug: "broken-skill", code: "skill_manifest_invalid" }],
        },
        mcp_servers: [
          {
            server_id: "mcp-1",
            org_id: null,
            name: "travel-tools",
            transport: "streamable_http",
            enabled: true,
            status: "offline",
            latency_ms: 500,
            checked_at: "2026-07-25T06:05:00Z",
            tools_count: 0,
            bound_agents: 1,
            error_code: "mcp_connection_failed",
          },
        ],
        agents: [
          {
            org_id: "org-1",
            org_name: "测试租户",
            status: "degraded",
            total_cards: 2,
            enabled_cards: 2,
            ready_cards: 1,
            running_sessions: 1,
            issues: [
              {
                code: "agent_skill_missing",
                card: "workbench",
                items: ["missing-skill"],
              },
            ],
          },
        ],
        schedules: {
          system: [
            {
              name: "icron-tick",
              label: "Agent 定时任务调度",
              task: "icron.tick",
              interval_seconds: 60,
              enabled: true,
              runner: "api",
              status: "healthy",
            },
          ],
          custom: [
            {
              org_id: "org-1",
              org_name: "测试租户",
              active_count: 2,
              paused_count: 0,
              archived_count: 0,
              overdue_count: 1,
              failed_runs_24h: 0,
            },
          ],
        },
        organizations: [
          {
            org_id: "org-1",
            org_name: "测试租户",
            remediation: ["上传文档停留时间过长"],
            health: {
              outbox: {
                pending_count: 1,
                oldest_pending_age_seconds: 30,
                dead_letter_count: 0,
                oldest_dead_letter_age_seconds: null,
              },
              documents: {
                uploaded_count: 1,
                oldest_uploaded_age_seconds: 1200,
                processing_count: 0,
                oldest_processing_age_seconds: null,
                failed_count: 0,
              },
              ingest_leases: { count: 0, oldest_age_seconds: null },
              image_enrichment: { count: 0, oldest_age_seconds: null },
              snapshot_builds: { count: 0, oldest_age_seconds: null },
              object_metadata_inconsistencies: {
                count: 0,
                oldest_age_seconds: null,
              },
              media_projection_failures: {
                count: 0,
                oldest_age_seconds: null,
              },
              vectorless_chunks: 0,
              pending_claims: 0,
            },
          },
        ],
      }),
    );

    expect(
      await screen.findByRole("button", { name: "查看Skills 与 MCP详情" }),
    ).toBeDefined();
    expect(screen.queryByText("Skill「broken-skill」不可加载")).toBeNull();
    expect(screen.queryByText("MCP「travel-tools」离线")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "查看问题清单" }));
    await screen.findByRole("dialog");
    expect(screen.getByText("Skill「broken-skill」不可加载")).toBeDefined();
    expect(screen.getByText("MCP「travel-tools」离线")).toBeDefined();
    expect(screen.getByText("1 个租户存在 Agent 配置问题")).toBeDefined();
    expect(screen.getByText("1 个租户的 Agent 定时任务异常")).toBeDefined();
    expect(screen.getByText("1 个租户的知识采集链路需要处理")).toBeDefined();

    await closePanel();
    const ecosystemDialog = await openPanel("Skills 与 MCP");
    expect(within(ecosystemDialog).getByText("broken-skill")).toBeDefined();
    expect(within(ecosystemDialog).getByText("travel-tools")).toBeDefined();

    await closePanel();
    const agentDialog = await openPanel("Agents 与定时任务");
    expect(within(agentDialog).getAllByText("测试租户")).toHaveLength(2);
    expect(within(agentDialog).getByText("1 个逾期")).toBeDefined();

    await closePanel();
    const knowledgeDialog = await openPanel("知识采集链路");
    fireEvent.click(within(knowledgeDialog).getByText("测试租户"));
    expect(
      within(knowledgeDialog).getByText(/上传文档停留时间过长/),
    ).toBeDefined();
  });
});
