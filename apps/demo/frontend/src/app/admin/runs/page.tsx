"use client";

// 运行日志(audit-only):服务端分页聚合 run 状态与趋势,前端负责筛选、可视化和
// 弹窗展示上下文快照。快照仅用于排障,不会反哺任何后续轮次。

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  Activity,
  CheckCircle2,
  CircleEllipsis,
  Clock3,
  Eye,
  FileSearch,
  XCircle,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import {
  RunActivityChart,
  type AgentRunTrendPoint,
} from "@/components/admin/run-activity-chart";
import {
  DataTable,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  SearchInput,
  ToneBadge,
  type Tone,
} from "@/components/shared";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { formatDuration } from "@/lib/duration";
import { fmtDateTime } from "@/lib/utils";
import { cn } from "@/lib/utils";

type AgentRunStatus = "success" | "failed" | "waiting" | "running";
type SnapshotFilter = "all" | "with" | "without";

interface AgentRunSummary {
  session_id: string;
  run_id: string;
  started_at: string | null;
  last_event_at: string | null;
  event_count: number;
  has_snapshot: boolean;
  status: AgentRunStatus;
}

interface AgentRunStats {
  total: number;
  success: number;
  failed: number;
  waiting: number;
  running: number;
  snapshots: number;
  average_events: number;
}

interface AgentRunPage {
  items: AgentRunSummary[];
  total: number;
  page: number;
  page_size: number;
  stats: AgentRunStats;
  trend: AgentRunTrendPoint[];
}

/** 快照载荷:字段清单见 backend/app/services/agent/runtime_snapshot.py */
interface ContextSnapshot {
  agent_card_id: string | null;
  card_version: string | null;
  model_task: string | null;
  thinking_level: string | null;
  model_override: { provider: string; model: string } | null;
  system_prompt_chars: number;
  system_prompt_preview: string;
  system_prompt_blocks: { id: string; chars: number }[];
  history_messages: number;
  history_chars: number;
  tools_allowed: { name: string; side_effect: string | null }[];
  tools_total: number;
  policy_profile: string | null;
  policy_version: number | null;
  policy_id: string | null;
  websearch_provider: string | null;
  notes: string[];
  [key: string]: unknown;
}

const STATUS_META: Record<
  AgentRunStatus,
  { label: string; tone: Tone }
> = {
  success: { label: "成功", tone: "success" },
  failed: { label: "失败", tone: "destructive" },
  waiting: { label: "等待中", tone: "warning" },
  running: { label: "运行中", tone: "info" },
};

const STATUS_OPTIONS: {
  value: AgentRunStatus | "all";
  label: string;
}[] = [
  { value: "all", label: "全部" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
  { value: "waiting", label: "等待中" },
  { value: "running", label: "运行中" },
];

const DAY_OPTIONS = [
  { value: "7", label: "近 7 天" },
  { value: "30", label: "近 30 天" },
  { value: "90", label: "近 90 天" },
];

const SNAPSHOT_OPTIONS = [
  { value: "all", label: "全部快照" },
  { value: "with", label: "有快照" },
  { value: "without", label: "无快照" },
];

const PAGE_SIZE_OPTIONS = [
  { value: "10", label: "10 条 / 页" },
  { value: "20", label: "20 条 / 页" },
  { value: "50", label: "50 条 / 页" },
];

const shortId = (id: string) => id.slice(0, 8);

function runDuration(run: AgentRunSummary): string {
  if (!run.started_at || !run.last_event_at) return "—";
  const start = Date.parse(run.started_at);
  const end = Date.parse(run.last_event_at);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  return formatDuration((end - start) / 1000) ?? "—";
}

function RunStatusBadge({ status }: { status: AgentRunStatus }) {
  const meta = STATUS_META[status];
  return <ToneBadge tone={meta.tone}>{meta.label}</ToneBadge>;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const RUN_COLUMNS: ColumnDef<AgentRunSummary, any>[] = [
  {
    accessorKey: "last_event_at",
    header: "最近活动",
    enableSorting: false,
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-xs">
        {fmtDateTime(row.original.last_event_at)}
      </span>
    ),
  },
  {
    accessorKey: "run_id",
    header: "Run",
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-mono text-xs" title={row.original.run_id}>
        {shortId(row.original.run_id)}
      </span>
    ),
  },
  {
    accessorKey: "session_id",
    header: "会话",
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-mono text-xs" title={row.original.session_id}>
        {shortId(row.original.session_id)}
      </span>
    ),
  },
  {
    accessorKey: "status",
    header: "状态",
    enableSorting: false,
    cell: ({ row }) => <RunStatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "event_count",
    header: "事件数",
    enableSorting: false,
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.event_count}</span>
    ),
  },
  {
    id: "duration",
    header: "持续时间",
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {runDuration(row.original)}
      </span>
    ),
  },
  {
    accessorKey: "has_snapshot",
    header: "上下文快照",
    enableSorting: false,
    cell: ({ row }) =>
      row.original.has_snapshot ? (
        <ToneBadge tone="success">已记录</ToneBadge>
      ) : (
        <ToneBadge tone="muted">无快照</ToneBadge>
      ),
  },
  {
    id: "detail",
    header: "",
    cell: () => (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-primary">
        查看
        <Eye className="size-3.5" />
      </span>
    ),
  },
];

function SnapshotSummary({ snapshot }: { snapshot: ContextSnapshot }) {
  const facts: { label: string; value: string }[] = [
    { label: "模型任务", value: snapshot.model_task ?? "—" },
    {
      label: "模型",
      value: snapshot.model_override
        ? `${snapshot.model_override.provider}/${snapshot.model_override.model}`
        : "路由默认",
    },
    { label: "思考等级", value: snapshot.thinking_level ?? "默认" },
    {
      label: "System Prompt",
      value: `${snapshot.system_prompt_chars} 字符 · ${snapshot.system_prompt_blocks.length} 块`,
    },
    {
      label: "历史消息",
      value: `${snapshot.history_messages} 条 · ${snapshot.history_chars} 字符`,
    },
    { label: "放行工具", value: `${snapshot.tools_total} 个` },
    {
      label: "权限策略",
      value: `${snapshot.policy_profile ?? "—"} v${snapshot.policy_version ?? "—"}`,
    },
    {
      label: "联网搜索",
      value: snapshot.websearch_provider ?? "未启用",
    },
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {facts.map((fact) => (
        <div key={fact.label} className="rounded-lg border border-border p-3">
          <div className="text-xs text-muted-foreground">{fact.label}</div>
          <div className="mt-1 truncate font-mono text-xs" title={fact.value}>
            {fact.value}
          </div>
        </div>
      ))}
    </div>
  );
}

function SnapshotDetail({ run }: { run: AgentRunSummary }) {
  const snapshotQuery = useQuery({
    queryKey: ["admin-run-snapshot", run.run_id],
    queryFn: () =>
      api.get<ContextSnapshot>(
        `/admin/agent-runs/${run.run_id}/context-snapshot`,
      ),
    enabled: run.has_snapshot,
    retry: false,
  });

  if (!run.has_snapshot) {
    return (
      <EmptyState
        icon={FileSearch}
        title="该 run 没有上下文快照"
        description="快照只在每个 run 的首轮记录;确认/审批后的续跑轮与功能上线前的历史 run 不会有快照。"
      />
    );
  }
  if (snapshotQuery.isLoading) return <Skeleton className="h-64 w-full" />;
  if (snapshotQuery.error) {
    if (
      snapshotQuery.error instanceof ApiError &&
      snapshotQuery.error.status === 404
    ) {
      return <EmptyState icon={FileSearch} title="该 run 没有上下文快照" />;
    }
    return (
      <ErrorState
        error={snapshotQuery.error}
        onRetry={() => snapshotQuery.refetch()}
      />
    );
  }
  const snapshot = snapshotQuery.data;
  if (!snapshot) return null;
  return (
    <div className="space-y-4">
      <SnapshotSummary snapshot={snapshot} />
      {snapshot.notes.length > 0 && (
        <div className="rounded-lg border border-warning/30 bg-warning/5 p-3">
          <div className="mb-1 text-xs font-medium text-warning">运行备注</div>
          <ul className="list-inside list-disc text-xs text-muted-foreground">
            {snapshot.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <div className="mb-2 text-sm font-medium">完整快照</div>
        <pre className="max-h-[28rem] overflow-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function StatCards({
  stats,
  loading,
}: {
  stats?: AgentRunStats;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-28 w-full" />
        ))}
      </div>
    );
  }
  if (!stats) return null;

  const successRate = stats.total
    ? `${((stats.success / stats.total) * 100).toFixed(1)}% 成功率`
    : "暂无请求";
  const failureRate = stats.total
    ? `${((stats.failed / stats.total) * 100).toFixed(1)}% 失败率`
    : "暂无请求";
  const cards = [
    {
      label: "请求总数",
      value: stats.total,
      detail: `平均 ${stats.average_events.toFixed(1)} 个事件`,
      icon: Activity,
      iconClass: "bg-primary/10 text-primary",
    },
    {
      label: "成功",
      value: stats.success,
      detail: successRate,
      icon: CheckCircle2,
      iconClass: "bg-success/15 text-success",
    },
    {
      label: "失败",
      value: stats.failed,
      detail: failureRate,
      icon: XCircle,
      iconClass: "bg-destructive/15 text-destructive",
    },
    {
      label: "进行中",
      value: stats.waiting + stats.running,
      detail: `等待 ${stats.waiting} · 运行 ${stats.running}`,
      icon: CircleEllipsis,
      iconClass: "bg-warning/15 text-warning",
    },
  ];

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => (
        <Card key={card.label}>
          <CardContent className="flex items-start justify-between">
            <div>
              <div className="text-xs text-muted-foreground">{card.label}</div>
              <div className="mt-1 text-3xl font-semibold tabular-nums">
                {card.value.toLocaleString()}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {card.detail}
              </div>
            </div>
            <span className={cn("rounded-lg p-2", card.iconClass)}>
              <card.icon className="size-4" />
            </span>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export default function AdminRunsPage() {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [days, setDays] = useState(30);
  const [status, setStatus] = useState<AgentRunStatus | "all">("all");
  const [snapshot, setSnapshot] = useState<SnapshotFilter>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<AgentRunSummary | null>(null);

  const queryPath = useMemo(() => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
      days: String(days),
    });
    if (status !== "all") params.set("run_status", status);
    if (snapshot !== "all") {
      params.set("has_snapshot", String(snapshot === "with"));
    }
    if (query) params.set("query", query);
    return `/admin/agent-runs?${params.toString()}`;
  }, [days, page, pageSize, query, snapshot, status]);

  const runsQuery = useQuery({
    queryKey: [
      "admin-agent-runs",
      page,
      pageSize,
      days,
      status,
      snapshot,
      query,
    ],
    queryFn: () => api.get<AgentRunPage>(queryPath),
  });

  const setSearchQuery = useCallback((next: string) => {
    setQuery(next.trim());
    setPage(1);
  }, []);

  const data = runsQuery.data;
  const totalPages = Math.max(
    1,
    Math.ceil((data?.total ?? 0) / pageSize),
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="运行日志"
        description="查看 Agent 请求状态、活动趋势与运行时上下文快照,用于审计和排障"
      />

      <StatCards stats={data?.stats} loading={runsQuery.isLoading} />

      <Card>
        <CardHeader className="border-b">
          <div>
            <CardTitle>请求趋势</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              按日统计当前时间、ID 与快照筛选范围内的运行结果
            </p>
          </div>
        </CardHeader>
        <CardContent>
          {runsQuery.isLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <RunActivityChart trend={data?.trend ?? []} />
          )}
        </CardContent>
      </Card>

      <div className="space-y-3">
        <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-3 lg:flex-row lg:items-center">
          <SearchInput
            value={query}
            onChange={setSearchQuery}
            label="搜索 Run 或会话 ID"
            placeholder="搜索 Run / 会话 ID"
            className="w-full lg:w-64"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Select
              items={DAY_OPTIONS}
              value={String(days)}
              onValueChange={(value) => {
                setDays(Number(value));
                setPage(1);
              }}
            >
              <SelectTrigger aria-label="时间范围">
                <Clock3 className="size-3.5 text-muted-foreground" />
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {DAY_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              items={SNAPSHOT_OPTIONS}
              value={snapshot}
              onValueChange={(value) => {
                setSnapshot(value as SnapshotFilter);
                setPage(1);
              }}
            >
              <SelectTrigger aria-label="快照筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {SNAPSHOT_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-wrap gap-1 rounded-lg border border-border p-0.5 lg:ml-auto">
            {STATUS_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                className={cn(
                  "rounded-md px-3 py-1 text-xs transition-colors",
                  status === option.value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
                onClick={() => {
                  setStatus(option.value);
                  setPage(1);
                }}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <DataTable
          columns={RUN_COLUMNS}
          data={data?.items}
          isLoading={runsQuery.isLoading}
          error={runsQuery.error}
          onRetry={() => runsQuery.refetch()}
          getRowId={(row) => row.run_id}
          onRowClick={setSelected}
          empty={{
            icon: Activity,
            title: "没有符合条件的运行记录",
            description: "调整时间范围或筛选条件后再试。",
          }}
        />

        {data && data.total > 0 && (
          <div className="flex flex-col-reverse items-center justify-between gap-3 sm:flex-row">
            <Select
              items={PAGE_SIZE_OPTIONS}
              value={String(pageSize)}
              onValueChange={(value) => {
                setPageSize(Number(value));
                setPage(1);
              }}
            >
              <SelectTrigger aria-label="每页条数" size="sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start">
                {PAGE_SIZE_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Pagination
              page={page}
              totalPages={totalPages}
              total={data.total}
              unit="次运行"
              onChange={setPage}
            />
          </div>
        )}
      </div>

      <Dialog
        open={selected !== null}
        onOpenChange={(open) => !open && setSelected(null)}
      >
        {selected && (
          <DialogContent className="max-h-[90vh] grid-rows-[auto_minmax(0,1fr)] overflow-hidden sm:max-w-5xl">
            <DialogHeader className="pr-10">
              <div className="flex flex-wrap items-center gap-2">
                <DialogTitle>运行详情</DialogTitle>
                <RunStatusBadge status={selected.status} />
              </div>
              <DialogDescription>
                Run{" "}
                <span className="font-mono text-foreground">
                  {selected.run_id}
                </span>
                {" · "}
                会话{" "}
                <span className="font-mono text-foreground">
                  {selected.session_id}
                </span>
                {" · "}
                {fmtDateTime(selected.started_at)}
                {" · "}
                {selected.event_count} 个事件
              </DialogDescription>
            </DialogHeader>
            <div className="min-h-0 overflow-y-auto pr-1">
              <SnapshotDetail run={selected} />
            </div>
          </DialogContent>
        )}
      </Dialog>
    </div>
  );
}
