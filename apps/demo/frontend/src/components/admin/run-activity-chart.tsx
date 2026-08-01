"use client";

import { useTheme } from "next-themes";
import {
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

export interface AgentRunTrendPoint {
  day: string;
  success: number;
  failed: number;
  waiting: number;
  running: number;
}

const SERIES = [
  { key: "success", label: "成功", light: "#16a36a", dark: "#34d399" },
  { key: "failed", label: "失败", light: "#dc3e42", dark: "#f87171" },
  { key: "waiting", label: "等待中", light: "#d68a00", dark: "#fbbf24" },
  { key: "running", label: "运行中", light: "#2a78d6", dark: "#60a5fa" },
] as const;

function formatDay(value: string): string {
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")}`;
}

interface TooltipEntry {
  color?: string;
  dataKey?: string;
  name?: string;
  value?: number | string;
}

function RunChartTooltip({
  active,
  label,
  payload,
}: {
  active?: boolean;
  label?: string;
  payload?: TooltipEntry[];
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="min-w-32 rounded-lg border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="mb-1.5 font-medium text-popover-foreground">
        {label ? formatDay(label) : "—"}
      </div>
      <div className="space-y-1">
        {payload
          .filter((entry) => Number(entry.value) > 0)
          .map((entry) => (
            <div key={entry.dataKey} className="flex items-center gap-2">
              <span
                className="size-2 rounded-sm"
                style={{ backgroundColor: entry.color }}
                aria-hidden
              />
              <span className="text-muted-foreground">{entry.name}</span>
              <span className="ml-auto font-mono tabular-nums text-popover-foreground">
                {entry.value}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}

export function RunActivityChart({
  trend,
}: {
  trend: AgentRunTrendPoint[];
}) {
  const { resolvedTheme } = useTheme();
  const mounted = useMounted();
  const dark = mounted && resolvedTheme === "dark";
  const gridStroke = dark ? "#333330" : "#e5e7eb";
  const tickFill = dark ? "#9ca3af" : "#6b7280";

  if (!trend.length) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        当前筛选范围内暂无趋势数据
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart
        accessibilityLayer
        data={trend}
        margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
      >
        <CartesianGrid
          stroke={gridStroke}
          strokeDasharray="3 3"
          vertical={false}
        />
        <XAxis
          dataKey="day"
          tickFormatter={formatDay}
          tick={{ fontSize: 11, fill: tickFill }}
          tickLine={false}
          axisLine={{ stroke: gridStroke }}
        />
        <YAxis
          allowDecimals={false}
          tick={{ fontSize: 11, fill: tickFill }}
          tickLine={false}
          axisLine={false}
          width={40}
        />
        <Tooltip
          content={<RunChartTooltip />}
          cursor={{ fill: gridStroke, opacity: 0.3 }}
        />
        <Legend
          iconType="circle"
          iconSize={7}
          wrapperStyle={{ fontSize: 12 }}
          formatter={(value: string) => (
            <span className="text-xs text-muted-foreground">{value}</span>
          )}
        />
        {SERIES.map((series) => (
          <Bar
            key={series.key}
            dataKey={series.key}
            name={series.label}
            stackId="runs"
            fill={dark ? series.dark : series.light}
            isAnimationActive={false}
            maxBarSize={36}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
