"use client";

/**
 * 运行中输入队列的客户端状态机(从 app/chat/page.tsx 拆出)。
 *
 * 三个写入源必须收敛到同一份缓存,否则排队卡片会闪烁/重影:
 *   1. `POST /messages` 在 run 进行中返回的 202 回执(乐观入列);
 *   2. SSE 的 `input.*` 事件(服务端权威:consumed / skipped / deleted / queued);
 *   3. 用户的行内编辑与删除(PATCH / DELETE)。
 *
 * 所以本模块是 `["chat-queued-inputs", sessionId]` 这份缓存的**唯一写入口**。
 */

import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { ApiError } from "@/lib/api";
import {
  deleteQueuedInput,
  updateQueuedInput,
  type AgentEvent,
  type QueuedInputListOut,
  type QueuedInputOut,
} from "@/lib/chat";
import { errMsg } from "@/lib/utils";

export type QueueEvent = Extract<AgentEvent, { type: `input.${string}` }>;

/**
 * 队列缓存的底层写函数。放在组件外:React Compiler 无法为组件内的高阶更新
 * 函数保留手动 memo(react-hooks/preserve-manual-memoization),模块级纯函数
 * 不参与编译。
 */
export function setQueuedCache(
  queryClient: QueryClient,
  sid: string,
  updater: (items: QueuedInputOut[]) => QueuedInputOut[],
) {
  queryClient.setQueryData<QueuedInputListOut>(
    ["chat-queued-inputs", sid],
    (data) => ({ items: updater(data?.items ?? []) }),
  );
}

export interface QueuedInputsController {
  /** 行内编辑态(null = 没有在编辑) */
  editing: { id: string; content: string } | null;
  setEditing: (draft: { id: string; content: string } | null) => void;
  savePending: boolean;
  save: (inputId: string, content: string) => void;
  remove: (item: QueuedInputOut) => void;
  /** SSE `input.*` 事件入口;返回 true = 该事件封存了当前直播段 */
  receiveQueueEvent: (sid: string, event: QueueEvent) => boolean;
}

export function useQueuedInputs(sessionId: string | null): QueuedInputsController {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<{ id: string; content: string } | null>(
    null,
  );

  const invalidate = (sid: string | null) => {
    if (sid)
      queryClient.invalidateQueries({ queryKey: ["chat-queued-inputs", sid] });
  };

  const patchMutation = useMutation({
    mutationFn: ({ inputId, content }: { inputId: string; content: string }) =>
      updateQueuedInput(sessionId as string, inputId, content),
    onSuccess: (item) => {
      if (sessionId)
        setQueuedCache(queryClient, sessionId, (items) =>
          items.map((it) => (it.id === item.id ? item : it)),
        );
      setEditing(null);
    },
    onError: (error) => {
      // 409 = 消息已被消费/跳过,不再是可编辑态,以服务端为准刷新
      if (error instanceof ApiError && error.status === 409) {
        toast.message("该消息已被处理");
        invalidate(sessionId);
        setEditing(null);
      } else toast.error(errMsg(error, "修改排队消息失败"));
    },
  });

  const removeMutation = useMutation({
    mutationFn: (inputId: string) =>
      deleteQueuedInput(sessionId as string, inputId),
    onSuccess: (_data, inputId) => {
      if (sessionId)
        setQueuedCache(queryClient, sessionId, (items) =>
          items.filter((it) => it.id !== inputId),
        );
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast.message("该消息已被处理");
        invalidate(sessionId);
      } else toast.error(errMsg(error, "删除排队消息失败"));
    },
  });

  function receiveQueueEvent(sid: string, event: QueueEvent): boolean {
    if (event.type === "input.consumed") {
      setQueuedCache(queryClient, sid, (items) =>
        items.filter((item) => item.id !== event.queued_input_id),
      );
      // 调用方据此封存当前直播段(排队卡片转为正式 user 气泡)
      return true;
    }
    if (event.type === "input.skipped") {
      setQueuedCache(queryClient, sid, (items) =>
        items.map((item) =>
          item.id === event.queued_input_id
            ? { ...item, status: "skipped" as const }
            : item,
        ),
      );
      return false;
    }
    if (event.type === "input.deleted") {
      setQueuedCache(queryClient, sid, (items) =>
        items.filter((item) => item.id !== event.queued_input_id),
      );
      return false;
    }
    if (event.type === "input.queued" || event.type === "input.updated") {
      // 常态由 202 回执 / 编辑响应维护;这里兜底断线重放时的状态同步
      setQueuedCache(queryClient, sid, (items) => {
        const next = items.filter((item) => item.id !== event.queued_input_id);
        if (event.status === "queued")
          next.push({
            id: event.queued_input_id,
            run_id: event.run_id,
            position: event.position,
            status: "queued",
            content: event.content,
            consumed_message_id: null,
            created_at: null,
            updated_at: null,
          });
        return next.sort((left, right) => left.position - right.position);
      });
    }
    // input.consuming:卡片保留,待 input.consumed 时转正式 user 气泡
    return false;
  }

  return {
    editing,
    setEditing,
    savePending: patchMutation.isPending,
    save: (inputId, content) => patchMutation.mutate({ inputId, content }),
    remove: (item) => {
      if (item.status === "queued") removeMutation.mutate(item.id);
      else if (sessionId)
        // 已跳过项只是本地提示,直接从卡片区移除
        setQueuedCache(queryClient, sessionId, (items) =>
          items.filter((it) => it.id !== item.id),
        );
    },
    receiveQueueEvent,
  };
}
