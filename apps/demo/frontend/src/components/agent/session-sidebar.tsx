"use client";

import {
  History,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Trash2,
} from "lucide-react";
import { ConfirmDialog } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type { ChatSessionOut } from "@/lib/chat";
import { cn } from "@/lib/utils";

interface SessionSidebarProps {
  scopeLabel: string;
  sessions: ChatSessionOut[] | undefined;
  activeSessionId: string | null;
  collapsed: boolean;
  mobileOpen: boolean;
  disabled: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onMobileOpenChange: (open: boolean) => void;
  onCreate: () => void;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => Promise<unknown>;
}

function SessionRows({
  sessions,
  activeSessionId,
  disabled,
  onSelect,
  onDelete,
}: Pick<
  SessionSidebarProps,
  "sessions" | "activeSessionId" | "disabled" | "onSelect" | "onDelete"
>) {
  if (!sessions) {
    return (
      <div className="space-y-2 px-2 py-3" aria-label="正在加载会话">
        {["w-4/5", "w-3/5", "w-2/3"].map((width) => (
          <div
            key={width}
            className={cn("h-8 animate-pulse rounded-lg bg-muted/70", width)}
          />
        ))}
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="px-3 py-8 text-center">
        <MessageSquareText className="mx-auto mb-2 size-4 text-muted-foreground/60" />
        <p className="text-xs text-muted-foreground">还没有会话</p>
      </div>
    );
  }

  return (
    <div className="space-y-0.5 p-2">
      {sessions.map((session) => {
        const active = session.id === activeSessionId;
        return (
          <div
            key={session.id}
            className={cn(
              "group flex items-center rounded-xl transition-colors",
              active
                ? "bg-white/88 text-foreground shadow-[0_7px_22px_rgb(15_23_42/0.08),inset_0_1px_0_rgb(255_255_255/0.8)] ring-1 ring-black/[0.04] dark:bg-white/[0.12] dark:shadow-[0_7px_22px_rgb(0_0_0/0.24),inset_0_1px_0_rgb(255_255_255/0.12)] dark:ring-white/[0.07]"
                : "text-muted-foreground hover:bg-white/58 hover:text-foreground hover:shadow-[inset_0_1px_0_rgb(255_255_255/0.72)] dark:hover:bg-white/[0.07]",
            )}
          >
            <button
              type="button"
              disabled={disabled}
              onClick={() => onSelect(session.id)}
              className="flex min-w-0 flex-1 items-center gap-2.5 px-2.5 py-2 text-left disabled:cursor-not-allowed disabled:opacity-60"
              aria-current={active ? "page" : undefined}
            >
              <MessageSquareText
                className={cn(
                  "size-3.5 shrink-0",
                  active ? "text-primary" : "text-muted-foreground/70",
                )}
              />
              <span
                className={cn(
                  "truncate text-[13px]",
                  !active && "text-muted-foreground",
                )}
              >
                {session.title}
              </span>
            </button>
            <ConfirmDialog
              trigger={
                <button
                  type="button"
                  disabled={disabled}
                  title="删除会话"
                  aria-label={`删除会话：${session.title}`}
                  className="mr-1.5 rounded-md p-1.5 opacity-0 transition-opacity hover:bg-destructive/10 disabled:cursor-not-allowed group-focus-within:opacity-100 group-hover:opacity-100"
                >
                  <Trash2 className="size-3.5 text-destructive" />
                </button>
              }
              title="删除该会话?"
              description="会话内的消息与执行记录将一并删除，不可恢复。"
              confirmLabel="删除"
              destructive
              onConfirm={() => onDelete(session.id)}
            />
          </div>
        );
      })}
    </div>
  );
}

export function SessionSidebar({
  scopeLabel,
  sessions,
  activeSessionId,
  collapsed,
  mobileOpen,
  disabled,
  onCollapsedChange,
  onMobileOpenChange,
  onCreate,
  onSelect,
  onDelete,
}: SessionSidebarProps) {
  function selectAndClose(sessionId: string) {
    onSelect(sessionId);
    onMobileOpenChange(false);
  }

  function createAndClose() {
    onCreate();
    onMobileOpenChange(false);
  }

  return (
    <>
      <aside
        className={cn(
          "nk-session-sidebar hidden shrink-0 flex-col transition-[width] duration-200 md:flex",
          collapsed ? "w-12" : "w-64",
        )}
        aria-label="会话导航"
      >
        {collapsed ? (
          <div className="flex flex-col items-center gap-1 p-1.5">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => onCollapsedChange(false)}
              title="展开会话"
              aria-label="展开会话列表"
            >
              <PanelLeftOpen className="size-4" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onCreate}
              disabled={disabled}
              title="新对话"
              aria-label="新建对话"
            >
              <Plus className="size-4" />
            </Button>
            <div className="my-1 h-px w-6 bg-border" />
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => onCollapsedChange(false)}
              title={scopeLabel}
              aria-label={`查看${scopeLabel}`}
              className={cn(
                activeSessionId &&
                  "bg-white/82 shadow-xs ring-1 ring-black/[0.04] dark:bg-white/[0.1] dark:ring-white/[0.065]",
              )}
            >
              <History className="size-4" />
            </Button>
          </div>
        ) : (
          <>
            <div className="flex items-center gap-2 p-2.5">
              <Button
                type="button"
                variant="ghost"
                className="min-w-0 flex-1 justify-start bg-white/52 shadow-[inset_0_1px_0_rgb(255_255_255/0.75)] ring-1 ring-black/[0.035] hover:bg-white/82 dark:bg-white/[0.055] dark:ring-white/[0.055] dark:hover:bg-white/[0.09]"
                onClick={onCreate}
                disabled={disabled}
              >
                <Plus className="size-4" />
                新对话
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => onCollapsedChange(true)}
                title="折叠会话"
                aria-label="折叠会话列表"
              >
                <PanelLeftClose className="size-4" />
              </Button>
            </div>
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="flex items-center gap-2 px-4 pt-3 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">
                <History className="size-3.5" />
                {scopeLabel}
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                <SessionRows
                  sessions={sessions}
                  activeSessionId={activeSessionId}
                  disabled={disabled}
                  onSelect={onSelect}
                  onDelete={onDelete}
                />
              </div>
            </div>
          </>
        )}
      </aside>

      <Sheet open={mobileOpen} onOpenChange={onMobileOpenChange}>
        <SheetContent
          side="left"
          className="nk-session-sidebar w-[min(22rem,88vw)] gap-0 p-0 md:hidden"
        >
          <SheetHeader className="border-b border-border/70 pr-12">
            <SheetTitle>会话</SheetTitle>
            <SheetDescription>{scopeLabel}</SheetDescription>
          </SheetHeader>
          <div className="border-b border-border/70 p-3">
            <Button
              type="button"
              className="w-full justify-start"
              onClick={createAndClose}
              disabled={disabled}
            >
              <Plus className="size-4" />
              新对话
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <SessionRows
              sessions={sessions}
              activeSessionId={activeSessionId}
              disabled={disabled}
              onSelect={selectAndClose}
              onDelete={onDelete}
            />
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
