"use client";

/**
 * 会话权限的"延迟应用"状态机(从 app/chat/page.tsx 拆出)。
 *
 * 为什么不能直接 PUT:权限更新带 `expected_revision` + `expected_policy_version`
 * 的乐观锁。run 进行中或有待批操作时提交,几乎必然撞版本冲突;更糟的是它会在
 * 半途改掉本轮的判定基准。所以这里的规则是:
 *   - 空闲 → 立即以最新 state rebase 后提交;
 *   - 忙碌 → 记下**意图**(不带版本号),等这一轮跑完再 rebase 提交。
 *
 * 意图刻意只存一份:用户连点两次,最后一次才是他真正想要的。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import {
  deferPermissionUpdate,
  rebasePermissionUpdate,
  type DeferredSessionPermissionUpdate,
  type SessionPermissionState,
  type SessionPermissionUpdate,
} from "@/lib/agent-permissions";
import { errMsg } from "@/lib/utils";

interface DeferredPermissionChange {
  sessionId: string;
  update: DeferredSessionPermissionUpdate;
}

export interface DeferredPermissionController {
  /** 当前会话待应用的意图(供 UI 显示"稍后生效") */
  deferredFor: (sessionId: string | null) => DeferredSessionPermissionUpdate | null;
  saving: boolean;
  /** 用户提交一次权限变更:忙则记意图,闲则立即提交 */
  request: (
    sessionId: string,
    update: SessionPermissionUpdate,
    busy: boolean,
    fallbackState?: SessionPermissionState,
  ) => Promise<void>;
  /** run 结束后调用:条件满足就把意图落地。返回是否真的提交了 */
  flush: (sessionId: string) => Promise<boolean>;
  /** 切会话时丢弃上一会话的意图 */
  reset: () => void;
}

export function useDeferredPermission(): DeferredPermissionController {
  const queryClient = useQueryClient();
  const [deferred, setDeferred] = useState<DeferredPermissionChange | null>(null);
  const deferredRef = useRef<DeferredPermissionChange | null>(null);
  const applyingRef = useRef(false);

  function replace(next: DeferredPermissionChange | null) {
    deferredRef.current = next;
    setDeferred(next);
  }

  const updatePermissions = useMutation({
    mutationFn: ({
      sessionId,
      update,
    }: {
      sessionId: string;
      update: SessionPermissionUpdate;
    }) =>
      api.put<SessionPermissionState>(
        `/chat/sessions/${sessionId}/permissions`,
        update,
      ),
    onSuccess: (state) => {
      queryClient.setQueryData(["chat-permissions", state.session_id], state);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
    onError: (error) =>
      toast.error(errMsg(error, "权限设置未更新，请刷新后重试")),
  });

  return {
    deferredFor: (sessionId) =>
      deferred && deferred.sessionId === sessionId ? deferred.update : null,
    saving: updatePermissions.isPending,

    async request(sessionId, update, busy, fallbackState) {
      const state =
        queryClient.getQueryData<SessionPermissionState>([
          "chat-permissions",
          sessionId,
        ]) ?? fallbackState;
      if (!state) throw new Error("权限状态尚未加载");
      const intent = deferPermissionUpdate(update);
      if (busy || state.active_run) {
        replace({ sessionId, update: intent });
        return;
      }
      await updatePermissions.mutateAsync({
        sessionId,
        update: rebasePermissionUpdate(state, intent),
      });
    },

    async flush(sessionId) {
      const pendingChange = deferredRef.current;
      if (
        !pendingChange ||
        pendingChange.sessionId !== sessionId ||
        applyingRef.current
      )
        return false;
      const state = queryClient.getQueryData<SessionPermissionState>([
        "chat-permissions",
        sessionId,
      ]);
      if (!state || state.active_run || state.pending_decision) return false;

      applyingRef.current = true;
      try {
        await updatePermissions.mutateAsync({
          sessionId,
          update: rebasePermissionUpdate(state, pendingChange.update),
        });
        return true;
      } finally {
        applyingRef.current = false;
        if (deferredRef.current === pendingChange) replace(null);
      }
    },

    reset: () => replace(null),
  };
}
