"use client";

import {
  Check,
  ChevronRight,
  CircleAlert,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Users,
} from "lucide-react";
import { useState } from "react";
import Markdown from "react-markdown";
import type { ToolRun } from "@/lib/agent-events";
import { toolLabel, type ReviewerOverride } from "@/lib/chat";
import { cn } from "@/lib/utils";
import { BusinessResult } from "./business-result";
import {
  AgentImageInspection,
  AgentKnowledgeEvidence,
} from "./knowledge-evidence";
import { SelectedKnowledgeMediaSection } from "@/components/projects/selected-knowledge-media";
import { Badge } from "@/components/ui/badge";
import type { SelectedKnowledgeMedia } from "@/lib/types";
import {
  businessResultKind,
  isRecord,
  readNumber,
  readRecordArray,
  readString,
  readStringArray,
  shouldExpandTool,
  toolResultSummary,
} from "./tool-presentation";

const LAYER_LABELS: Record<string, string> = {
  tenant: "本社",
  shared: "共享",
  platform: "平台",
  ota: "OTA",
  supplier: "供应商",
};

function MetaBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full bg-muted/70 px-2 py-0.5 text-[10px] text-muted-foreground">
      {children}
    </span>
  );
}

function EvidenceBadges({ output }: { output: unknown }) {
  if (!isRecord(output)) return null;
  const badges: React.ReactNode[] = [];
  const item = isRecord(output.item) ? output.item : undefined;
  if (item?.price_source) {
    badges.push(<MetaBadge key="src">来源 {String(item.price_source)}</MetaBadge>);
    const ref = isRecord(item.source_ref) ? item.source_ref : undefined;
    if (ref?.name)
      badges.push(
        <MetaBadge key="ref">
          {LAYER_LABELS[String(ref.layer)] ?? String(ref.layer ?? "")} ·{" "}
          {String(ref.name)}
        </MetaBadge>,
      );
    if (typeof item.confidence === "number")
      badges.push(
        <MetaBadge key="conf">
          置信 {(item.confidence * 100).toFixed(0)}%
        </MetaBadge>,
      );
  }
  if (Array.isArray(output.hits)) {
    for (const [index, raw] of output.hits.slice(0, 3).entries()) {
      if (!isRecord(raw)) continue;
      badges.push(
        <MetaBadge key={`hit-${index}`}>
          {LAYER_LABELS[String(raw.layer)] ?? String(raw.layer ?? "")} ·{" "}
          {String(raw.kind ?? "")} · 置信{" "}
          {typeof raw.confidence === "number"
            ? `${(raw.confidence * 100).toFixed(0)}%`
            : "—"}
        </MetaBadge>,
      );
    }
  }
  return badges.length ? (
    <div className="flex flex-wrap gap-1">{badges}</div>
  ) : null;
}

type SourceTier = "official" | "media" | "ugc" | "unknown";

const TIER_STYLES: Record<
  Exclude<SourceTier, "unknown">,
  { label: string; className: string }
> = {
  official: {
    label: "官方",
    className:
      "bg-emerald-500/10 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400",
  },
  media: {
    label: "媒体",
    className:
      "bg-sky-500/10 text-sky-700 dark:bg-sky-500/15 dark:text-sky-400",
  },
  ugc: { label: "社区", className: "bg-muted text-muted-foreground" },
};

const DAY_MS = 86_400_000;
const SOURCE_PREVIEW_COUNT = 8;
const FETCH_PREVIEW_LINES = 3;
// 专员结论是给主 agent 的中间产物,默认只露头几行,免得盖过主回答
const SUBAGENT_PREVIEW_LINES = 4;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}/u;

interface WebSource {
  ref: number;
  title: string;
  url: string;
  domain: string;
  snippet?: string;
  publishedAt?: string;
  tier: SourceTier;
}

type FetchedPageStatus = "ok" | "empty" | "blocked" | "unavailable";

interface FetchedPage extends WebSource {
  status: FetchedPageStatus;
  content?: string;
  error?: string;
  truncated: boolean;
}

function readTier(value: unknown): SourceTier {
  const tier = readString(value, "source_tier");
  return tier === "official" || tier === "media" || tier === "ugc"
    ? tier
    : "unknown";
}

function readDomain(url: string, supplied?: string): string {
  if (supplied) return supplied;
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

interface Freshness {
  label: string;
  /** 超过一年的资料在旅游政策场景里通常已失效,需要显式提示。 */
  stale: boolean;
}

function freshness(value: string | undefined): Freshness | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return { label: value, stale: false };
  const date = ISO_DATE.test(value)
    ? value.slice(0, 10)
    : new Date(timestamp).toISOString().slice(0, 10);
  const days = Math.floor((Date.now() - timestamp) / DAY_MS);
  if (days <= 0) return { label: days === 0 ? "今天" : date, stale: false };
  if (days < 30) return { label: `${days} 天前`, stale: false };
  if (days < 365)
    return { label: `${Math.floor(days / 30)} 个月前`, stale: false };
  return { label: date, stale: true };
}

function webSource(value: unknown, index: number): WebSource | null {
  const url = readString(value, "url");
  if (!url) return null;
  return {
    ref: readNumber(value, "ref") ?? index + 1,
    title: readString(value, "title") ?? url,
    url,
    domain: readDomain(url, readString(value, "domain")),
    snippet: readString(value, "snippet"),
    publishedAt: readString(value, "published_at"),
    tier: readTier(value),
  };
}

function fetchedPages(value: unknown): FetchedPage[] {
  return readRecordArray(value).flatMap((raw, index) => {
    const finalUrl = readString(raw, "final_url");
    const source = webSource(
      finalUrl ? { ...raw, url: finalUrl } : raw,
      index,
    );
    if (!source) return [];
    const status = readString(raw, "status");
    return [
      {
        ...source,
        status:
          status === "empty" || status === "blocked" || status === "unavailable"
            ? status
            : "ok",
        content: readString(raw, "content"),
        error: readString(raw, "error"),
        truncated: raw.truncated === true,
      },
    ];
  });
}

function SourceFavicon({ domain }: { domain: string }) {
  const [broken, setBroken] = useState(false);
  if (!domain || broken) return null;
  return (
    // 外部站点图标无法走 next/image 的远端白名单,失败时静默隐藏。
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={`https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`}
      alt=""
      width={14}
      height={14}
      loading="lazy"
      onError={() => setBroken(true)}
      className="mt-0.5 size-3.5 shrink-0 rounded-[3px]"
    />
  );
}

function TierBadge({ tier }: { tier: SourceTier }) {
  if (tier === "unknown") return null;
  const style = TIER_STYLES[tier];
  return (
    <Badge
      variant="secondary"
      className={cn("h-4 px-1.5 text-[10px]", style.className)}
    >
      {style.label}
    </Badge>
  );
}

function RefBadge({ value }: { value: number }) {
  return (
    <span className="mt-0.5 shrink-0 rounded bg-muted px-1 font-mono text-[10px] leading-4 text-muted-foreground tabular-nums">
      [{value}]
    </span>
  );
}

function SourceMeta({
  domain,
  publishedAt,
}: {
  domain: string;
  publishedAt?: string;
}) {
  const time = freshness(publishedAt);
  if (!domain && !time) return null;
  return (
    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted-foreground">
      {domain && <span className="truncate">{domain}</span>}
      {time && <span>· {time.label}</span>}
      {time?.stale && (
        <span className="text-amber-700 dark:text-amber-400">
          · 信息可能过时
        </span>
      )}
    </div>
  );
}

function SourceHeadline({
  source,
  children,
}: {
  source: WebSource;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-1.5">
      <RefBadge value={source.ref} />
      <SourceFavicon domain={source.domain} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <a
            href={source.url}
            target="_blank"
            rel="noreferrer"
            className="font-medium text-primary hover:underline"
          >
            {source.title}
          </a>
          <TierBadge tier={source.tier} />
        </div>
        <SourceMeta domain={source.domain} publishedAt={source.publishedAt} />
        {children}
      </div>
    </div>
  );
}

function WebSearchResults({ output }: { output: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const sources = readRecordArray(output.results).flatMap((raw, index) => {
    const source = webSource(raw, index);
    return source ? [source] : [];
  });
  if (!sources.length) {
    return <div className="text-xs text-muted-foreground">未搜到相关网页</div>;
  }
  const provider = readString(output, "provider");
  const count = readNumber(output, "count") ?? sources.length;
  const queries = readStringArray(output.queries);
  const visible = expanded ? sources : sources.slice(0, SOURCE_PREVIEW_COUNT);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
        <span>
          {provider ? `${provider} · ` : ""}
          {count} 条来源
        </span>
        {output.cached === true && (
          <Badge variant="secondary" className="h-4 px-1.5 text-[10px]">
            缓存
          </Badge>
        )}
      </div>
      {queries.length > 1 && (
        <ul className="space-y-0.5 text-[11px] leading-4 text-muted-foreground">
          {queries.map((query, index) => (
            <li key={`${index}-${query}`} className="truncate">
              检索式 {index + 1}：{query}
            </li>
          ))}
        </ul>
      )}
      <ol className="space-y-2">
        {visible.map((source, index) => (
          <li
            key={`${index}-${source.url}`}
            id={`cite-${source.ref}`}
            data-ref={source.ref}
            className="scroll-mt-20 rounded-lg text-xs"
          >
            <SourceHeadline source={source}>
              {source.snippet && (
                <p className="mt-0.5 line-clamp-2 text-muted-foreground">
                  {source.snippet}
                </p>
              )}
            </SourceHeadline>
          </li>
        ))}
      </ol>
      {sources.length > SOURCE_PREVIEW_COUNT && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="rounded-md px-1 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          {expanded ? "收起来源" : `显示全部 ${sources.length} 条`}
        </button>
      )}
    </div>
  );
}

function Result({ value }: { value: unknown }) {
  if (typeof value === "string") {
    return (
      <div className="prose prose-sm dark:prose-invert max-w-none text-xs">
        <Markdown>{value}</Markdown>
      </div>
    );
  }
  return (
    <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function FetchedPageBody({ page }: { page: FetchedPage }) {
  const [expanded, setExpanded] = useState(false);
  if (page.status === "blocked") {
    return (
      <p className="mt-1 text-[11px] leading-4 text-amber-700 dark:text-amber-400">
        已拦截：{page.error ?? "目标站点不允许抓取"}
      </p>
    );
  }
  if (page.status === "unavailable") {
    return (
      <p className="mt-1 text-[11px] leading-4 text-rose-700 dark:text-rose-400">
        读取失败：{page.error ?? "网页暂时无法访问"}
      </p>
    );
  }
  if (page.status === "empty" || !page.content) {
    return (
      <p className="mt-1 text-[11px] text-muted-foreground">
        该页面未提取到正文
      </p>
    );
  }
  const lines = page.content.split("\n").filter((line) => line.trim());
  const collapsible = lines.length > FETCH_PREVIEW_LINES;
  const preview = lines.slice(0, FETCH_PREVIEW_LINES).join("\n");
  return (
    <div className="mt-1.5 space-y-1">
      <div className="max-h-80 overflow-auto rounded-lg bg-muted/45 px-2.5 py-2">
        <Result value={expanded || !collapsible ? page.content : preview} />
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        {collapsible && (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="rounded-md px-1 py-0.5 hover:bg-muted hover:text-foreground"
          >
            {expanded ? "收起正文" : "展开全文"}
          </button>
        )}
        {page.truncated && <span>正文已截断</span>}
      </div>
    </div>
  );
}

function WebFetchResults({ output }: { output: Record<string, unknown> }) {
  const pages = fetchedPages(output.pages);
  if (!pages.length) {
    return <div className="text-xs text-muted-foreground">未读取到网页</div>;
  }
  return (
    <ol className="space-y-2.5">
      {pages.map((page, index) => (
        <li
          key={`${index}-${page.url}`}
          id={`cite-${page.ref}`}
          data-ref={page.ref}
          className="scroll-mt-20 rounded-lg text-xs"
        >
          <SourceHeadline source={page}>
            <FetchedPageBody page={page} />
          </SourceHeadline>
        </li>
      ))}
    </ol>
  );
}

/** 委派卡片:专员名 + 任务摘要 + 结论(默认折叠,主 agent 的转述才是正文)。 */
function SubAgentDelegation({ run }: { run: ToolRun }) {
  const [expanded, setExpanded] = useState(false);
  const output = isRecord(run.output) ? run.output : undefined;
  const agent =
    readString(output, "agent") ?? readString(run.input, "target_agent");
  const task = readString(run.input, "task");
  const error = readString(output, "error");
  const answer = readString(output, "output");
  const turns = readNumber(output, "turns");
  const lines = (answer ?? "").split("\n").filter((line) => line.trim());
  const collapsible = lines.length > SUBAGENT_PREVIEW_LINES;
  const preview = lines.slice(0, SUBAGENT_PREVIEW_LINES).join("\n");
  return (
    <div className="space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="secondary" className="h-4 gap-1 px-1.5 text-[10px]">
          <Users className="size-3" />
          {agent ?? "专员"}
        </Badge>
        {turns !== undefined && (
          <span className="text-[11px] text-muted-foreground">
            独立执行 {turns} 轮
          </span>
        )}
      </div>
      {task && (
        <p className="line-clamp-3 rounded-lg bg-muted/45 px-2.5 py-1.5 leading-5 text-muted-foreground">
          {task}
        </p>
      )}
      {error ? (
        <p className="leading-5 text-amber-700 dark:text-amber-400">{error}</p>
      ) : answer ? (
        <div className="space-y-1">
          <div className="max-h-80 overflow-auto rounded-lg bg-muted/45 px-2.5 py-2">
            <Result value={expanded || !collapsible ? answer : preview} />
          </div>
          {collapsible && (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="rounded-md px-1 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              {expanded ? "收起专员结论" : "展开专员结论"}
            </button>
          )}
        </div>
      ) : (
        <p className="text-muted-foreground">专员未产出结论</p>
      )}
    </div>
  );
}

function FriendlyOutput({
  run,
  projectId,
}: {
  run: ToolRun;
  projectId?: string;
}) {
  if (run.name === "Agent") {
    return <SubAgentDelegation run={run} />;
  }
  if (businessResultKind(run.name, run.output)) {
    return (
      <BusinessResult
        name={run.name}
        output={run.output}
        projectId={projectId}
      />
    );
  }
  if (run.status === "ok" && isRecord(run.output)) {
    if (run.name === "kb_search") {
      return <AgentKnowledgeEvidence output={run.output} />;
    }
    if (run.name === "kb_image_inspect") {
      return <AgentImageInspection output={run.output} />;
    }
    if (run.name === "business_media_select") {
      return (
        <SelectedKnowledgeMediaSection
          items={
            Array.isArray(run.output.selected_kb_media)
              ? (run.output.selected_kb_media as SelectedKnowledgeMedia[])
              : []
          }
          emptyLabel="已清除业务记录中的知识来源图片。"
        />
      );
    }
    if (run.name === "web_search" && Array.isArray(run.output.results)) {
      return <WebSearchResults output={run.output} />;
    }
    if (run.name === "web_fetch" && Array.isArray(run.output.pages)) {
      return <WebFetchResults output={run.output} />;
    }
  }
  if (typeof run.output === "string") return <Result value={run.output} />;
  return (
    <div className="space-y-2">
      <EvidenceBadges output={run.output} />
      <p className="text-xs text-muted-foreground">
        操作已完成，完整输入与输出可在高级详情中查看。
      </p>
    </div>
  );
}

function reviewerSummary(run: ToolRun): string | null {
  const reviewer = run.permission?.reviewer;
  const override = reviewer?.override;
  if (override?.status === "completed")
    return override.ok
      ? "一次性覆盖已执行"
      : "一次性覆盖未执行";
  if (override?.status === "consumed") return "一次性覆盖已提交";
  if (reviewer?.circuitBreaker) return "智能审批已触发熔断";
  if (reviewer?.decision === "deny")
    return override?.status === "available"
      ? "Reviewer 未批准 · 可仅此次继续"
      : "Reviewer 未批准";
  if (reviewer?.decision === "escalate") return "Reviewer 已转交你确认";
  if (reviewer?.requested && !reviewer.decision) return "Reviewer 审批中";

  const approval = run.permission?.approval;
  if (!approval) return null;
  if (approval.bundleStatus !== "completed")
    return approval.itemStatus === "allowed"
      ? "等待本组审批完成"
      : "等待你的决定";
  if (approval.itemStatus === "denied") return "已拒绝执行";
  if (approval.decisionScope === "session_tool") return "已允许并记住到本会话";
  if (approval.itemStatus === "approved") return "已允许此次执行";
  return "本组审批已完成";
}

function ReviewerDetails({
  run,
  busy,
  onOverride,
}: {
  run: ToolRun;
  busy: boolean;
  onOverride?: (override: ReviewerOverride) => void;
}) {
  const reviewer = run.permission?.reviewer;
  if (!reviewer) return null;
  const override = reviewer.override;
  const disruptive =
    reviewer.decision === "deny" ||
    reviewer.decision === "escalate" ||
    reviewer.circuitBreaker ||
    !!override;
  const title = reviewer.circuitBreaker
    ? "智能审批已停止继续尝试"
    : reviewer.decision === "approve"
      ? "Reviewer 已批准"
      : reviewer.decision === "deny"
        ? "Reviewer 未批准"
        : reviewer.decision === "escalate"
          ? "Reviewer 已转交用户确认"
          : "Reviewer 正在审批";

  return (
    <div
      className={cn(
        "rounded-xl px-3 py-2.5 text-xs ring-1 ring-inset",
        disruptive
          ? "bg-amber-500/[0.06] text-amber-950 ring-amber-500/20 dark:text-amber-100"
          : "bg-violet-500/[0.045] text-foreground/80 ring-violet-500/15",
      )}
    >
      <div className="flex items-center gap-2 font-medium">
        {reviewer.decision === "approve" ? (
          <ShieldCheck className="size-3.5" />
        ) : reviewer.requested && !reviewer.decision ? (
          <Sparkles className="size-3.5" />
        ) : (
          <ShieldAlert className="size-3.5" />
        )}
        {title}
      </div>
      {reviewer.rationale && (
        <p className="mt-1.5 leading-5 text-current/75">
          {reviewer.rationale}
        </p>
      )}
      {reviewer.riskFlags.length > 0 && (
        <p className="mt-1.5 text-[11px] leading-4 text-current/65">
          风险标记：{reviewer.riskFlags.join("、")}
        </p>
      )}
      {reviewer.circuitBreaker && reviewer.denialCount !== undefined && (
        <p className="mt-1.5 text-[11px] text-current/65">
          本轮连续未通过 {reviewer.denialCount} 次
        </p>
      )}
      {override?.status === "available" && override.override && onOverride && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onOverride(override.override as ReviewerOverride)}
            className="rounded-lg bg-amber-600 px-2.5 py-1.5 text-[11px] font-medium text-white outline-none hover:bg-amber-700 focus-visible:ring-2 focus-visible:ring-amber-500/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            仅此次仍要执行
          </button>
          <span className="text-[10px] text-current/60">
            仅覆盖这一项准确操作，不会创建长期授权
          </span>
        </div>
      )}
      {override?.status === "consumed" && (
        <p className="mt-2 text-[11px] text-current/65">一次性覆盖已提交</p>
      )}
      {override?.status === "completed" && (
        <p className="mt-2 text-[11px] text-current/65">
          {override.ok ? "一次性覆盖执行完成" : "一次性覆盖未执行"}
          {override.reasonCode ? ` · ${override.reasonCode}` : ""}
        </p>
      )}
    </div>
  );
}

function AdvancedDetails({ run }: { run: ToolRun }) {
  if (!run.input && run.output === undefined) return null;
  return (
    <details className="group/details">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 rounded-md px-1.5 py-1 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground">
        <SlidersHorizontal className="size-3.5" />
        高级详情
      </summary>
      <div className="mt-1 space-y-3 rounded-xl bg-muted/45 p-3">
        {run.input && (
          <div>
            <p className="mb-1 text-[11px] font-medium text-muted-foreground">
              输入
            </p>
            <Result value={run.input} />
          </div>
        )}
        {run.output !== undefined && (
          <div>
            <p className="mb-1 text-[11px] font-medium text-muted-foreground">
              输出
            </p>
            <Result value={run.output} />
          </div>
        )}
      </div>
    </details>
  );
}

export function ToolRunCard({
  run,
  projectId,
  showBusinessResult = true,
  showStatusIcon = true,
  busy = false,
  onOverride,
}: {
  run: ToolRun;
  projectId?: string;
  showBusinessResult?: boolean;
  showStatusIcon?: boolean;
  busy?: boolean;
  onOverride?: (override: ReviewerOverride) => void;
}) {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const contextualResult =
    run.name === "image_generate" ||
    businessResultKind(run.name, run.output) !== null;
  const defaultOpen =
    run.status === "running" ||
    run.status === "failed" ||
    run.permission?.reviewer?.decision === "deny" ||
    run.permission?.reviewer?.decision === "escalate" ||
    run.permission?.reviewer?.circuitBreaker === true ||
    run.permission?.reviewer?.override?.status === "available" ||
    (showBusinessResult && shouldExpandTool(run.name, run.status, run.output));
  const open = manualOpen ?? defaultOpen;
  const expandable =
    run.progress.length > 0 ||
    !!run.input ||
    run.output !== undefined ||
    !!run.permission?.reviewer;
  const Icon =
    run.status === "running"
      ? Loader2
      : run.status === "waiting"
        ? ShieldAlert
      : run.status === "ok"
        ? Check
        : CircleAlert;
  const permissionSummary = reviewerSummary(run);
  const summary =
    permissionSummary ??
    (run.status === "running"
      ? (run.progress.at(-1) ?? "执行中…")
      : run.status === "waiting"
        ? "等待审批"
      : run.status === "failed"
        ? "执行失败"
        : toolResultSummary(run.name, run.output));

  return (
    <div
      className={cn(
        "rounded-lg transition-colors",
        run.status === "failed" && "bg-destructive/[0.035]",
      )}
    >
      <button
        type="button"
        disabled={!expandable}
        aria-expanded={open}
        onClick={() => expandable && setManualOpen(!open)}
        className="flex min-h-8 w-full items-center gap-2 rounded-lg px-1.5 text-left text-sm transition-colors hover:bg-muted/55 disabled:cursor-default disabled:hover:bg-transparent"
      >
        {showStatusIcon && (
          <span
            className={cn(
              "flex size-4 shrink-0 items-center justify-center",
              run.status === "running" && "text-foreground/70",
              run.status === "waiting" && "text-amber-700 dark:text-amber-300",
              run.status === "ok" && "text-muted-foreground",
              run.status === "failed" && "text-destructive",
            )}
          >
            <Icon
              className={cn(
                "size-3.5",
                run.status === "running" && "animate-spin",
              )}
            />
          </span>
        )}
        <span className="shrink-0 text-xs font-medium text-foreground/85">
          {toolLabel(run.name)}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
          {summary}
        </span>
        {expandable && (
          <ChevronRight
            className={cn(
              "size-4 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
        )}
      </button>
      {open && expandable && (
        <div
          className={cn(
            "mr-2 mb-2 space-y-3 pt-1",
            showStatusIcon ? "ml-7" : "ml-1",
          )}
        >
          {run.progress.length > 0 && (
            <ol className="space-y-1 text-xs text-muted-foreground">
              {run.progress.slice(-4).map((item, index) => (
                <li key={`${index}-${item}`} className="flex gap-2">
                  <span className="mt-2 size-1 shrink-0 rounded-full bg-primary/50" />
                  <span>{item}</span>
                </li>
              ))}
            </ol>
          )}
          <ReviewerDetails run={run} busy={busy} onOverride={onOverride} />
          {run.status === "failed" && run.output !== undefined ? (
            <div className="rounded-lg bg-destructive/5 p-3 text-destructive">
              <Result value={run.output} />
            </div>
          ) : run.output !== undefined &&
            (!contextualResult || showBusinessResult) ? (
            <FriendlyOutput run={run} projectId={projectId} />
          ) : null}
          <AdvancedDetails run={run} />
        </div>
      )}
    </div>
  );
}
