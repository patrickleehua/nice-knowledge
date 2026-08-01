// 通知(stage-d 05):DTO 与共享 query hook。全部本人视角,读写他人通知一律 404。

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface NotificationOut {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  /** 前端路由(如 /app/projects/{id}?tab=quote),点击通知直接跳转;可能为空 */
  link: string | null;
  read_at: string | null;
  created_at: string | null;
}

export interface NotificationList {
  items: NotificationOut[];
  total: number;
  page: number;
  page_size: number;
}

export const NOTIFICATION_KIND_LABELS: Record<string, string> = {
  "quote.pending_review": "待审价",
  "quote.rejected": "报价退回",
  "pipeline.failed": "任务失败",
  "task.recovered": "任务回收",
  "kb.doc_expired": "资料过期",
  // 定时任务:kind 同时是通知去重键的一部分(icron.{run 状态},见后端 icron.py)
  "icron.success": "定时任务完成",
  "icron.failed": "定时任务失败",
  "icron.skipped": "定时任务跳过",
};

/** 铃铛徽标未读数,45s 轮询(文档建议 30-60s,与 run 轮询节奏错开) */
export function useUnreadCount() {
  return useQuery({
    queryKey: ["notifications-unread"],
    queryFn: () => api.get<{ count: number }>("/notifications/unread-count"),
    refetchInterval: 45_000,
  });
}

export function useNotifications(opts: {
  page?: number;
  pageSize?: number;
  unreadOnly?: boolean;
  enabled?: boolean;
}) {
  const { page = 1, pageSize = 20, unreadOnly = false, enabled = true } = opts;
  return useQuery({
    queryKey: ["notifications", page, pageSize, unreadOnly],
    queryFn: () =>
      api.get<NotificationList>(
        `/notifications?page=${page}&page_size=${pageSize}&unread_only=${unreadOnly}`,
      ),
    enabled,
  });
}

export function useInvalidateNotifications() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
    queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
  };
}
