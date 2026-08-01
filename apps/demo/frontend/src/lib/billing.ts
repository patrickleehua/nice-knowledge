// 计费(stage-d 09):模型单价 CRUD 与用量计费看板 DTO + hooks。
// 口径:tokens_in = 非缓存新输入;成本 = Σ tokens × 单价(USD / 1M tokens)。

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ModelPriceOut {
  id: string;
  provider: string;
  model: string;
  display_name: string;
  // Numeric 经 JSON 序列化为字符串
  input_price: string;
  output_price: string;
  cache_read_price: string;
  cache_write_price: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface BillingDaily {
  usage_date: string;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  calls: number;
  cost: number;
}

export interface BillingModelRow {
  provider: string;
  model: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost: number | null; // null = 未定价
  avg_cost: number | null;
}

export interface BillingOrgRow {
  org_id: string;
  org_name: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost: number;
}

export interface BillingSummary {
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cost: number;
  cache_hit_rate: number | null;
  unpriced: string[];
}

export interface BillingOut {
  summary: BillingSummary;
  daily: BillingDaily[];
  by_model: BillingModelRow[];
  by_org: BillingOrgRow[];
}

export interface TraceRow {
  id: string;
  created_at: string | null;
  org_name: string;
  task: string;
  provider: string;
  model: string;
  status: string;
  error: string | null;
  attempt: number;
  fallback_from: string | null;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  latency_ms: number;
  cost: number | null;
}

export interface TracePage {
  items: TraceRow[];
  total: number;
  page: number;
  page_size: number;
}

export function useModelPrices() {
  return useQuery({
    queryKey: ["model-prices"],
    queryFn: () => api.get<ModelPriceOut[]>("/admin/model-prices"),
  });
}

export function useBilling(days: number) {
  return useQuery({
    queryKey: ["billing", days],
    queryFn: () => api.get<BillingOut>(`/admin/billing?days=${days}`),
  });
}

export function useLlmTraces(opts: {
  days: number;
  page: number;
  status?: "success" | "error";
  enabled?: boolean;
}) {
  const { days, page, status, enabled = true } = opts;
  const statusParam = status ? `&status_filter=${status}` : "";
  return useQuery({
    queryKey: ["llm-traces", days, page, status ?? "all"],
    queryFn: () =>
      api.get<TracePage>(`/admin/llm-traces?days=${days}&page=${page}&page_size=50${statusParam}`),
    enabled,
  });
}

/** $12.3456 / $0.0012 —— 成本展示统一 4 位小数,大额去尾零 */
export function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

/** 92.3 万 / 9.23 亿 —— 大数中文单位缩写(tokens 汇总卡) */
export function fmtCompact(n: number): string {
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(2)} 亿`;
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)} 万`;
  return n.toLocaleString("zh-CN");
}
