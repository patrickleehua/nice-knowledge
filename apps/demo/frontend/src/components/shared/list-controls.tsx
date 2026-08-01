"use client";

/**
 * 列表页通用控件:搜索框 / 已应用筛选 chips / 批量操作条 / 分页。
 * 抽出来是为了让知识库各列表(资料、图片、审核)有一致的工具条语言——
 * 此前每个视图各写一套,有的连搜索和全选都没有。
 */

import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * 列表搜索框:内部防抖 250ms,IME 组字期间不向外提交
 * (中文输入法逐字上屏会打出一串无意义查询)。
 */
export function SearchInput({
  value,
  onChange,
  placeholder = "搜索…",
  label,
  className,
  debounceMs = 250,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  label: string;
  className?: string;
  debounceMs?: number;
}) {
  const [draft, setDraft] = useState(value);
  const composing = useRef(false);

  // 外部值(如 URL 回退、清除筛选)变化时同步回输入框
  const [lastValue, setLastValue] = useState(value);
  if (lastValue !== value) {
    setLastValue(value);
    setDraft(value);
  }

  useEffect(() => {
    if (draft === value) return;
    const timer = setTimeout(() => {
      if (!composing.current) onChange(draft);
    }, debounceMs);
    return () => clearTimeout(timer);
  }, [draft, debounceMs, onChange, value]);

  return (
    <div className={cn("relative min-w-0", className)}>
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onCompositionStart={() => (composing.current = true)}
        onCompositionEnd={(event) => {
          composing.current = false;
          setDraft(event.currentTarget.value);
        }}
        aria-label={label}
        placeholder={placeholder}
        className="h-8 pr-7 pl-7.5 text-sm"
      />
      {draft && (
        <button
          type="button"
          aria-label="清除搜索"
          onClick={() => {
            setDraft("");
            onChange("");
          }}
          className="absolute top-1/2 right-2 -translate-y-1/2 rounded-sm p-0.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

export interface FilterChip {
  key: string;
  label: string;
  onRemove: () => void;
}

/** 已应用筛选的可见反馈:每个 chip 可单独移除,末尾给「全部清除」。 */
export function FilterChips({
  chips,
  onClearAll,
  className,
}: {
  chips: FilterChip[];
  onClearAll: () => void;
  className?: string;
}) {
  if (chips.length === 0) return null;
  return (
    <div
      className={cn("flex flex-wrap items-center gap-1.5", className)}
      aria-label="已应用的筛选"
    >
      {chips.map((chip) => (
        <span
          key={chip.key}
          className="inline-flex items-center gap-1 rounded-full border border-border bg-muted/50 py-0.5 pr-1 pl-2 text-xs"
        >
          {chip.label}
          <button
            type="button"
            aria-label={`移除筛选:${chip.label}`}
            onClick={chip.onRemove}
            className="rounded-full p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      {chips.length > 1 && (
        <Button
          variant="ghost"
          size="xs"
          className="h-6 text-muted-foreground"
          onClick={onClearAll}
        >
          全部清除
        </Button>
      )}
    </div>
  );
}

/** 批量操作条:选中数 + 操作按钮 + 取消选择,吸附在列表顶部。 */
export function BulkActionBar({
  count,
  onClear,
  children,
  className,
}: {
  count: number;
  onClear: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  if (count === 0) return null;
  return (
    <div
      role="toolbar"
      aria-label="批量操作"
      className={cn(
        "sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-2.5 py-1.5 text-xs backdrop-blur-sm",
        className,
      )}
    >
      <span className="font-medium tabular-nums">已选 {count} 项</span>
      {children}
      <Button
        variant="ghost"
        size="xs"
        className="ml-auto text-muted-foreground"
        onClick={onClear}
      >
        取消选择
      </Button>
    </div>
  );
}

/** 标准分页:总数 + 页码 + 上下页;总数未知时传 total=null 只显示页码。 */
export function Pagination({
  page,
  totalPages,
  total,
  unit = "条",
  onChange,
  className,
}: {
  page: number;
  totalPages: number;
  total?: number | null;
  unit?: string;
  onChange: (next: number) => void;
  className?: string;
}) {
  if (totalPages <= 1) return null;
  return (
    <nav
      aria-label="分页"
      className={cn("flex items-center justify-center gap-2", className)}
    >
      <Button
        variant="outline"
        size="sm"
        disabled={page <= 1}
        onClick={() => onChange(Math.max(1, page - 1))}
      >
        <ChevronLeft />
        上一页
      </Button>
      <span className="text-xs text-muted-foreground tabular-nums">
        第 {page} / {totalPages} 页
        {typeof total === "number" ? ` · 共 ${total} ${unit}` : ""}
      </span>
      <Button
        variant="outline"
        size="sm"
        disabled={page >= totalPages}
        onClick={() => onChange(Math.min(totalPages, page + 1))}
      >
        下一页
        <ChevronRight />
      </Button>
    </nav>
  );
}
