"use client";

// DB 可编辑 Prompt 注册表(重设计):按 task 聚合为单表通览,
// 中文名/作用/内置|自定义标识来自后端目录元信息(GET /admin/prompts 附加);
// 版本明细、启停与发新版收进任务详情弹窗;stat 瓦片 + 可折叠图表补全概览。
// mutation 与查询 key 沿用原实现(admin-prompts),行为不变只换壳。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ChartColumnBig, ChevronDown, Plus, ScrollText } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  DataTable,
  EmptyState,
  FormField,
  SearchInput,
  Spinner,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { errMsg, fmtDateTime } from "@/lib/utils";
import type { PromptDto, PromptWithCatalogDto } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CopyTextButton } from "./copy-text-button";
import { PromptCatalogEntryEditor } from "./prompt-catalog-entry-editor";
import {
  extractPromptVariables,
  PlaceholderBadges,
  PlaceholderHint,
} from "./prompt-placeholder";
import { PromptRegistryCharts, type PromptChartRow } from "./prompt-registry-charts";

// 分类中文映射;未知分类原样展示,后端新增前缀时前端不至于显示空白
const CATEGORY_LABELS: Record<string, string> = {
  kb: "知识库",
  agent: "Agent",
  pipeline: "业务流水线",
  quality: "产物质检",
  render: "渲染",
  other: "其他",
};
const categoryLabel = (category: string) => CATEGORY_LABELS[category] ?? category;

const fmt = new Intl.NumberFormat("zh-CN");

/** 按 task 聚合后的表格行:展示元信息取自任意一版(同 task 各版相同) */
interface TaskRow {
  task: string;
  name: string;
  description: string;
  category: string;
  builtin: boolean;
  /** true=自定义任务已在 prompt_catalog_entries 登记中文名/描述 */
  registered: boolean;
  /** 版本降序 */
  versions: PromptWithCatalogDto[];
  /** 运行时生效版 = 最高启用版本(与后端 get_active_prompt 口径一致) */
  activeVersion: PromptWithCatalogDto | null;
  versionCount: number;
  activeVersionNum: number | null;
  activeChars: number;
  latestAt: string | null;
}

/** 内置=中性色、自定义=强调色:一眼区分来源 */
function SourceBadge({ builtin }: { builtin: boolean }) {
  return (
    <ToneBadge tone={builtin ? "muted" : "info"}>{builtin ? "内置" : "自定义"}</ToneBadge>
  );
}

function FilterChipButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
        active
          ? "border-primary/50 bg-primary/10 font-medium text-primary"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

const rightHeader = (label: string) =>
  function RightHeader() {
    return <span className="block w-full text-right">{label}</span>;
  };

/** 登记/发新版弹窗草稿:nameZh/description 只在自定义任务的首次登记流程里用 */
interface EditorDraft {
  task: string;
  content: string;
  nameZh: string;
  description: string;
}

/** GET /admin/prompts 按任务聚合(展示元信息各版相同,取任意一版) */
function toTaskRows(prompts: PromptWithCatalogDto[]): TaskRow[] {
  const byTask = new Map<string, PromptWithCatalogDto[]>();
  for (const p of prompts) {
    byTask.set(p.task, [...(byTask.get(p.task) ?? []), p]);
  }
  return [...byTask.entries()].map(([task, versions]) => {
    // 后端已按版本降序返回,这里再排一遍防御接口顺序变化
    const sorted = [...versions].sort((a, b) => b.version - a.version);
    const meta = sorted[0];
    const active = sorted.find((v) => v.is_active) ?? null;
    const latestAt = sorted.reduce<string | null>(
      (acc, v) => (v.created_at && (!acc || v.created_at > acc) ? v.created_at : acc),
      null,
    );
    return {
      task,
      name: meta.name_zh,
      description: meta.description,
      category: meta.category,
      builtin: meta.builtin,
      registered: meta.registered,
      versions: sorted,
      activeVersion: active,
      versionCount: sorted.length,
      activeVersionNum: active?.version ?? null,
      activeChars: active?.content.length ?? 0,
      latestAt,
    };
  });
}

export function PromptRegistryTab() {
  const queryClient = useQueryClient();
  // 新建/发新版共用:task 预填且锁定 = 发新版
  const [editor, setEditor] = useState<EditorDraft | null>(null);
  const [taskLocked, setTaskLocked] = useState(false);
  // 详情弹窗只存 task 与选中版本 id,行数据从查询派生——启停/发版后自动反映新状态
  const [detailTask, setDetailTask] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [chartsOpen, setChartsOpen] = useState(true);

  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("all");
  const [source, setSource] = useState<"all" | "builtin" | "custom">("all");
  const [status, setStatus] = useState<"all" | "active" | "none">("all");

  // 全量查询:stat 瓦片/分类 chips/内置任务集合的口径,刻意不随筛选变化
  const promptsQuery = useQuery({
    queryKey: ["admin-prompts"],
    queryFn: () => api.get<PromptWithCatalogDto[]>("/admin/prompts"),
  });
  const prompts = useMemo(() => promptsQuery.data ?? [], [promptsQuery.data]);

  // 列表查询:category/q 走服务端过滤(SearchInput 自带 250ms 防抖,
  // search 落到这里时已是防抖后的值);无筛选时直接复用全量查询,不发第二个请求
  const trimmedSearch = search.trim();
  const hasServerFilter = category !== "all" || trimmedSearch !== "";
  const filteredQuery = useQuery({
    queryKey: ["admin-prompts", { category, q: trimmedSearch }],
    queryFn: () => {
      const params = new URLSearchParams();
      if (category !== "all") params.set("category", category);
      if (trimmedSearch) params.set("q", trimmedSearch);
      return api.get<PromptWithCatalogDto[]>(`/admin/prompts?${params.toString()}`);
    },
    enabled: hasServerFilter,
    // 换筛选条件时沿用上一次结果,表格不闪空
    placeholderData: (prev) => prev,
  });
  const listPrompts = hasServerFilter ? (filteredQuery.data ?? []) : prompts;

  const createMutation = useMutation({
    mutationFn: (payload: { task: string; content: string }) =>
      api.post<PromptDto>("/admin/prompts", payload),
    onSuccess: (created) => {
      toast.success(`已登记 ${created.task} v${created.version}`);
      setEditor(null);
      // 详情弹窗开着时把选中版本切到刚登记的新版,发版结果立即可见
      if (detailTask === created.task) setSelectedId(created.id);
      queryClient.invalidateQueries({ queryKey: ["admin-prompts"] });
    },
    onError: (err) => toast.error(errMsg(err, "登记失败")),
  });

  const toggleMutation = useMutation({
    mutationFn: (p: PromptDto) =>
      api.patch<PromptDto>(`/admin/prompts/${p.id}`, { is_active: !p.is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin-prompts"] }),
    onError: (err) => toast.error(errMsg(err, "操作失败")),
  });

  // 全量聚合:stat 瓦片/分类 chips/内置任务集合的数据源
  const taskRows = useMemo<TaskRow[]>(() => toTaskRows(prompts), [prompts]);

  const categories = useMemo(
    () => [...new Set(taskRows.map((row) => row.category))].sort(),
    [taskRows],
  );
  const builtinTasks = useMemo(
    () => new Set(taskRows.filter((row) => row.builtin).map((row) => row.task)),
    [taskRows],
  );

  /** 登记提交:先建 prompt,自定义任务再补目录 upsert(两个请求,部分失败要说清楚) */
  const submitEditor = async (draft: EditorDraft) => {
    const task = draft.task.trim();
    try {
      await createMutation.mutateAsync({ task, content: draft.content });
    } catch {
      return; // 失败提示已在 createMutation.onError 里给出,目录请求不再发
    }
    const nameZh = draft.nameZh.trim();
    const description = draft.description.trim();
    if (taskLocked || builtinTasks.has(task) || (!nameZh && !description)) return;
    try {
      await api.put(`/admin/prompt-catalog/${encodeURIComponent(task)}`, {
        // 只填了描述没填中文名时,用 task 兜底满足 name_zh 必填,信息不丢
        name_zh: nameZh || task,
        description,
      });
      queryClient.invalidateQueries({ queryKey: ["admin-prompts"] });
    } catch {
      toast.error("prompt 已登记,目录信息保存失败,可稍后在详情弹窗中补填");
    }
  };

  // category/q 已由服务端过滤(listPrompts);来源/状态是聚合后的派生维度,留在前端过滤
  const filteredRows = useMemo(() => {
    return toTaskRows(listPrompts).filter((row) => {
      if (source === "builtin" && !row.builtin) return false;
      if (source === "custom" && row.builtin) return false;
      if (status === "active" && !row.activeVersion) return false;
      if (status === "none" && row.activeVersion) return false;
      return true;
    });
  }, [listPrompts, source, status]);

  // 图表与表格吃同一份筛选结果:一条筛选行统辖其下所有视图
  const chartRows = useMemo<PromptChartRow[]>(
    () =>
      filteredRows.map((row) => ({
        task: row.task,
        name: row.name,
        builtin: row.builtin,
        versions: row.versionCount,
        chars: row.activeChars,
      })),
    [filteredRows],
  );

  // stat 瓦片是全量概览,刻意不随筛选变化(筛选只作用于图表与表格)
  const stats = useMemo(() => {
    const activeCount = prompts.filter((p) => p.is_active).length;
    const noActive = taskRows.filter((row) => !row.activeVersion).length;
    const builtinCount = taskRows.filter((row) => row.builtin).length;
    const latest = taskRows.reduce<TaskRow | null>(
      (acc, row) =>
        row.latestAt && (!acc?.latestAt || row.latestAt > acc.latestAt) ? row : acc,
      null,
    );
    return {
      taskCount: taskRows.length,
      versionTotal: prompts.length,
      activeCount,
      noActive,
      builtinCount,
      customCount: taskRows.length - builtinCount,
      latest,
    };
  }, [prompts, taskRows]);

  const detailRow = detailTask
    ? (taskRows.find((row) => row.task === detailTask) ?? null)
    : null;
  const selectedVersion =
    detailRow?.versions.find((v) => v.id === selectedId) ?? detailRow?.versions[0] ?? null;

  const openDetail = (row: TaskRow) => {
    setDetailTask(row.task);
    // 默认选中运行时生效版;无启用版本时落到最新版
    setSelectedId((row.activeVersion ?? row.versions[0])?.id ?? null);
  };
  const openNewVersion = (task: string, content: string) => {
    setEditor({ task, content, nameZh: "", description: "" });
    setTaskLocked(true);
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns: ColumnDef<TaskRow, any>[] = [
    {
      accessorKey: "name",
      header: "任务",
      cell: ({ row }) => (
        <div className="min-w-40">
          <div className="flex items-center gap-1.5 text-sm font-medium">
            {row.original.name}
            {/* 自定义且未登记目录:轻提示,点击直达详情里的目录编辑(行点击同路) */}
            {!row.original.builtin && !row.original.registered && (
              <ToneBadge tone="warning" className="cursor-pointer font-normal">
                未登记说明
              </ToneBadge>
            )}
          </div>
          <div className="font-mono text-xs text-muted-foreground">{row.original.task}</div>
        </div>
      ),
    },
    {
      accessorKey: "category",
      header: "分类",
      enableSorting: false,
      cell: ({ row }) => <ToneBadge tone="muted">{categoryLabel(row.original.category)}</ToneBadge>,
    },
    {
      accessorKey: "builtin",
      header: "来源",
      enableSorting: false,
      cell: ({ row }) => <SourceBadge builtin={row.original.builtin} />,
    },
    {
      accessorKey: "description",
      header: "作用",
      enableSorting: false,
      cell: ({ row }) =>
        row.original.description ? (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger className="block max-w-72 truncate text-left text-xs text-muted-foreground">
                {row.original.description}
              </TooltipTrigger>
              <TooltipContent className="max-w-sm leading-5">
                {row.original.description}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        ),
    },
    {
      accessorKey: "versionCount",
      header: rightHeader("版本数"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">{row.original.versionCount}</span>
      ),
    },
    {
      accessorKey: "activeVersionNum",
      header: "当前启用",
      cell: ({ row }) =>
        row.original.activeVersionNum !== null ? (
          <ToneBadge tone="success" className="font-mono">
            v{row.original.activeVersionNum}
          </ToneBadge>
        ) : (
          <ToneBadge tone="warning">无启用</ToneBadge>
        ),
    },
    {
      accessorKey: "latestAt",
      header: "最近登记",
      cell: ({ row }) => (
        <span className="text-xs whitespace-nowrap text-muted-foreground">
          {row.original.latestAt ? fmtDateTime(row.original.latestAt) : "—"}
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      cell: ({ row }) => (
        <Button
          variant="outline"
          size="sm"
          onClick={(event) => {
            event.stopPropagation();
            // 预填当前生效版(无启用版本时落到最新版)的内容供修改
            openNewVersion(
              row.original.task,
              (row.original.activeVersion ?? row.original.versions[0])?.content ?? "",
            );
          }}
        >
          <Plus className="size-3.5" />
          发新版
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          内容一经登记不可改,改动即发新版本;运行时取每个任务最高的启用版本
        </p>
        <Button
          onClick={() => {
            setEditor({ task: "", content: "", nameZh: "", description: "" });
            setTaskLocked(false);
          }}
        >
          <Plus className="size-4" />
          登记 Prompt
        </Button>
      </div>

      {promptsQuery.isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-60 w-full" />
        </div>
      ) : promptsQuery.error ? (
        <DataTable
          columns={columns}
          data={undefined}
          error={promptsQuery.error}
          onRetry={() => promptsQuery.refetch()}
        />
      ) : taskRows.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={ScrollText}
              title="还没有登记任何 Prompt"
              description="LLM 任务运行前必须在此登记。"
            />
          </CardContent>
        </Card>
      ) : (
        <>
          {/* 通览 stat 瓦片(全量口径) */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[
              {
                label: "任务总数",
                value: fmt.format(stats.taskCount),
                sub: `共 ${fmt.format(stats.versionTotal)} 个版本`,
              },
              {
                label: "启用版本",
                value: fmt.format(stats.activeCount),
                sub:
                  stats.noActive > 0
                    ? `${fmt.format(stats.noActive)} 个任务无启用版本`
                    : "全部任务均有启用版本",
                warn: stats.noActive > 0,
              },
              {
                label: "内置 / 自定义",
                value: `${fmt.format(stats.builtinCount)} / ${fmt.format(stats.customCount)}`,
                sub: "内置随源码 seed,自定义由业务方登记",
              },
              {
                label: "最近登记",
                value: stats.latest?.latestAt ? fmtDateTime(stats.latest.latestAt) : "—",
                sub: stats.latest ? stats.latest.name : "尚无登记记录",
                small: true,
              },
            ].map((s) => (
              <Card key={s.label}>
                <CardContent className="pt-4">
                  <div className="text-xs text-muted-foreground">{s.label}</div>
                  <div className={cn("font-semibold", s.small ? "pt-1 text-base" : "text-xl")}>
                    {s.value}
                  </div>
                  <div
                    className={cn(
                      "truncate text-xs",
                      s.warn ? "text-warning" : "text-muted-foreground",
                    )}
                  >
                    {s.sub}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>

          {/* 过滤行:统辖下方图表与表格 */}
          <Card>
            <CardContent className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-4">
              <SearchInput
                value={search}
                onChange={setSearch}
                label="搜索 Prompt 任务"
                placeholder="搜索 task / 中文名 / 作用"
                className="w-full sm:w-64"
              />
              <div className="flex flex-wrap items-center gap-1.5" aria-label="按分类过滤">
                <span className="text-xs text-muted-foreground">分类</span>
                {["all", ...categories].map((item) => (
                  <FilterChipButton
                    key={item}
                    active={category === item}
                    onClick={() => setCategory(item)}
                  >
                    {item === "all" ? "全部" : categoryLabel(item)}
                  </FilterChipButton>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-1.5" aria-label="按来源过滤">
                <span className="text-xs text-muted-foreground">来源</span>
                {(
                  [
                    ["all", "全部"],
                    ["builtin", "内置"],
                    ["custom", "自定义"],
                  ] as const
                ).map(([value, label]) => (
                  <FilterChipButton
                    key={value}
                    active={source === value}
                    onClick={() => setSource(value)}
                  >
                    {label}
                  </FilterChipButton>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-1.5" aria-label="按启用状态过滤">
                <span className="text-xs text-muted-foreground">状态</span>
                {(
                  [
                    ["all", "全部"],
                    ["active", "有启用版本"],
                    ["none", "无启用版本"],
                  ] as const
                ).map(([value, label]) => (
                  <FilterChipButton
                    key={value}
                    active={status === value}
                    onClick={() => setStatus(value)}
                  >
                    {label}
                  </FilterChipButton>
                ))}
              </div>
            </CardContent>
          </Card>

          {/* 可折叠图表概览 */}
          <Card>
            <CardContent className="pt-4">
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2"
                aria-expanded={chartsOpen}
                onClick={() => setChartsOpen((open) => !open)}
              >
                <span className="flex items-center gap-1.5 text-sm font-medium">
                  <ChartColumnBig className="size-4 text-muted-foreground" />
                  图表概览
                </span>
                <ChevronDown
                  className={cn(
                    "size-4 text-muted-foreground transition-transform",
                    chartsOpen && "rotate-180",
                  )}
                />
              </button>
              {chartsOpen && (
                <div className="mt-3">
                  <PromptRegistryCharts rows={chartRows} />
                </div>
              )}
            </CardContent>
          </Card>

          {/* 主表:每任务一行,点行进详情弹窗 */}
          <DataTable
            columns={columns}
            data={filteredRows}
            getRowId={(row) => row.task}
            onRowClick={openDetail}
            empty={{
              icon: ScrollText,
              title: "没有匹配的任务",
              description: "调整搜索词或筛选条件再试。",
            }}
          />
        </>
      )}

      {/* 任务详情弹窗:版本时间线 + 内容查看 + 启停/发版操作 */}
      <Dialog
        open={detailRow !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDetailTask(null);
            setSelectedId(null);
          }
        }}
      >
        {/* 基础 Dialog 自带 sm:max-w-sm,裸 max-w-3xl 会在 sm 断点被它压住,
            必须用同断点变体覆盖,否则版本时间线+正文双栏挤成竖排 */}
        <DialogContent className="sm:max-w-3xl">
          {detailRow && (
            <>
              <DialogHeader>
                <DialogTitle className="flex flex-wrap items-center gap-2">
                  {detailRow.name}
                  <ToneBadge tone="muted">{categoryLabel(detailRow.category)}</ToneBadge>
                  <SourceBadge builtin={detailRow.builtin} />
                </DialogTitle>
                <div className="space-y-1 text-left">
                  <p className="font-mono text-xs text-muted-foreground">{detailRow.task}</p>
                  {detailRow.description && (
                    <p className="text-sm leading-6 text-muted-foreground">
                      {detailRow.description}
                    </p>
                  )}
                </div>
              </DialogHeader>

              {/* 自定义任务:目录信息可编辑/可清除(内置随源码治理,后端 403,不渲染) */}
              {!detailRow.builtin && (
                <PromptCatalogEntryEditor
                  key={detailRow.task}
                  task={detailRow.task}
                  registered={detailRow.registered}
                  initialName={detailRow.name}
                  initialDescription={detailRow.description}
                />
              )}

              <div className="grid gap-4 md:grid-cols-[200px_minmax(0,1fr)]">
                {/* 左:版本时间线 */}
                <div className="space-y-1.5">
                  <p className="text-xs text-muted-foreground">
                    版本时间线 · 共 {detailRow.versionCount} 版
                  </p>
                  <div className="max-h-80 space-y-1.5 overflow-y-auto pr-1">
                    {detailRow.versions.map((version) => (
                      <button
                        key={version.id}
                        type="button"
                        onClick={() => setSelectedId(version.id)}
                        className={cn(
                          "block w-full rounded-md border px-2.5 py-1.5 text-left transition-colors",
                          selectedVersion?.id === version.id
                            ? "border-primary/50 bg-primary/5"
                            : "border-border hover:bg-accent/50",
                        )}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-sm">v{version.version}</span>
                          <ToneBadge tone={version.is_active ? "success" : "muted"}>
                            {version.is_active ? "启用" : "停用"}
                          </ToneBadge>
                        </div>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {version.created_at ? fmtDateTime(version.created_at) : "—"}
                        </p>
                      </button>
                    ))}
                  </div>
                </div>

                {/* 右:选中版本内容 */}
                <div className="min-w-0 space-y-2">
                  {selectedVersion ? (
                    <>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs whitespace-nowrap text-muted-foreground">
                          v{selectedVersion.version} 内容 ·{" "}
                          {fmt.format(selectedVersion.content.length)} 字符
                        </span>
                        <CopyTextButton text={selectedVersion.content} label="复制内容" />
                      </div>
                      {/* variables 为后端扫描结果;兜底 ?? [] 防旧缓存数据缺字段 */}
                      <PlaceholderBadges variables={selectedVersion.variables ?? []} />
                      {(selectedVersion.variables ?? []).length > 0 && <PlaceholderHint />}
                      <pre className="max-h-72 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
                        {selectedVersion.content}
                      </pre>
                    </>
                  ) : (
                    <p className="py-8 text-center text-sm text-muted-foreground">
                      左侧选择一个版本查看内容
                    </p>
                  )}
                </div>
              </div>

              <DialogFooter className="flex-wrap sm:justify-between">
                <div className="flex items-center gap-2">
                  {selectedVersion && (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={toggleMutation.isPending}
                      onClick={() => toggleMutation.mutate(selectedVersion)}
                    >
                      {toggleMutation.isPending && <Spinner size={3.5} />}
                      {selectedVersion.is_active
                        ? `停用 v${selectedVersion.version}`
                        : `启用 v${selectedVersion.version}`}
                    </Button>
                  )}
                </div>
                <Button
                  size="sm"
                  onClick={() =>
                    openNewVersion(detailRow.task, selectedVersion?.content ?? "")
                  }
                >
                  <Plus className="size-3.5" />
                  登记新版
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>

      {/* 登记/发新版弹窗 */}
      <Dialog open={editor !== null} onOpenChange={(open) => !open && setEditor(null)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{taskLocked ? `发新版本 — ${editor?.task}` : "登记 Prompt"}</DialogTitle>
          </DialogHeader>
          {editor && (
            <>
              <div className="space-y-4">
                {!taskLocked && (
                  <FormField label="任务标识" htmlFor="prompt-task" required>
                    <Input
                      id="prompt-task"
                      value={editor.task}
                      onChange={(e) => setEditor({ ...editor, task: e.target.value })}
                      placeholder="如:kb.extract.hotel / my.custom.task"
                      className="font-mono"
                    />
                    {editor.task.trim() && !builtinTasks.has(editor.task.trim()) && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        自定义任务:建议同时填写中文名称与作用描述,列表将以此展示
                      </p>
                    )}
                  </FormField>
                )}
                {/* 自定义任务首次登记顺手补目录;内置任务的元信息随源码,不给入口 */}
                {!taskLocked && editor.task.trim() && !builtinTasks.has(editor.task.trim()) && (
                  <div className="grid gap-4 sm:grid-cols-[200px_minmax(0,1fr)]">
                    <FormField label="中文名称" htmlFor="prompt-name-zh">
                      <Input
                        id="prompt-name-zh"
                        value={editor.nameZh}
                        maxLength={100}
                        onChange={(e) => setEditor({ ...editor, nameZh: e.target.value })}
                        placeholder="如:对账单生成"
                      />
                    </FormField>
                    <FormField label="作用描述" htmlFor="prompt-description">
                      <Input
                        id="prompt-description"
                        value={editor.description}
                        onChange={(e) =>
                          setEditor({ ...editor, description: e.target.value })
                        }
                        placeholder="这条 prompt 在哪个链路里干什么"
                      />
                    </FormField>
                  </div>
                )}
                <FormField label="内容" htmlFor="prompt-content" required>
                  <Textarea
                    id="prompt-content"
                    value={editor.content}
                    onChange={(e) => setEditor({ ...editor, content: e.target.value })}
                    rows={14}
                    className="font-mono text-xs"
                  />
                  {/* 实时扫描 {{占位符}}:有才展示,配套提示条说明"不做变量替换" */}
                  {(() => {
                    const variables = extractPromptVariables(editor.content);
                    if (variables.length === 0) return null;
                    return (
                      <div className="mt-2 space-y-2">
                        <PlaceholderBadges variables={variables} />
                        <PlaceholderHint />
                      </div>
                    );
                  })()}
                </FormField>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setEditor(null)}>
                  取消
                </Button>
                <Button
                  disabled={
                    !editor.task.trim() || !editor.content.trim() || createMutation.isPending
                  }
                  onClick={() => void submitEditor(editor)}
                >
                  {createMutation.isPending && <Spinner size={3.5} />}
                  登记为新版本
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
