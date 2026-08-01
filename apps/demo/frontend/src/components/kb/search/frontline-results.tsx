"use client";

import {
  ArrowUpRight,
  BookOpenText,
  CheckCircle2,
  CircleAlert,
  FileText,
  Landmark,
  Loader2,
  MapPin,
  Search,
  SearchX,
  Sparkles,
} from "lucide-react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AnswerFeedback } from "@/components/kb/search/answer-feedback";
import { sourceTarget } from "@/components/kb/search/deep-link";
import {
  citationLocation,
  frontlineHitSummary,
  frontlineHitTitle,
  hitKindLabel,
  linkifyAnswerCitations,
  sourceLayerLabel,
} from "@/components/kb/search/frontline-result-utils";
import { Highlight } from "@/components/kb/search/highlight";
import type { KbAnswerStreamStatus } from "@/components/kb/search/use-kb-answer-stream";
import { SEARCH_TOP_K_LIMIT } from "@/components/kb/workbench/kb-data";
import { EmptyState, ToneBadge } from "@/components/shared";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type { KnowledgeAnswerSource, SearchHit } from "@/lib/types";

const KIND_ICONS = {
  destination: Landmark,
  poi: MapPin,
  chunk: FileText,
};

function ResultIcon({ kind }: { kind: string }) {
  const Icon = KIND_ICONS[kind as keyof typeof KIND_ICONS] ?? BookOpenText;
  return (
    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-foreground">
      <Icon className="size-4.5" />
    </span>
  );
}

function SourceMeta({ hit }: { hit: SearchHit }) {
  const location = citationLocation(hit.citation);
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>{sourceLayerLabel(hit.layer)}</span>
      <span aria-hidden="true">·</span>
      <span>{hitKindLabel(hit.kind)}</span>
      {location && (
        <>
          <span aria-hidden="true">·</span>
          <span>{location}</span>
        </>
      )}
      {hit.data.stale === true && (
        <ToneBadge tone="warning">可能过期</ToneBadge>
      )}
    </div>
  );
}

function OriginalLink({ hit }: { hit: SearchHit }) {
  const target = sourceTarget(hit);
  if (!target) return null;
  return (
    <Link
      href={target.href}
      className={cn(
        buttonVariants({ variant: "ghost", size: "sm" }),
        "text-primary",
      )}
    >
      查看原文
      <ArrowUpRight />
    </Link>
  );
}

export function FrontlineSearchResults({
  hits,
  tokens,
  onLoadMore,
  canLoadMore,
  loadingMore,
  atLimit,
}: {
  hits: SearchHit[];
  tokens: string[];
  onLoadMore: () => void;
  canLoadMore: boolean;
  loadingMore: boolean;
  /** 已取满单次检索上限,不会再有「查看更多」 */
  atLimit: boolean;
}) {
  return (
    <section className="space-y-3" aria-label="原文检索结果">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold">最相关的资料</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            已按相关性整理 {hits.length} 条结果
          </p>
        </div>
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        {hits.map((hit, index) => (
          <article
            key={
              (typeof hit.data.id === "string" && hit.data.id) ||
              `${hit.source}-${index}`
            }
            className="flex gap-3 border-b border-border p-4 last:border-b-0 hover:bg-muted/30"
          >
            <ResultIcon kind={hit.kind} />
            <div className="min-w-0 flex-1">
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <h3 className="truncate text-sm font-semibold">
                    {frontlineHitTitle(hit)}
                  </h3>
                  <div className="mt-1">
                    <SourceMeta hit={hit} />
                  </div>
                </div>
                <OriginalLink hit={hit} />
              </div>
              <p className="mt-2 line-clamp-3 text-sm leading-6 text-foreground/80">
                <Highlight text={frontlineHitSummary(hit)} tokens={tokens} />
              </p>
            </div>
          </article>
        ))}
      </div>

      {canLoadMore && (
        <div className="flex justify-center pt-2">
          <Button variant="outline" disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "正在加载…" : "查看更多资料"}
          </Button>
        </div>
      )}
      {/* 到达检索上限时按钮会消失,不解释一句用户会以为"就这些了" */}
      {atLimit && (
        <p className="pt-2 text-center text-xs text-muted-foreground">
          已达单次检索上限 {SEARCH_TOP_K_LIMIT}{" "}
          条,可收窄关键词或知识库范围继续查找
        </p>
      )}
    </section>
  );
}

// 注意:来源编号字段名为 ref,与 React 保留 prop 同名,必须整包传入而不是展开成
// JSX props(React 19 虽把 ref 透传给函数组件,但语义仍是引用而非业务数据)。
function AnswerSource({ source }: { source: KnowledgeAnswerSource }) {
  const { ref, hit } = source;
  return (
    <article
      id={`source-${ref}`}
      className="scroll-mt-20 rounded-xl border border-border bg-card p-4 transition-colors target:border-primary/60 target:bg-accent/30"
    >
      <div className="flex items-start gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-semibold text-primary-foreground">
          {ref}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 flex-1 truncate text-sm font-semibold">
              {frontlineHitTitle(hit)}
            </h3>
            <OriginalLink hit={hit} />
          </div>
          <div className="mt-1">
            <SourceMeta hit={hit} />
          </div>
          <p className="mt-2 line-clamp-3 text-sm leading-6 text-muted-foreground">
            {hit.citation?.quote_text || frontlineHitSummary(hit)}
          </p>
        </div>
      </div>
    </article>
  );
}

/**
 * 正文里的引用编号 [n]:点击仍是锚点跳到来源卡,hover 时浮层预览对应来源
 * (标题 + 引文摘录 + 位置),省去"跳下去再滚回来"的往返。
 * 用现有 base-ui Tooltip 原语(与 wiki wikilink 提示一致),不引新依赖;
 * 沿用默认深色气泡样式,箭头/动画/定位全部免费拿到。
 */
function CitationLink({
  href,
  source,
  children,
}: {
  href?: string;
  source?: KnowledgeAnswerSource;
  children: React.ReactNode;
}) {
  const anchor = (
    <a
      href={href}
      className="mx-0.5 inline-flex min-w-5 items-center justify-center rounded bg-primary/10 px-1 text-xs font-semibold text-primary no-underline hover:bg-primary/20"
    >
      {children}
    </a>
  );
  if (!source) return anchor;
  const location = citationLocation(source.hit.citation);
  return (
    <Tooltip>
      <TooltipTrigger render={anchor} />
      <TooltipContent className="block w-72 space-y-1.5 px-3.5 py-3 text-left">
        <p className="text-xs font-semibold">
          [{source.ref}] {frontlineHitTitle(source.hit)}
        </p>
        <p className="line-clamp-4 text-xs leading-5 opacity-80">
          {source.hit.citation?.quote_text || frontlineHitSummary(source.hit)}
        </p>
        <p className="text-[11px] opacity-60">
          {[sourceLayerLabel(source.hit.layer), location]
            .filter(Boolean)
            .join(" · ")}
        </p>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * AI 解答面板(流式版):消费 useKbAnswerStream 的状态而不是一次性响应。
 * sources 帧一到就先渲染"答案依据"区(用户在等正文时即可开始核对来源),
 * 正文随 delta 增量渲染;done 后 hook 已把 sources 裁剪为实际引用子集。
 */
export function KnowledgeAnswerPanel({
  status,
  answerText,
  sources,
  errorMessage,
  onRetry,
  query,
}: {
  status: KbAnswerStreamStatus;
  answerText: string;
  sources: KnowledgeAnswerSource[];
  errorMessage?: string | null;
  onRetry?: () => void;
  /** 提供时在答案完成后展示赞/踩反馈条(反馈快照需要原始问题) */
  query?: string;
}) {
  // idle:query 尚未提交;no_evidence 由页面层渲染 KnowledgeAnswerEmptyState
  if (status === "idle" || status === "no_evidence") return null;

  const streaming = status === "streaming";
  // 流式期间 done 未到,引用锚点用当前候选全集判定;done 后 sources 已是裁剪结果
  const refs = sources.map((source) => source.ref);
  const linkedAnswer = linkifyAnswerCitations(answerText, refs);
  const sourceByRef = new Map(sources.map((source) => [source.ref, source]));

  // 检索尚未返回(sources 帧未到)且没有任何正文:整块进行中占位
  if (streaming && sources.length === 0 && !answerText) {
    return (
      <div className="space-y-3" aria-label="AI 解答生成中">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          正在检索并核对内部资料…
        </div>
        <Skeleton className="h-48 rounded-2xl" />
        <div className="grid gap-3 lg:grid-cols-2">
          <Skeleton className="h-32 rounded-xl" />
          <Skeleton className="h-32 rounded-xl" />
        </div>
      </div>
    );
  }

  return (
    <TooltipProvider delay={150}>
      <div className="space-y-6">
        <article className="overflow-hidden rounded-2xl border border-primary/15 bg-card shadow-sm">
          <div className="flex flex-wrap items-center gap-2 border-b border-border bg-accent/35 px-5 py-3">
            <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <Sparkles className="size-4" />
            </span>
            <span className="text-sm font-semibold">AI 解答</span>
            {streaming ? (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                正在生成，来源已就绪
              </span>
            ) : status === "success" ? (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <CheckCircle2 className="size-3.5 text-success" />
                基于 {sources.length} 个可核验来源
              </span>
            ) : null}
            <span className="ml-auto text-xs text-muted-foreground">
              只读检索，不会修改业务数据
            </span>
          </div>
          <div className="px-5 py-5 sm:px-7 sm:py-6">
            {status === "error" && (
              <div
                role="alert"
                className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm text-destructive"
              >
                <CircleAlert className="size-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  {errorMessage || "生成失败，请稍后重试。"}
                </span>
                {onRetry && (
                  <Button variant="outline" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                )}
              </div>
            )}
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href, children }) => {
                  const refMatch = href?.match(/^#source-(\d+)$/);
                  return (
                    <CitationLink
                      href={href}
                      source={
                        refMatch
                          ? sourceByRef.get(Number(refMatch[1]))
                          : undefined
                      }
                    >
                      {children}
                    </CitationLink>
                  );
                },
              }}
            >
              {linkedAnswer}
            </ReactMarkdown>
            {streaming && (
              // 打字机式追尾光标:提示"还在写",restart 清空正文后它也还在
              <span
                aria-hidden="true"
                className="ml-0.5 inline-block h-4 w-2 animate-pulse rounded-sm bg-primary/60 align-text-bottom"
              />
            )}
          </div>
          {status === "success" && query && answerText && (
            <div className="border-t border-border px-5 py-3 sm:px-7">
              <AnswerFeedback
                query={query}
                answerText={answerText}
                sources={sources}
              />
            </div>
          )}
        </article>

        {sources.length > 0 && (
          <section className="space-y-3" aria-label="AI 解答来源">
            <div>
              <h2 className="text-base font-semibold">答案依据</h2>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {streaming
                  ? "已检索到的候选依据，答案完成后会标出实际引用"
                  : "点击编号或“查看原文”核对上下文"}
              </p>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              {sources.map((source) => (
                <AnswerSource key={source.ref} source={source} />
              ))}
            </div>
          </section>
        )}
      </div>
    </TooltipProvider>
  );
}

/**
 * AI 解答无证据空态:不编造答案,同时给一条最短出路——一键切到"查原文"
 * 直接换关键词检索,而不是让用户自己找 tab 在哪。
 */
export function KnowledgeAnswerEmptyState({
  onShowSources,
}: {
  onShowSources: () => void;
}) {
  return (
    <EmptyState
      icon={SearchX}
      title="现有资料不足以给出可靠答案"
      description="没有找到带原文依据的内容。可以换个问法，或直接检索原文关键词。"
      action={
        <Button variant="outline" onClick={onShowSources}>
          <Search />
          改用查原文检索
        </Button>
      }
      className="rounded-2xl border border-border bg-card py-16"
    />
  );
}
