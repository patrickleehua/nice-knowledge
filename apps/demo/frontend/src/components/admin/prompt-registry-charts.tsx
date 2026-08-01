"use client";

// 可编辑 Prompt 概览图(dataviz 规范落地):两张单轴横向条形图——
// 各任务版本数 + 运行时生效版本内容体量,量纲不同故各自成图(不做双轴)。
// 颜色编码"来源"身份而非数值:内置=slot1 蓝、自定义=slot2 橙
// (光暗两模式均过 validate_palette 六检:CVD ΔE 24.7/26.8,对比度 ≥3:1),
// 两个系列故图例常驻;数值由悬停 tooltip 与下方表格承载,不在每根条上标数。

import { useTheme } from "next-themes";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useMounted } from "@/lib/auth";

// dataviz 参考色板 slot1/slot2:颜色跟随"内置/自定义"实体属性,过滤不重排
const SOURCE_COLORS = {
  builtin: { label: "内置", light: "#2a78d6", dark: "#3987e5" },
  custom: { label: "自定义", light: "#eb6834", dark: "#d95926" },
} as const;

const fmt = new Intl.NumberFormat("zh-CN");
const fmtCompact = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export interface PromptChartRow {
  task: string;
  name: string;
  builtin: boolean;
  /** 该任务的登记版本数 */
  versions: number;
  /** 运行时生效版本(最高启用版)的内容字符数;无启用版本为 0 */
  chars: number;
}

interface TooltipPayload {
  payload?: PromptChartRow;
  value?: number | string;
}

function ChartTooltip({
  active,
  payload,
  unit,
  compact,
}: {
  active?: boolean;
  payload?: TooltipPayload[];
  unit: string;
  compact?: boolean;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return (
    <div className="rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md">
      <div className="font-medium text-popover-foreground">{row.name}</div>
      <div className="font-mono text-muted-foreground">{row.task}</div>
      <div className="mt-1 flex items-center justify-between gap-3">
        <span className="text-muted-foreground">
          {SOURCE_COLORS[row.builtin ? "builtin" : "custom"].label}
        </span>
        <span className="font-mono tabular-nums text-popover-foreground">
          {compact && Number(payload?.[0]?.value) >= 10000
            ? fmtCompact.format(Number(payload?.[0]?.value))
            : fmt.format(Number(payload?.[0]?.value))}
          {unit}
        </span>
      </div>
    </div>
  );
}

function SourceLegend({ dark }: { dark: boolean }) {
  return (
    <div className="flex items-center gap-3" aria-label="来源图例">
      {(["builtin", "custom"] as const).map((key) => (
        <span key={key} className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span
            className="size-2 rounded-[2px]"
            style={{ backgroundColor: dark ? SOURCE_COLORS[key].dark : SOURCE_COLORS[key].light }}
            aria-hidden
          />
          {SOURCE_COLORS[key].label}
        </span>
      ))}
    </div>
  );
}

function TaskBarChart({
  rows,
  dataKey,
  unit,
  dark,
  compact,
  allowDecimals,
}: {
  rows: PromptChartRow[];
  dataKey: "versions" | "chars";
  unit: string;
  dark: boolean;
  compact?: boolean;
  allowDecimals?: boolean;
}) {
  const gridStroke = dark ? "#333330" : "#E5E7EB";
  const tickFill = dark ? "#9CA3AF" : "#6B7280";
  // 高度随任务数走(每行 26px + 轴带),避免固定高度把 x 轴文字挤出容器
  const height = rows.length * 26 + 36;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 12, bottom: 0, left: 0 }}>
        {/* 实线细网格,只画值轴方向(横向条形图纵向格线才有对照意义) */}
        <CartesianGrid stroke={gridStroke} horizontal={false} />
        <XAxis
          type="number"
          tickFormatter={(v: number) => (compact ? fmtCompact.format(v) : fmt.format(v))}
          tick={{ fontSize: 11, fill: tickFill }}
          tickLine={false}
          axisLine={{ stroke: gridStroke }}
          allowDecimals={allowDecimals ?? false}
        />
        <YAxis
          type="category"
          dataKey="name"
          width={104}
          tick={{ fontSize: 11, fill: tickFill }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          content={<ChartTooltip unit={unit} compact={compact} />}
          cursor={{ fill: gridStroke, opacity: 0.3 }}
        />
        <Bar dataKey={dataKey} maxBarSize={14} radius={[0, 4, 4, 0]}>
          {rows.map((row) => {
            const color = SOURCE_COLORS[row.builtin ? "builtin" : "custom"];
            return <Cell key={row.task} fill={dark ? color.dark : color.light} />;
          })}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function PromptRegistryCharts({ rows }: { rows: PromptChartRow[] }) {
  const { resolvedTheme } = useTheme();
  const mounted = useMounted();
  const dark = mounted && resolvedTheme === "dark";

  if (!rows.length) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        当前筛选条件下没有任务,图表暂无数据。
      </p>
    );
  }

  // 幅度比较图按各自数值降序;颜色跟实体属性走,排序/过滤不影响用色
  const byVersions = [...rows].sort((a, b) => b.versions - a.versions);
  const byChars = [...rows].sort((a, b) => b.chars - a.chars);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          随上方筛选联动;悬停查看数值,精确值见下方表格
        </p>
        <SourceLegend dark={dark} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-2">
          <h3 className="text-sm font-medium">各任务版本数</h3>
          <TaskBarChart rows={byVersions} dataKey="versions" unit=" 个版本" dark={dark} />
        </div>
        <div className="space-y-2">
          <h3 className="text-sm font-medium">生效版本内容体量(字符)</h3>
          <TaskBarChart rows={byChars} dataKey="chars" unit=" 字符" dark={dark} compact />
        </div>
      </div>
    </div>
  );
}
