"use client";

import { Check, ChevronLeft, ChevronRight, CircleHelp } from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
  PendingUserInput,
  UserInputAction,
  UserInputQuestion,
} from "@/lib/chat";
import { cn } from "@/lib/utils";

interface AnswerDraft {
  selected: string[];
  otherSelected: boolean;
  otherText: string;
}

function emptyDrafts(
  questions: UserInputQuestion[],
): Record<string, AnswerDraft> {
  return Object.fromEntries(
    questions.map((question) => [
      question.id,
      { selected: [], otherSelected: false, otherText: "" },
    ]),
  );
}

function answerIsValid(
  question: UserInputQuestion,
  draft: AnswerDraft,
): boolean {
  const other = draft.otherSelected ? draft.otherText.trim() : "";
  if (draft.otherSelected && !other) return false;
  const count = draft.selected.length + (other ? 1 : 0);
  return question.multi_select ? count > 0 : count === 1;
}

export function UserInputCard({
  request,
  busy = false,
  onSubmit,
}: {
  request: PendingUserInput;
  busy?: boolean;
  onSubmit: (action: UserInputAction) => void;
}) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [drafts, setDrafts] = useState(() => emptyDrafts(request.questions));
  const question = request.questions[activeIndex];
  const validity = useMemo(
    () => request.questions.map((item) => answerIsValid(item, drafts[item.id])),
    [drafts, request.questions],
  );
  const allValid = validity.every(Boolean);

  if (!question) return null;
  const draft = drafts[question.id];

  function updateDraft(
    questionId: string,
    update: (current: AnswerDraft) => AnswerDraft,
  ) {
    setDrafts((current) => ({
      ...current,
      [questionId]: update(current[questionId]),
    }));
  }

  function selectFixed(label: string, checked: boolean) {
    updateDraft(question.id, (current) => {
      if (!question.multi_select)
        return {
          ...current,
          selected: [label],
          otherSelected: false,
        };
      return {
        ...current,
        selected: checked
          ? [...current.selected, label]
          : current.selected.filter((item) => item !== label),
      };
    });
  }

  function selectOther(checked: boolean) {
    updateDraft(question.id, (current) => ({
      ...current,
      selected: question.multi_select ? current.selected : [],
      otherSelected: checked,
    }));
  }

  function submit() {
    if (!allValid || busy) return;
    onSubmit({
      request_id: request.request_id,
      answers: request.questions.map((item) => {
        const answer = drafts[item.id];
        return {
          question_id: item.id,
          selected: answer.selected,
          other: answer.otherSelected ? answer.otherText.trim() : undefined,
        };
      }),
    });
  }

  return (
    <section
      aria-label="需要补充的信息"
      className="overflow-hidden rounded-2xl bg-white shadow-[0_8px_26px_rgb(0_0_0/0.075)] ring-1 ring-black/[0.075] dark:bg-[#242422] dark:ring-white/[0.09]"
    >
      <div className="flex items-start gap-3 border-b border-border/55 px-4 py-3.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-xl bg-primary/9 text-primary">
          <CircleHelp className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">需要你补充信息</p>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            可使用上一题、下一题返回修改，完成后统一提交。
          </p>
        </div>
        <span className="pt-0.5 text-xs tabular-nums text-muted-foreground">
          {activeIndex + 1}/{request.questions.length}
        </span>
      </div>

      {request.questions.length > 1 && (
        <div
          role="tablist"
          aria-label="问题"
          className="flex gap-1 overflow-x-auto border-b border-border/45 px-3 py-2"
        >
          {request.questions.map((item, index) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`question-tab-${request.request_id}-${item.id}`}
              aria-selected={index === activeIndex}
              aria-controls={`question-panel-${request.request_id}-${item.id}`}
              disabled={busy}
              onClick={() => setActiveIndex(index)}
              className={cn(
                "flex min-w-0 shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-50",
                index === activeIndex
                  ? "bg-foreground text-background"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {validity[index] ? (
                <Check className="size-3" />
              ) : (
                <span className="text-[10px] tabular-nums">{index + 1}</span>
              )}
              <span className="max-w-28 truncate">{item.header}</span>
            </button>
          ))}
        </div>
      )}

      <div
        role={request.questions.length > 1 ? "tabpanel" : undefined}
        id={`question-panel-${request.request_id}-${question.id}`}
        aria-labelledby={
          request.questions.length > 1
            ? `question-tab-${request.request_id}-${question.id}`
            : undefined
        }
        className="max-h-[min(24rem,50dvh)] overflow-y-auto px-4 py-4"
      >
        <fieldset disabled={busy}>
          <legend className="text-sm leading-6 font-medium">
            {question.question}
          </legend>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {question.multi_select ? "可选择多项" : "请选择一项"}
          </p>
          <div className="mt-3 space-y-2">
            {question.options.map((option, optionIndex) => {
              const checked = draft.selected.includes(option.label);
              return (
                <label
                  key={option.label}
                  className={cn(
                    "flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors",
                    checked
                      ? "border-primary/35 bg-primary/[0.055]"
                      : "border-border/65 hover:bg-muted/45",
                  )}
                >
                  <input
                    type={question.multi_select ? "checkbox" : "radio"}
                    name={`question-${request.request_id}-${question.id}`}
                    checked={checked}
                    onChange={(event) =>
                      selectFixed(option.label, event.target.checked)
                    }
                    className="mt-1 size-3.5 accent-primary"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm leading-5">
                      {option.label}
                    </span>
                    {option.description && (
                      <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                        {option.description}
                      </span>
                    )}
                  </span>
                  <span className="sr-only">选项 {optionIndex + 1}</span>
                </label>
              );
            })}

            <div
              className={cn(
                "rounded-xl border px-3 py-2.5 transition-colors",
                draft.otherSelected
                  ? "border-primary/35 bg-primary/[0.055]"
                  : "border-border/65",
              )}
            >
              <label className="flex cursor-pointer items-center gap-3">
                <input
                  type={question.multi_select ? "checkbox" : "radio"}
                  name={`question-${request.request_id}-${question.id}`}
                  checked={draft.otherSelected}
                  onChange={(event) => selectOther(event.target.checked)}
                  className="size-3.5 accent-primary"
                />
                <span className="text-sm">其他（自行填写）</span>
              </label>
              {draft.otherSelected && (
                <Input
                  value={draft.otherText}
                  maxLength={1000}
                  autoFocus
                  onChange={(event) =>
                    updateDraft(question.id, (current) => ({
                      ...current,
                      otherSelected: true,
                      otherText: event.target.value,
                    }))
                  }
                  placeholder="填写你的答案"
                  aria-label={`${question.header}的其他答案`}
                  className="mt-2 bg-background"
                />
              )}
            </div>
          </div>
        </fieldset>
      </div>

      <div className="flex items-center gap-2 border-t border-border/55 px-4 py-3">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy || activeIndex === 0}
          onClick={() => setActiveIndex((index) => Math.max(0, index - 1))}
        >
          <ChevronLeft />
          上一题
        </Button>
        <span className="min-w-0 flex-1 text-center text-[11px] text-muted-foreground">
          已完成 {validity.filter(Boolean).length}/{request.questions.length}
        </span>
        {activeIndex < request.questions.length - 1 ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() =>
              setActiveIndex((index) =>
                Math.min(request.questions.length - 1, index + 1),
              )
            }
          >
            下一题
            <ChevronRight />
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            disabled={busy || !allValid}
            onClick={submit}
          >
            <Check />
            提交答案
          </Button>
        )}
      </div>
    </section>
  );
}
