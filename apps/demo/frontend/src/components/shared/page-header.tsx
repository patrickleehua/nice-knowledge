/**
 * 列表页/工作区统一页头:挂到 AppShell 顶栏，左 title + description,右 actions 区。
 *
 * @example
 * ```tsx
 * <PageHeader
 *   title="旅行计划列表"
 *   description="管理全部定制旅行计划"
 *   actions={<Button><Plus />新建旅行计划</Button>}
 * />
 * ```
 */

import { ShellHeader } from "@/components/shell/app-shell";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <ShellHeader>
      <div
        className={cn(
          "flex h-full min-w-0 items-center gap-3 overflow-hidden",
          className,
        )}
      >
        <div className="min-w-0">
          <h1 className="font-heading truncate text-sm font-semibold sm:text-base">
            {title}
          </h1>
          {description && (
            <p className="hidden truncate text-xs text-muted-foreground xl:block">
              {description}
            </p>
          )}
        </div>
        {actions && (
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {actions}
          </div>
        )}
      </div>
    </ShellHeader>
  );
}
