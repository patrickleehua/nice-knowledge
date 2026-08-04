"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  ChevronDown,
  Library,
  Loader2,
  Search,
  SearchX,
  Send,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import Image from "next/image";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import {
  FrontlineSearchResults,
  KnowledgeAnswerEmptyState,
  KnowledgeAnswerPanel,
} from "@/components/kb/search/frontline-results";
import { tokenize } from "@/components/kb/search/highlight";
import {
  SearchHistory,
  useSearchHistory,
} from "@/components/kb/search/history";
import { useKbAnswerStream } from "@/components/kb/search/use-kb-answer-stream";
import {
  SEARCH_TOP_K_LIMIT,
  SEARCH_TOP_K_STEP,
  useKbSearch,
} from "@/components/kb/workbench/kb-data";
import { EmptyState, ErrorState, PageHeader } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { KnowledgeBase } from "@/lib/types";

type SearchMode = "answer" | "sources";

// 中性示例问题(宿主按自己的知识域替换即可)
const EXAMPLE_QUESTIONS = [
  "这批资料里关于数据留存的约定是什么？",
  "对外交付物有哪些格式要求？",
  "最近一次流程调整改了哪些环节？",
  "有没有关于权限审批的明确条款？",
];

function SearchPageFallback() {
  return (
    <div className="nk-liquid-surface -m-3 min-h-[calc(100dvh-3.5rem)] px-3 py-6 sm:-m-6 sm:px-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <Skeleton className="mx-auto mt-16 h-8 w-64" />
        <Skeleton className="mx-auto h-16 w-full max-w-3xl rounded-2xl" />
        <Skeleton className="mx-auto h-32 w-full max-w-3xl rounded-2xl" />
      </div>
    </div>
  );
}

export default function KbSearchPage() {
  return (
    <Suspense fallback={<SearchPageFallback />}>
      <KbSearch />
    </Suspense>
  );
}

function KbSearch() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const submitted = searchParams.get("q") ?? "";
  const mode: SearchMode =
    searchParams.get("mode") === "sources" ? "sources" : "answer";
  const selectedKbIds = searchParams.getAll("kb");
  const [input, setInput] = useState(submitted);
  const [topK, setTopK] = useState(20);
  const composing = useRef(false);
  const history = useSearchHistory();

  const searchContext = `${submitted}\u0000${selectedKbIds.join(",")}\u0000${mode}`;
  const [lastSearchContext, setLastSearchContext] = useState(searchContext);
  if (lastSearchContext !== searchContext) {
    setLastSearchContext(searchContext);
    setTopK(20);
  }

  const [lastSubmitted, setLastSubmitted] = useState(submitted);
  if (lastSubmitted !== submitted) {
    setLastSubmitted(submitted);
    setInput(submitted);
  }

  const bases = useQuery({
    queryKey: ["kb-bases"],
    queryFn: () => api.get<KnowledgeBase[]>("/kb/bases"),
  });
  const scope = selectedKbIds.length > 0 ? selectedKbIds : undefined;
  const answer = useKbAnswerStream(mode === "answer" ? submitted : "", scope);
  const search = useKbSearch(mode === "sources" ? submitted : "", topK, scope);

  // 提问框自动增高:先复位为 auto 再量 scrollHeight(否则缩短内容时高度回不去),
  // 上限仍由 CSS max-h-32 兜住,超限后浏览器裁剪高度并出滚动条。
  // input 可能被示例问题/历史记录/URL 回退整段替换,统一在 effect 里量而不是 onChange。
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  function replaceParams(update: (params: URLSearchParams) => void) {
    const params = new URLSearchParams(searchParams.toString());
    update(params);
    const queryString = params.toString();
    router.replace(queryString ? `${pathname}?${queryString}` : pathname, {
      scroll: false,
    });
  }

  function submit(raw: string) {
    const query = raw.trim();
    if (query) history.push(query);
    replaceParams((params) => {
      if (query) params.set("q", query);
      else params.delete("q");
    });
  }

  function setMode(nextMode: SearchMode) {
    replaceParams((params) => {
      if (nextMode === "sources") params.set("mode", "sources");
      else params.delete("mode");
    });
  }

  function setKbScope(kbIds: string[]) {
    replaceParams((params) => {
      params.delete("kb");
      for (const kbId of kbIds) params.append("kb", kbId);
    });
  }

  function toggleKb(kbId: string) {
    setKbScope(
      selectedKbIds.includes(kbId)
        ? selectedKbIds.filter((id) => id !== kbId)
        : [...selectedKbIds, kbId],
    );
  }

  const scopeLabel =
    selectedKbIds.length === 0
      ? "全部知识库"
      : selectedKbIds.length === 1
        ? (bases.data?.find((kb) => kb.id === selectedKbIds[0])?.name ??
          "1 个知识库")
        : `${selectedKbIds.length} 个知识库`;
  const isFetching =
    mode === "answer" ? answer.status === "streaming" : search.isFetching;
  const hits = search.data ?? [];
  const showLanding = !submitted;

  return (
    <div className="nk-liquid-surface -m-3 min-h-[calc(100dvh-3.5rem)] px-3 py-4 sm:-m-6 sm:px-6 sm:py-6">
      <div className="mx-auto max-w-5xl">
      <PageHeader title="知识检索" description="直接提问，或定位内部知识原文" />

      <section
        className={cn(
          "mx-auto transition-all",
          showLanding ? "max-w-3xl pt-[8vh] sm:pt-[11vh]" : "max-w-4xl",
        )}
      >
        {showLanding && (
          <div className="mb-7 text-center">
            <div className="nk-brand-glass-mark mx-auto mb-5 flex size-16 items-center justify-center">
              <Image
                src="/images/nicekit.png"
                alt="NiceKit"
                width={52}
                height={52}
                priority
                className="size-13 rounded-[1rem] object-cover"
              />
            </div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              有问题，直接问
            </h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground sm:text-base">
              从组织内部资料中找到答案，并把每条依据标出来
            </p>
          </div>
        )}

        <div className="nk-liquid-card rounded-[1.6rem] p-2 transition-[box-shadow] focus-within:shadow-[0_22px_58px_rgb(15_23_42/0.12),inset_0_1px_0_rgb(255_255_255/0.86)] dark:focus-within:shadow-[0_22px_58px_rgb(0_0_0/0.28)]">
          <div
            className="mb-1 flex w-fit items-center rounded-xl bg-white/48 p-1 ring-1 ring-black/[0.035] dark:bg-white/[0.055] dark:ring-white/[0.06]"
            role="tablist"
            aria-label="知识检索方式"
          >
            <button
              type="button"
              role="tab"
              aria-selected={mode === "answer"}
              onClick={() => setMode("answer")}
              className={cn(
                "flex h-8 items-center gap-1.5 rounded-lg px-3 text-sm font-medium transition-colors",
                mode === "answer"
                  ? "bg-white/88 text-foreground shadow-sm ring-1 ring-black/[0.035] dark:bg-white/[0.12] dark:ring-white/[0.06]"
                  : "text-muted-foreground hover:bg-white/48 hover:text-foreground dark:hover:bg-white/[0.06]",
              )}
            >
              <Sparkles className="size-4" />
              AI 解答
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "sources"}
              onClick={() => setMode("sources")}
              className={cn(
                "flex h-8 items-center gap-1.5 rounded-lg px-3 text-sm font-medium transition-colors",
                mode === "sources"
                  ? "bg-white/88 text-foreground shadow-sm ring-1 ring-black/[0.035] dark:bg-white/[0.12] dark:ring-white/[0.06]"
                  : "text-muted-foreground hover:bg-white/48 hover:text-foreground dark:hover:bg-white/[0.06]",
              )}
            >
              <Search className="size-4" />
              查原文
            </button>
          </div>

          <form
            className="flex items-end gap-2 px-2 pb-1"
            onSubmit={(event) => {
              event.preventDefault();
              submit(input);
            }}
          >
            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onCompositionStart={() => (composing.current = true)}
              onCompositionEnd={() => (composing.current = false)}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !composing.current &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  submit(input);
                }
              }}
              aria-label={mode === "answer" ? "向知识库提问" : "检索知识原文"}
              placeholder={
                mode === "answer"
                  ? "例如：这批资料里关于数据留存的约定是什么？"
                  : "输入实体名称或文档关键词"
              }
              className="max-h-32 min-h-12 min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-1 py-3 text-base leading-6 outline-none placeholder:text-muted-foreground"
            />
            {input && !isFetching && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="清空"
                onClick={() => {
                  setInput("");
                  submit("");
                }}
              >
                <X />
              </Button>
            )}
            <Button
              type="submit"
              size="icon-lg"
              aria-label={mode === "answer" ? "获取解答" : "开始检索"}
              disabled={!input.trim() || isFetching}
              className="rounded-full shadow-[0_8px_22px_rgb(15_23_42/0.12)]"
            >
              {isFetching ? (
                <Loader2 className="animate-spin" />
              ) : mode === "answer" ? (
                <Send />
              ) : (
                <Search />
              )}
            </Button>
          </form>

          <div className="flex min-h-8 items-center gap-2 border-t border-white/58 px-2 pt-1 dark:border-white/[0.07]">
            {(bases.data?.length ?? 0) > 1 ? (
              <DropdownMenu>
                <DropdownMenuTrigger
                  render={
                    <Button
                      variant="ghost"
                      size="sm"
                      className="max-w-56 text-muted-foreground"
                    />
                  }
                >
                  <Library />
                  <span className="truncate">{scopeLabel}</span>
                  <ChevronDown />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-h-72">
                  <DropdownMenuGroup>
                    <DropdownMenuLabel>检索范围</DropdownMenuLabel>
                    <DropdownMenuCheckboxItem
                      checked={selectedKbIds.length === 0}
                      onCheckedChange={() => setKbScope([])}
                    >
                      全部知识库
                    </DropdownMenuCheckboxItem>
                    <DropdownMenuSeparator />
                    {bases.data?.map((kb) => (
                      <DropdownMenuCheckboxItem
                        key={kb.id}
                        checked={selectedKbIds.includes(kb.id)}
                        onCheckedChange={() => toggleKb(kb.id)}
                      >
                        {kb.name}
                      </DropdownMenuCheckboxItem>
                    ))}
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              <span className="flex items-center gap-1.5 px-2 text-xs text-muted-foreground">
                <ShieldCheck className="size-3.5" />
                仅检索你有权限查看的资料
              </span>
            )}
            <span className="ml-auto hidden items-center gap-1.5 pr-1 text-xs text-muted-foreground sm:flex">
              {mode === "answer" ? (
                <>
                  <Bot className="size-3.5" />
                  归纳答案并标注来源
                </>
              ) : (
                <>
                  <Search className="size-3.5" />
                  按相关性定位原文
                </>
              )}
            </span>
          </div>
        </div>

        {showLanding && (
          <div className="mt-5 space-y-5">
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLE_QUESTIONS.map((question) => (
                <button
                  key={question}
                  type="button"
                  onClick={() => {
                    setInput(question);
                    submit(question);
                  }}
                  className="rounded-full border border-white/62 bg-white/58 px-3 py-1.5 text-xs text-muted-foreground shadow-[inset_0_1px_0_rgb(255_255_255/0.72)] transition-colors hover:border-primary/30 hover:bg-white/86 hover:text-foreground dark:border-white/[0.08] dark:bg-white/[0.055] dark:hover:bg-white/[0.09]"
                >
                  {question}
                </button>
              ))}
            </div>
            <SearchHistory
              className="pt-1"
              onPick={(query) => {
                setInput(query);
                submit(query);
              }}
            />
          </div>
        )}
      </section>

      {!showLanding && (
        <div className="mx-auto mt-8 max-w-4xl pb-12">
          {mode === "answer" ? (
            // 流式状态机:no_evidence 走空态(带一键切"查原文"),其余交给面板
            // (streaming 的进行中占位 / 增量正文、error 的提示与重试都在面板内)
            answer.status === "no_evidence" ? (
              <KnowledgeAnswerEmptyState
                onShowSources={() => setMode("sources")}
              />
            ) : (
              <KnowledgeAnswerPanel
                status={answer.status}
                answerText={answer.answerText}
                sources={answer.sources}
                errorMessage={answer.errorMessage}
                onRetry={answer.retry}
                query={submitted}
              />
            )
          ) : search.error ? (
            <ErrorState error={search.error} onRetry={() => search.refetch()} />
          ) : search.isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-28 rounded-xl" />
              <Skeleton className="h-28 rounded-xl" />
              <Skeleton className="h-28 rounded-xl" />
            </div>
          ) : hits.length > 0 ? (
            <FrontlineSearchResults
              hits={hits}
              tokens={tokenize(submitted)}
              canLoadMore={hits.length >= topK && topK < SEARCH_TOP_K_LIMIT}
              atLimit={
                topK >= SEARCH_TOP_K_LIMIT && hits.length >= SEARCH_TOP_K_LIMIT
              }
              loadingMore={search.isFetching}
              onLoadMore={() =>
                setTopK((current) =>
                  Math.min(current + SEARCH_TOP_K_STEP, SEARCH_TOP_K_LIMIT),
                )
              }
            />
          ) : (
            <EmptyState
              icon={SearchX}
              title="没有找到相关资料"
              description="试试更具体的实体名称、字段值或文档关键词。"
              className="nk-liquid-card rounded-2xl py-16"
            />
          )}
        </div>
      )}
      </div>
    </div>
  );
}
