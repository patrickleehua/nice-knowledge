"use client";

// 计费看板趋势图(stage-d 09):tokens 堆叠面积 + 成本柱,两张单轴图
// (刻意不做双轴图——两个量纲各自成图)。色板为 dataviz 校验通过的
// 分类色 slot1-4(light/dark 各自校验:亮色面 CVD ΔE 24.2),图例常驻。

import { useTheme } from "next-themes";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useMounted } from "@/lib/auth";
import { fmtCompact, fmtUsd, type BillingDaily } from "@/lib/billing";

// dataviz 参考色板 slot1-4,固定顺序分配给固定序列(不随序列数变化重排)
const SERIES = [
  { key: "tokens_in", label: "新增输入", light: "#2a78d6", dark: "#3987e5" },
  { key: "tokens_out", label: "输出", light: "#1baf7a", dark: "#199e70" },
  { key: "cache_write_tokens", label: "缓存创建", light: "#eda100", dark: "#c98500" },
  { key: "cache_read_tokens", label: "缓存命中", light: "#008300", dark: "#008300" },
] as const;

function fmtDay(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
}

interface TooltipEntry {
  name?: string;
  value?: number | string;
  color?: string;
  dataKey?: string;
}

function ChartTooltip({
  active,
  payload,
  label,
  money,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string;
  money?: boolean;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md">
      <div className="mb-1 font-medium text-popover-foreground">{label && fmtDay(label)}</div>
      {payload.map((entry) => (
        <div key={entry.dataKey} className="flex items-center gap-1.5">
          <span
            className="size-2 rounded-[2px]"
            style={{ backgroundColor: entry.color }}
            aria-hidden
          />
          <span className="text-muted-foreground">{entry.name}</span>
          <span className="ml-auto pl-3 font-mono tabular-nums text-popover-foreground">
            {money ? fmtUsd(Number(entry.value)) : fmtCompact(Number(entry.value))}
          </span>
        </div>
      ))}
    </div>
  );
}

export function BillingTrendCharts({ daily }: { daily: BillingDaily[] }) {
  const { resolvedTheme } = useTheme();
  const mounted = useMounted();
  const dark = mounted && resolvedTheme === "dark";

  const gridStroke = dark ? "#333330" : "#E5E7EB";
  const tickFill = dark ? "#9CA3AF" : "#6B7280";
  const axisTick = { fontSize: 11, fill: tickFill };
  const costColor = dark ? "#3987e5" : "#2a78d6";

  if (!daily.length) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        该时间段内没有 LLM 用量,趋势图暂无数据。
      </p>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="space-y-2">
        <h3 className="text-sm font-medium">Token 用量趋势(按日,四类计量堆叠)</h3>
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={daily} margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={gridStroke} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="usage_date"
              tickFormatter={fmtDay}
              tick={axisTick}
              tickLine={false}
              axisLine={{ stroke: gridStroke }}
            />
            <YAxis
              tickFormatter={(v: number) => fmtCompact(v)}
              tick={axisTick}
              tickLine={false}
              axisLine={false}
              width={56}
            />
            <Tooltip content={<ChartTooltip />} />
            <Legend
              iconType="plainline"
              wrapperStyle={{ fontSize: 12 }}
              formatter={(value: string) => (
                <span className="text-xs text-muted-foreground">{value}</span>
              )}
            />
            {SERIES.map((s) => (
              <Area
                key={s.key}
                dataKey={s.key}
                name={s.label}
                stackId="tokens"
                stroke={dark ? s.dark : s.light}
                fill={dark ? s.dark : s.light}
                fillOpacity={0.25}
                strokeWidth={2}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">成本趋势(按日,USD)</h3>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={daily} margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={gridStroke} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="usage_date"
              tickFormatter={fmtDay}
              tick={axisTick}
              tickLine={false}
              axisLine={{ stroke: gridStroke }}
            />
            <YAxis
              tickFormatter={(v: number) => `$${v}`}
              tick={axisTick}
              tickLine={false}
              axisLine={false}
              width={56}
            />
            <Tooltip content={<ChartTooltip money />} cursor={{ fill: gridStroke, opacity: 0.3 }} />
            <Bar
              dataKey="cost"
              name="成本"
              fill={costColor}
              radius={[4, 4, 0, 0]}
              maxBarSize={28}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
