"use client";

// 知识工作台骨架:顶部视图 Tab | (可选)左侧导航栏 + 中央视图 | 底部发布状态条。
//
// 相对旧版的三处改变:
// 1. 去掉常驻纯图标栏,导航改由顶部带文字的 Tab 承担(见 view-tabs.tsx);
// 2. 左栏改为「按需 + 可折叠 + 宽度持久化」,只有真正有导航内容的视图才渲染;
// 3. 移动端不再把左栏 `hidden` 掉(旧版导致 Wiki/文档树在手机上完全不可达),
//    改为同一份内容走 Sheet 抽屉。
//
// 拖拽期间在 body 上标记 data-panel-resizing,图谱等重渲染敏感组件可据此暂停响应。

import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet";
import { useStoredFlag, useStoredNumber } from "@/lib/use-local-storage";
import { cn } from "@/lib/utils";

const MIN_PANEL_WIDTH = 180;
const MAX_PANEL_WIDTH = 420;
const DEFAULT_PANEL_WIDTH = 240;
const WIDTH_KEY = "kb-workbench-panel-width";
const COLLAPSED_KEY = "kb-workbench-panel-collapsed";

export interface WorkbenchSidebar {
  /** 侧栏标题(同时用作移动端抽屉标题与折叠按钮的无障碍名) */
  title: string;
  content: ReactNode;
}

export function WorkbenchShell({
  tabs,
  sidebar,
  statusBar,
  children,
}: {
  /** 顶部视图导航 */
  tabs: ReactNode;
  /** 左栏(文档树 / 页面列表 / 目的地列表);不传表示当前视图不需要 */
  sidebar?: WorkbenchSidebar;
  /** 底部常驻发布状态条 */
  statusBar?: ReactNode;
  children: ReactNode;
}) {
  const [storedWidth, persistWidth] = useStoredNumber(
    WIDTH_KEY,
    DEFAULT_PANEL_WIDTH,
    MIN_PANEL_WIDTH,
    MAX_PANEL_WIDTH,
  );
  const [collapsed, setCollapsed] = useStoredFlag(COLLAPSED_KEY, false);
  const [mobileOpen, setMobileOpen] = useState(false);
  // 拖拽过程中的即时宽度:不落盘,松手时才写偏好,避免每帧一次 localStorage 写入
  const [dragWidth, setDragWidth] = useState<number | null>(null);
  const panelWidth = dragWidth ?? storedWidth;

  const toggleCollapsed = () => setCollapsed(!collapsed);

  function onHandleMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = panelWidth;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.dataset.panelResizing = "true";
    let latest = startWidth;
    const onMove = (ev: MouseEvent) => {
      latest = Math.max(
        MIN_PANEL_WIDTH,
        Math.min(MAX_PANEL_WIDTH, startWidth + ev.clientX - startX),
      );
      setDragWidth(latest);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      delete document.body.dataset.panelResizing;
      persistWidth(latest);
      setDragWidth(null);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  // 键盘可调宽:方向键 ±16px,Home/End 归到上下限
  function onHandleKeyDown(e: React.KeyboardEvent) {
    const step = e.shiftKey ? 48 : 16;
    let next: number | null = null;
    if (e.key === "ArrowLeft") next = panelWidth - step;
    else if (e.key === "ArrowRight") next = panelWidth + step;
    else if (e.key === "Home") next = MIN_PANEL_WIDTH;
    else if (e.key === "End") next = MAX_PANEL_WIDTH;
    if (next === null) return;
    e.preventDefault();
    persistWidth(Math.max(MIN_PANEL_WIDTH, Math.min(MAX_PANEL_WIDTH, next)));
  }

  const showDesktopPanel = Boolean(sidebar) && !collapsed;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex shrink-0 items-center gap-1 border-b border-border pr-2">
        {sidebar && (
          <>
            <Button
              variant="ghost"
              size="icon"
              className="ml-1 hidden size-8 shrink-0 md:inline-flex"
              aria-label={
                collapsed ? `展开${sidebar.title}` : `折叠${sidebar.title}`
              }
              aria-expanded={!collapsed}
              title={
                collapsed ? `展开${sidebar.title}` : `折叠${sidebar.title}`
              }
              onClick={toggleCollapsed}
            >
              {collapsed ? (
                <PanelLeftOpen className="size-4" />
              ) : (
                <PanelLeftClose className="size-4" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="ml-1 size-8 shrink-0 md:hidden"
              aria-label={`打开${sidebar.title}`}
              onClick={() => setMobileOpen(true)}
            >
              <PanelLeftOpen className="size-4" />
            </Button>
          </>
        )}
        <div className="min-w-0 flex-1">{tabs}</div>
      </div>

      <div className="flex min-h-0 flex-1">
        {showDesktopPanel && sidebar && (
          <>
            <aside
              style={{ width: panelWidth }}
              className="hidden min-h-0 shrink-0 flex-col bg-sidebar md:flex"
              aria-label={sidebar.title}
            >
              <div className="min-h-0 flex-1 overflow-y-auto">
                {sidebar.content}
              </div>
            </aside>
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="调整左栏宽度"
              aria-valuenow={panelWidth}
              aria-valuemin={MIN_PANEL_WIDTH}
              aria-valuemax={MAX_PANEL_WIDTH}
              tabIndex={0}
              onMouseDown={onHandleMouseDown}
              onKeyDown={onHandleKeyDown}
              className="hidden w-1.5 shrink-0 cursor-col-resize border-l border-border transition-colors hover:bg-border focus-visible:bg-primary/40 focus-visible:outline-none active:bg-border md:block"
            />
          </>
        )}

        <main className={cn("flex min-w-0 flex-1 flex-col")}>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
            {children}
          </div>
        </main>
      </div>

      {statusBar}

      {sidebar && (
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent side="left" className="w-72 gap-0 bg-sidebar p-0">
            <SheetTitle className="border-b border-border px-3 py-2.5 text-sm font-semibold">
              {sidebar.title}
            </SheetTitle>
            <SheetDescription className="sr-only">
              知识工作台{sidebar.title}
            </SheetDescription>
            <div
              className="min-h-0 flex-1 overflow-y-auto"
              onClick={(event) => {
                const target = event.target;
                if (
                  target instanceof Element &&
                  target.closest("[data-panel-navigation]")
                ) {
                  setMobileOpen(false);
                }
              }}
            >
              {sidebar.content}
            </div>
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
