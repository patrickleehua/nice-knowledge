"use client";

// 运行中输入队列的卡片区(从 app/chat/page.tsx 拆出)。
//
// run 进行中发消息不会 409,而是被服务端以 202 入队;这些卡片就是"排队中"的
// 那几条。状态机与缓存写入在 use-queued-inputs.ts,这里只负责渲染与交互。

import { CircleSlash, Clock3, Pencil, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { QueuedInputOut } from "@/lib/chat";
import { cn } from "@/lib/utils";

import type { QueuedInputsController } from "./use-queued-inputs";

export function QueuedInputList({
  items,
  queue,
}: {
  items: QueuedInputOut[];
  queue: QueuedInputsController;
}) {
  if (!items.length) return null;
  const editing = queue.editing;

  return (
    <div className="mx-auto w-full max-w-[50rem] px-4 pb-6 sm:px-6">
      <div className="space-y-2" aria-label="排队中的消息">
        {items.map((item) => (
          <div
            key={item.id}
            className={cn(
              "rounded-xl border px-3.5 py-2.5 text-sm",
              item.status === "skipped"
                ? "border-dashed border-border/70 opacity-70"
                : "border-border/80 bg-white dark:bg-[#232321]",
            )}
          >
            {editing?.id === item.id ? (
              <div className="space-y-2">
                <Textarea
                  value={editing.content}
                  onChange={(event) =>
                    queue.setEditing({
                      id: item.id,
                      content: event.target.value,
                    })
                  }
                  rows={2}
                  autoFocus
                />
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={() => queue.setEditing(null)}
                  >
                    取消
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    disabled={!editing.content.trim() || queue.savePending}
                    onClick={() =>
                      queue.save(item.id, editing.content.trim())
                    }
                  >
                    保存
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex items-start gap-2.5">
                <span className="mt-1 shrink-0 text-muted-foreground">
                  {item.status === "skipped" ? (
                    <CircleSlash className="size-3.5" />
                  ) : (
                    <Clock3 className="size-3.5" />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="leading-6 break-words whitespace-pre-wrap">
                    {item.content}
                  </p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {item.status === "skipped"
                      ? "已跳过 · 本轮已终止，该消息未发送"
                      : "排队中 · Agent 空闲时自动发送"}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {item.status === "queued" && (
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      aria-label="编辑排队消息"
                      title="编辑"
                      onClick={() =>
                        queue.setEditing({
                          id: item.id,
                          content: item.content,
                        })
                      }
                    >
                      <Pencil className="size-3.5" />
                    </Button>
                  )}
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="ghost"
                    aria-label="删除排队消息"
                    title="删除"
                    onClick={() => queue.remove(item)}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
