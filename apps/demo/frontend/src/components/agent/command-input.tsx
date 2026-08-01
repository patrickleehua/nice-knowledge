"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowUp,
  ChevronDown,
  ChevronUp,
  Hash,
  ListPlus,
  Play,
  Square,
  X,
} from "lucide-react";
import { useMemo, type ReactNode, type Ref } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import type { ProjectListOut, ProjectOut } from "@/lib/utils";

export const ACTION_PRESETS = [
  {
    label: "新建旅行计划并解析需求",
    prompt: "根据以下客户询价新建旅行计划并解析需求：",
  },
  {
    label: "按完整计划推进",
    prompt:
      "为当前旅行计划制定并执行完整计划：解析需求→检索→行程→报价→文档，每步汇报进度。",
  },
  {
    label: "生成行程方案",
    prompt: "为当前旅行计划检索资料并生成多套行程方案。",
  },
  { label: "选定方案细化", prompt: "根据客户偏好选定方案并细化每日行程。" },
  {
    label: "生成拆分报价",
    prompt: "基于已确认行程生成拆分报价，并列出待人工确认价格。",
  },
  {
    label: "发起OTA比价",
    prompt: "对当前报价发起 OTA 实时比价，汇报与成本行的价差并给出回填建议。",
  },
  { label: "提交审价", prompt: "检查当前报价校验结果，没有阻塞就提交审价。" },
  { label: "生成文档", prompt: "为当前旅行计划生成可交付文档。" },
  {
    label: "打包导出",
    prompt: "把当前旅行计划已生成的文档打包成导出包并给出清单。",
  },
  {
    label: "旅行计划标记已发送",
    prompt: "把当前旅行计划状态推进为已发送（sent）。",
  },
  { label: "查询进行中任务", prompt: "查询当前旅行计划进行中的任务和异常。" },
] as const;

export interface QueuedTurn {
  id: string;
  content: string;
}

function QueuedTurns({
  turns,
  running,
  draftOccupied,
  onEdit,
  onMove,
  onRemove,
  onRun,
}: {
  turns: QueuedTurn[];
  running: boolean;
  draftOccupied: boolean;
  onEdit: (id: string) => void;
  onMove: (id: string, direction: "up" | "down") => void;
  onRemove: (id: string) => void;
  onRun: () => void;
}) {
  if (!turns.length) return null;

  return (
    <section
      aria-label="下一轮消息队列"
      className="mb-2 overflow-hidden rounded-2xl bg-white/95 shadow-[0_5px_18px_rgb(0_0_0/0.055)] ring-1 ring-black/[0.07] dark:bg-[#242422]/95 dark:ring-white/[0.08]"
    >
      <div className="flex min-h-9 items-center gap-2 border-b border-black/[0.055] px-3 text-[11px] text-muted-foreground dark:border-white/[0.06]">
        <ListPlus className="size-3.5" />
        <span className="font-medium text-foreground/80">下一轮</span>
        <span>{turns.length} 条</span>
        <span className="hidden truncate sm:inline">
          · {running ? "本轮结束后自动继续" : "队列已暂停"}
        </span>
        {!running && (
          <button
            type="button"
            onClick={onRun}
            className="ml-auto flex items-center gap-1 rounded-lg px-2 py-1 font-medium text-foreground hover:bg-black/[0.045] dark:hover:bg-white/[0.07]"
          >
            <Play className="size-3" />
            继续
          </button>
        )}
      </div>
      <ol className="max-h-36 overflow-y-auto p-1.5" aria-live="polite">
        {turns.map((turn, index) => (
          <li
            key={turn.id}
            className="group flex min-h-9 items-center gap-2 rounded-xl px-2 hover:bg-black/[0.035] dark:hover:bg-white/[0.055]"
          >
            <span className="w-4 shrink-0 text-center text-[10px] tabular-nums text-muted-foreground/70">
              {index + 1}
            </span>
            <button
              type="button"
              disabled={draftOccupied}
              onClick={() => onEdit(turn.id)}
              title={
                draftOccupied
                  ? "先清空当前草稿再编辑排队消息"
                  : "回到输入框编辑"
              }
              className="min-w-0 flex-1 truncate text-left text-xs text-foreground/80 disabled:cursor-not-allowed disabled:opacity-75"
            >
              {turn.content}
            </button>
            <div className="flex shrink-0 items-center opacity-60 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
              <button
                type="button"
                disabled={index === 0}
                onClick={() => onMove(turn.id, "up")}
                className="rounded-md p-1 hover:bg-black/[0.06] disabled:opacity-25 dark:hover:bg-white/[0.08]"
                aria-label={`提前第 ${index + 1} 条消息`}
                title="提前"
              >
                <ChevronUp className="size-3.5" />
              </button>
              <button
                type="button"
                disabled={index === turns.length - 1}
                onClick={() => onMove(turn.id, "down")}
                className="rounded-md p-1 hover:bg-black/[0.06] disabled:opacity-25 dark:hover:bg-white/[0.08]"
                aria-label={`延后第 ${index + 1} 条消息`}
                title="延后"
              >
                <ChevronDown className="size-3.5" />
              </button>
              <button
                type="button"
                onClick={() => onRemove(turn.id)}
                className="rounded-md p-1 hover:bg-black/[0.06] dark:hover:bg-white/[0.08]"
                aria-label={`移除第 ${index + 1} 条消息`}
                title="移除"
              >
                <X className="size-3.5" />
              </button>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function CommandInput({
  value,
  onChange,
  onProject,
  placeholder,
  above,
  toolbar,
  actions,
  queuedTurns,
  running,
  disabled = false,
  onSubmit,
  onStop,
  onQueuedEdit,
  onQueuedMove,
  onQueuedRemove,
  onQueuedRun,
  inputRef,
}: {
  value: string;
  onChange: (value: string) => void;
  onProject: (project: ProjectOut) => void;
  placeholder: string;
  above?: ReactNode;
  toolbar?: ReactNode;
  actions?: ReactNode;
  queuedTurns: QueuedTurn[];
  running: boolean;
  disabled?: boolean;
  onSubmit: () => void;
  onStop: () => void;
  onQueuedEdit: (id: string) => void;
  onQueuedMove: (id: string, direction: "up" | "down") => void;
  onQueuedRemove: (id: string) => void;
  onQueuedRun: () => void;
  inputRef?: Ref<HTMLTextAreaElement>;
}) {
  const projectTerm = value.startsWith("#") ? value.slice(1).trim() : "";
  const projects = useQuery({
    queryKey: ["agent-project-search", projectTerm],
    queryFn: () =>
      api.get<ProjectListOut>(
        `/projects?page_size=8&query=${encodeURIComponent(projectTerm)}`,
      ),
    enabled: !disabled && value.startsWith("#"),
  });
  const showPresets = !disabled && value.startsWith("/");
  const filteredPresets = useMemo(
    () => ACTION_PRESETS.filter((item) => item.label.includes(value.slice(1))),
    [value],
  );

  return (
    <div className="shrink-0 bg-gradient-to-t from-[#f7f7f5] via-[#f7f7f5] to-transparent px-3 pt-2 pb-3 dark:from-[#191918] dark:via-[#191918] sm:px-5 sm:pb-4">
      <div className="mx-auto w-full max-w-[50rem]">
        <QueuedTurns
          turns={queuedTurns}
          running={running}
          draftOccupied={!!value.trim()}
          onEdit={onQueuedEdit}
          onMove={onQueuedMove}
          onRemove={onQueuedRemove}
          onRun={onQueuedRun}
        />
        {above && <div className="mb-2">{above}</div>}
        {toolbar && <div className="mb-1 px-1">{toolbar}</div>}
        <div className="relative">
          {((!disabled && value.startsWith("#")) || showPresets) && (
            <div className="absolute right-0 bottom-full left-0 z-20 mb-2 max-h-64 overflow-auto rounded-2xl bg-popover p-1.5 shadow-xl ring-1 ring-foreground/10">
              {value.startsWith("#")
                ? projects.data?.items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => {
                        onProject(item);
                        onChange("");
                      }}
                      className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm hover:bg-accent"
                    >
                      <Hash className="size-3.5 text-primary" />
                      <span className="truncate">{item.title}</span>
                      <span className="ml-auto text-xs text-muted-foreground">
                        {item.status}
                      </span>
                    </button>
                  ))
                : filteredPresets.map((item) => (
                    <button
                      key={item.label}
                      type="button"
                      onClick={() => onChange(item.prompt)}
                      className="block w-full rounded-lg px-2.5 py-2 text-left text-sm hover:bg-accent"
                    >
                      {item.label}
                    </button>
                  ))}
            </div>
          )}
          <div className="rounded-[1.65rem] bg-white p-2 shadow-[0_8px_28px_rgb(0_0_0/0.075)] ring-1 ring-black/[0.075] transition-[box-shadow] focus-within:shadow-[0_10px_34px_rgb(0_0_0/0.11)] dark:bg-[#242422] dark:ring-white/[0.09]">
            <Textarea
              ref={inputRef}
              value={value}
              disabled={disabled}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  onSubmit();
                }
              }}
              placeholder={placeholder}
              rows={1}
              className="max-h-40 min-h-12 resize-none border-0 bg-transparent px-3 py-2.5 text-[15px] shadow-none focus-visible:border-transparent focus-visible:ring-0 dark:bg-transparent"
            />
            <div className="flex items-center gap-2 px-1.5 pt-0.5">
              {actions}
              <p className="min-w-0 flex-1 truncate px-1 text-[11px] text-muted-foreground/80">
                <span className="hidden sm:inline">
                  {running
                    ? `Enter 加入下一轮${queuedTurns.length ? ` · 已排队 ${queuedTurns.length} 条` : ""}`
                    : "/ 快捷操作 · # 选择旅行计划"}
                </span>
              </p>
              {running && (
                <Button
                  type="button"
                  size="icon-lg"
                  variant="outline"
                  onClick={onStop}
                  title="停止当前运行"
                  aria-label="停止当前运行"
                  className="rounded-full border-transparent bg-black/[0.055] shadow-none hover:bg-black/[0.09] dark:bg-white/[0.08] dark:hover:bg-white/[0.13]"
                >
                  <Square className="size-3.5 fill-current" />
                </Button>
              )}
              <Button
                type="button"
                size="icon-lg"
                variant="default"
                disabled={disabled || !value.trim()}
                onClick={onSubmit}
                title={running ? "加入下一轮" : "发送"}
                aria-label={running ? "加入下一轮" : "发送消息"}
                className="rounded-full"
              >
                {running ? (
                  <ListPlus className="size-4" />
                ) : (
                  <ArrowUp className="size-4" />
                )}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
