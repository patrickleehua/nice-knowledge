"use client";

import {
  ArrowUp,
  ChevronDown,
  ChevronUp,
  ListPlus,
  Play,
  Square,
  X,
} from "lucide-react";
import { useMemo, type ReactNode, type Ref } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/**
 * 输入框里敲 `/` 弹出的快捷指令。SDK 不知道宿主的业务动作,默认只给
 * **通用能力**示例;宿主换成自己的一组即可(`<CommandInput presets={…} />`)。
 */
export const ACTION_PRESETS: readonly { label: string; prompt: string }[] = [
  { label: "检索知识库", prompt: "帮我在知识库里查一下：" },
  {
    label: "联网调研并标注来源",
    prompt: "帮我联网查一下,逐条标注来源与发布时间：",
  },
  {
    label: "读取网页并总结",
    prompt: "读取下面这些网页并总结要点(保留原文引用)：",
  },
  { label: "记住一条长期偏好", prompt: "请记住:" },
  { label: "设定会话目标", prompt: "本次会话的目标是:" },
  {
    label: "安排一个定时任务",
    prompt: "每周一早上 9 点帮我做一次,并把结果通知我：",
  },
  { label: "生成一张配图", prompt: "帮我生成一张图片：" },
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
  presets = ACTION_PRESETS,
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
  /** `/` 快捷指令表(宿主可整组替换) */
  presets?: readonly { label: string; prompt: string }[];
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
  const showPresets = !disabled && value.startsWith("/");
  const filteredPresets = useMemo(
    () => presets.filter((item) => item.label.includes(value.slice(1))),
    [presets, value],
  );

  return (
    <div className="nk-liquid-composer shrink-0 px-3 pt-2 pb-3 sm:px-5 sm:pb-4">
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
          {showPresets && (
            <div className="absolute right-0 bottom-full left-0 z-20 mb-2 max-h-64 overflow-auto rounded-2xl bg-popover p-1.5 shadow-xl ring-1 ring-foreground/10">
              {filteredPresets.map((item) => (
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
                    : "/ 快捷操作"}
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
