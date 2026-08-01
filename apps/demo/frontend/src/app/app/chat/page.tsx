"use client";

// Agent 全交互工作台。
//
// SDK 化改造(MIGRATION-PLAN §5.8):
// - 删掉"旅行计划模式 / 通用模式"双模开关与 ProjectPicker。会话与宿主业务对象
//   的联系统一走 `scope_type` + `scope_id`(chat_sessions 的两列,无 FK),从
//   URL query 读入;两者必须成对,只给一个后端直接 422。
// - `stage.update` 事件已从后端删除(不再按工具名前缀推导业务阶段)。
// - 工具结果渲染走可注册的渲染器表(components/agent/result-renderers.tsx)。
// - 输入队列缓存与权限延迟应用拆成两个 hook(use-queued-inputs /
//   use-deferred-permission),这里只剩 SSE 状态机与页面装配。

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { Loader2, PanelLeftOpen } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AgentConversation } from "@/components/agent/agent-conversation";
import { CommandInput } from "@/components/agent/command-input";
import { ConfirmCard } from "@/components/agent/confirm-card";
import { GoalBanner } from "@/components/agent/goal-banner";
import { PlanChecklist } from "@/components/agent/plan-checklist";
import { PermissionControl } from "@/components/agent/permission-control";
import { QueuedInputList } from "@/components/agent/queued-input-list";
import { hasToolResultRenderer } from "@/components/agent/result-renderers";
import { RuntimeControls } from "@/components/agent/runtime-controls";
import { SessionSidebar } from "@/components/agent/session-sidebar";
import { useDeferredPermission } from "@/components/agent/use-deferred-permission";
import {
  setQueuedCache,
  useQueuedInputs,
  type QueueEvent,
} from "@/components/agent/use-queued-inputs";
import { UserInputCard } from "@/components/agent/user-input-card";
import { ShellHeader } from "@/components/shell/app-shell";
import { Breadcrumbs } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import {
  historyToItems,
  isSemanticAgentEvent,
  type ConversationItem,
} from "@/lib/agent-events";
import { useMounted } from "@/lib/auth";
import { preserveSelectedSession } from "@/lib/chat-session-state";
import type {
  SessionPermissionState,
  SessionPermissionUpdate,
} from "@/lib/agent-permissions";
import { confirmationKey } from "@/lib/approval-decisions";
import {
  type AgentContinuationAction,
  cancelSessionGoal,
  completeSessionGoal,
  type ConfirmationAction,
  getSessionGoal,
  isConfirmationAction,
  listQueuedInputs,
  sendChatMessage,
  setSessionGoal,
  type AgentCardOut,
  type AgentEvent,
  type ChatMessageOut,
  type ChatSessionListOut,
  type ChatSessionOut,
  type PendingConfirmation,
  type PendingUserInput,
  type PlanStep,
  type QueuedAck,
  type QueuedInputListOut,
  type ReviewerOverride,
  type RuntimeToolsOut,
  type SessionGoalDetailOut,
  type SessionGoalIn,
  type ThinkingLevelsOut,
  type UserInputAction,
  type WebSearchOptionsOut,
} from "@/lib/chat";
import { errMsg } from "@/lib/utils";

interface RecentTurn {
  id: string;
  runId: string | null;
  sessionId: string;
  historyCount: number;
  content: string;
  startedAt: string;
  completedAt: string;
  events: AgentEvent[];
}

interface QueuedConfirmation {
  action: ConfirmationAction;
  key: string;
}

const SESSION_NAV_PREFERENCE_KEY = "chat-session-nav-collapsed";

function removeSessionFromLists(queryClient: QueryClient, sessionId: string) {
  queryClient.setQueriesData<ChatSessionListOut>(
    { queryKey: ["chat-sessions"] },
    (data) => {
      if (!data) return data;
      const items = data.items.filter((item) => item.id !== sessionId);
      return items.length === data.items.length
        ? data
        : { ...data, items, total: Math.max(0, data.total - 1) };
    },
  );
}

function ChatWorkbench() {
  const params = useSearchParams();
  const router = useRouter();
  const queryClient = useQueryClient();
  const mounted = useMounted();
  // 宿主作用域:两者要么同时给(scoped 会话),要么都不给(general 会话)。
  // 只给一个视为都没给——后端对半截 scope 直接 422,不如在前端就归一。
  const rawScopeType = params.get("scope_type");
  const rawScopeId = params.get("scope_id");
  const scoped = !!rawScopeType && !!rawScopeId;
  const scopeType = scoped ? rawScopeType : null;
  const scopeId = scoped ? rawScopeId : null;
  const sessionId = params.get("session");
  const [draft, setDraft] = useState("");
  const [optimisticUser, setOptimisticUser] = useState<{
    content: string;
    createdAt: string;
  } | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [recentTurns, setRecentTurns] = useState<RecentTurn[]>([]);
  const [running, setRunning] = useState(false);
  const [runStartedAt, setRunStartedAt] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [agentCardId, setAgentCardId] = useState<string | undefined>();
  const [sessionNavCollapsed, setSessionNavCollapsed] = useState(
    () =>
      typeof window !== "undefined" &&
      localStorage.getItem(SESSION_NAV_PREFERENCE_KEY) !== "false",
  );
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  // 思考等级(随时可调):档位表由后端配置下发,localStorage 记住偏好;
  // 偏好失效(档位表变更)或未设置时回退后端默认档
  const [thinkingPick, setThinkingPick] = useState<string | null>(() =>
    typeof window === "undefined"
      ? null
      : localStorage.getItem("chat-thinking-level"),
  );
  const thinkingLevels = useQuery({
    queryKey: ["thinking-levels"],
    queryFn: () => api.get<ThinkingLevelsOut>("/chat/thinking-levels"),
    staleTime: 5 * 60 * 1000,
  });
  // 联网方式:选项由后端按管理端配置下发;null = 跟随配置(auto)
  const [webSearchPick, setWebSearchPick] = useState<string | null>(() =>
    typeof window === "undefined"
      ? null
      : localStorage.getItem("chat-web-search"),
  );
  const webSearchOptions = useQuery({
    queryKey: ["websearch-options"],
    queryFn: () => api.get<WebSearchOptionsOut>("/chat/websearch-options"),
    staleTime: 5 * 60 * 1000,
  });
  // 偏好指向的源被管理端撤掉时回退"自动",避免拿一个选不了的值发请求
  const webSearch =
    webSearchPick &&
    webSearchOptions.data?.options.some(
      (o) => o.value === webSearchPick && o.ready,
    )
      ? webSearchPick
      : undefined;
  function pickWebSearch(value: string | undefined) {
    setWebSearchPick(value ?? null);
    if (value) localStorage.setItem("chat-web-search", value);
    else localStorage.removeItem("chat-web-search");
  }
  const thinking = thinkingLevels.data
    ? thinkingLevels.data.levels.some((l) => l.value === thinkingPick)
      ? (thinkingPick as string)
      : thinkingLevels.data.default
    : undefined;
  function pickThinking(level: string) {
    setThinkingPick(level);
    localStorage.setItem("chat-thinking-level", level);
  }
  function pickSessionNavCollapsed(collapsed: boolean) {
    setSessionNavCollapsed(collapsed);
    localStorage.setItem(SESSION_NAV_PREFERENCE_KEY, String(collapsed));
  }
  // 实时截停的待批操作(权威值来自会话详情,SSE 抢先一步渲染)
  // undefined 表示尚无实时覆盖；null 表示本地已明确清空，避免旧查询回流。
  const [livePending, setLivePending] = useState<
    PendingConfirmation | null | undefined
  >(undefined);
  const [livePendingUserInput, setLivePendingUserInput] = useState<
    PendingUserInput | null | undefined
  >(undefined);
  const [dismissedPendingKey, setDismissedPendingKey] = useState<string | null>(
    null,
  );
  const [decisionSubmitting, setDecisionSubmitting] = useState(false);
  const [userInputSubmitting, setUserInputSubmitting] = useState(false);
  const [pendingSession, setPendingSession] = useState(sessionId);
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string | null>(null);
  const maxSeqRef = useRef(0);
  const eventsRef = useRef<AgentEvent[]>([]);
  // 当前直播运行段对应的 user 气泡内容/历史切片/段起点:
  // 服务端在同一 run 内消费排队输入(input.consumed)时封存旧段、开新段
  const liveUserContentRef = useRef("");
  const liveHistoryCountRef = useRef(0);
  const liveSegmentStartedAtRef = useRef<string | null>(null);
  const activeRunSessionRef = useRef<string | null>(null);
  const startingRef = useRef(false);
  const followOutputRef = useRef(true);
  const queuedConfirmationRef = useRef<QueuedConfirmation | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const queue = useQueuedInputs(sessionId);
  const permissionIntent = useDeferredPermission();

  // 切换会话时同步丢弃上一会话的瞬时审批/下一轮权限意图。
  if (pendingSession !== sessionId) {
    setPendingSession(sessionId);
    setLivePending(undefined);
    setLivePendingUserInput(undefined);
    setDismissedPendingKey(null);
    setDecisionSubmitting(false);
    setUserInputSubmitting(false);
    permissionIntent.reset();
  }

  function go(next: { session?: string | null }) {
    const contextChanged = (next.session ?? null) !== sessionId;
    if (!running && contextChanged) {
      activeRunSessionRef.current = next.session ?? null;
      followOutputRef.current = true;
      queue.setEditing(null);
    }
    const qs = new URLSearchParams();
    if (scopeType && scopeId) {
      qs.set("scope_type", scopeType);
      qs.set("scope_id", scopeId);
    }
    if (next.session) qs.set("session", next.session);
    const query = qs.toString();
    router.replace(query ? `/app/chat?${query}` : "/app/chat");
  }

  const sessionQueryKey = ["chat-sessions", scopeType, scopeId] as const;
  const sessions = useQuery({
    queryKey: sessionQueryKey,
    queryFn: async () => {
      // scope=general 只列未绑作用域的会话;给了 scope_id 则只列绑到该作用域的
      const search = new URLSearchParams({ page_size: "50" });
      if (scopeType && scopeId) {
        search.set("scope_type", scopeType);
        search.set("scope_id", scopeId);
      } else {
        search.set("scope", "general");
      }
      const refreshed = await api.get<ChatSessionListOut>(
        `/chat/sessions?${search.toString()}`,
      );
      return preserveSelectedSession(
        queryClient.getQueryData<ChatSessionListOut>(sessionQueryKey),
        refreshed,
        sessionId,
      );
    },
  });
  const cards = useQuery({
    queryKey: ["agent-cards"],
    queryFn: () => api.get<AgentCardOut[]>("/chat/agent-cards"),
  });
  const current = sessions.data?.items.find((item) => item.id === sessionId);
  const selectedAgentCardId = current?.agent_card_id ?? agentCardId;
  // 本轮临时工具:候选随 agent 卡变化(只读且不在该卡白名单里),故按卡缓存
  const runtimeToolOptions = useQuery({
    queryKey: ["chat-runtime-tools", selectedAgentCardId ?? "default"],
    queryFn: () =>
      api.get<RuntimeToolsOut>(
        selectedAgentCardId
          ? `/chat/runtime-tools?agent_card_id=${selectedAgentCardId}`
          : "/chat/runtime-tools",
      ),
    staleTime: 5 * 60 * 1000,
  });
  // 刻意不落 localStorage:临时工具的语义就是"这一条消息",记住偏好会让它
  // 悄悄变成常开,与后端"不写 agent 卡白名单"的承诺自相矛盾
  const [runtimeToolPicks, setRuntimeToolPicks] = useState<string[]>([]);
  const runtimeTools = runtimeToolPicks.filter((name) =>
    runtimeToolOptions.data?.tools.some((tool) => tool.name === name),
  );
  const messages = useQuery({
    queryKey: ["chat-messages", sessionId],
    queryFn: () =>
      api.get<ChatMessageOut[]>(`/chat/sessions/${sessionId}/messages`),
    enabled: !!sessionId,
  });
  useEffect(() => {
    if (
      !sessionId ||
      !(messages.error instanceof ApiError) ||
      (messages.error.status !== 403 && messages.error.status !== 404)
    )
      return;
    removeSessionFromLists(queryClient, sessionId);
    router.replace("/app/chat");
  }, [messages.error, sessionId, queryClient, router]);
  const permissions = useQuery({
    queryKey: ["chat-permissions", sessionId],
    queryFn: () =>
      api.get<SessionPermissionState>(
        `/chat/sessions/${sessionId}/permissions`,
      ),
    enabled: !!sessionId,
  });
  // 服务端排队中的运行输入(重进会话时恢复遗留队列;实时状态由 202 回执与 SSE 维护)
  const queuedInputs = useQuery({
    queryKey: ["chat-queued-inputs", sessionId],
    queryFn: () => listQueuedInputs(sessionId as string),
    enabled: !!sessionId,
  });
  const queuedItems = queuedInputs.data?.items ?? [];
  // 会话目标:目标 active 时后台会自动续跑(没有面向本客户端的 SSE),
  // 因此空闲期轮询目标进度——否则用户看不出 Agent 还在替他跑
  const sessionGoal = useQuery({
    queryKey: ["chat-goal", sessionId],
    queryFn: () => getSessionGoal(sessionId as string),
    enabled: !!sessionId,
    refetchInterval: (query) =>
      query.state.data?.goal?.status === "active" ? 10000 : false,
  });
  const goal = sessionGoal.data?.goal ?? null;
  const goalAutoRuns = goal?.status === "active" ? goal.auto_runs : null;
  useEffect(() => {
    // 后台续跑写出的新消息只能靠这次刷新拉回来(客户端不在那条 SSE 上)
    if (goalAutoRuns !== null && goalAutoRuns > 0 && sessionId && !running)
      queryClient.invalidateQueries({ queryKey: ["chat-messages", sessionId] });
  }, [goalAutoRuns, sessionId, running, queryClient]);
  const cachedPending = permissions.isSuccess
    ? permissions.data.pending_decision
    : (current?.pending_confirmation ?? null);
  const pendingCandidate =
    livePending !== undefined ? livePending : cachedPending;
  const pending =
    pendingCandidate &&
    confirmationKey(pendingCandidate) !== dismissedPendingKey
      ? pendingCandidate
      : null;
  const pendingUserInput =
    livePendingUserInput !== undefined
      ? livePendingUserInput
      : (current?.pending_user_input ?? null);
  // 计划:实时 plan.update 优先,回退会话落库值(重进会话恢复)
  const plan = useMemo<PlanStep[] | null>(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (event.type === "plan.update") return event.steps;
    }
    return current?.plan ?? null;
  }, [events, current?.plan]);
  const items = useMemo<ConversationItem[]>(() => {
    const completed = recentTurns.filter(
      (turn) => turn.sessionId === sessionId,
    );
    const historyCount = completed[0]?.historyCount;
    const allMessages = messages.data ?? [];
    const persistedMessages =
      historyCount === undefined
        ? allMessages
        : allMessages.slice(0, historyCount);
    const result = historyToItems(persistedMessages);
    // 有专属渲染器的工具结果:落库版优先于直播版(内容一致但带权威 id)
    const persistedToolResults = new Map<string, ConversationItem>();
    if (historyCount !== undefined) {
      for (const message of allMessages.slice(historyCount)) {
        if (
          message.role !== "tool" ||
          !message.run_id ||
          !message.tool_call_id ||
          !hasToolResultRenderer(message.tool_name ?? "", message.tool_output)
        )
          continue;
        const item = historyToItems([message])[0];
        if (item)
          persistedToolResults.set(
            `${message.run_id} ${message.tool_call_id}`,
            { ...item, resultOnly: true },
          );
      }
    }
    const addedPersistedResults = new Set<string>();
    const appendPersistedToolResults = (
      runId: string | null,
      turnEvents: AgentEvent[],
    ) => {
      if (!runId) return;
      for (const event of turnEvents) {
        if (
          event.type !== "tool.result" ||
          !event.ok ||
          !hasToolResultRenderer(event.name, event.output)
        )
          continue;
        const identity = `${runId} ${event.tool_call_id}`;
        const persisted = persistedToolResults.get(identity);
        if (persisted && !addedPersistedResults.has(identity)) {
          result.push(persisted);
          addedPersistedResults.add(identity);
        }
      }
    };
    for (const turn of completed) {
      if (turn.content)
        result.push({
          key: `${turn.id}-user`,
          role: "user",
          content: turn.content,
          createdAt: turn.startedAt,
        });
      result.push({
        key: `${turn.id}-run`,
        role: "run",
        source: "live",
        runId: turn.runId,
        events: turn.events,
        createdAt: turn.startedAt,
        completedAt: turn.completedAt,
      });
      appendPersistedToolResults(turn.runId, turn.events);
    }
    if (optimisticUser)
      result.push({
        key: "live-user",
        role: "user",
        content: optimisticUser.content,
        createdAt: optimisticUser.createdAt,
      });
    // running 即渲染 live-run:首个事件到达前就有"思考中"占位,不再干等
    if (events.length || running)
      result.push({
        key: "live-run",
        role: "run",
        source: "live",
        runId: activeRunId,
        events,
        createdAt: runStartedAt,
      });
    if (events.length) appendPersistedToolResults(activeRunId, events);
    return result;
  }, [
    messages.data,
    recentTurns,
    sessionId,
    optimisticUser,
    events,
    running,
    runStartedAt,
    activeRunId,
  ]);
  useEffect(() => {
    if (scrollRef.current && followOutputRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [items, running, plan, pending]);
  useEffect(() => {
    queuedConfirmationRef.current = null;
  }, [sessionId]);
  useEffect(() => () => abortRef.current?.abort(), []);

  const sessionScopeBody = scoped
    ? { scope_type: scopeType, scope_id: scopeId }
    : {};

  const createSession = useMutation({
    mutationFn: (nextAgentCardId?: string) =>
      api.post<ChatSessionOut>("/chat/sessions", {
        agent_card_id: nextAgentCardId,
        ...sessionScopeBody,
      }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      go({ session: session.id });
    },
    onError: (error) => toast.error(errMsg(error, "创建会话失败")),
  });
  const deleteSession = useMutation({
    mutationFn: (id: string) => api.delete(`/chat/sessions/${id}`),
    onSuccess: (_data, id) => {
      toast.success("会话已删除");
      removeSessionFromLists(queryClient, id);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      if (id === sessionId) go({});
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409)
        toast.error("会话有任务在执行，先停止再删除");
      else toast.error(errMsg(error, "删除失败"));
    },
  });
  const revokeGrant = useMutation({
    mutationFn: (grantId: string) =>
      api.delete(`/agent/permissions/grants/${grantId}`),
    onSuccess: () => {
      if (sessionId)
        queryClient.invalidateQueries({
          queryKey: ["chat-permissions", sessionId],
        });
      toast.success("会话授权已撤销");
    },
    onError: (error) => toast.error(errMsg(error, "撤销授权失败")),
  });
  const saveGoal = useMutation({
    mutationFn: (body: SessionGoalIn) =>
      setSessionGoal(sessionId as string, body),
    onSuccess: (updated) => {
      queryClient.setQueryData<SessionGoalDetailOut>(
        ["chat-goal", updated.session_id],
        { goal: updated },
      );
      toast.success("会话目标已设定");
    },
    onError: (error) => toast.error(errMsg(error, "设定会话目标失败")),
  });
  const dropGoal = useMutation({
    mutationFn: () => cancelSessionGoal(sessionId as string),
    onSuccess: (updated) => {
      queryClient.setQueryData<SessionGoalDetailOut>(
        ["chat-goal", updated.session_id],
        { goal: updated },
      );
      toast.message("会话目标已取消，Agent 不再自动续跑");
    },
    onError: (error) => toast.error(errMsg(error, "取消会话目标失败")),
  });
  // 目标达成但模型没自觉收尾时,用户自己标完成(与 update_goal 同一终态)
  const finishGoal = useMutation({
    mutationFn: () => completeSessionGoal(sessionId as string),
    onSuccess: (updated) => {
      queryClient.setQueryData<SessionGoalDetailOut>(
        ["chat-goal", updated.session_id],
        { goal: updated },
      );
      toast.success("会话目标已标记完成");
    },
    onError: (error) => toast.error(errMsg(error, "标记目标完成失败")),
  });

  async function authoritativeRefresh(sid: string, includeMessages = true) {
    await Promise.all([
      ...(includeMessages
        ? [
            queryClient.invalidateQueries({
              queryKey: ["chat-messages", sid],
            }),
          ]
        : []),
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] }),
      ...(sid
        ? [
            queryClient.invalidateQueries({
              queryKey: ["chat-permissions", sid],
            }),
          ]
        : []),
    ]);
  }

  function optimisticallyClearPending(sid: string) {
    queryClient.setQueryData<SessionPermissionState>(
      ["chat-permissions", sid],
      (state) => (state ? { ...state, pending_decision: null } : state),
    );
    queryClient.setQueriesData<ChatSessionListOut>(
      { queryKey: ["chat-sessions"] },
      (data) =>
        data
          ? {
              ...data,
              items: data.items.map((item) =>
                item.id === sid
                  ? { ...item, pending_confirmation: null }
                  : item,
              ),
            }
          : data,
    );
  }

  function syncLivePendingFromCache(sid: string) {
    const state = queryClient.getQueryData<SessionPermissionState>([
      "chat-permissions",
      sid,
    ]);
    setLivePending(state?.pending_decision ?? null);
  }

  function syncLiveUserInputFromCache(sid: string) {
    const state = queryClient.getQueryData<ChatSessionListOut>(sessionQueryKey);
    setLivePendingUserInput(
      state?.items.find((item) => item.id === sid)?.pending_user_input ?? null,
    );
  }

  async function requestPermissionUpdate(update: SessionPermissionUpdate) {
    if (!sessionId) throw new Error("请先创建会话");
    await permissionIntent.request(
      sessionId,
      update,
      running || !!pending || decisionSubmitting,
      permissions.data,
    );
  }

  /**
   * 服务端在同一 run 内消费了一条排队输入:把当前直播段封存为完成卡片,
   * 再以该输入为新的乐观 user 气泡开一个新段——视觉上与"用户消息→Agent
   * 回复"完全一致,即"排队卡片转为正常 user 消息"。
   */
  function sealLiveSegment(nextUserContent: string) {
    const sid = activeRunSessionRef.current ?? sessionId;
    const segmentEvents = eventsRef.current;
    const now = new Date().toISOString();
    if (sid && segmentEvents.length) {
      const startedAt = liveSegmentStartedAtRef.current ?? now;
      setRecentTurns((previous) => [
        ...previous,
        {
          id: `${runIdRef.current ?? "run"}-seg-${segmentEvents[0].seq}`,
          runId: runIdRef.current,
          sessionId: sid,
          historyCount: liveHistoryCountRef.current,
          content: liveUserContentRef.current,
          startedAt,
          completedAt: now,
          events: segmentEvents,
        },
      ]);
    }
    eventsRef.current = [];
    setEvents([]);
    liveUserContentRef.current = nextUserContent;
    liveSegmentStartedAtRef.current = now;
    setRunStartedAt(now);
    setOptimisticUser({ content: nextUserContent, createdAt: now });
  }

  function receive(event: AgentEvent) {
    maxSeqRef.current = Math.max(maxSeqRef.current, event.seq);
    // Primary Agent UX renders complete loop steps, not transport token deltas.
    if (!isSemanticAgentEvent(event)) return;
    if (event.type.startsWith("input.")) {
      // 队列事件驱动排队卡片与运行段封存,不进运行卡时间线
      const sid = activeRunSessionRef.current ?? sessionId;
      if (!sid) return;
      const consumed = queue.receiveQueueEvent(sid, event as QueueEvent);
      if (consumed && event.type === "input.consumed")
        sealLiveSegment(event.content);
      return;
    }
    if (event.type === "runtime.tools") {
      // 本轮临时工具的判定回执:被拒项必须当场说清原因,否则用户只会觉得
      // "我明明勾了却没生效"。通过的项不打扰(工具会在时间线上自己出现)。
      const rejected = event.requests.filter((item) => !item.accepted);
      if (rejected.length)
        toast.error(
          `本轮工具未开启：${rejected
            .map((item) => `${item.label}（${item.hint}）`)
            .join("、")}`,
        );
      return;
    }
    if (eventsRef.current.some((item) => item.seq === event.seq)) return;
    eventsRef.current = [...eventsRef.current, event];
    setEvents(eventsRef.current);
    if (
      event.type === "approval.bundle.requested" ||
      event.type === "approval.bundle.updated"
    ) {
      setDismissedPendingKey(null);
      setLivePending(event.bundle);
    }
    if (event.type === "approval.bundle.completed") setLivePending(null);
    if (event.type === "user.input.required")
      setLivePendingUserInput(event.request);
    if (event.type === "user.input.resolved") setLivePendingUserInput(null);
    if (event.type === "goal.updated") {
      // 模型调 update_goal 时实时刷新横幅,不必等下一次轮询
      const sid = activeRunSessionRef.current ?? sessionId;
      if (sid)
        queryClient.setQueryData<SessionGoalDetailOut>(["chat-goal", sid], {
          goal: {
            id: event.goal_id,
            session_id: event.session_id,
            objective: event.objective,
            status: event.status,
            token_budget: event.token_budget,
            tokens_used: event.tokens_used,
            auto_runs: event.auto_runs,
            max_auto_runs: event.max_auto_runs,
            summary: event.summary,
            created_at: null,
            updated_at: null,
          },
        });
    }
    if (
      event.type === "tool.confirm" &&
      !eventsRef.current.some(
        (item) =>
          (item.type === "approval.bundle.requested" ||
            item.type === "approval.bundle.updated") &&
          item.bundle.status === "pending",
      )
    ) {
      setDismissedPendingKey(null);
      setLivePending({
        tool_call_id: event.tool_call_id,
        name: event.name,
        input: event.input,
        summary: event.summary,
      });
    }
  }

  const messageContext =
    scopeType && scopeId
      ? { scope_type: scopeType, scope_id: scopeId }
      : undefined;

  async function executeTurn(
    sid: string,
    content: string,
    action: AgentContinuationAction | undefined,
    startedAt: string,
    historyCount: number,
  ): Promise<boolean> {
    eventsRef.current = [];
    setEvents([]);
    setRunStartedAt(startedAt);
    maxSeqRef.current = 0;
    runIdRef.current = null;
    setActiveRunId(null);
    liveUserContentRef.current = content;
    liveHistoryCountRef.current = historyCount;
    liveSegmentStartedAtRef.current = startedAt;
    const controller = new AbortController();
    abortRef.current = controller;
    let completed = true;
    try {
      await sendChatMessage(
        sid,
        content,
        messageContext,
        receive,
        (runId) => {
          runIdRef.current = runId;
          setActiveRunId(runId);
        },
        controller.signal,
        action,
        thinking,
        undefined,
        webSearch,
        undefined,
        runtimeTools,
      );
    } catch (error) {
      completed = false;
      if (error instanceof ApiError && error.status === 409)
        toast.error(
          error.message === "no_pending_confirmation"
            ? "该操作已处理过"
            : error.message === "no_pending_user_input"
              ? "这组问题已处理或已更新"
              : error.message === "plan_step_not_continuable"
                ? "该计划步骤当前无法继续"
                : error.message === "no_reviewer_override"
                  ? "该一次性覆盖已失效或处理过"
                  : "当前会话有任务在执行",
        );
      else if (!controller.signal.aborted && runIdRef.current) {
        try {
          const replay = await api.get<AgentEvent[]>(
            `/chat/sessions/${sid}/events?run_id=${runIdRef.current}&after_seq=${maxSeqRef.current}`,
          );
          replay.forEach(receive);
          completed = replay.some((event) => event.type === "turn.done");
        } catch {
          toast.error(errMsg(error, "连接中断，事件补齐失败"));
        }
      } else if (!controller.signal.aborted)
        toast.error(errMsg(error, "发送失败"));
    } finally {
      await authoritativeRefresh(sid, false);
      syncLivePendingFromCache(sid);
      syncLiveUserInputFromCache(sid);
      const turnEvents = eventsRef.current;
      const terminal = [...turnEvents]
        .reverse()
        .find((event) => event.type === "turn.done");
      if (
        terminal?.type === "turn.done" &&
        (terminal.reason === "cancelled" || terminal.reason === "error")
      )
        completed = false;
      if (turnEvents.length)
        setRecentTurns((previous) => [
          ...previous,
          {
            id: runIdRef.current ?? `local-${startedAt}`,
            runId: runIdRef.current,
            sessionId: sid,
            historyCount,
            // 服务端消费过排队输入时,最后一段的 user 气泡是最近一条被消费的内容
            content: liveUserContentRef.current,
            startedAt: liveSegmentStartedAtRef.current ?? startedAt,
            completedAt: new Date().toISOString(),
            events: turnEvents,
          },
        ]);
      setOptimisticUser(null);
      eventsRef.current = [];
      setEvents([]);
      setRunStartedAt(null);
      setActiveRunId(null);
      abortRef.current = null;
    }
    return completed;
  }

  async function runTurn(
    sid: string,
    content: string,
    action?: AgentContinuationAction,
    startedAt = new Date().toISOString(),
  ) {
    const existingRecent = recentTurns.find((turn) => turn.sessionId === sid);
    const historyCount =
      existingRecent?.historyCount ?? messages.data?.length ?? 0;
    let turn:
      | {
          content: string;
          action?: AgentContinuationAction;
          startedAt: string;
        }
      | undefined = { content, action, startedAt };
    activeRunSessionRef.current = sid;
    followOutputRef.current = true;
    setRunning(true);

    try {
      while (turn) {
        if (turn.content) {
          setOptimisticUser({
            content: turn.content,
            createdAt: turn.startedAt,
          });
          setLivePending(null);
        }
        const turnCompleted = await executeTurn(
          sid,
          turn.content,
          turn.action,
          turn.startedAt,
          historyCount,
        );
        const confirmationTurn = isConfirmationAction(turn.action);
        if (confirmationTurn) {
          setDecisionSubmitting(false);
          setDismissedPendingKey(null);
        }

        const queuedConfirmation = queuedConfirmationRef.current;
        if (queuedConfirmation) {
          const authoritativePending =
            queryClient.getQueryData<SessionPermissionState>([
              "chat-permissions",
              sid,
            ])?.pending_decision ?? null;
          if (
            confirmationKey(authoritativePending) === queuedConfirmation.key
          ) {
            queuedConfirmationRef.current = null;
            turn = {
              content: "",
              action: queuedConfirmation.action,
              startedAt: new Date().toISOString(),
            };
            continue;
          }
          queuedConfirmationRef.current = null;
          setDecisionSubmitting(false);
          setDismissedPendingKey(null);
        }

        if (!turnCompleted) break;
        try {
          await permissionIntent.flush(sid);
        } catch {
          // Mutation owner already surfaced the authoritative save error.
        }
        // 排队中的后续输入由服务端在 run 内的安全检查点消费(input.consumed),
        // 客户端不再自行串行补发。
        turn = undefined;
      }
    } finally {
      if (queuedConfirmationRef.current) {
        queuedConfirmationRef.current = null;
        setDecisionSubmitting(false);
        setDismissedPendingKey(null);
        syncLivePendingFromCache(sid);
      }
      // run 结束仍有"排队中"卡片(断线漏掉 input.consumed 等)时刷新队列快照;
      // 只剩"已跳过"提示时保留本地展示,不被空结果冲掉
      if (
        queryClient
          .getQueryData<QueuedInputListOut>(["chat-queued-inputs", sid])
          ?.items.some((item) => item.status === "queued")
      )
        queryClient.invalidateQueries({
          queryKey: ["chat-queued-inputs", sid],
        });
      setRunning(false);
    }
  }

  /**
   * run 进行中发消息:send_message 由服务端以 202 入队(不再 409),排队卡片
   * 渲染在消息列表底部。极小概率 run 恰好刚结束——服务端会直接开新 run 并
   * 返回 SSE 流,此时整轮收集完落成一张完成卡片,不与直播中的事件流互抢。
   */
  async function queueDuringRun(sid: string, content: string) {
    const collected: AgentEvent[] = [];
    const startedAt = new Date().toISOString();
    let queuedAck = null as QueuedAck | null;
    let raceRunId: string | null = null;
    // 勾选态在 send() 里已随发送清空,这里先取一份快照给乐观卡片显示
    const queuedRuntimeTools = runtimeTools;
    try {
      await sendChatMessage(
        sid,
        content,
        messageContext,
        (event) => collected.push(event),
        (runId) => {
          raceRunId = runId;
        },
        undefined,
        undefined,
        thinking,
        undefined,
        webSearch,
        (ack) => {
          queuedAck = ack;
        },
        runtimeTools,
      );
    } catch (error) {
      setDraft(content); // 入队失败把内容还给输入框,避免丢字
      toast.error(errMsg(error, "发送失败"));
      return;
    }
    if (queuedAck) {
      const ack: QueuedAck = queuedAck;
      setQueuedCache(queryClient, sid, (items) =>
        items.some((item) => item.id === ack.queued_input_id)
          ? items
          : [
              ...items,
              {
                id: ack.queued_input_id,
                run_id: ack.run_id,
                position: ack.position,
                status: "queued",
                content,
                runtime_tools: queuedRuntimeTools,
                consumed_message_id: null,
                created_at: startedAt,
                updated_at: startedAt,
              },
            ],
      );
      return;
    }
    if (collected.length) {
      const existingRecent = recentTurns.find((turn) => turn.sessionId === sid);
      setRecentTurns((previous) => [
        ...previous,
        {
          id: raceRunId ?? `race-${startedAt}`,
          runId: raceRunId,
          sessionId: sid,
          historyCount:
            existingRecent?.historyCount ?? messages.data?.length ?? 0,
          content,
          startedAt,
          completedAt: new Date().toISOString(),
          events: collected,
        },
      ]);
      await authoritativeRefresh(sid, false);
    }
  }

  async function send() {
    const content = draft.trim();
    if (!content) return;
    if (pendingUserInput) {
      toast.message("请先完成上方问题");
      return;
    }
    if (running) {
      const sid = activeRunSessionRef.current ?? sessionId;
      if (!sid) return;
      setDraft("");
      // 勾选态随消息一起送走:"本轮"就是这一条,留在面板上会被误当成常开开关。
      // 闭包里的 runtimeTools 是本次渲染的值,清空不影响正在发出的这条。
      setRuntimeToolPicks([]);
      await queueDuringRun(sid, content);
      return;
    }
    if (startingRef.current) return;
    startingRef.current = true;
    let sid = sessionId;
    if (!sid) {
      try {
        const session = await api.post<ChatSessionOut>("/chat/sessions", {
          agent_card_id: agentCardId,
          ...sessionScopeBody,
        });
        sid = session.id;
        go({ session: sid });
      } catch (error) {
        toast.error(errMsg(error, "创建会话失败"));
        startingRef.current = false;
        return;
      }
    }
    const startedAt = new Date().toISOString();
    setDraft("");
    setRuntimeToolPicks([]); // 同上:临时工具只作用于这一条消息
    try {
      await runTurn(sid, content, undefined, startedAt);
    } finally {
      startingRef.current = false;
    }
  }

  async function decide(confirm: ConfirmationAction) {
    if (!sessionId || !pending || decisionSubmitting) return;
    const key = confirmationKey(pending);
    if (!key) return;
    setDecisionSubmitting(true);
    setDismissedPendingKey(key);
    setLivePending(null);
    optimisticallyClearPending(sessionId);
    if (running) {
      queuedConfirmationRef.current = { action: confirm, key };
      return;
    }
    await runTurn(sessionId, "", confirm);
  }

  async function answerUserInput(action: UserInputAction) {
    if (!sessionId || !pendingUserInput || running || userInputSubmitting)
      return;
    setUserInputSubmitting(true);
    setLivePendingUserInput(null);
    try {
      await runTurn(sessionId, "", action);
    } finally {
      setUserInputSubmitting(false);
    }
  }

  async function applyReviewerOverride(override: ReviewerOverride) {
    if (!sessionId || running || pending) return;
    await runTurn(sessionId, "", {
      candidate_id: override.candidate_id,
      action_hash: override.action_hash,
    });
  }

  function adjustGeneratedImage(nextDraft: string) {
    setDraft(nextDraft);
    requestAnimationFrame(() => composerRef.current?.focus());
  }

  async function generateImageAgain(message: string) {
    if (!sessionId) return;
    if (pendingUserInput) {
      toast.message("请先完成上方问题");
      return;
    }
    if (pending && !running) {
      toast.message("请先处理当前待确认操作");
      return;
    }
    if (running) {
      await queueDuringRun(sessionId, message);
      toast.message("已加入队列");
      return;
    }
    await runTurn(sessionId, message);
  }

  // 展示计划不是可执行流水线，因此这里只发结构化“继续处理”控制动作。
  async function continuePlanStep(step: PlanStep) {
    if (!sessionId || running) return;
    if (pendingUserInput || pending) {
      toast.message("请先处理当前待交互内容");
      return;
    }
    await runTurn(sessionId, "", { step_id: step.id });
  }

  async function stop() {
    const sid = activeRunSessionRef.current ?? sessionId;
    if (!sid) return;
    try {
      await api.post(`/chat/sessions/${sid}/cancel`);
      toast.message("已请求停止");
    } catch (error) {
      toast.error(errMsg(error, "停止失败"));
    }
  }

  function pickAgent(nextAgentCardId: string | undefined) {
    if (nextAgentCardId === selectedAgentCardId) return;
    setAgentCardId(nextAgentCardId);
    if (sessionId) go({});
  }

  function openSession(nextSessionId: string) {
    const nextSession = sessions.data?.items.find(
      (item) => item.id === nextSessionId,
    );
    if (nextSession) setAgentCardId(nextSession.agent_card_id);
    go({ session: nextSessionId });
  }

  const composerToolbar = (
    <RuntimeControls
      thinkingLevels={thinkingLevels.data}
      thinking={thinking}
      cards={cards.data}
      agentCardId={selectedAgentCardId}
      disabled={running}
      onThinkingChange={pickThinking}
      onAgentCardChange={pickAgent}
      webSearchOptions={webSearchOptions.data}
      webSearch={webSearch}
      onWebSearchChange={pickWebSearch}
      runtimeToolOptions={runtimeToolOptions.data}
      runtimeTools={runtimeTools}
      onRuntimeToolsChange={setRuntimeToolPicks}
    />
  );
  const composerActions = (
    <PermissionControl
      state={permissions.data}
      loading={permissions.isLoading}
      disabled={running}
      pending={permissionIntent.saving || revokeGrant.isPending}
      deferredUpdate={permissionIntent.deferredFor(sessionId)}
      onUpdate={async (update) => {
        await requestPermissionUpdate(update);
      }}
      onRevokeGrant={async (grantId) => {
        await revokeGrant.mutateAsync(grantId);
      }}
    />
  );
  const composerDock =
    pendingUserInput || pending || plan?.length ? (
      <div className="space-y-2">
        {pendingUserInput ? (
          <UserInputCard
            key={pendingUserInput.request_id}
            request={pendingUserInput}
            busy={running || userInputSubmitting}
            onSubmit={(action) => void answerUserInput(action)}
          />
        ) : pending ? (
          <ConfirmCard
            pending={pending}
            active
            busy={decisionSubmitting}
            onDecide={(confirmation) => void decide(confirmation)}
          />
        ) : null}
        {plan && plan.length > 0 && (
          <PlanChecklist
            steps={plan}
            onContinueStep={(step) => void continuePlanStep(step)}
            continueDisabled={running || !!pendingUserInput || !!pending}
          />
        )}
      </div>
    ) : undefined;

  return (
    <>
      <ShellHeader position="centered">
        <div className="flex h-full w-full items-center justify-center lg:grid lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] lg:gap-3">
          <div className="hidden min-w-0 lg:block">
            <Breadcrumbs
              items={[
                { label: "工作台", href: "/app/chat" },
                { label: "AI 助手" },
              ]}
            />
          </div>
          <div className="justify-self-center">
            {scoped && (
              <span className="rounded-full bg-black/[0.045] px-3 py-1.5 text-xs text-muted-foreground ring-1 ring-inset ring-black/[0.045] dark:bg-white/[0.07] dark:ring-white/[0.045]">
                作用域 {scopeType} · {scopeId}
              </span>
            )}
          </div>
          <div className="hidden lg:block" aria-hidden="true" />
        </div>
      </ShellHeader>
      <div className="h-[calc(100dvh-5rem)] sm:h-[calc(100dvh-6.5rem)]">
        <div className="flex h-full min-h-0 overflow-hidden bg-[#f7f7f5] dark:bg-[#191918]">
          <SessionSidebar
            scopeLabel={scoped ? `${scopeType} 会话` : "通用会话"}
            sessions={sessions.data?.items}
            activeSessionId={sessionId}
            collapsed={mounted && sessionNavCollapsed}
            mobileOpen={mobileSessionsOpen}
            disabled={running || createSession.isPending}
            onCollapsedChange={pickSessionNavCollapsed}
            onMobileOpenChange={setMobileSessionsOpen}
            onCreate={() => createSession.mutate(selectedAgentCardId)}
            onSelect={openSession}
            onDelete={(id) => deleteSession.mutateAsync(id)}
          />
          <main className="flex min-w-0 flex-1 flex-col bg-[#f7f7f5] dark:bg-[#191918]">
            <div className="flex h-11 shrink-0 items-center gap-2 px-3 sm:px-4">
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="md:hidden"
                onClick={() => setMobileSessionsOpen(true)}
                aria-label="打开会话列表"
                title="会话"
              >
                <PanelLeftOpen className="size-4" />
              </Button>
              <span className="truncate text-sm font-medium tracking-tight">
                {current?.title ?? "通用助手"}
              </span>
              {running && (
                <Loader2 className="ml-auto size-3.5 animate-spin text-primary" />
              )}
            </div>
            {sessionId && (
              <GoalBanner
                goal={goal}
                disabled={running}
                busy={
                  saveGoal.isPending ||
                  dropGoal.isPending ||
                  finishGoal.isPending
                }
                onSubmit={async (body) => {
                  await saveGoal.mutateAsync(body);
                }}
                onCancel={async () => {
                  await dropGoal.mutateAsync();
                }}
                onComplete={async () => {
                  await finishGoal.mutateAsync();
                }}
              />
            )}
            <div
              ref={scrollRef}
              data-conversation-scroll
              onScroll={(event) => {
                const element = event.currentTarget;
                followOutputRef.current =
                  element.scrollHeight -
                    element.scrollTop -
                    element.clientHeight <
                  96;
              }}
              className="min-h-0 flex-1 overflow-y-auto bg-[#f7f7f5] dark:bg-[#191918]"
            >
              <AgentConversation
                items={items}
                running={running}
                liveStartedAt={runStartedAt}
                scopeType={scopeType}
                scopeId={scopeId}
                pending={pending}
                reviewerOverrides={permissions.data?.reviewer_overrides ?? []}
                onPreset={setDraft}
                onOverride={(override) => void applyReviewerOverride(override)}
                onAdjustImage={adjustGeneratedImage}
                onGenerateImageAgain={(message) =>
                  void generateImageAgain(message)
                }
              />
              <QueuedInputList items={queuedItems} queue={queue} />
            </div>
            <CommandInput
              value={draft}
              onChange={setDraft}
              above={composerDock}
              toolbar={pendingUserInput ? undefined : composerToolbar}
              actions={pendingUserInput ? undefined : composerActions}
              placeholder={
                pendingUserInput
                  ? "请先完成上方问题"
                  : pending && !running
                    ? "有待确认操作：直接发消息视为不批准"
                    : "问点什么,或让助手检索知识、联网调研、安排定时任务…"
              }
              queuedTurns={[]}
              running={running}
              disabled={!!pendingUserInput}
              onSubmit={() => void send()}
              onStop={() => void stop()}
              onQueuedEdit={() => {}}
              onQueuedMove={() => {}}
              onQueuedRemove={() => {}}
              onQueuedRun={() => {}}
              inputRef={composerRef}
            />
          </main>
        </div>
      </div>
    </>
  );
}

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex justify-center py-12">
          <Loader2 className="size-5 animate-spin" />
        </div>
      }
    >
      <ChatWorkbench />
    </Suspense>
  );
}
