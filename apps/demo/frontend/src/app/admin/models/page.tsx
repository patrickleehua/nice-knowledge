"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Pencil,
  Plus,
  Route,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  FormField,
  PageHeader,
  Spinner,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type {
  AdminOrg,
  LlmProviderDto,
  ModelCapability,
  ModelRouteDto,
  ModelRouteTaskDto,
} from "@/lib/types";

// 协议色点:路由链徽章按提供商实例的接入协议着色
const PROTOCOL_DOT: Record<string, string> = {
  openai: "bg-teal",
  anthropic: "bg-warning",
};

// 四个系统能力板块。与后端 capability_routes.SYSTEM_ROUTE_CAPABILITIES 一一对应：
// 板块的模型候选按这些能力过滤，视觉需要同时具备 generation + vision。
interface SystemRouteDef {
  label: string;
  description: string;
  capabilities: ModelCapability[];
  /** 仅 OpenAI 兼容协议可服务（/embeddings 与 /rerank 线）。 */
  openaiOnly: boolean;
  timeout: number;
  maxTokens: number;
  /** 嵌入/重排不消耗生成 token,表单不显示 max_tokens。 */
  showsMaxTokens: boolean;
}

const SYSTEM_ROUTES: Record<string, SystemRouteDef> = {
  "llm.default": {
    label: "对话模型",
    description: "任务没有专属路由时的统一兜底",
    capabilities: ["generation"],
    openaiOnly: false,
    timeout: 120,
    maxTokens: 8192,
    showsMaxTokens: true,
  },
  "kb.image.caption": {
    label: "视觉模型",
    description: "图片描述与 agent 图像核验",
    capabilities: ["generation", "vision"],
    openaiOnly: false,
    timeout: 120,
    maxTokens: 2048,
    showsMaxTokens: true,
  },
  "kb.embedding": {
    label: "嵌入模型",
    description: "知识库向量化；换主模型会先启动重嵌",
    capabilities: ["embedding"],
    openaiOnly: true,
    timeout: 30,
    maxTokens: 1,
    showsMaxTokens: false,
  },
  "kb.search.rerank": {
    label: "重排序模型",
    description: "检索结果重排；按顺序即时降级",
    capabilities: ["rerank"],
    openaiOnly: true,
    timeout: 3,
    maxTokens: 1,
    showsMaxTokens: false,
  },
};

const SYSTEM_ROUTE_TASKS = Object.keys(SYSTEM_ROUTES);

const CATEGORY_LABELS: Record<string, string> = {
  agent: "Agent",
  kb: "知识库",
  pipeline: "业务流水线",
  quality: "质量与评测",
  render: "交付输出",
  other: "其他任务",
};

// 标签和色值与模型提供商页的模型类型保持一致。
const MODEL_CAPABILITY_DETAILS: Record<
  ModelCapability,
  { label: string; color: string }
> = {
  generation: { label: "生成", color: "#13c2c2" },
  vision: { label: "视觉", color: "#00b96b" },
  web_search: { label: "联网", color: "#1677ff" },
  reasoning: { label: "推理", color: "#6372bd" },
  function_call: { label: "工具", color: "#f18737" },
  rerank: { label: "重排", color: "#6495ED" },
  embedding: { label: "嵌入", color: "#FFA500" },
};

function routeCapabilities(task: string): ModelCapability[] {
  return SYSTEM_ROUTES[task]?.capabilities ?? ["generation"];
}

interface Hop {
  provider: string;
  model: string;
}

interface CatalogModelOption {
  model: string;
  capabilities: ModelCapability[];
}

interface RouteForm {
  id: string | null; // null = 新建
  org_id: string; // "" = 平台默认
  task: string;
  tasks: string[]; // 批量新建的任务
  primary_provider: string;
  primary_model: string;
  fallback_chain: Hop[];
  max_tokens: number;
  timeout_seconds: number;
}

const EMPTY_FORM: RouteForm = {
  id: null,
  org_id: "",
  task: "",
  tasks: [],
  primary_provider: "openai",
  primary_model: "",
  fallback_chain: [],
  max_tokens: 8192,
  timeout_seconds: 120,
};

/** 路由链徽章:实例名+模型,按接入协议着色点;primary 实底、fallback 描边灰。 */
function HopBadge({
  hop,
  primary,
  protocolOf,
}: {
  hop: Hop;
  primary?: boolean;
  protocolOf: (name: string) => string | undefined;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 font-mono text-xs",
        primary
          ? "border border-primary/30 bg-primary/10 text-foreground"
          : "border border-border bg-muted/40 text-muted-foreground",
      )}
      title={hop.provider}
    >
      <span
        className={cn(
          "size-1.5 rounded-full",
          PROTOCOL_DOT[protocolOf(hop.provider) ?? ""] ?? "bg-muted-foreground",
        )}
      />
      {hop.provider}/{hop.model || "?"}
    </span>
  );
}

/** 表格里的整条路由链:主模型 → 降级链,箭头连接。 */
function RouteChain({
  route,
  protocolOf,
}: {
  route: ModelRouteDto;
  protocolOf: (name: string) => string | undefined;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <HopBadge
        primary
        protocolOf={protocolOf}
        hop={{ provider: route.primary_provider, model: route.primary_model }}
      />
      {(route.fallback_chain ?? []).map((hop, index) => (
        <span key={index} className="flex items-center gap-1.5">
          <ArrowRight className="size-3 text-muted-foreground/60" />
          <HopBadge hop={hop} protocolOf={protocolOf} />
        </span>
      ))}
    </div>
  );
}

function ModelCapabilityBadges({
  capabilities,
}: {
  capabilities: ModelCapability[];
}) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      {capabilities.map((capability) => {
        const detail = MODEL_CAPABILITY_DETAILS[capability];
        return (
          <span
            key={capability}
            className="inline-flex rounded-full px-1.5 py-0.5 text-[10px] leading-none whitespace-nowrap"
            style={{
              color: detail.color,
              backgroundColor: `${detail.color}20`,
            }}
          >
            {detail.label}
          </span>
        );
      })}
    </span>
  );
}

function ModelSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: CatalogModelOption[];
  onChange: (value: string) => void;
}) {
  const selected = options.find((option) => option.model === value);
  const items = Object.fromEntries(
    options.map((option) => [option.model, option.model]),
  );

  return (
    <Select
      items={items}
      value={selected?.model ?? null}
      onValueChange={(nextValue) => onChange(nextValue as string)}
    >
      <SelectTrigger
        aria-label={label}
        aria-invalid={Boolean(value && !selected)}
        className="h-auto min-h-9 w-full min-w-0"
        disabled={!options.length}
      >
        {selected ? (
          <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5 py-0.5">
            <span className="truncate font-mono text-xs">{selected.model}</span>
            <ModelCapabilityBadges capabilities={selected.capabilities} />
          </span>
        ) : (
          <span className="min-w-0 flex-1 truncate text-left text-muted-foreground">
            {options.length
              ? value
                ? "重新选择模型"
                : "选择模型"
              : "没有可用模型"}
          </span>
        )}
      </SelectTrigger>
      <SelectContent
        alignItemWithTrigger={false}
        className="w-[min(30rem,calc(100vw-2rem))] max-w-[calc(100vw-2rem)]"
      >
        {options.map((option) => (
          <SelectItem
            key={option.model}
            value={option.model}
            label={option.model}
            className="py-2"
          >
            <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
              <span className="font-mono text-xs">{option.model}</span>
              <ModelCapabilityBadges capabilities={option.capabilities} />
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export default function AdminModelsPage() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<RouteForm | null>(null);
  const [taskQuery, setTaskQuery] = useState("");

  const routesQuery = useQuery({
    queryKey: ["admin-models"],
    queryFn: () => api.get<ModelRouteDto[]>("/admin/models"),
  });
  const taskCatalogQuery = useQuery({
    queryKey: ["admin-model-route-tasks"],
    queryFn: () => api.get<ModelRouteTaskDto[]>("/admin/model-route-tasks"),
  });
  const { data: orgs } = useQuery({
    queryKey: ["admin-orgs"],
    queryFn: () => api.get<AdminOrg[]>("/admin/orgs"),
  });
  // 提供商实例(Cherry Studio 式多实例):下拉选项、协议色点、模型候选都来自这里
  const providersQuery = useQuery({
    queryKey: ["admin-providers"],
    queryFn: () => api.get<LlmProviderDto[]>("/admin/providers"),
    staleTime: 2 * 60 * 1000,
  });
  const providerNames = useMemo(
    () =>
      (providersQuery.data ?? []).filter((p) => p.enabled).map((p) => p.name),
    [providersQuery.data],
  );
  const providerOf = (name: string) =>
    providersQuery.data?.find((provider) => provider.name === name);
  const protocolOf = (name: string) => providerOf(name)?.protocol;
  // 能力组合按"全部满足"过滤:视觉板块要求 generation + vision 同时具备。
  const modelsOf = (name: string, capabilities: ModelCapability[] = []) => {
    const provider = providerOf(name);
    if (!provider) return [];
    if (!capabilities.length) return provider.models;
    return provider.models.filter((model) =>
      capabilities.every((capability) =>
        provider.model_metadata?.[model]?.capabilities.includes(capability),
      ),
    );
  };
  const modelOptionsOf = (
    name: string,
    capabilities: ModelCapability[],
  ): CatalogModelOption[] => {
    const provider = providerOf(name);
    if (!provider) return [];
    return modelsOf(name, capabilities).map((model) => ({
      model,
      capabilities: provider.model_metadata?.[model]?.capabilities ?? [],
    }));
  };
  const providerNamesForTask = (task: string) => {
    const definition = SYSTEM_ROUTES[task];
    const capabilities = routeCapabilities(task);
    return providerNames.filter(
      (name) =>
        (!definition?.openaiOnly || protocolOf(name) === "openai") &&
        modelsOf(name, capabilities).length > 0,
    );
  };
  const firstModelFor = (provider: string, task: string) =>
    modelsOf(provider, routeCapabilities(task))[0] ?? "";

  const orgName = (id: string | null) =>
    id === null
      ? "平台默认"
      : (orgs?.find((o) => o.id === id)?.name ?? id.slice(0, 8));

  const scopeItems: Record<string, string> = useMemo(
    () => ({
      "": "平台默认(全部租户)",
      ...Object.fromEntries((orgs ?? []).map((o) => [o.id, `仅 ${o.name}`])),
    }),
    [orgs],
  );

  const taskCatalogByTask = useMemo(
    () =>
      new Map((taskCatalogQuery.data ?? []).map((item) => [item.task, item])),
    [taskCatalogQuery.data],
  );
  const customRoutes = useMemo(
    () =>
      (routesQuery.data ?? []).filter((route) => !SYSTEM_ROUTES[route.task]),
    [routesQuery.data],
  );

  const saveMutation = useMutation<
    ModelRouteDto | ModelRouteDto[],
    Error,
    RouteForm
  >({
    mutationFn: (f: RouteForm) => {
      const payload = {
        primary_provider: f.primary_provider,
        primary_model: f.primary_model.trim(),
        fallback_chain: f.fallback_chain.map((hop) => ({
          provider: hop.provider,
          model: hop.model.trim(),
        })),
        max_tokens: f.max_tokens,
        timeout_seconds: f.timeout_seconds,
      };
      if (f.id) {
        return api.patch<ModelRouteDto>(`/admin/models/${f.id}`, payload);
      }
      if (SYSTEM_ROUTES[f.task]) {
        return api.post<ModelRouteDto>("/admin/models", {
          ...payload,
          task: f.task,
          org_id: null,
        });
      }
      return api.post<ModelRouteDto[]>("/admin/models/batch", {
        ...payload,
        tasks: f.tasks,
        org_id: f.org_id || null,
      });
    },
    onSuccess: (_, savedForm) => {
      toast.success(
        !savedForm.id && savedForm.tasks.length > 1
          ? `已创建 ${savedForm.tasks.length} 条任务专属路由`
          : "路由已保存",
      );
      setForm(null);
      setTaskQuery("");
      queryClient.invalidateQueries({ queryKey: ["admin-models"] });
      queryClient.invalidateQueries({
        queryKey: ["admin-model-route-tasks"],
      });
    },
    onError: (err) => toast.error(errMsg(err, "保存失败")),
  });

  const toggleMutation = useMutation({
    mutationFn: (r: ModelRouteDto) =>
      api.patch<ModelRouteDto>(`/admin/models/${r.id}`, {
        is_active: !r.is_active,
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["admin-models"] }),
    onError: (err) => toast.error(errMsg(err, "操作失败")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/models/${id}`),
    onSuccess: () => {
      toast.success("路由已删除");
      queryClient.invalidateQueries({ queryKey: ["admin-models"] });
    },
    onError: (err) => toast.error(errMsg(err, "删除失败")),
  });

  function openEdit(r: ModelRouteDto) {
    setForm({
      id: r.id,
      org_id: r.org_id ?? "",
      task: r.task,
      tasks: [],
      primary_provider: r.primary_provider,
      primary_model: r.primary_model,
      fallback_chain: r.fallback_chain ?? [],
      max_tokens: r.max_tokens,
      timeout_seconds: r.timeout_seconds,
    });
  }

  // 系统板块只认平台级(org_id = null)那一行
  const systemRouteOf = (task: string) =>
    routesQuery.data?.find(
      (route) => route.task === task && route.org_id === null,
    );

  function openSystemRoute(task: string) {
    const existing = systemRouteOf(task);
    if (existing) {
      openEdit(existing);
      return;
    }
    const eligibleProviders = providerNamesForTask(task);
    const primaryProvider = eligibleProviders[0] ?? "";
    setForm({
      ...EMPTY_FORM,
      task,
      org_id: "",
      primary_provider: primaryProvider,
      primary_model: firstModelFor(primaryProvider, task),
      max_tokens: SYSTEM_ROUTES[task].maxTokens,
      timeout_seconds: SYSTEM_ROUTES[task].timeout,
    });
  }

  function patchHop(index: number, patch: Partial<Hop>) {
    if (!form) return;
    const chain = [
      {
        provider: form.primary_provider,
        model: form.primary_model,
      },
      ...form.fallback_chain,
    ];
    chain[index] = { ...chain[index], ...patch };
    const [primary, ...fallbackChain] = chain;
    setForm({
      ...form,
      primary_provider: primary.provider,
      primary_model: primary.model,
      fallback_chain: fallbackChain,
    });
  }

  function moveHop(index: number, delta: -1 | 1) {
    if (!form) return;
    const chain = [
      {
        provider: form.primary_provider,
        model: form.primary_model,
      },
      ...form.fallback_chain,
    ];
    const target = index + delta;
    if (target < 0 || target >= chain.length) return;
    [chain[index], chain[target]] = [chain[target], chain[index]];
    const [primary, ...fallbackChain] = chain;
    setForm({
      ...form,
      primary_provider: primary.provider,
      primary_model: primary.model,
      fallback_chain: fallbackChain,
    });
  }

  function removeHop(index: number) {
    if (!form || index === 0) return;
    const chain = [
      {
        provider: form.primary_provider,
        model: form.primary_model,
      },
      ...form.fallback_chain,
    ].filter((_, current) => current !== index);
    const [primary, ...fallbackChain] = chain;
    setForm({
      ...form,
      primary_provider: primary.provider,
      primary_model: primary.model,
      fallback_chain: fallbackChain,
    });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns: ColumnDef<ModelRouteDto, any>[] = [
    {
      accessorKey: "task",
      header: "任务",
      cell: ({ row }) => {
        const metadata = taskCatalogByTask.get(row.original.task);
        return (
          <div>
            <div className="text-xs font-medium">
              {metadata?.label ?? row.original.task}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              {row.original.task}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {orgName(row.original.org_id)}
            </div>
          </div>
        );
      },
    },
    {
      id: "chain",
      header: "路由链(主模型 → 降级)",
      enableSorting: false,
      cell: ({ row }) => (
        <RouteChain route={row.original} protocolOf={protocolOf} />
      ),
    },
    {
      id: "limits",
      header: "参数",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-xs text-muted-foreground">
          {row.original.max_tokens.toLocaleString()} tok ·{" "}
          {row.original.timeout_seconds}s
        </span>
      ),
    },
    {
      accessorKey: "is_active",
      header: "启用",
      enableSorting: false,
      cell: ({ row }) => (
        <Switch
          checked={row.original.is_active}
          onCheckedChange={() => toggleMutation.mutate(row.original)}
          aria-label={row.original.is_active ? "停用路由" : "启用路由"}
        />
      ),
    },
    {
      id: "__actions__",
      enableSorting: false,
      header: () => <span className="sr-only">操作</span>,
      cell: ({ row }) => (
        <div className="text-right">
          <Button
            variant="ghost"
            size="icon"
            aria-label="编辑路由"
            onClick={() => openEdit(row.original)}
          >
            <Pencil className="size-4" />
          </Button>
          <ConfirmDialog
            trigger={
              <Button variant="ghost" size="icon" aria-label="删除路由">
                <Trash2 className="size-4 text-destructive" />
              </Button>
            }
            title={`删除任务「${row.original.task}」的路由?`}
            description="删除后该任务将继承上方的对话模型。"
            destructive
            confirmLabel="删除"
            onConfirm={() => deleteMutation.mutateAsync(row.original.id)}
          />
        </div>
      ),
    },
  ];

  const formCapabilities = routeCapabilities(form?.task ?? "");
  const formDefinition = SYSTEM_ROUTES[form?.task ?? ""];
  const formIsSystem = Boolean(formDefinition);
  const formTaskMetadata = taskCatalogByTask.get(form?.task ?? "");
  const formShowsMaxTokens = !formIsSystem || formDefinition.showsMaxTokens;
  const formProviderNames = providerNamesForTask(form?.task ?? "");
  const formProviderItems = Object.fromEntries(
    formProviderNames.map((name) => [name, name]),
  );
  const eligibleHop = (hop: Hop) =>
    formProviderNames.includes(hop.provider) &&
    (!formCapabilities.length ||
      modelsOf(hop.provider, formCapabilities).includes(hop.model.trim()));
  const formChain: Hop[] = form
    ? [
        {
          provider: form.primary_provider,
          model: form.primary_model,
        },
        ...form.fallback_chain,
      ]
    : [];
  const chainKeys = formChain
    .filter((hop) => hop.provider && hop.model.trim())
    .map((hop) => `${hop.provider}\u0000${hop.model.trim()}`);
  const hasDuplicateHop = new Set(chainKeys).size !== chainKeys.length;
  const configuredTasks = new Set(
    customRoutes
      .filter((route) => (route.org_id ?? "") === (form?.org_id ?? ""))
      .map((route) => route.task),
  );
  const customTaskCatalog = (taskCatalogQuery.data ?? []).filter(
    (item) => !item.is_system,
  );
  const taskNeedle = taskQuery.trim().toLocaleLowerCase();
  const visibleTasks = customTaskCatalog.filter(
    (item) =>
      !taskNeedle ||
      [item.label, item.task, item.description].some((value) =>
        value.toLocaleLowerCase().includes(taskNeedle),
      ),
  );
  const selectableVisibleTasks = visibleTasks.filter(
    (item) => !configuredTasks.has(item.task),
  );
  const selectedTasks = new Set(form?.tasks ?? []);
  const allVisibleSelected =
    selectableVisibleTasks.length > 0 &&
    selectableVisibleTasks.every((item) => selectedTasks.has(item.task));
  const someVisibleSelected = selectableVisibleTasks.some((item) =>
    selectedTasks.has(item.task),
  );
  const groupedTasks = Object.entries(
    visibleTasks.reduce<Record<string, ModelRouteTaskDto[]>>((groups, item) => {
      (groups[item.category] ??= []).push(item);
      return groups;
    }, {}),
  );
  const hasTaskTarget = Boolean(
    form && (form.id || formIsSystem ? form.task : form.tasks.length > 0),
  );
  const canSave =
    form &&
    hasTaskTarget &&
    (!formCapabilities.length || form.org_id === "") &&
    !hasDuplicateHop &&
    formChain.every(
      (hop) => hop.provider && hop.model.trim() && eligibleHop(hop),
    );

  function toggleTask(task: string, checked: boolean) {
    if (!form) return;
    const next = new Set(form.tasks);
    if (checked) next.add(task);
    else next.delete(task);
    setForm({ ...form, tasks: [...next] });
  }

  function toggleVisibleTasks(checked: boolean) {
    if (!form) return;
    const next = new Set(form.tasks);
    for (const item of selectableVisibleTasks) {
      if (checked) next.add(item.task);
      else next.delete(item.task);
    }
    setForm({ ...form, tasks: [...next] });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="模型路由"
        description="先配置系统能力基线；只有需要不同模型策略的任务再添加专属覆盖"
        actions={
          <Button
            onClick={() => {
              setTaskQuery("");
              const primaryProvider = providerNamesForTask("")[0] ?? "";
              setForm({
                ...EMPTY_FORM,
                primary_provider: primaryProvider,
                primary_model: firstModelFor(primaryProvider, ""),
              });
            }}
          >
            <Plus className="size-4" />
            添加任务专属路由
          </Button>
        }
      />

      {/* 四个系统能力板块:系统运行所需的外部模型能力,各自一张卡。 */}
      <div>
        <div className="mb-3">
          <h2 className="text-sm font-medium">系统能力基线</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            对话模型是普通生成任务的默认路由；视觉、嵌入和重排序使用各自的能力路由。
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {SYSTEM_ROUTE_TASKS.map((task) => {
            const definition = SYSTEM_ROUTES[task];
            const route = systemRouteOf(task);
            const eligibleCount = providerNamesForTask(task).length;
            return (
              <section
                key={task}
                className={cn(
                  "flex flex-col gap-2 rounded-lg border p-4",
                  route
                    ? "border-border bg-card"
                    : "border-warning/40 bg-warning/5",
                )}
              >
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-sm font-medium">
                        {definition.label}
                      </h2>
                      {route ? (
                        route.is_active ? (
                          <ToneBadge tone="success">已启用</ToneBadge>
                        ) : (
                          <ToneBadge tone="muted">已停用</ToneBadge>
                        )
                      ) : (
                        <ToneBadge tone="warning">未配置</ToneBadge>
                      )}
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {definition.description}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant={route ? "outline" : "default"}
                    className="h-7 shrink-0"
                    disabled={!route && eligibleCount === 0}
                    title={
                      !route && eligibleCount === 0
                        ? "没有已标注该能力的模型，先在模型提供商导入并标注"
                        : undefined
                    }
                    onClick={() => openSystemRoute(task)}
                  >
                    {route ? "调整" : "配置"}
                  </Button>
                </div>
                {route ? (
                  <RouteChain route={route} protocolOf={protocolOf} />
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {eligibleCount === 0
                      ? "没有可用模型：先在模型提供商导入并标注能力"
                      : `${eligibleCount} 个提供商有可用模型`}
                  </p>
                )}
              </section>
            );
          })}
        </div>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="mb-3 text-sm font-medium">
            任务专属路由
            <p className="mt-0.5 text-xs font-normal text-muted-foreground">
              仅列出覆盖项；没有专属路由的普通任务自动继承上方“对话模型”。
            </p>
          </div>
          <DataTable
            columns={columns}
            data={customRoutes}
            isLoading={routesQuery.isLoading}
            error={routesQuery.error}
            onRetry={() => routesQuery.refetch()}
            getRowId={(r) => r.id}
            empty={{
              icon: Route,
              title: "还没有任务专属路由",
              description: "当前所有普通生成任务都继承上方的对话模型。",
            }}
          />
        </CardContent>
      </Card>

      <Dialog
        open={form !== null}
        onOpenChange={(open) => {
          if (!open) {
            setForm(null);
            setTaskQuery("");
          }
        }}
      >
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {formIsSystem
                ? `配置${formDefinition?.label ?? "系统路由"}`
                : form?.id
                  ? "编辑任务专属路由"
                  : "添加任务专属路由"}
            </DialogTitle>
          </DialogHeader>
          {form && (
            <>
              <div className="space-y-5">
                {!form.id && !formIsSystem && (
                  <>
                    <div className="rounded-lg border border-primary/20 bg-primary/5 p-3">
                      <p className="text-sm font-medium">
                        专属路由会覆盖“对话模型”
                      </p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        未勾选的普通任务继续继承上方对话模型；视觉、嵌入和重排序仍由各自的系统能力卡负责。
                      </p>
                    </div>

                    <FormField label="作用域" htmlFor="route-scope">
                      <Select
                        items={scopeItems}
                        value={form.org_id}
                        onValueChange={(value) =>
                          setForm({
                            ...form,
                            org_id: value as string,
                            tasks: [],
                          })
                        }
                      >
                        <SelectTrigger id="route-scope" className="h-9 w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="">平台默认(全部租户)</SelectItem>
                          {orgs?.map((org) => (
                            <SelectItem key={org.id} value={org.id}>
                              仅 {org.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </FormField>

                    <section aria-labelledby="route-task-picker-title">
                      <div className="mb-2 flex items-end justify-between gap-3">
                        <div>
                          <h3
                            id="route-task-picker-title"
                            className="text-sm font-medium"
                          >
                            选择需要单独配置的任务
                            <span className="ml-1 text-destructive">*</span>
                          </h3>
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            可多选；已配置任务需回到列表逐条编辑。
                          </p>
                        </div>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          已选 {form.tasks.length} 项
                        </span>
                      </div>

                      <div className="relative">
                        <Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" />
                        <Input
                          aria-label="搜索任务"
                          value={taskQuery}
                          onChange={(event) => setTaskQuery(event.target.value)}
                          placeholder="搜索任务名称、说明或标识"
                          className="pl-9"
                        />
                      </div>

                      <div className="mt-2 flex items-center gap-2 rounded-md border px-3 py-2">
                        <Checkbox
                          id="route-select-visible"
                          checked={allVisibleSelected}
                          indeterminate={
                            someVisibleSelected && !allVisibleSelected
                          }
                          disabled={!selectableVisibleTasks.length}
                          onCheckedChange={(checked) =>
                            toggleVisibleTasks(checked)
                          }
                        />
                        <label
                          htmlFor="route-select-visible"
                          className="flex-1 cursor-pointer text-xs font-medium"
                        >
                          全选当前结果
                        </label>
                        <span className="text-xs text-muted-foreground">
                          {selectableVisibleTasks.length} 项可选
                        </span>
                      </div>

                      <div className="mt-2 max-h-64 overflow-y-auto rounded-lg border">
                        {taskCatalogQuery.isLoading ? (
                          <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
                            <Spinner size={4} />
                            正在加载任务
                          </div>
                        ) : taskCatalogQuery.error ? (
                          <div className="p-6 text-center">
                            <p className="text-sm text-destructive">
                              任务目录加载失败
                            </p>
                            <Button
                              className="mt-2"
                              size="sm"
                              variant="outline"
                              onClick={() => taskCatalogQuery.refetch()}
                            >
                              重试
                            </Button>
                          </div>
                        ) : groupedTasks.length === 0 ? (
                          <p className="p-8 text-center text-sm text-muted-foreground">
                            没有匹配的任务
                          </p>
                        ) : (
                          groupedTasks.map(([category, tasks]) => (
                            <div
                              key={category}
                              className="border-b last:border-b-0"
                            >
                              <h4 className="bg-muted/40 px-3 py-1.5 text-[11px] font-medium text-muted-foreground">
                                {CATEGORY_LABELS[category] ?? category}
                              </h4>
                              {tasks.map((item) => {
                                const configured = configuredTasks.has(
                                  item.task,
                                );
                                const checkboxId = `route-task-${item.task}`;
                                return (
                                  <div
                                    key={item.task}
                                    className={cn(
                                      "flex items-start gap-3 border-t px-3 py-2.5 first:border-t-0",
                                      configured && "bg-muted/20",
                                    )}
                                  >
                                    <Checkbox
                                      id={checkboxId}
                                      className="mt-0.5"
                                      checked={selectedTasks.has(item.task)}
                                      disabled={configured}
                                      onCheckedChange={(checked) =>
                                        toggleTask(item.task, checked)
                                      }
                                    />
                                    <label
                                      htmlFor={checkboxId}
                                      className={cn(
                                        "min-w-0 flex-1",
                                        configured
                                          ? "cursor-not-allowed"
                                          : "cursor-pointer",
                                      )}
                                    >
                                      <span className="flex flex-wrap items-center gap-2 text-xs font-medium">
                                        {item.label}
                                        {configured && (
                                          <ToneBadge tone="muted">
                                            已配置
                                          </ToneBadge>
                                        )}
                                      </span>
                                      {item.description && (
                                        <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                                          {item.description}
                                        </span>
                                      )}
                                      <span className="mt-0.5 block font-mono text-[11px] text-muted-foreground">
                                        {item.task}
                                      </span>
                                    </label>
                                  </div>
                                );
                              })}
                            </div>
                          ))
                        )}
                      </div>
                    </section>
                  </>
                )}

                {(form.id || formIsSystem) && (
                  <div className="rounded-lg border bg-muted/20 p-3">
                    <p className="text-sm font-medium">
                      {formDefinition?.label ??
                        formTaskMetadata?.label ??
                        form.task}
                    </p>
                    <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                      {form.task}
                      {!formIsSystem && ` · ${orgName(form.org_id || null)}`}
                    </p>
                  </div>
                )}

                <FormField
                  label={
                    formCapabilities.length
                      ? `模型尝试顺序（仅显示具备 ${formCapabilities
                          .map(
                            (capability) =>
                              MODEL_CAPABILITY_DETAILS[capability].label,
                          )
                          .join(" + ")} 能力的模型）`
                      : "模型尝试顺序"
                  }
                >
                  <p className="mb-2 text-xs text-muted-foreground">
                    第 1
                    个是主模型，失败后依次尝试后续模型；可用箭头调整完整顺序。
                  </p>
                  <div className="space-y-2">
                    {formChain.map((hop, index) => {
                      const hopLabel = index === 0 ? "主模型" : `降级 ${index}`;
                      const providerSelectLabel =
                        index === 0 ? "主模型提供商" : `${hopLabel}提供商`;
                      const modelSelectLabel =
                        index === 0 ? "主模型选择" : `${hopLabel}模型选择`;
                      const providerEligible = formProviderNames.includes(
                        hop.provider,
                      );
                      const modelOptions = modelOptionsOf(
                        hop.provider,
                        formCapabilities,
                      );
                      const modelEligible = modelOptions.some(
                        (option) => option.model === hop.model,
                      );
                      return (
                        <div
                          key={index}
                          className="rounded-lg border bg-card p-2"
                        >
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={cn(
                                "flex h-7 w-16 shrink-0 items-center justify-center rounded-md text-xs font-medium",
                                index === 0
                                  ? "bg-primary/10 text-primary"
                                  : "bg-muted text-muted-foreground",
                              )}
                            >
                              {hopLabel}
                            </span>
                            <Select
                              items={formProviderItems}
                              value={providerEligible ? hop.provider : null}
                              onValueChange={(value) => {
                                const provider = value as string;
                                patchHop(index, {
                                  provider,
                                  model: firstModelFor(provider, form.task),
                                });
                              }}
                            >
                              <SelectTrigger
                                aria-label={providerSelectLabel}
                                aria-invalid={!providerEligible}
                                className="h-9 w-32 shrink-0"
                              >
                                <span className="min-w-0 flex-1 truncate text-left">
                                  {providerEligible
                                    ? hop.provider
                                    : "重新选择提供商"}
                                </span>
                              </SelectTrigger>
                              <SelectContent>
                                {formProviderNames.map((provider) => (
                                  <SelectItem key={provider} value={provider}>
                                    {provider}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            <div className="min-w-48 flex-1">
                              <ModelSelect
                                label={modelSelectLabel}
                                value={hop.model}
                                options={modelOptions}
                                onChange={(value) =>
                                  patchHop(index, { model: value })
                                }
                              />
                            </div>
                            <span className="flex shrink-0 items-center">
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                aria-label={`上移 ${hop.provider}/${hop.model || "未选择模型"}`}
                                disabled={index === 0}
                                onClick={() => moveHop(index, -1)}
                              >
                                <ChevronUp className="size-3.5" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                aria-label={`下移 ${hop.provider}/${hop.model || "未选择模型"}`}
                                disabled={index === formChain.length - 1}
                                onClick={() => moveHop(index, 1)}
                              >
                                <ChevronDown className="size-3.5" />
                              </Button>
                              {index > 0 && (
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label={`移除 ${hop.provider}/${hop.model || "未选择模型"}`}
                                  onClick={() => removeHop(index)}
                                >
                                  <X className="size-3.5" />
                                </Button>
                              )}
                            </span>
                          </div>
                          {!providerEligible ? (
                            <p
                              role="alert"
                              className="mt-2 text-xs text-destructive"
                            >
                              当前提供商已停用或没有满足要求的目录模型，请重新选择提供商。
                            </p>
                          ) : !modelEligible ? (
                            <p
                              role="alert"
                              className="mt-2 text-xs text-destructive"
                            >
                              {hop.model ? (
                                <>
                                  原模型{" "}
                                  <span className="font-mono">{hop.model}</span>{" "}
                                  已不在当前可用目录中，请重新选择模型。
                                </>
                              ) : (
                                "请选择模型。"
                              )}
                            </p>
                          ) : null}
                        </div>
                      );
                    })}
                    {hasDuplicateHop && (
                      <p role="alert" className="text-xs text-destructive">
                        同一个提供商和模型不能在路由链中重复。
                      </p>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!formProviderNames.length}
                      onClick={() => {
                        const existing = new Set(chainKeys);
                        const candidate = formProviderNames
                          .flatMap((provider) =>
                            modelsOf(provider, formCapabilities).map(
                              (model) => ({ provider, model }),
                            ),
                          )
                          .find(
                            (hop) =>
                              !existing.has(
                                `${hop.provider}\u0000${hop.model}`,
                              ),
                          );
                        const next = candidate ?? {
                          provider: formProviderNames[0] ?? "",
                          model: "",
                        };
                        setForm({
                          ...form,
                          fallback_chain: [...form.fallback_chain, next],
                        });
                      }}
                    >
                      <Plus className="size-4" />
                      添加降级模型
                    </Button>
                  </div>
                </FormField>

                <div
                  className={cn(
                    "grid gap-3",
                    formShowsMaxTokens
                      ? "grid-cols-1 sm:grid-cols-2"
                      : "grid-cols-1",
                  )}
                >
                  {formShowsMaxTokens && (
                    <FormField label="max_tokens" htmlFor="route-max-tokens">
                      <Input
                        id="route-max-tokens"
                        type="number"
                        value={form.max_tokens}
                        onChange={(event) =>
                          setForm({
                            ...form,
                            max_tokens: Number(event.target.value) || 1,
                          })
                        }
                      />
                    </FormField>
                  )}
                  <FormField label="超时（秒）" htmlFor="route-timeout">
                    <Input
                      id="route-timeout"
                      type="number"
                      value={form.timeout_seconds}
                      onChange={(event) =>
                        setForm({
                          ...form,
                          timeout_seconds: Number(event.target.value) || 1,
                        })
                      }
                    />
                  </FormField>
                </div>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setForm(null)}>
                  取消
                </Button>
                <Button
                  disabled={!canSave || saveMutation.isPending}
                  onClick={() => saveMutation.mutate(form)}
                >
                  {saveMutation.isPending && <Spinner size={3.5} />}
                  {!form.id && !formIsSystem && form.tasks.length > 1
                    ? `保存 ${form.tasks.length} 条路由`
                    : "保存"}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
