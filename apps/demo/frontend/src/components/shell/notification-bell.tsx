"use client";

// 通知铃铛(stage-d 05):未读徽标 45s 轮询 + Popover 最近通知,点击条目标记已读并跳转。
// 挂在 AppShell 顶栏,三区域(/admin /org /app)共用;完整列表在 /app/notifications。

import { Popover } from "@base-ui/react/popover";
import { useMutation } from "@tanstack/react-query";
import { Bell, CheckCheck, Inbox } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Spinner, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import {
  notificationKindLabel,
  useInvalidateNotifications,
  useNotifications,
  useUnreadCount,
  type NotificationOut,
} from "@/lib/notifications";
import { cn, errMsg, fmtDateTime } from "@/lib/utils";
export function NotificationBell() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const invalidate = useInvalidateNotifications();

  const unreadQuery = useUnreadCount();
  const unread = unreadQuery.data?.count ?? 0;

  // 面板只在打开时拉取最近 10 条
  const listQuery = useNotifications({ pageSize: 10, enabled: open });
  const items = listQuery.data?.items ?? [];

  const readMutation = useMutation({
    mutationFn: (id: string) =>
      api.post<NotificationOut>(`/notifications/${id}/read`),
    onSuccess: invalidate,
  });

  const readAllMutation = useMutation({
    mutationFn: () => api.post<{ marked: number }>("/notifications/read-all"),
    onSuccess: (res) => {
      toast.success(`已将 ${res.marked} 条通知标记为已读`);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "标记已读失败")),
  });

  function openItem(n: NotificationOut) {
    if (!n.read_at) readMutation.mutate(n.id);
    setOpen(false);
    if (n.link) router.push(n.link);
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger
        render={
          <Button variant="ghost" size="icon" aria-label="通知" className="relative" />
        }
      >
        <Bell className="size-4" />
        {unread > 0 && (
          <span className="absolute top-1 right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-semibold leading-none text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner side="bottom" align="end" sideOffset={8} className="z-50">
          <Popover.Popup className="w-88 rounded-lg border border-border bg-popover text-popover-foreground shadow-md outline-none">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <span className="text-sm font-medium">通知</span>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-xs"
                disabled={unread === 0 || readAllMutation.isPending}
                onClick={() => readAllMutation.mutate()}
              >
                {readAllMutation.isPending ? (
                  <Spinner size={3.5} />
                ) : (
                  <CheckCheck className="size-3.5" />
                )}
                全部已读
              </Button>
            </div>

            <div className="max-h-96 overflow-y-auto">
              {listQuery.isLoading && (
                <div className="flex justify-center py-6">
                  <Spinner />
                </div>
              )}
              {!listQuery.isLoading && items.length === 0 && (
                <div className="flex flex-col items-center gap-1.5 py-8 text-muted-foreground">
                  <Inbox className="size-6" />
                  <span className="text-sm">暂无通知</span>
                </div>
              )}
              {items.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  className={cn(
                    "flex w-full flex-col gap-0.5 border-b border-border px-3 py-2 text-left transition-colors last:border-0 hover:bg-accent",
                    !n.read_at && "bg-primary/5",
                  )}
                  onClick={() => openItem(n)}
                >
                  <span className="flex w-full items-center gap-1.5">
                    {!n.read_at && (
                      <span className="size-1.5 shrink-0 rounded-full bg-primary" />
                    )}
                    <ToneBadge tone="muted">
                      {notificationKindLabel(n.kind)}
                    </ToneBadge>
                    <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                      {fmtDateTime(n.created_at)}
                    </span>
                  </span>
                  <span
                    className={cn("text-sm", !n.read_at && "font-medium")}
                  >
                    {n.title}
                  </span>
                  {n.body && (
                    <span className="line-clamp-2 text-xs text-muted-foreground">
                      {n.body}
                    </span>
                  )}
                </button>
              ))}
            </div>

            <div className="border-t border-border p-1.5">
              <Link
                href="/app/notifications"
                className="block rounded-md px-3 py-1.5 text-center text-sm text-primary hover:bg-accent"
                onClick={() => setOpen(false)}
              >
                查看全部通知
              </Link>
            </div>
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
