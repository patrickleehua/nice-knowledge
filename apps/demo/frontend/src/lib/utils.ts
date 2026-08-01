import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

import { ApiError } from "@/lib/api-error";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ---------- 徽章色调 / 状态元数据 ----------

export type BadgeTone =
  | "muted"
  | "primary"
  | "warning"
  | "success"
  | "destructive"
  | "teal";

export interface StatusMeta {
  label: string;
  tone: BadgeTone;
}

// ---------- 通用格式化 ----------

export function errMsg(err: unknown, fallback = "操作失败"): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return `角色权限不足:${err.message}`;
    return err.message;
  }
  return fallback;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
