/**
 * 路由面包屑(手动传层级):next/link + ChevronRight 分隔,末项高亮。
 *
 * @example
 * ```tsx
 * <Breadcrumbs
 *   items={[
 *     { label: "旅行计划", href: "/app/projects" },
 *     { label: "英国苏格兰10日" },
 *   ]}
 * />
 * ```
 */

import Link from "next/link";
import { ChevronRight } from "lucide-react";

export function Breadcrumbs({
  items,
}: {
  items: { label: string; href?: string }[];
}) {
  return (
    <nav aria-label="面包屑" className="min-w-0 overflow-hidden">
      <ol className="flex min-w-0 flex-nowrap items-center gap-1.5 overflow-hidden text-sm">
        {items.map((item, index) => {
          const isLast = index === items.length - 1;
          return (
            <li
              key={index}
              className={`flex items-center gap-1.5 ${isLast ? "min-w-0" : "shrink-0"}`}
            >
              {index > 0 && (
                <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/60" />
              )}
              {isLast ? (
                <span
                  aria-current="page"
                  className="truncate font-medium text-foreground"
                >
                  {item.label}
                </span>
              ) : item.href ? (
                <Link
                  href={item.href}
                  className="text-muted-foreground transition-colors hover:text-foreground"
                >
                  {item.label}
                </Link>
              ) : (
                <span className="text-muted-foreground">{item.label}</span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
