/**
 * 统一空状态:icon + title + description + action,居中 py-12。
 *
 * @example
 * ```tsx
 * import { FolderOpen } from "lucide-react";
 *
 * <EmptyState
 *   icon={FolderOpen}
 *   title="暂无数据"
 *   description="创建第一个知识库,开始上传资料"
 *   action={<Button>新建知识库</Button>}
 * />
 * ```
 */

import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 py-12 text-center",
        className,
      )}
    >
      {Icon && <Icon className="size-8 text-muted-foreground/60" />}
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description && (
          <p className="text-sm text-muted-foreground">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}
