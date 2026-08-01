"use client";

import {
  Brain,
  Check,
  ChevronRight,
  CircleAlert,
  Globe,
  Loader2,
  MessageSquareText,
  ShieldAlert,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  buildActivityTimeline,
  buildImageArtifacts,
  type ActivityTimelineItem,
  type ImageArtifact,
  type PermissionTimelineProjection,
  type ToolRun,
} from "@/lib/agent-events";
import {
  isApprovalBundle,
  toolLabel,
  type ApprovalBundle,
  type AgentEvent,
  type PendingConfirmation,
  type ReviewerOverride,
} from "@/lib/chat";
import { cn } from "@/lib/utils";
import {
  agentMarkdownComponents,
  sanitizeAgentMarkdown,
} from "./agent-markdown";
import { GeneratedImagePlaceholder, GeneratedImages } from "./generated-images";
import { hasToolResultRenderer, ToolResult } from "./result-renderers";
import { defaultActivityDisclosureOpen } from "./run-section-presentation";
import { ToolRunCard } from "./tool-run-card";
import { toolResultSummary } from "./tool-presentation";

function timeValue(value: string | null | undefined): number | undefined {
  if (!value) return undefined;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : undefined;
}

function formatElapsed(milliseconds: number): string | null {
  if (milliseconds < 1_000) return null;
  const seconds = Math.floor(milliseconds / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60)
    return `${minutes}m${remainingSeconds ? ` ${remainingSeconds}s` : ""}`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h${remainingMinutes ? ` ${remainingMinutes}m` : ""}`;
}

function useElapsedLabel({
  startedAt,
  completedAt,
  active,
}: {
  startedAt?: string | null;
  completedAt?: string | null;
  active: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const start = timeValue(startedAt);
  const end = timeValue(completedAt);

  useEffect(() => {
    if (!active || start === undefined) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [active, start]);

  if (start === undefined) return null;
  const elapsed = (active ? now : end) ?? start;
  return formatElapsed(Math.max(0, elapsed - start));
}

function toolActivitySummary(tools: ToolRun[]): string | null {
  if (!tools.length) return null;
  if (tools.length === 1) {
    const tool = tools[0];
    if (tool.status === "running") return toolLabel(tool.name);
    if (tool.status === "waiting") return `${toolLabel(tool.name)} · 等待审批`;
    if (tool.status === "failed") return `${toolLabel(tool.name)} · 执行失败`;
    if (hasToolResultRenderer(tool.name, tool.output))
      return toolLabel(tool.name);
    return `${toolLabel(tool.name)} · ${toolResultSummary(tool.name, tool.output)}`;
  }
  const labels = [...new Set(tools.map((tool) => toolLabel(tool.name)))];
  const visible = labels.slice(0, 2).join("、");
  return `${visible}${labels.length > 2 ? "等" : ""} · ${tools.length} 项操作`;
}

function thoughtTitle(text: string, index: number): string {
  const plain = text
    .replace(/```[\s\S]*?```/g, "")
    .replace(/[#*_`>\-[\]]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const first = plain.split(/[。！？.!?]/)[0]?.trim();
  if (!first) return `推理步骤 ${index + 1}`;
  return first.length > 42 ? `${first.slice(0, 42)}…` : first;
}

function ThoughtStep({
  item,
  index,
}: {
  item: Extract<ActivityTimelineItem, { type: "thought" }>;
  index: number;
}) {
  const [open, setOpen] = useState(false);
  const title = thoughtTitle(item.text, index);
  const normalized = item.text.replace(/\s+/g, " ").trim();
  const expandable =
    item.text.includes("\n") ||
    normalized.length > title.replace(/…$/, "").length;
  return (
    <div className="min-w-0">
      <button
        type="button"
        disabled={!expandable}
        onClick={() => expandable && setOpen((value) => !value)}
        className="group/thought flex min-h-7 max-w-full items-center gap-1.5 rounded-md px-1 text-left text-xs hover:bg-foreground/[0.035] disabled:cursor-default disabled:hover:bg-transparent"
        aria-expanded={expandable ? open : undefined}
      >
        <span className="shrink-0 font-medium text-foreground/80">
          推理 {index + 1}
        </span>
        <span className="min-w-0 truncate text-muted-foreground">
          · {title}
        </span>
        {expandable && (
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
        )}
      </button>
      {open && expandable && (
        <div className="prose prose-sm dark:prose-invert mt-1.5 max-w-none px-1 text-[13px] text-muted-foreground prose-p:my-1 prose-p:leading-6">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={agentMarkdownComponents}
          >
            {item.text}
          </Markdown>
        </div>
      )}
    </div>
  );
}

function AgentMessage({ text }: { text: string }) {
  const content = sanitizeAgentMarkdown(text);
  if (!content) return null;
  return (
    <div className="prose prose-sm dark:prose-invert max-w-none px-1 py-0.5 text-[13px] text-foreground/85 prose-p:my-1 prose-p:leading-6">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={agentMarkdownComponents}
      >
        {content}
      </Markdown>
    </div>
  );
}

function ResolvedApproval({
  pending,
}: {
  pending: PendingConfirmation;
}) {
  if (!isApprovalBundle(pending)) {
    return (
      <p className="px-1 py-1 text-xs text-muted-foreground">
        已处理 {toolLabel(pending.name)} 的审批
      </p>
    );
  }
  const approved = pending.items.filter(
    (item) => item.status === "approved" || item.status === "allowed",
  ).length;
  const denied = pending.items.filter(
    (item) => item.status === "denied",
  ).length;
  const waiting = pending.items.filter(
    (item) => item.status === "pending",
  ).length;
  const summary = waiting
    ? "等待审批"
    : [
        approved ? `允许 ${approved} 项` : null,
        denied ? `拒绝 ${denied} 项` : null,
      ]
        .filter(Boolean)
        .join(" · ") || "审批已处理";
  return (
    <div className="flex min-h-7 items-center gap-1.5 px-1 text-xs">
      <span className="font-medium text-foreground/80">{summary}</span>
      <span className="min-w-0 truncate text-muted-foreground">
        · {approvalToolLabels(pending)}
      </span>
    </div>
  );
}

function approvalToolLabels(bundle: ApprovalBundle): string {
  const labels = [
    ...new Set(bundle.items.map((item) => item.label || toolLabel(item.name))),
  ];
  return `${labels.slice(0, 2).join("、")}${labels.length > 2 ? "等" : ""}`;
}

function BuiltinSearchStep({
  item,
}: {
  item: Extract<ActivityTimelineItem, { type: "builtin-search" }>;
}) {
  // 内置搜索由模型服务端执行,没有 tool.call/tool.result,因此这里直接把过程
  // 与来源摊开——否则用户只看到结论,无从核实出处。
  return (
    <div className="px-1 py-1 text-xs">
      <div className="text-foreground/80">
        <span className="font-medium">模型联网搜索</span>
        {item.query && (
          <span className="ml-1.5 text-muted-foreground">· {item.query}</span>
        )}
        {item.results.length > 0 ? (
          <span className="ml-1.5 text-muted-foreground">
            · {item.results.length} 条来源
          </span>
        ) : (
          // 0 条来源却给出结论是最需要警惕的情况:要么没搜到、要么来源被上游裁掉,
          // 两种都意味着这段结论没有可核实的出处,必须让用户一眼看见
          <span className="ml-1.5 text-amber-700 dark:text-amber-300">
            · 未返回来源，结论需自行核实
          </span>
        )}
      </div>
      {item.results.length > 0 && (
        <ol className="mt-1 space-y-0.5">
          {item.results.slice(0, 6).map((source, index) => (
            <li key={`${index}-${source.url}`} className="truncate">
              <a
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="text-primary hover:underline"
              >
                {source.title || source.url}
              </a>
              {source.page_age && (
                <span className="ml-1.5 text-muted-foreground">
                  {source.page_age}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ActivityDot({ item }: { item: ActivityTimelineItem }) {
  if (item.type === "thought") return <Brain className="size-3.5" />;
  if (item.type === "builtin-search") return <Globe className="size-3.5" />;
  if (item.type === "message")
    return <MessageSquareText className="size-3.5" />;
  if (item.type === "tool")
    return item.run.status === "running" ? (
      <Loader2 className="size-3.5 animate-spin" />
    ) : item.run.status === "waiting" ? (
      <ShieldAlert className="size-3.5 text-amber-700 dark:text-amber-300" />
    ) : item.run.status === "failed" ? (
      <CircleAlert className="size-3.5 text-destructive" />
    ) : (
      <Check className="size-3.5" />
    );
  if (item.type === "approval")
    return isApprovalBundle(item.pending) &&
      item.pending.status === "completed" ? (
      <Check className="size-3.5" />
    ) : (
      <ShieldAlert className="size-3.5 text-amber-700 dark:text-amber-300" />
    );
  return <CircleAlert className="size-3.5 text-destructive" />;
}

function sameConfirmation(
  left: PendingConfirmation,
  right: PendingConfirmation | null | undefined,
): boolean {
  if (!right) return false;
  if (isApprovalBundle(left) || isApprovalBundle(right))
    return (
      isApprovalBundle(left) &&
      isApprovalBundle(right) &&
      left.bundle_id === right.bundle_id
    );
  return left.tool_call_id === right.tool_call_id;
}

function ActivityWaterfall({
  items,
  scopeType,
  scopeId,
  busy,
  onOverride,
}: {
  items: ActivityTimelineItem[];
  scopeType?: string | null;
  scopeId?: string | null;
  busy: boolean;
  onOverride?: (override: ReviewerOverride) => void;
}) {
  let thoughtIndex = 0;
  return (
    <ol className="pt-1">
      {items.map((item, index) => {
        const currentThoughtIndex =
          item.type === "thought" ? thoughtIndex++ : thoughtIndex;
        return (
          <li
            key={item.id}
            className="relative grid grid-cols-[1.25rem_minmax(0,1fr)] gap-2.5 pb-3.5 last:pb-1 motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-1 motion-safe:duration-300"
          >
            {index < items.length - 1 && (
              <span className="absolute top-5 bottom-0 left-[0.59rem] w-px bg-border/85" />
            )}
            <span className="relative z-10 mt-1 flex size-5 items-center justify-center rounded-full bg-[#f7f7f5] text-muted-foreground ring-4 ring-[#f7f7f5] dark:bg-[#191918] dark:ring-[#191918]">
              <ActivityDot item={item} />
            </span>
            <div className="min-w-0">
              {item.type === "thought" ? (
                <ThoughtStep item={item} index={currentThoughtIndex} />
              ) : item.type === "builtin-search" ? (
                <BuiltinSearchStep item={item} />
              ) : item.type === "message" ? (
                <AgentMessage text={item.text} />
              ) : item.type === "tool" ? (
                <ToolRunCard
                  run={item.run}
                  scopeType={scopeType}
                  scopeId={scopeId}
                  showToolResult={false}
                  showStatusIcon={false}
                  busy={busy}
                  onOverride={onOverride}
                />
              ) : item.type === "approval" ? (
                <ResolvedApproval pending={item.pending} />
              ) : (
                <div className="rounded-lg bg-destructive/6 px-2.5 py-2 text-xs leading-5 text-destructive">
                  {item.message}
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** One Codex-style run: summary header, ordered waterfall, then final answer. */
export function RunSections({
  events,
  streaming = false,
  busy = streaming,
  startedAt,
  completedAt,
  scopeType,
  scopeId,
  permissionProjection,
  imageArtifacts: suppliedImageArtifacts,
  toolResultKeys,
  showExecutionActivity = true,
  pending,
  onOverride,
  onAdjustImage,
  onGenerateImageAgain,
}: {
  events: AgentEvent[];
  streaming?: boolean;
  busy?: boolean;
  startedAt?: string | null;
  completedAt?: string | null;
  scopeType?: string | null;
  scopeId?: string | null;
  permissionProjection?: PermissionTimelineProjection;
  imageArtifacts?: ImageArtifact[];
  /** 已在别处渲染过的工具结果去重键(避免同一结果重复出现) */
  toolResultKeys?: ReadonlyMap<string, string>;
  showExecutionActivity?: boolean;
  pending?: PendingConfirmation | null;
  onOverride?: (override: ReviewerOverride) => void;
  onAdjustImage?: (draft: string) => void;
  onGenerateImageAgain?: (message: string) => void;
}) {
  const timeline = buildActivityTimeline(events, { permissionProjection });
  const localImageArtifacts = useMemo(
    () => buildImageArtifacts(events),
    [events],
  );
  const imageArtifacts = suppliedImageArtifacts ?? localImageArtifacts;
  const activeApproval = timeline.items.find(
    (item) =>
      item.type === "approval" && sameConfirmation(item.pending, pending),
  );
  const waterfallItems = activeApproval
    ? timeline.items.filter((item) => item !== activeApproval)
    : timeline.items;
  const tools = timeline.items.flatMap((item) =>
    item.type === "tool" ? [item.run] : [],
  );
  const failed =
    tools.some((tool) => tool.status === "failed") ||
    timeline.items.some((item) => item.type === "error");
  const waitingForApproval = activeApproval !== undefined;
  const reviewerStopped = tools.some(
    (tool) =>
      tool.permission?.reviewer?.decision === "deny" ||
      tool.permission?.reviewer?.circuitBreaker,
  );
  const userStopped = tools.some(
    (tool) =>
      tool.permission?.approval?.itemStatus === "denied" ||
      (typeof tool.output === "object" &&
        tool.output !== null &&
        "executed" in tool.output &&
        tool.output.executed === false),
  );
  const runningTool = tools.find((tool) => tool.status === "running");
  const traceAvailable = timeline.items.length > 0;
  const showActivity = showExecutionActivity && (streaming || traceAvailable);
  const pausedForApproval = timeline.items.some(
    (item) => item.type === "approval",
  );
  const contextualResults = [
    ...timeline.items.flatMap((item) =>
      item.type === "tool" &&
      item.run.status === "ok" &&
      (toolResultKeys === undefined || toolResultKeys.has(item.run.id)) &&
      hasToolResultRenderer(item.run.name, item.run.output)
        ? [{ type: "result" as const, seq: item.seq, tool: item.run }]
        : [],
    ),
    ...imageArtifacts.flatMap((artifact) =>
      artifact.status === "running" || artifact.status === "success"
        ? [{ type: "image" as const, seq: artifact.seq, artifact }]
        : [],
    ),
  ].sort((left, right) => left.seq - right.seq);
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const defaultOpen = defaultActivityDisclosureOpen({
    streaming,
    waitingForApproval,
    pausedForApproval,
  });
  const open =
    failed ||
    reviewerStopped ||
    (manualOpen ?? defaultOpen);
  const elapsed = useElapsedLabel({
    startedAt,
    completedAt,
    active: streaming,
  });
  const activitySummary = toolActivitySummary(tools);
  const latestItem = timeline.items.at(-1);
  const finalText = sanitizeAgentMarkdown(timeline.finalText);
  const status = waitingForApproval
    ? "等待你的决定"
    : streaming
      ? runningTool
        ? `正在${toolLabel(runningTool.name)}`
        : latestItem?.type === "thought"
          ? "正在推理"
          : latestItem?.type === "message"
            ? "Agent 正在处理"
            : "处理中…"
      : failed
        ? reviewerStopped || userStopped
          ? "操作未执行"
          : "处理遇到问题"
        : "已处理";
  return (
    <div className="space-y-4">
      {showActivity && (
        <section aria-label="Agent 执行过程">
          <button
            type="button"
            disabled={!traceAvailable}
            aria-expanded={traceAvailable ? open : undefined}
            onClick={() => traceAvailable && setManualOpen(!open)}
            className="group -ml-1 flex min-h-9 max-w-full items-center gap-2 rounded-lg px-2 text-left text-xs text-muted-foreground transition-colors hover:bg-foreground/[0.04] hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent"
          >
            <span className="flex size-4 shrink-0 items-center justify-center">
              {waitingForApproval ? (
                <ShieldAlert className="size-3.5 text-amber-700 dark:text-amber-300" />
              ) : streaming ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : failed ? (
                <CircleAlert className="size-3.5 text-destructive" />
              ) : (
                <Check className="size-3.5" />
              )}
            </span>
            <span className="shrink-0 font-medium text-foreground/85">
              {status}
            </span>
            {elapsed && (
              <span className="shrink-0 tabular-nums">{elapsed}</span>
            )}
            {activitySummary && (
              <span className="min-w-0 truncate">· {activitySummary}</span>
            )}
            {traceAvailable && (
              <ChevronRight
                className={cn(
                  "ml-0.5 size-3.5 shrink-0 transition-transform",
                  open && "rotate-90",
                )}
              />
            )}
          </button>

          {open && traceAvailable && (
            <div className="mt-1 ml-1 py-1" aria-live="polite">
              <ActivityWaterfall
                items={waterfallItems}
                scopeType={scopeType}
                scopeId={scopeId}
                busy={busy}
                onOverride={onOverride}
              />
            </div>
          )}
        </section>
      )}

      {contextualResults.map((result) =>
        result.type === "result" ? (
          <ToolResult
            key={
              toolResultKeys?.get(result.tool.id) ?? `result-${result.tool.id}`
            }
            name={result.tool.name}
            output={result.tool.output as Record<string, unknown>}
            scopeType={scopeType}
            scopeId={scopeId}
          />
        ) : result.artifact.status === "running" ? (
          <GeneratedImagePlaceholder
            key={`image-${result.artifact.id}`}
            count={result.artifact.requestedCount}
            width={result.artifact.requestedWidth}
            height={result.artifact.requestedHeight}
          />
        ) : (
          <GeneratedImages
            key={`image-${result.artifact.id}`}
            images={result.artifact.images}
            prompt={result.artifact.prompt}
            onAdjust={onAdjustImage}
            onGenerateAgain={onGenerateImageAgain}
          />
        ),
      )}

      {finalText && (
        <div
          className="prose prose-sm dark:prose-invert max-w-none text-[15px] prose-headings:tracking-tight prose-p:leading-7 prose-pre:rounded-xl motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-1 motion-safe:duration-300"
          aria-live="polite"
        >
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={agentMarkdownComponents}
          >
            {finalText}
          </Markdown>
        </div>
      )}
    </div>
  );
}
