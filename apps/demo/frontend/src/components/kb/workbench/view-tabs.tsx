"use client";

// 工作台顶部视图导航:带文字标签的下划线 Tab(替代原纯图标侧边栏)。
// 分组间插竖分隔线,「设置」固定在右端;审核待办数以徽章挂在对应 Tab 上。
// 键盘可达性由 Base UI Tabs 提供(roving tabindex + 左右方向键)。

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import {
  KB_TAB_GROUPS,
  KB_VIEWS,
  type KbViewKey,
  type KbViewMeta,
} from "./views";

function ViewTab({ view, badge }: { view: KbViewMeta; badge?: number }) {
  const Icon = view.icon;
  return (
    <TabsTrigger
      value={view.key}
      className="h-8 flex-none gap-1.5 px-2.5 text-[13px]"
    >
      <Icon className="size-4" />
      {view.label}
      {badge !== undefined && badge > 0 && (
        <span className="ml-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] leading-none font-medium text-primary-foreground tabular-nums">
          {badge > 99 ? "99+" : badge}
        </span>
      )}
    </TabsTrigger>
  );
}

export function ViewTabs({
  view,
  onViewChange,
  reviewCount = 0,
  className,
}: {
  view: KbViewKey;
  onViewChange: (next: KbViewKey) => void;
  /** 待审核事实数,>0 时在「审核」Tab 上显示计数徽章 */
  reviewCount?: number;
  className?: string;
}) {
  const settings = KB_VIEWS.find((item) => item.group === "config");

  return (
    <Tabs
      value={view}
      onValueChange={(next) => onViewChange(next as KbViewKey)}
      className={cn("gap-0", className)}
    >
      <TabsList
        variant="line"
        aria-label="知识工作台视图"
        className="h-10 w-full justify-start gap-0 overflow-x-auto px-1"
      >
        {KB_TAB_GROUPS.map((group, groupIndex) => (
          <div key={group} className="flex flex-none items-center gap-0.5">
            {groupIndex > 0 && (
              <span
                aria-hidden
                className="mx-1.5 h-4 w-px shrink-0 bg-border"
              />
            )}
            {KB_VIEWS.filter((item) => item.group === group).map((item) => (
              <ViewTab
                key={item.key}
                view={item}
                badge={item.key === "review" ? reviewCount : undefined}
              />
            ))}
          </div>
        ))}
        {settings && (
          <div className="ml-auto flex flex-none items-center pl-2">
            <ViewTab view={settings} />
          </div>
        )}
      </TabsList>
    </Tabs>
  );
}
