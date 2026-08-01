"use client";

// AI 解答的赞/踩反馈条。独立组件不侵入 frontline-results:反馈只需要
// 答案的静态快照(query/answer/sources),与解答流的加载态解耦,由
// KnowledgeAnswerPanel 侧接线挂在答案卡下方即可。

import { useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { KnowledgeAnswerSource } from "@/lib/types";

/** 后端 POST /kb/answer/feedback 的来源最小快照(kb_feedback.py 契约)。 */
export interface AnswerFeedbackSourceSnapshot {
  ref: number;
  kind: string;
  layer: string;
  source: string;
  source_doc_id?: string;
}

export interface AnswerFeedbackProps {
  query: string;
  answerText: string;
  sources: KnowledgeAnswerSource[];
}

type FeedbackPhase = "idle" | "down" | "done";

// 与后端校验上限对齐,客户端先行截断,避免边界值 422
const QUERY_MAX = 1000;
const SOURCE_MAX = 300;
const COMMENT_MAX = 500;
const SOURCES_MAX = 50;

function toSnapshot(sources: KnowledgeAnswerSource[]): AnswerFeedbackSourceSnapshot[] {
  return sources.slice(0, SOURCES_MAX).map(({ ref, hit }) => ({
    ref,
    kind: hit.kind,
    layer: hit.layer,
    source: hit.source.slice(0, SOURCE_MAX),
    ...(hit.citation?.source_doc_id
      ? { source_doc_id: hit.citation.source_doc_id }
      : {}),
  }));
}

/** 反馈状态机与提交逻辑,便于单测与后续复用(如放入分享页)。 */
export function useAnswerFeedback({ query, answerText, sources }: AnswerFeedbackProps) {
  const [phase, setPhase] = useState<FeedbackPhase>("idle");
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (rating: "up" | "down") => {
    if (submitting || phase === "done") return;
    setSubmitting(true);
    try {
      await api.post("/kb/answer/feedback", {
        query: query.slice(0, QUERY_MAX),
        answer_text: answerText,
        rating,
        comment: rating === "down" && comment.trim() ? comment.trim() : undefined,
        sources: toSnapshot(sources),
      });
      setPhase("done");
    } catch (err) {
      toast.error(errMsg(err, "反馈提交失败"));
    } finally {
      setSubmitting(false);
    }
  };

  return { phase, setPhase, comment, setComment, submitting, submit };
}

export function AnswerFeedback(props: AnswerFeedbackProps) {
  const { phase, setPhase, comment, setComment, submitting, submit } =
    useAnswerFeedback(props);

  if (phase === "done") {
    return (
      <p className="text-xs text-muted-foreground">已收到反馈,谢谢</p>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span>这个回答有帮助吗?</span>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="有帮助"
        disabled={submitting}
        onClick={() => void submit("up")}
      >
        <ThumbsUp />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="没帮助"
        aria-expanded={phase === "down"}
        disabled={submitting}
        onClick={() => setPhase("down")}
      >
        <ThumbsDown />
      </Button>
      {phase === "down" && (
        <form
          className="flex min-w-0 flex-1 items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void submit("down");
          }}
        >
          <Input
            autoFocus
            value={comment}
            maxLength={COMMENT_MAX}
            placeholder="哪里不对?(选填)"
            aria-label="反馈原因"
            className="h-7 flex-1 text-xs"
            onChange={(event) => setComment(event.target.value)}
          />
          <Button type="submit" size="sm" variant="outline" disabled={submitting}>
            提交
          </Button>
        </form>
      )}
    </div>
  );
}
