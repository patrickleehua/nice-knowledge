"use client";

// AI 解答流式 hook:消费 POST /kb/answer/stream 的 SSE 帧
// (sources → delta* → [restart → delta*] → done | error;或 no_evidence 直接结束)。
// 不走 react-query:流式增量状态与 useQuery 的"一次性结果"模型不匹配,自管状态机。
//
// 计费约束是核心设计动机:每次生成都是一次真实 LLM 计费,所以
// 1) 同一 query+kbIds 的成功结果缓存在模块级 Map,组件重挂载/回退浏览历史时直接回放;
// 2) 失败不缓存,由用户点"重试"显式重打(retry 只对 error 态暴露);
// 3) query/kbIds 变化即 abort 旧流,卸载同样 abort,不留孤儿请求继续烧钱。

import { useCallback, useEffect, useRef, useState } from "react";
import { postSse } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { KnowledgeAnswerSource } from "@/lib/types";

export type KbAnswerStreamStatus =
  | "idle"
  | "streaming"
  | "success"
  | "no_evidence"
  | "error";

export interface KbAnswerStreamState {
  status: KbAnswerStreamStatus;
  /** 已累积的答案 Markdown(restart 帧会清零重新累积) */
  answerText: string;
  /** 候选证据全集;done 后裁剪为 used_refs 对应子集(保持原 ref 编号) */
  sources: KnowledgeAnswerSource[];
  usedRefs: number[] | null;
  errorMessage: string | null;
}

/** 后端 /kb/answer/stream 的 SSE 帧契约(字段按 type 判别,未知 type 忽略以便向前兼容) */
interface KbAnswerStreamFrame {
  type?: string;
  sources?: KnowledgeAnswerSource[];
  text?: string;
  used_refs?: number[];
  code?: string;
  message?: string;
}

const IDLE_STATE: KbAnswerStreamState = {
  status: "idle",
  answerText: "",
  sources: [],
  usedRefs: null,
  errorMessage: null,
};

interface CachedAnswer {
  status: "success" | "no_evidence";
  answerText: string;
  sources: KnowledgeAnswerSource[];
  usedRefs: number[] | null;
}

// 模块级缓存:会话(页面生命周期)内同一 query+scope 不重复计费。
// no_evidence 也是确定性终态(检索完成、无可引用证据),同样缓存以免重复检索。
const answerCache = new Map<string, CachedAnswer>();

/** 错误码兜底文案:后端 message 缺失时按 code 给用户可理解的提示 */
function errorFallback(code: string | undefined): string {
  switch (code) {
    case "budget":
      return "本月 AI 解答额度已用完，请联系管理员或稍后再试。";
    case "unavailable":
      return "AI 解答服务暂时不可用，请稍后重试。";
    case "invalid_citations":
      return "答案引用未通过校验，已中止输出，请重试。";
    default:
      return "生成失败，请稍后重试。";
  }
}

/** 新 key 的起点状态:空查询回 idle,缓存命中直接回放终态,否则进入流式占位 */
function initialStateFor(cacheKey: string): KbAnswerStreamState {
  const [queryPart] = cacheKey.split("\u0000");
  if (!queryPart) return IDLE_STATE;
  const cached = answerCache.get(cacheKey);
  if (cached) return { ...cached, errorMessage: null };
  return { ...IDLE_STATE, status: "streaming" };
}

export function useKbAnswerStream(query: string, kbIds?: string[]) {
  // 用字符串 key 承载 query+scope 的语义身份:kbIds 每次渲染都是新数组
  // (searchParams.getAll),直接进 effect deps 会无限重连,拼成 key 后天然去重。
  // kb id 为 UUID,不含 "\u0000"/","(分隔符安全)。
  const normalizedQuery = query.trim();
  const cacheKey = `${normalizedQuery}\u0000${(kbIds ?? []).join(",")}`;

  const [state, setState] = useState<KbAnswerStreamState>(() =>
    initialStateFor(cacheKey),
  );
  // retry 只是把 attempt +1 触发 effect 重跑;失败不进缓存,所以重跑必然重新打接口
  const [attempt, setAttempt] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  // key 变化的状态复位放在渲染期(adjust state 模式,与 page.tsx 同步输入框一致):
  // effect 里同步 setState 会触发级联渲染(react-hooks lint 禁止),
  // 且渲染期复位能保证切换 query 的那一帧就不再展示旧答案。
  const [lastKey, setLastKey] = useState(cacheKey);
  if (lastKey !== cacheKey) {
    setLastKey(cacheKey);
    setState(initialStateFor(cacheKey));
  }

  useEffect(() => {
    const [queryPart, kbCsv] = cacheKey.split("\u0000");
    // 空查询无事可做;缓存命中时回放已在渲染期完成,不建流(不重复计费)
    if (!queryPart || answerCache.has(cacheKey)) return;

    const controller = new AbortController();
    abortRef.current = controller;

    // 累积量放在 effect 局部而不是 state 回调里读取,保证 restart/done 的
    // 裁剪逻辑基于同一份权威数据,不依赖 React 批处理时序。
    let answerText = "";
    let sources: KnowledgeAnswerSource[] = [];
    // 是否收到终态帧(done/error/no_evidence);流自然结束但没有终态帧视为中断
    let terminal = false;

    const handleFrame = (data: unknown) => {
      if (controller.signal.aborted) return;
      const frame = data as KbAnswerStreamFrame;
      switch (frame.type) {
        case "sources":
          sources = frame.sources ?? [];
          setState((prev) => ({ ...prev, sources }));
          break;
        case "no_evidence":
          terminal = true;
          answerCache.set(cacheKey, {
            status: "no_evidence",
            answerText: "",
            sources: [],
            usedRefs: null,
          });
          setState({ ...IDLE_STATE, status: "no_evidence" });
          break;
        case "delta":
          answerText += frame.text ?? "";
          setState((prev) => ({ ...prev, answerText }));
          break;
        case "restart":
          // 服务端推倒重来(引用修复/降级):清空已累积正文,来源全集仍有效
          answerText = "";
          setState((prev) => ({ ...prev, answerText: "" }));
          break;
        case "done": {
          terminal = true;
          const usedRefs = frame.used_refs ?? [];
          const usedSet = new Set(usedRefs);
          // 裁剪为实际被引用的来源子集,ref 编号保持不变(正文 [n] 锚点依赖它)
          const usedSources = sources.filter((source) =>
            usedSet.has(source.ref),
          );
          const settled: CachedAnswer = {
            status: "success",
            answerText,
            sources: usedSources,
            usedRefs,
          };
          answerCache.set(cacheKey, settled);
          setState({ ...settled, errorMessage: null });
          break;
        }
        case "error":
          terminal = true;
          setState((prev) => ({
            ...prev,
            status: "error",
            errorMessage: frame.message || errorFallback(frame.code),
          }));
          break;
        default:
          // 未知帧类型:忽略,保证后端加帧不炸旧前端
          break;
      }
    };

    postSse(
      "/kb/answer/stream",
      { query: queryPart, kb_ids: kbCsv ? kbCsv.split(",") : undefined },
      handleFrame,
      controller.signal,
    )
      .then(() => {
        // 流正常关闭但没收到终态帧 = 连接中途断了,按错误处理(可重试)
        if (terminal || controller.signal.aborted) return;
        setState((prev) => ({
          ...prev,
          status: "error",
          errorMessage: "回答流意外中断，请重试。",
        }));
      })
      .catch((error: unknown) => {
        if (terminal || controller.signal.aborted) return;
        setState((prev) => ({
          ...prev,
          status: "error",
          errorMessage: errMsg(error, "生成失败，请稍后重试。"),
        }));
      });

    // query/scope 变化或卸载:取消旧流,后端可据此停掉生成
    return () => controller.abort();
  }, [cacheKey, attempt]);

  // 重试前先把 error 态复位为流式占位(事件回调里 setState,不受 effect 限制),
  // 否则新流的首帧到达前面板还挂着旧错误提示。
  const retry = useCallback(() => {
    setState({ ...IDLE_STATE, status: "streaming" });
    setAttempt((n) => n + 1);
  }, []);

  return { ...state, retry };
}
