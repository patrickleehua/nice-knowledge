"use client";

import {
  BookOpenText,
  FolderPlus,
  ListTodo,
  Map as MapIcon,
  ReceiptText,
  Sparkles,
  Target,
} from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  buildConversationAnchors,
  buildImageArtifactsByRun,
  buildPermissionTimelineProjection,
  flattenAgentEventGroups,
  groupAssistantSections,
  groupConversationItems,
  type ConversationGroup,
  type ConversationItem,
} from "@/lib/agent-events";
import {
  type PendingConfirmation,
  type ReviewerOverride,
} from "@/lib/chat";
import {
  businessResultKeysForItems,
  reconcileBusinessResults,
  type BusinessResultReconciliation,
} from "./business-result-reconciliation";
import {
  agentMarkdownComponents,
  sanitizeAgentMarkdown,
} from "./agent-markdown";
import { ConversationLocator } from "./conversation-locator";
import { ReviewerOverrideCard } from "./reviewer-override-card";
import { RunSections } from "./run-sections";
import { businessResultKind } from "./tool-presentation";

type ConversationMode = "general" | "project";

const EMPTY_PRESETS = {
  general: [
    {
      icon: FolderPlus,
      title: "创建旅行计划",
      description: "粘贴客户询价，让 AI 建档并解析需求",
      prompt: "根据以下客户询价新建旅行计划并解析需求：",
    },
    {
      icon: BookOpenText,
      title: "检索知识",
      description: "查询目的地、酒店、成本和历史线路",
      prompt: "帮我在知识库里查一下：",
    },
    {
      icon: ListTodo,
      title: "盘点任务",
      description: "跨旅行计划查看进度、异常和待处理事项",
      prompt: "帮我查一下最近的旅行计划和进行中的任务，有异常的重点说明。",
    },
  ],
  project: [
    {
      icon: BookOpenText,
      title: "解析需求",
      description: "整理当前旅行计划询价并找出待补信息",
      prompt: "解析当前旅行计划的客户需求，并列出需要补充的信息。",
    },
    {
      icon: MapIcon,
      title: "生成行程",
      description: "检索资料后生成多套可比较方案",
      prompt: "为当前旅行计划检索资料并生成多套行程方案。",
    },
    {
      icon: ReceiptText,
      title: "生成报价",
      description: "基于已确认行程生成拆分报价草稿",
      prompt: "基于已确认行程生成拆分报价，并列出待人工确认价格。",
    },
  ],
} as const;

function EmptyState({
  mode,
  onPreset,
}: {
  mode: ConversationMode;
  onPreset?: (prompt: string) => void;
}) {
  const presets = EMPTY_PRESETS[mode];
  return (
    <div className="flex min-h-[min(32rem,62dvh)] flex-col items-center justify-center py-10 text-center">
      <div className="mb-5 flex size-10 items-center justify-center rounded-full bg-foreground text-background">
        <Sparkles className="size-4" />
      </div>
      <h2 className="text-xl font-semibold tracking-[-0.02em]">
        {mode === "general"
          ? "今天想让 AI 帮你处理什么？"
          : "从当前旅行计划继续推进"}
      </h2>
      <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
        {mode === "general"
          ? "可以搜索知识、创建旅行计划，或盘点多个旅行计划的进展。"
          : "Agent 会自动使用当前旅行计划的需求、资料与业务状态。"}
      </p>
      <div className="mt-8 grid w-full gap-2 sm:grid-cols-3">
        {presets.map((preset) => (
          <button
            key={preset.title}
            type="button"
            onClick={() => onPreset?.(preset.prompt)}
            className="group rounded-2xl bg-white/65 p-3.5 text-left ring-1 ring-inset ring-black/[0.055] transition-colors hover:bg-white dark:bg-white/[0.045] dark:ring-white/[0.065] dark:hover:bg-white/[0.075]"
          >
            <preset.icon className="size-4 text-muted-foreground transition-colors group-hover:text-foreground" />
            <span className="mt-3 block text-sm font-medium tracking-tight">
              {preset.title}
            </span>
            <span className="mt-1 block text-xs leading-5 text-muted-foreground">
              {preset.description}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function AssistantTurn({
  group,
  running,
  liveStartedAt,
  projectId,
  pending,
  businessResultReconciliation,
  onOverride,
  onAdjustImage,
  onGenerateImageAgain,
}: {
  group: ConversationGroup;
  running: boolean;
  liveStartedAt?: string | null;
  projectId?: string;
  pending?: PendingConfirmation | null;
  businessResultReconciliation: BusinessResultReconciliation;
  onOverride?: (override: ReviewerOverride) => void;
  onAdjustImage?: (draft: string) => void;
  onGenerateImageAgain?: (message: string) => void;
}) {
  const sections = groupAssistantSections(group.items);
  const permissionProjection = buildPermissionTimelineProjection(
    group.items.flatMap((item) => (item.events ? [item.events] : [])),
  );
  const runSections = sections.filter(
    (section): section is Extract<(typeof sections)[number], { type: "run" }> =>
      section.type === "run",
  );
  const normalizedEventsBySection = new Map(
    runSections.map((section) => [
      section.key,
      flattenAgentEventGroups(
        section.items.map((item) => item.events ?? []),
      ),
    ]),
  );
  const imageArtifactsByRun = buildImageArtifactsByRun(
    runSections.map(
      (section) => normalizedEventsBySection.get(section.key) ?? [],
    ),
  );
  const imageArtifactsBySection = new Map(
    runSections.map((section, index) => [
      section.key,
      imageArtifactsByRun[index] ?? [],
    ]),
  );

  return (
    <div className="space-y-4">
      {sections.map((section, index) => {
        if (section.type === "run") {
          const streaming =
            running && section.items.some((item) => item.key === "live-run");
          const nextSection = sections[index + 1];
          return (
            <RunSections
              key={section.key}
              events={normalizedEventsBySection.get(section.key) ?? []}
              streaming={streaming}
              busy={running}
              startedAt={
                streaming
                  ? liveStartedAt
                  : section.items.find((item) => item.createdAt)?.createdAt
              }
              completedAt={
                streaming
                  ? undefined
                  : nextSection?.type === "text"
                    ? nextSection.item.createdAt
                    : (section.items.at(-1)?.completedAt ??
                      section.items.at(-1)?.createdAt)
              }
              projectId={projectId}
              permissionProjection={permissionProjection}
              imageArtifacts={imageArtifactsBySection.get(section.key)}
              businessResultKeys={businessResultKeysForItems(
                section.items,
                businessResultReconciliation,
              )}
              showExecutionActivity={section.items.some(
                (item) => !item.resultOnly,
              )}
              pending={pending}
              onOverride={onOverride}
              onAdjustImage={onAdjustImage}
              onGenerateImageAgain={onGenerateImageAgain}
            />
          );
        }
        const item = section.item;
        const content = sanitizeAgentMarkdown(item.content ?? "");
        return item.role === "assistant" && content ? (
          <div
            key={item.key}
            className="prose prose-sm dark:prose-invert max-w-none text-[15px] text-foreground prose-headings:tracking-tight prose-p:leading-7 prose-pre:rounded-xl"
          >
            <Markdown
              remarkPlugins={[remarkGfm]}
              components={agentMarkdownComponents}
            >
              {content}
            </Markdown>
          </div>
        ) : null;
      })}
    </div>
  );
}

export function AgentConversation({
  items,
  running,
  liveStartedAt,
  mode,
  projectId,
  pending,
  reviewerOverrides = [],
  onPreset,
  onOverride,
  onAdjustImage,
  onGenerateImageAgain,
}: {
  items: ConversationItem[];
  running: boolean;
  liveStartedAt?: string | null;
  mode: ConversationMode;
  projectId?: string;
  pending?: PendingConfirmation | null;
  reviewerOverrides?: ReviewerOverride[];
  onPreset?: (prompt: string) => void;
  onOverride?: (override: ReviewerOverride) => void;
  onAdjustImage?: (draft: string) => void;
  onGenerateImageAgain?: (message: string) => void;
}) {
  const groups = groupConversationItems(items);
  const businessResultReconciliation = reconcileBusinessResults(
    items,
    (name, output) => businessResultKind(name, output) !== null,
  );
  const anchors = buildConversationAnchors(groups);
  const anchorIds = new Map(
    anchors.map((anchor) => [anchor.groupIndex, anchor.id]),
  );
  const empty =
    groups.length === 0 &&
    reviewerOverrides.length === 0;
  const allEvents = items.flatMap((item) => item.events ?? []);
  const visibleToolCalls = new Set(
    allEvents.flatMap((event) =>
      event.type === "tool.call" ? [event.tool_call_id] : [],
    ),
  );
  const representedOverrideIds = new Set(
    allEvents.flatMap((event) =>
      event.type === "reviewer.override.available" &&
      visibleToolCalls.has(event.override.tool_call_id)
        ? [event.override.candidate_id]
        : [],
    ),
  );
  const fallbackOverrides = reviewerOverrides.filter(
    (override) => !representedOverrideIds.has(override.candidate_id),
  );

  if (empty)
    return (
      <div className="mx-auto w-full max-w-[50rem] px-4 py-7 sm:px-6 sm:py-10">
        <EmptyState mode={mode} onPreset={onPreset} />
      </div>
    );

  return (
    <div className="mx-auto grid w-full max-w-[69rem] grid-cols-1 gap-5 px-4 py-7 sm:px-6 sm:py-10 xl:grid-cols-[9rem_minmax(0,50rem)] xl:justify-center">
      <ConversationLocator anchors={anchors} />
      <div className="min-w-0 space-y-9 pb-5">
        {groups.map((group, groupIndex) => {
          const anchorId = anchorIds.get(groupIndex);
          if (group.role === "notice")
            return (
              <div
                key={group.key}
                className="flex items-center gap-2 text-xs text-muted-foreground"
              >
                <span className="h-px flex-1 bg-border/70" />
                <span className="flex items-center gap-1.5 whitespace-nowrap">
                  <Target className="size-3" />
                  {group.items[0]?.content}
                </span>
                <span className="h-px flex-1 bg-border/70" />
              </div>
            );
          return group.role === "user" ? (
            <div
              key={group.key}
              id={anchorId}
              className="flex scroll-mt-6 justify-end"
            >
              <div className="max-w-[88%] rounded-[1.35rem] bg-[#e9e9e6] px-4 py-2.5 text-[15px] leading-6 whitespace-pre-wrap text-foreground dark:bg-[#2d2d2b] sm:max-w-[78%]">
                {group.items[0]?.content}
              </div>
            </div>
          ) : (
            <div key={group.key} id={anchorId} className="scroll-mt-6">
              <AssistantTurn
                group={group}
                running={running}
                liveStartedAt={liveStartedAt}
                projectId={projectId}
                pending={pending}
                businessResultReconciliation={businessResultReconciliation}
                onOverride={onOverride}
                onAdjustImage={onAdjustImage}
                onGenerateImageAgain={onGenerateImageAgain}
              />
            </div>
          );
        })}
        {fallbackOverrides.map((override) => (
          <ReviewerOverrideCard
            key={override.candidate_id}
            override={override}
            busy={running}
            onOverride={onOverride}
          />
        ))}
      </div>
    </div>
  );
}
