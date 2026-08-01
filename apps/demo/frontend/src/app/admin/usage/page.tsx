"use client";

// 使用统计与计费看板(stage-d 09):tokens 四桶汇总 + 缓存命中率 + 成本
// (按 /admin/model-prices 现算,未定价如实标注)+ 趋势图 + 四个明细面:
// 模型统计 / 租户成本 / 租户明细(原 org×task,含非 LLM 计量)/ 请求日志。

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { BarChart3, CircleDollarSign, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import {
  DataTable,
  EmptyState,
  ErrorState,
  PageHeader,
  ToneBadge,
} from "@/components/shared";
import { BillingTrendCharts } from "@/components/admin/billing-charts";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import {
  fmtCompact,
  fmtUsd,
  useBilling,
  useLlmTraces,
  type BillingModelRow,
  type BillingOrgRow,
  type TraceRow,
} from "@/lib/billing";
import { cn, fmtDateTime, fmtSize } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface UsageRow {
  org_id: string;
  org_name: string;
  task: string;
  tokens_in: number;
  tokens_out: number;
  calls: number;
  /** 通用计量数:上传等字节口径的行用它,LLM 行恒 0 */
  quantity: number;
}

const RANGES = [
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
  { days: 90, label: "近 90 天" },
] as const;

const fmt = new Intl.NumberFormat("zh-CN");

const num = (v: number) => (
  <span className="block text-right tabular-nums">{fmt.format(v)}</span>
);
const rightHeader = (label: string) =>
  function RightHeader() {
    return <span className="block w-full text-right">{label}</span>;
  };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const MODEL_COLUMNS: ColumnDef<BillingModelRow, any>[] = [
  {
    accessorKey: "model",
    header: "模型",
    cell: ({ row }) => (
      <div>
        <div className="font-mono text-xs">{row.original.model}</div>
        <div className="text-xs text-muted-foreground">{row.original.provider}</div>
      </div>
    ),
  },
  { accessorKey: "calls", header: rightHeader("请求数"), cell: ({ row }) => num(row.original.calls) },
  { accessorKey: "tokens_in", header: rightHeader("新增输入"), cell: ({ row }) => num(row.original.tokens_in) },
  { accessorKey: "tokens_out", header: rightHeader("输出"), cell: ({ row }) => num(row.original.tokens_out) },
  { accessorKey: "cache_read_tokens", header: rightHeader("缓存命中"), cell: ({ row }) => num(row.original.cache_read_tokens) },
  { accessorKey: "cache_write_tokens", header: rightHeader("缓存创建"), cell: ({ row }) => num(row.original.cache_write_tokens) },
  {
    accessorKey: "cost",
    header: rightHeader("总成本"),
    cell: ({ row }) =>
      row.original.cost === null ? (
        <span className="flex justify-end">
          <ToneBadge tone="warning">未定价</ToneBadge>
        </span>
      ) : (
        <span className="block text-right font-mono tabular-nums">{fmtUsd(row.original.cost)}</span>
      ),
  },
  {
    accessorKey: "avg_cost",
    header: rightHeader("平均成本"),
    cell: ({ row }) => (
      <span className="block text-right font-mono tabular-nums">
        {row.original.avg_cost === null ? "—" : fmtUsd(row.original.avg_cost)}
      </span>
    ),
  },
];

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const ORG_COLUMNS: ColumnDef<BillingOrgRow, any>[] = [
  { accessorKey: "org_name", header: "租户" },
  { accessorKey: "calls", header: rightHeader("请求数"), cell: ({ row }) => num(row.original.calls) },
  { accessorKey: "tokens_in", header: rightHeader("新增输入"), cell: ({ row }) => num(row.original.tokens_in) },
  { accessorKey: "tokens_out", header: rightHeader("输出"), cell: ({ row }) => num(row.original.tokens_out) },
  { accessorKey: "cache_read_tokens", header: rightHeader("缓存命中"), cell: ({ row }) => num(row.original.cache_read_tokens) },
  {
    accessorKey: "cost",
    header: rightHeader("成本"),
    cell: ({ row }) => (
      <span className="block text-right font-mono tabular-nums">{fmtUsd(row.original.cost)}</span>
    ),
  },
];

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const USAGE_COLUMNS: ColumnDef<UsageRow, any>[] = [
  {
    accessorKey: "task",
    header: "任务",
    cell: ({ row }) => <span className="font-mono text-xs">{row.original.task}</span>,
  },
  { accessorKey: "tokens_in", header: rightHeader("输入 tokens"), cell: ({ row }) => num(row.original.tokens_in) },
  { accessorKey: "tokens_out", header: rightHeader("输出 tokens"), cell: ({ row }) => num(row.original.tokens_out) },
  { accessorKey: "calls", header: rightHeader("调用"), cell: ({ row }) => num(row.original.calls) },
  {
    accessorKey: "quantity",
    header: rightHeader("计量量"),
    cell: ({ row }) => (
      <span className="block text-right tabular-nums">
        {row.original.quantity > 0 ? fmtSize(row.original.quantity) : "—"}
      </span>
    ),
  },
];

function TenantDetail({ days }: { days: number }) {
  const usageQuery = useQuery({
    queryKey: ["admin-usage", days],
    queryFn: () => api.get<UsageRow[]>(`/admin/usage?days=${days}`),
  });
  const rows = usageQuery.data;

  const byOrg = new Map<string, { name: string; rows: UsageRow[] }>();
  for (const r of rows ?? []) {
    const entry = byOrg.get(r.org_id) ?? { name: r.org_name, rows: [] };
    entry.rows.push(r);
    byOrg.set(r.org_id, entry);
  }
  const orgs = [...byOrg.entries()].sort(
    (a, b) =>
      b[1].rows.reduce((s, r) => s + r.tokens_in + r.tokens_out, 0) -
      a[1].rows.reduce((s, r) => s + r.tokens_in + r.tokens_out, 0),
  );

  if (usageQuery.isLoading) return <Skeleton className="h-40 w-full" />;
  if (usageQuery.error)
    return <ErrorState error={usageQuery.error} onRetry={() => usageQuery.refetch()} />;
  if (!orgs.length)
    return (
      <Card>
        <CardContent>
          <EmptyState icon={BarChart3} title="该时间段内没有用量记录" />
        </CardContent>
      </Card>
    );
  return (
    <div className="space-y-4">
      {orgs.map(([orgId, { name, rows: orgRows }]) => (
        <Card key={orgId}>
          <CardContent className="pt-4">
            <div className="mb-2 font-medium">{name}</div>
            <DataTable
              columns={USAGE_COLUMNS}
              data={orgRows}
              getRowId={(r) => `${r.org_id}:${r.task}`}
              empty={{ title: "暂无用量" }}
            />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function TraceLog({ days }: { days: number }) {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<"success" | "error" | undefined>(undefined);
  const tracesQuery = useLlmTraces({ days: Math.min(days, 90), page, status });
  const data = tracesQuery.data;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns: ColumnDef<TraceRow, any>[] = [
    {
      accessorKey: "created_at",
      header: "时间",
      cell: ({ row }) => (
        <span className="whitespace-nowrap text-xs">{fmtDateTime(row.original.created_at)}</span>
      ),
    },
    { accessorKey: "org_name", header: "租户" },
    {
      accessorKey: "task",
      header: "任务",
      cell: ({ row }) => <span className="font-mono text-xs">{row.original.task}</span>,
    },
    {
      accessorKey: "model",
      header: "模型",
      cell: ({ row }) => (
        <div>
          <div className="font-mono text-xs">{row.original.model}</div>
          <div className="text-xs text-muted-foreground">
            {row.original.provider}
            {row.original.fallback_from && ` · 降级自 ${row.original.fallback_from}`}
          </div>
        </div>
      ),
    },
    {
      accessorKey: "tokens_in",
      header: rightHeader("输入"),
      cell: ({ row }) => (
        <div className="text-right tabular-nums">
          <div>{fmt.format(row.original.tokens_in)}</div>
          {(row.original.cache_read_tokens > 0 || row.original.cache_write_tokens > 0) && (
            <div className="text-xs text-muted-foreground">
              R{fmt.format(row.original.cache_read_tokens)}·W
              {fmt.format(row.original.cache_write_tokens)}
            </div>
          )}
        </div>
      ),
    },
    { accessorKey: "tokens_out", header: rightHeader("输出"), cell: ({ row }) => num(row.original.tokens_out) },
    {
      accessorKey: "cost",
      header: rightHeader("成本"),
      cell: ({ row }) => (
        <span className="block text-right font-mono tabular-nums">
          {row.original.cost === null ? "—" : fmtUsd(row.original.cost)}
        </span>
      ),
    },
    {
      accessorKey: "latency_ms",
      header: rightHeader("用时"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">
          {(row.original.latency_ms / 1000).toFixed(1)}s
        </span>
      ),
    },
    {
      accessorKey: "status",
      header: "状态",
      cell: ({ row }) =>
        row.original.status === "success" ? (
          <ToneBadge tone="success">成功</ToneBadge>
        ) : (
          <span title={row.original.error ?? undefined}>
            <ToneBadge tone="destructive">失败</ToneBadge>
          </span>
        ),
    },
  ];

  return (
    <div className="space-y-3">
      <div className="flex gap-1 rounded-md border border-border p-0.5 w-fit">
        {(
          [
            [undefined, "全部"],
            ["success", "成功"],
            ["error", "失败"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={label}
            type="button"
            className={cn(
              "rounded px-3 py-1 text-sm",
              status === value
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={() => {
              setStatus(value);
              setPage(1);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {tracesQuery.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : tracesQuery.error ? (
        <ErrorState error={tracesQuery.error} onRetry={() => tracesQuery.refetch()} />
      ) : !data?.items.length ? (
        <Card>
          <CardContent>
            <EmptyState icon={BarChart3} title="没有符合条件的请求记录" />
          </CardContent>
        </Card>
      ) : (
        <>
          <DataTable
            columns={columns}
            data={data.items}
            getRowId={(r) => r.id}
            empty={{ title: "暂无记录" }}
          />
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2">
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                上一页
              </Button>
              <span className="text-sm tabular-nums text-muted-foreground">
                {page} / {totalPages}(共 {data.total} 条)
              </span>
              <Button
                size="sm"
                variant="ghost"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function AdminUsagePage() {
  const [days, setDays] = useState(30);
  const billingQuery = useBilling(days);
  const billing = billingQuery.data;
  const summary = billing?.summary;

  return (
    <div className="space-y-6">
      <PageHeader
        title="使用统计"
        description="AI 模型用量与成本(tokens 口径:新增输入不含缓存;成本按模型价格表现算)+ 渲染/存储等非 LLM 计量"
        actions={
          <div className="flex gap-1 rounded-md border border-border p-0.5">
            {RANGES.map((r) => (
              <button
                key={r.days}
                type="button"
                className={cn(
                  "rounded px-3 py-1 text-sm",
                  days === r.days
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
                onClick={() => setDays(r.days)}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      />

      {summary && summary.unpriced.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>
            以下模型有用量但未定价,成本按 0 计入:
            <span className="font-mono text-xs">{summary.unpriced.join("、")}</span>
            <Link href="/admin/prices" className="ml-2 text-primary underline-offset-2 hover:underline">
              前往设置价格
            </Link>
          </span>
        </div>
      )}

      {billingQuery.isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-60 w-full" />
        </div>
      ) : billingQuery.error ? (
        <ErrorState error={billingQuery.error} onRetry={() => billingQuery.refetch()} />
      ) : summary ? (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            {[
              {
                label: "真实消耗 Tokens",
                value: fmtCompact(summary.total_tokens),
                sub: fmt.format(summary.total_tokens),
              },
              { label: "总成本", value: fmtUsd(summary.cost), sub: `${fmt.format(summary.calls)} 次请求` },
              {
                label: "缓存命中率",
                value:
                  summary.cache_hit_rate === null
                    ? "—"
                    : `${(summary.cache_hit_rate * 100).toFixed(1)}%`,
                sub: "缓存命中 /(命中 + 新增输入)",
              },
            ].map((s) => (
              <Card key={s.label}>
                <CardContent className="pt-4">
                  <div className="text-xs text-muted-foreground">{s.label}</div>
                  <div className="text-2xl font-semibold tabular-nums">{s.value}</div>
                  <div className="text-xs text-muted-foreground">{s.sub}</div>
                </CardContent>
              </Card>
            ))}
          </div>

          <div className="grid gap-4 md:grid-cols-4">
            {[
              { label: "新增输入", value: summary.tokens_in },
              { label: "输出", value: summary.tokens_out },
              { label: "缓存创建", value: summary.cache_write_tokens },
              { label: "缓存命中", value: summary.cache_read_tokens },
            ].map((s) => (
              <Card key={s.label}>
                <CardContent className="pt-4">
                  <div className="text-xs text-muted-foreground">{s.label}</div>
                  <div className="text-xl font-semibold tabular-nums">{fmtCompact(s.value)}</div>
                </CardContent>
              </Card>
            ))}
          </div>

          <Card>
            <CardContent className="pt-4">
              <BillingTrendCharts daily={billing.daily} />
            </CardContent>
          </Card>
        </>
      ) : null}

      <Tabs defaultValue="models">
        <TabsList>
          <TabsTrigger value="models">模型统计</TabsTrigger>
          <TabsTrigger value="orgs">租户成本</TabsTrigger>
          <TabsTrigger value="detail">租户明细</TabsTrigger>
          <TabsTrigger value="traces">请求日志</TabsTrigger>
        </TabsList>

        <TabsContent value="models">
          {billing && (
            <DataTable
              columns={MODEL_COLUMNS}
              data={billing.by_model}
              getRowId={(r) => `${r.provider}:${r.model}`}
              empty={{ title: "该时间段内没有 LLM 用量" }}
            />
          )}
        </TabsContent>

        <TabsContent value="orgs">
          {billing && (
            <DataTable
              columns={ORG_COLUMNS}
              data={billing.by_org}
              getRowId={(r) => r.org_id}
              empty={{ title: "该时间段内没有 LLM 用量" }}
            />
          )}
        </TabsContent>

        <TabsContent value="detail">
          <TenantDetail days={days} />
        </TabsContent>

        <TabsContent value="traces">
          <TraceLog days={days} />
        </TabsContent>
      </Tabs>

      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <CircleDollarSign className="size-3.5" />
        成本按「模型价格」页当前单价现算,改价即时反映到全部时间段(不做历史价快照)。
      </p>
    </div>
  );
}
