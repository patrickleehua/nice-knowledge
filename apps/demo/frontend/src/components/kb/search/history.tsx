"use client";

// 检索历史(检索页与 ⌘K 命令面板共用)。本地保存,不上行——查询词可能含客户信息,
// 没必要为了一个便利功能把它送到服务端。

import { History, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useStoredList } from "@/lib/use-local-storage";

const HISTORY_KEY = "kb-search-history";
const HISTORY_MAX = 8;

export function useSearchHistory() {
  return useStoredList(HISTORY_KEY, HISTORY_MAX);
}

/** 历史词条列表。空历史时不渲染任何东西。 */
export function SearchHistory({
  onPick,
  className,
}: {
  onPick: (query: string) => void;
  className?: string;
}) {
  const { items, remove, clear } = useSearchHistory();
  if (items.length === 0) return null;

  return (
    <div className={className}>
      <div className="flex flex-wrap items-center justify-center gap-1.5">
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <History className="size-3.5" />
          最近检索
        </span>
        {items.map((item) => (
          <span
            key={item}
            className="inline-flex items-center gap-0.5 rounded-full border border-white/62 bg-white/52 py-0.5 pr-1 pl-2.5 text-xs shadow-[inset_0_1px_0_rgb(255_255_255/0.72)] transition-colors hover:border-primary/35 hover:bg-white/84 dark:border-white/[0.08] dark:bg-white/[0.055] dark:hover:bg-white/[0.09]"
          >
            <button
              type="button"
              className="max-w-40 truncate"
              title={item}
              onClick={() => onPick(item)}
            >
              {item}
            </button>
            <button
              type="button"
              aria-label={`从历史中移除「${item}」`}
              onClick={() => remove(item)}
              className="rounded-full p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
        <Button
          variant="ghost"
          size="xs"
          className="h-6 text-muted-foreground"
          onClick={clear}
        >
          清除历史
        </Button>
      </div>
    </div>
  );
}
