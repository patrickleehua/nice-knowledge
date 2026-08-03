"use client";

import {
  BookOpenText,
  CalendarClock,
  Globe,
  Target,
  type LucideIcon,
} from "lucide-react";
import Image from "next/image";
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
  reconcileToolResults,
  toolResultKeysForItems,
  type ToolResultReconciliation,
} from "./tool-result-reconciliation";
import {
  agentMarkdownComponents,
  sanitizeAgentMarkdown,
} from "./agent-markdown";
import { ConversationLocator } from "./conversation-locator";
import { hasToolResultRenderer } from "./result-renderers";
import { ReviewerOverrideCard } from "./reviewer-override-card";
import { RunSections } from "./run-sections";

export interface ConversationPreset {
  icon: LucideIcon;
  title: string;
  description: string;
  prompt: string;
}

/**
 * 空态引导卡。SDK 不知道宿主是干什么的,所以默认只给**通用能力**示例
 * (知识检索 / 联网 / 定时任务),宿主可以整组换掉:
 * `<AgentConversation presets={myPresets} emptyTitle=… emptyDescription=… />`
 */
export const DEFAULT_EMPTY_PRESETS: readonly ConversationPreset[] = [
  {
    icon: BookOpenText,
    title: "检索知识库",
    description: "在已发布的知识快照里找依据并给出引用",
    prompt: "帮我在知识库里查一下：",
  },
  {
    icon: Globe,
    title: "联网调研",
    description: "搜索并读取网页,汇总带来源的结论",
    prompt: "帮我联网查一下,并逐条标注来源：",
  },
  {
    icon: CalendarClock,
    title: "安排定时任务",
    description: "让助手按计划周期性执行并把结果发给我",
    prompt: "每周一早上 9 点帮我做一次：",
  },
];

function EmptyState({
  presets,
  title,
  description,
  onPreset,
}: {
  presets: readonly ConversationPreset[];
  title: string;
  description: string;
  onPreset?: (prompt: string) => void;
}) {
  return (
    <div className="flex min-h-[min(32rem,62dvh)] flex-col items-center justify-center py-10 text-center">
      <div className="nk-brand-glass-mark mb-5 flex size-16 items-center justify-center">
        <Image
          src="/images/nicekit.png"
          alt="NiceKit"
          width={52}
          height={52}
          priority
          className="size-13 rounded-[1rem] object-cover"
        />
      </div>
      <h2 className="text-xl font-semibold tracking-[-0.02em]">{title}</h2>
      <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
        {description}
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
  scopeType,
  scopeId,
  pending,
  toolResultReconciliation,
  onOverride,
  onAdjustImage,
  onGenerateImageAgain,
}: {
  group: ConversationGroup;
  running: boolean;
  liveStartedAt?: string | null;
  scopeType?: string | null;
  scopeId?: string | null;
  pending?: PendingConfirmation | null;
  toolResultReconciliation: ToolResultReconciliation;
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
              scopeType={scopeType}
              scopeId={scopeId}
              permissionProjection={permissionProjection}
              imageArtifacts={imageArtifactsBySection.get(section.key)}
              toolResultKeys={toolResultKeysForItems(
                section.items,
                toolResultReconciliation,
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
  scopeType,
  scopeId,
  presets = DEFAULT_EMPTY_PRESETS,
  emptyTitle = "今天想让助手帮你做什么？",
  emptyDescription = "可以检索知识库、联网调研,或安排一个定时任务。",
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
  /** 会话绑定的宿主作用域,透传给工具结果渲染器 */
  scopeType?: string | null;
  scopeId?: string | null;
  /** 空态引导卡(宿主可整组替换) */
  presets?: readonly ConversationPreset[];
  emptyTitle?: string;
  emptyDescription?: string;
  pending?: PendingConfirmation | null;
  reviewerOverrides?: ReviewerOverride[];
  onPreset?: (prompt: string) => void;
  onOverride?: (override: ReviewerOverride) => void;
  onAdjustImage?: (draft: string) => void;
  onGenerateImageAgain?: (message: string) => void;
}) {
  const groups = groupConversationItems(items);
  const toolResultReconciliation = reconcileToolResults(
    items,
    hasToolResultRenderer,
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
        <EmptyState
          presets={presets}
          title={emptyTitle}
          description={emptyDescription}
          onPreset={onPreset}
        />
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
                scopeType={scopeType}
                scopeId={scopeId}
                pending={pending}
                toolResultReconciliation={toolResultReconciliation}
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
