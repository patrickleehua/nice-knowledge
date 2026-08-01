"use client";

// 定时任务页(iCron):列表 + 新建/编辑表单(带运行时间预览)+ 运行历史。
// 到点由后端起一次真实的 agent 运行,结果转成通知——页面本身不驱动任何执行。

import { useMutation } from "@tanstack/react-query";
import {
  CalendarClock,
  History,
  Pencil,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  FormField,
  PageHeader,
  Spinner,
  StatusBadge,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import {
  RUN_STATUS_META,
  SCHEDULE_KIND_LABELS,
  SKIP_REASON_LABELS,
  TASK_STATUS_META,
  TRIGGER_SOURCE_LABELS,
  useIcronRuns,
  useIcronTasks,
  useInvalidateIcronTasks,
  useSchedulePreview,
  type IcronScheduleKind,
  type IcronTask,
  type IcronTaskPayload,
} from "@/lib/icron";
import { errMsg, fmtDateTime } from "@/lib/utils";

const SCHEDULE_KINDS: IcronScheduleKind[] = [
  "cron",
  "interval",
  "once",
  "manual",
];

interface FormState {
  name: string;
  description: string;
  instruction: string;
  schedule_kind: IcronScheduleKind;
  cron_expr: string;
  interval_minutes: string;
  run_at: string;
  timezone: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  description: "",
  instruction: "",
  schedule_kind: "cron",
  cron_expr: "0 9 * * *",
  interval_minutes: "60",
  run_at: "",
  timezone: "Asia/Shanghai",
};

function toForm(task: IcronTask): FormState {
  return {
    name: task.name,
    description: task.description ?? "",
    instruction: task.instruction,
    schedule_kind: task.schedule_kind,
    cron_expr: task.cron_expr ?? "0 9 * * *",
    interval_minutes: task.interval_seconds
      ? String(Math.round(task.interval_seconds / 60))
      : "60",
    // datetime-local 只认无时区的本地串
    run_at: task.run_at ? task.run_at.slice(0, 16) : "",
    timezone: task.timezone,
  };
}

/** 表单 → 后端契约。间隔在界面上以分钟填写(秒级粒度对定时任务没有意义)。 */
function toPayload(form: FormState): IcronTaskPayload {
  const minutes = Number(form.interval_minutes);
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    instruction: form.instruction.trim(),
    schedule_kind: form.schedule_kind,
    cron_expr: form.schedule_kind === "cron" ? form.cron_expr.trim() : null,
    interval_seconds:
      form.schedule_kind === "interval" && Number.isFinite(minutes)
        ? Math.round(minutes * 60)
        : null,
    run_at:
      form.schedule_kind === "once" && form.run_at
        ? new Date(form.run_at).toISOString()
        : null,
    timezone: form.timezone.trim() || "Asia/Shanghai",
  };
}

function RunHistory({ task }: { task: IcronTask }) {
  const query = useIcronRuns(task.id);

  if (query.isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-2/3" />
      </div>
    );
  }
  if (query.error) {
    return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  }
  if (!query.data || query.data.length === 0) {
    return (
      <EmptyState
        icon={History}
        title="还没有运行记录"
        description="到点执行、手动运行与被跳过的触发都会记录在这里。"
      />
    );
  }
  return (
    <div className="max-h-[26rem] space-y-2 overflow-y-auto">
      {query.data.map((run) => (
        <div
          key={run.id}
          className="space-y-1 rounded-md border border-border px-3 py-2"
        >
          <div className="flex items-center gap-2">
            <StatusBadge meta={RUN_STATUS_META[run.status]} />
            <ToneBadge tone="muted">
              {TRIGGER_SOURCE_LABELS[run.trigger_source] ?? run.trigger_source}
            </ToneBadge>
            <span className="ml-auto text-xs text-muted-foreground">
              {fmtDateTime(run.started_at)}
            </span>
          </div>
          {run.skip_reason && (
            <p className="text-xs text-muted-foreground">
              跳过原因:
              {SKIP_REASON_LABELS[run.skip_reason] ?? run.skip_reason}
            </p>
          )}
          {run.error && (
            <p className="text-xs text-destructive">{run.error}</p>
          )}
          {run.agent_run_id ? (
            <p className="font-mono text-[11px] text-muted-foreground">
              agent run {run.agent_run_id.slice(0, 8)}
              {run.session_id && ` · 会话 ${run.session_id.slice(0, 8)}`}
            </p>
          ) : (
            // 没有 agent_run_id 就必须有跳过/失败原因,不允许"什么都查不到"
            <p className="text-[11px] text-muted-foreground">未起运行</p>
          )}
        </div>
      ))}
    </div>
  );
}

function TaskForm({
  task,
  onClose,
}: {
  task: IcronTask | null;
  onClose: () => void;
}) {
  const invalidate = useInvalidateIcronTasks();
  const [form, setForm] = useState<FormState>(
    task ? toForm(task) : EMPTY_FORM,
  );
  const preview = useSchedulePreview();

  const save = useMutation({
    mutationFn: () =>
      task
        ? api.patch<IcronTask>(`/icron/tasks/${task.id}`, toPayload(form))
        : api.post<IcronTask>("/icron/tasks", toPayload(form)),
    onSuccess: () => {
      toast.success(task ? "定时任务已更新" : "定时任务已创建");
      invalidate();
      onClose();
    },
    onError: (err) => toast.error(errMsg(err, "保存失败")),
  });

  function runPreview() {
    const payload = toPayload(form);
    preview.mutate(
      {
        schedule_kind: payload.schedule_kind,
        cron_expr: payload.cron_expr,
        interval_seconds: payload.interval_seconds,
        run_at: payload.run_at,
        timezone: payload.timezone,
      },
      { onError: (err) => toast.error(errMsg(err, "调度参数无法解析")) },
    );
  }

  const canSave = form.name.trim() && form.instruction.trim();

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSave) save.mutate();
      }}
    >
      <FormField label="任务名称" htmlFor="icron-name" required>
        <Input
          id="icron-name"
          value={form.name}
          maxLength={200}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </FormField>

      <FormField
        label="任务说明"
        htmlFor="icron-description"
        description="只给人看的备注,不会进入 Agent 的执行上下文"
      >
        <Input
          id="icron-description"
          value={form.description}
          maxLength={500}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
        />
      </FormField>

      <FormField
        label="任务指令"
        htmlFor="icron-instruction"
        required
        description="到点无人值守执行,没有人能回答追问:请写清目标、已知事实(对象 ID、日期、口径)、约束与期望产出"
      >
        <Textarea
          id="icron-instruction"
          rows={6}
          value={form.instruction}
          onChange={(e) => setForm({ ...form, instruction: e.target.value })}
        />
      </FormField>

      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="调度方式" htmlFor="icron-kind">
          <Select
            value={form.schedule_kind}
            onValueChange={(value) =>
              setForm({ ...form, schedule_kind: value as IcronScheduleKind })
            }
          >
            <SelectTrigger id="icron-kind" className="h-9 w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCHEDULE_KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {SCHEDULE_KIND_LABELS[kind]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </FormField>

        <FormField
          label="时区"
          htmlFor="icron-timezone"
          description="cron 表达式按此时区解释"
        >
          <Input
            id="icron-timezone"
            value={form.timezone}
            onChange={(e) => setForm({ ...form, timezone: e.target.value })}
          />
        </FormField>

        {form.schedule_kind === "cron" && (
          <FormField
            label="cron 表达式"
            htmlFor="icron-cron"
            description="五段式:分 时 日 月 周(如 0 9 * * * = 每天 9 点)"
          >
            <Input
              id="icron-cron"
              className="font-mono"
              value={form.cron_expr}
              onChange={(e) => setForm({ ...form, cron_expr: e.target.value })}
            />
          </FormField>
        )}

        {form.schedule_kind === "interval" && (
          <FormField
            label="间隔(分钟)"
            htmlFor="icron-interval"
            description="每次触发都是一次完整的 Agent 运行,最小 1 分钟"
          >
            <Input
              id="icron-interval"
              type="number"
              min={1}
              value={form.interval_minutes}
              onChange={(e) =>
                setForm({ ...form, interval_minutes: e.target.value })
              }
            />
          </FormField>
        )}

        {form.schedule_kind === "once" && (
          <FormField
            label="执行时间"
            htmlFor="icron-run-at"
            description="必须晚于当前时间;跑完自动归档"
          >
            <Input
              id="icron-run-at"
              type="datetime-local"
              value={form.run_at}
              onChange={(e) => setForm({ ...form, run_at: e.target.value })}
            />
          </FormField>
        )}
      </div>

      <div className="rounded-md border border-border bg-muted/40 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">运行时间预览</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={preview.isPending}
            onClick={runPreview}
          >
            {preview.isPending ? <Spinner size={3.5} /> : <CalendarClock />}
            预览未来 5 次
          </Button>
        </div>
        {preview.data ? (
          preview.data.upcoming.length === 0 ? (
            <p className="mt-2 text-xs text-muted-foreground">
              {preview.data.schedule_summary}——不会自动触发,只能手动运行。
            </p>
          ) : (
            <ul className="mt-2 space-y-0.5">
              {preview.data.upcoming.map((moment) => (
                <li
                  key={moment}
                  className="text-xs tabular-nums text-muted-foreground"
                >
                  {fmtDateTime(moment)}
                </li>
              ))}
            </ul>
          )
        ) : (
          <p className="mt-2 text-xs text-muted-foreground">
            保存前先预览,确认节奏和你想的一致。
          </p>
        )}
      </div>

      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose}>
          取消
        </Button>
        <Button type="submit" disabled={!canSave || save.isPending}>
          {save.isPending && <Spinner size={3.5} />}
          {task ? "保存修改" : "创建任务"}
        </Button>
      </DialogFooter>
    </form>
  );
}

function TaskRow({
  task,
  onEdit,
  onHistory,
}: {
  task: IcronTask;
  onEdit: () => void;
  onHistory: () => void;
}) {
  const invalidate = useInvalidateIcronTasks();

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post<IcronTask>(`/icron/tasks/${task.id}/status`, {
        status: next ? "active" : "paused",
      }),
    onSuccess: invalidate,
    onError: (err) => toast.error(errMsg(err, "切换状态失败")),
  });

  const runNow = useMutation({
    mutationFn: () => api.post<{ accepted: boolean }>(`/icron/tasks/${task.id}/run`),
    onSuccess: () => {
      toast.success("已在后台开始执行,结果会以通知发出");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "运行失败")),
  });

  const remove = useMutation({
    mutationFn: () => api.delete(`/icron/tasks/${task.id}`),
    onSuccess: () => {
      toast.success(`已删除定时任务「${task.name}」`);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "删除失败")),
  });

  const archived = task.status === "archived";

  return (
    <div className="flex flex-col gap-2 border-b border-border px-4 py-3 last:border-b-0 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{task.name}</span>
          <StatusBadge meta={TASK_STATUS_META[task.status]} />
        </div>
        <p className="truncate text-xs text-muted-foreground">
          {task.schedule_summary}
          {task.next_run_at && ` · 下次 ${fmtDateTime(task.next_run_at)}`}
        </p>
        {task.last_run ? (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            上次
            <StatusBadge meta={RUN_STATUS_META[task.last_run.status]} />
            {fmtDateTime(task.last_run.started_at)}
            {task.last_run.skip_reason &&
              ` · ${SKIP_REASON_LABELS[task.last_run.skip_reason] ?? task.last_run.skip_reason}`}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">还没有运行过</p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Switch
          checked={task.status === "active"}
          disabled={archived || toggle.isPending}
          onCheckedChange={(next) => toggle.mutate(next)}
          aria-label={`启用或暂停 ${task.name}`}
        />
        <Button
          size="sm"
          variant="ghost"
          disabled={archived || runNow.isPending}
          onClick={() => runNow.mutate()}
          aria-label={`立即运行 ${task.name}`}
        >
          {runNow.isPending ? <Spinner size={3.5} /> : <Play />}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={onHistory}
          aria-label={`${task.name} 的运行历史`}
        >
          <History />
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={onEdit}
          aria-label={`编辑 ${task.name}`}
        >
          <Pencil />
        </Button>
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="ghost" aria-label={`删除 ${task.name}`}>
              <Trash2 className="text-destructive" />
            </Button>
          }
          title={`删除定时任务「${task.name}」?`}
          description="任务及其全部运行历史会一并删除,已产生的对话与业务数据不受影响。此操作不可撤销。"
          confirmLabel="删除"
          destructive
          onConfirm={() => remove.mutateAsync()}
        />
      </div>
    </div>
  );
}

export default function IcronPage() {
  const query = useIcronTasks();
  // undefined = 表单未打开;null = 新建;IcronTask = 编辑
  const [editing, setEditing] = useState<IcronTask | null | undefined>();
  const [historyTask, setHistoryTask] = useState<IcronTask | null>(null);

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <PageHeader
        title="定时任务"
        description="到点自动起一次 Agent 运行执行任务指令,结果转成通知"
        actions={
          <Button size="sm" onClick={() => setEditing(null)}>
            <Plus />
            新建定时任务
          </Button>
        }
      />

      {query.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : query.error ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : !query.data || query.data.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={CalendarClock}
              title="还没有定时任务"
              description="每日出团提醒、定期比价、到期跟进这类重复工作可以交给定时任务;也可以直接让 AI 助手帮你建。"
            />
          </CardContent>
        </Card>
      ) : (
        <Card className="py-0">
          <CardContent className="px-0">
            {query.data.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                onEdit={() => setEditing(task)}
                onHistory={() => setHistoryTask(task)}
              />
            ))}
          </CardContent>
        </Card>
      )}

      <Dialog
        open={editing !== undefined}
        onOpenChange={(next) => !next && setEditing(undefined)}
      >
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {editing ? "编辑定时任务" : "新建定时任务"}
            </DialogTitle>
          </DialogHeader>
          {editing !== undefined && (
            <TaskForm
              key={editing?.id ?? "new"}
              task={editing}
              onClose={() => setEditing(undefined)}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={historyTask !== null}
        onOpenChange={(next) => !next && setHistoryTask(null)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              运行历史{historyTask ? ` · ${historyTask.name}` : ""}
            </DialogTitle>
          </DialogHeader>
          {historyTask && <RunHistory task={historyTask} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
