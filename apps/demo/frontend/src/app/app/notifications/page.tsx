"use client";

// 通知列表页(stage-d 05):全部/未读切换 + 分页,点击条目标记已读并跳转。
// 入口在顶栏铃铛「查看全部通知」;数据契约见 docs/api/stage-d/05-notifications.md。

import { useMutation } from "@tanstack/react-query";
import { Bell, CheckCheck, ChevronLeft, ChevronRight } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import {
  EmptyState,
  ErrorState,
  PageHeader,
  Spinner,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import {
  NOTIFICATION_KIND_LABELS,
  useInvalidateNotifications,
  useNotifications,
  useUnreadCount,
  type NotificationOut,
} from "@/lib/notifications";
import { errMsg, fmtDateTime } from "@/lib/utils";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

export default function NotificationsPage() {
  const router = useRouter();
  const invalidate = useInvalidateNotifications();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [page, setPage] = useState(1);

  const unreadQuery = useUnreadCount();
  const unread = unreadQuery.data?.count ?? 0;

  const listQuery = useNotifications({ page, pageSize: PAGE_SIZE, unreadOnly });
  const list = listQuery.data;
  const totalPages = list ? Math.max(1, Math.ceil(list.total / PAGE_SIZE)) : 1;

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
    if (n.link) router.push(n.link);
  }

  function switchTab(next: boolean) {
    setUnreadOnly(next);
    setPage(1);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title="通知"
        description="报价待审/退回、流水线失败、任务回收、资料过期等系统通知(仅本人可见)"
        actions={
          <Button
            size="sm"
            variant="outline"
            disabled={unread === 0 || readAllMutation.isPending}
            onClick={() => readAllMutation.mutate()}
          >
            {readAllMutation.isPending ? <Spinner size={3.5} /> : <CheckCheck />}
            全部已读
          </Button>
        }
      />

      <div className="flex gap-1 rounded-md border border-border p-0.5 w-fit">
        {[
          { value: false, label: "全部" },
          { value: true, label: unread > 0 ? `未读 ${unread}` : "未读" },
        ].map((tab) => (
          <button
            key={String(tab.value)}
            type="button"
            className={cn(
              "rounded px-3 py-1 text-sm",
              unreadOnly === tab.value
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={() => switchTab(tab.value)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {listQuery.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-2/3" />
        </div>
      ) : listQuery.error ? (
        <ErrorState error={listQuery.error} onRetry={() => listQuery.refetch()} />
      ) : !list || list.items.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={Bell}
              title={unreadOnly ? "没有未读通知" : "暂无通知"}
              description="报价状态流转、后台任务异常与资料过期都会在这里提醒你。"
            />
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="space-y-2">
            {list.items.map((n) => (
              <button
                key={n.id}
                type="button"
                className={cn(
                  "flex w-full flex-col gap-1 rounded-lg border border-border px-4 py-3 text-left transition-colors hover:bg-accent",
                  !n.read_at && "border-primary/30 bg-primary/5",
                )}
                onClick={() => openItem(n)}
              >
                <span className="flex w-full items-center gap-2">
                  {!n.read_at && (
                    <span className="size-1.5 shrink-0 rounded-full bg-primary" />
                  )}
                  <ToneBadge tone="muted">
                    {NOTIFICATION_KIND_LABELS[n.kind] ?? n.kind}
                  </ToneBadge>
                  <span className={cn("text-sm", !n.read_at && "font-medium")}>
                    {n.title}
                  </span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                    {fmtDateTime(n.created_at)}
                  </span>
                </span>
                {n.body && (
                  <span className="text-sm text-muted-foreground">{n.body}</span>
                )}
              </button>
            ))}
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
              >
                <ChevronLeft />
                上一页
              </Button>
              <span className="text-sm tabular-nums text-muted-foreground">
                {page} / {totalPages}
              </span>
              <Button
                size="sm"
                variant="ghost"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页
                <ChevronRight />
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
