// iCron 自主定时任务(后端 api/v1/icron.py):DTO、共享 hook 与展示元数据。
// 全部本人视角——后端所有端点都锚定 ctx.user_id,别人的任务一律 404。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { StatusMeta } from "@/lib/utils";

export type IcronScheduleKind = "manual" | "once" | "interval" | "cron";
export type IcronTaskStatus = "active" | "paused" | "archived";
export type IcronRunStatus = "running" | "success" | "failed" | "skipped";

export interface IcronTaskRun {
  id: string;
  task_id: string;
  trigger_source: "schedule" | "manual" | "test" | "agent";
  status: IcronRunStatus;
  /** 底层 agent run 标识:有它才说明真的起了一次运行 */
  agent_run_id: string | null;
  session_id: string | null;
  skip_reason: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface IcronTask {
  id: string;
  name: string;
  description: string | null;
  instruction: string;
  schedule_kind: IcronScheduleKind;
  /** 后端算好的一行中文调度说明,前端不重复实现一套解析 */
  schedule_summary: string;
  cron_expr: string | null;
  interval_seconds: number | null;
  run_at: string | null;
  timezone: string;
  target_session_id: string | null;
  project_id: string | null;
  status: IcronTaskStatus;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  last_run: IcronTaskRun | null;
}

export interface IcronSchedulePayload {
  schedule_kind: IcronScheduleKind;
  cron_expr?: string | null;
  interval_seconds?: number | null;
  run_at?: string | null;
  timezone?: string;
}

export interface IcronTaskPayload extends IcronSchedulePayload {
  name: string;
  description?: string | null;
  instruction: string;
  target_session_id?: string | null;
  project_id?: string | null;
}

export interface IcronPreview {
  schedule_summary: string;
  timezone: string;
  /** 未来 5 次运行时刻(带任务时区偏移的 ISO 串) */
  upcoming: string[];
}

export const SCHEDULE_KIND_LABELS: Record<IcronScheduleKind, string> = {
  manual: "仅手动",
  once: "一次性",
  interval: "固定间隔",
  cron: "cron 表达式",
};

export const TASK_STATUS_META: Record<IcronTaskStatus, StatusMeta> = {
  active: { label: "已启用", tone: "success" },
  paused: { label: "已暂停", tone: "muted" },
  archived: { label: "已归档", tone: "muted" },
};

export const RUN_STATUS_META: Record<IcronRunStatus, StatusMeta> = {
  running: { label: "运行中", tone: "primary" },
  success: { label: "成功", tone: "success" },
  failed: { label: "失败", tone: "destructive" },
  skipped: { label: "已跳过", tone: "warning" },
};

export const TRIGGER_SOURCE_LABELS: Record<string, string> = {
  schedule: "定时触发",
  manual: "手动运行",
  test: "试运行",
  agent: "Agent 触发",
};

/** 跳过原因是给人看的,后端存的是稳定英文码,展示层在此翻译 */
export const SKIP_REASON_LABELS: Record<string, string> = {
  session_busy: "目标会话有正在进行的运行",
  pending_confirmation: "目标会话有待确认的操作",
  session_missing: "目标会话已不存在",
  owner_inactive: "任务创建者已停用或不在本组织",
  task_archived: "任务已归档",
};

const TASKS_KEY = ["icron-tasks"];

export function useIcronTasks() {
  return useQuery({
    queryKey: TASKS_KEY,
    queryFn: () => api.get<IcronTask[]>("/icron/tasks"),
    // 定时任务的状态由后台推进,页面开着时要能看到"下次运行"往前走
    refetchInterval: 30_000,
  });
}

export function useIcronRuns(taskId: string | null) {
  return useQuery({
    queryKey: ["icron-runs", taskId],
    queryFn: () => api.get<IcronTaskRun[]>(`/icron/tasks/${taskId}/runs`),
    enabled: !!taskId,
    refetchInterval: 15_000,
  });
}

export function useInvalidateIcronTasks() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: TASKS_KEY });
    queryClient.invalidateQueries({ queryKey: ["icron-runs"] });
  };
}

/**
 * 保存前预览未来 5 次运行时间。用 mutation 而不是 query:预览是显式动作
 * (改一个字符就打一次后端毫无意义),而且非法参数要以 422 原文提示用户。
 */
export function useSchedulePreview() {
  return useMutation({
    mutationFn: (payload: IcronSchedulePayload) =>
      api.post<IcronPreview>("/icron/schedule/preview", payload),
  });
}
