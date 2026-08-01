/**
 * 查询失败统一内联块:CircleAlert + errMsg(error) + 可选重试按钮。
 * 用于 useQuery 的 error 分支,禁止查询失败退化成误导性空态。
 *
 * @example
 * ```tsx
 * const { data, error, refetch } = useQuery(...);
 * if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
 * ```
 */
"use client";

import { CircleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn, errMsg } from "@/lib/utils";
export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 py-8 text-center",
        className,
      )}
    >
      <CircleAlert className="size-6 text-destructive" />
      <p className="text-sm text-muted-foreground">{errMsg(error)}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          重试
        </Button>
      )}
    </div>
  );
}
