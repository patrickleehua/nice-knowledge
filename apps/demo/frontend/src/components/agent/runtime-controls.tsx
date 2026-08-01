"use client";

import { Menu } from "@base-ui/react/menu";
import {
  Bot,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Globe,
  Wrench,
} from "lucide-react";
import type {
  AgentCardOut,
  RuntimeToolsOut,
  ThinkingLevelsOut,
  WebSearchOptionsOut,
} from "@/lib/chat";

const DEFAULT_AGENT = "__default_agent__";
// Menu.RadioGroup 需要一个具体值,用哨兵表示"跟随管理端配置(auto)"
const WEB_SEARCH_AUTO = "__auto__";

const popupSurfaceClass =
  "origin-(--transform-origin) rounded-2xl bg-popover p-1.5 text-popover-foreground shadow-xl ring-1 ring-foreground/10 outline-hidden transition-[transform,opacity] duration-100 data-ending-style:scale-[0.98] data-ending-style:opacity-0 data-starting-style:scale-[0.98] data-starting-style:opacity-0";
const summaryItemClass =
  "flex min-h-10 w-full cursor-default items-center gap-2 rounded-xl px-3 py-2 text-sm outline-hidden select-none data-highlighted:bg-black/[0.055] data-popup-open:bg-black/[0.055] dark:data-highlighted:bg-white/[0.075] dark:data-popup-open:bg-white/[0.075]";
const radioItemClass =
  "relative flex min-h-9 cursor-default items-center gap-2 rounded-lg px-2.5 py-2 text-sm outline-hidden select-none data-highlighted:bg-black/[0.055] data-disabled:pointer-events-none data-disabled:opacity-50 dark:data-highlighted:bg-white/[0.075]";

interface RuntimeControlsProps {
  thinkingLevels: ThinkingLevelsOut | undefined;
  thinking: string | undefined;
  cards: AgentCardOut[] | undefined;
  agentCardId: string | undefined;
  disabled: boolean;
  onThinkingChange: (level: string) => void;
  onAgentCardChange: (agentCardId: string | undefined) => void;
  webSearchOptions: WebSearchOptionsOut | undefined;
  // undefined = 跟随管理端配置(auto 故障转移链)
  webSearch: string | undefined;
  onWebSearchChange: (value: string | undefined) => void;
  // 本轮临时工具:候选由后端按当前 agent 卡下发(只读且不在卡白名单里),
  // 勾选只对下一次发送生效,不写 agent 配置
  runtimeToolOptions: RuntimeToolsOut | undefined;
  runtimeTools: string[];
  onRuntimeToolsChange: (names: string[]) => void;
}

export function RuntimeControls({
  thinkingLevels,
  thinking,
  cards,
  agentCardId,
  disabled,
  onThinkingChange,
  onAgentCardChange,
  webSearchOptions,
  webSearch,
  onWebSearchChange,
  runtimeToolOptions,
  runtimeTools,
  onRuntimeToolsChange,
}: RuntimeControlsProps) {
  const agentValue = agentCardId ?? DEFAULT_AGENT;
  const agent = cards?.find((card) => card.id === agentCardId);
  const agentLabel = agent?.name ?? (agentCardId ? "Agent" : "默认 Agent");
  const reasoningLabel =
    thinkingLevels?.levels.find((level) => level.value === thinking)?.label ??
    "推理";
  const webSearchValue = webSearch ?? WEB_SEARCH_AUTO;
  const webSearchLabel =
    webSearch === undefined
      ? webSearchOptions?.ready
        ? "自动"
        : "未配置"
      : (webSearchOptions?.options.find((o) => o.value === webSearch)?.label ??
        "自动");
  const unavailable = disabled || !cards || !thinkingLevels || !thinking;
  // 候选可能因换卡而变化,勾选态只保留仍在候选里的项——避免把一个已进卡白名单
  // (或已下架)的工具继续当"临时开启"发出去
  const candidates = runtimeToolOptions?.tools ?? [];
  const activeRuntimeTools = runtimeTools.filter((name) =>
    candidates.some((tool) => tool.name === name),
  );
  const runtimeToolsLabel =
    activeRuntimeTools.length === 0
      ? "本轮工具"
      : activeRuntimeTools.length === 1
        ? (candidates.find((tool) => tool.name === activeRuntimeTools[0])
            ?.label ?? "本轮工具")
        : `本轮工具 ${activeRuntimeTools.length}`;
  function toggleRuntimeTool(name: string, checked: boolean) {
    onRuntimeToolsChange(
      checked
        ? [...activeRuntimeTools.filter((item) => item !== name), name]
        : activeRuntimeTools.filter((item) => item !== name),
    );
  }

  return (
    <Menu.Root disabled={unavailable}>
      <Menu.Trigger
        aria-label={`运行设置：${agentLabel}，推理强度 ${reasoningLabel}${
          activeRuntimeTools.length ? `，本轮临时工具 ${activeRuntimeTools.length} 项` : ""
        }`}
        className="group/runtime flex h-8 max-w-full items-center gap-1.5 rounded-xl px-2.5 text-xs text-foreground/85 outline-none transition-colors hover:bg-black/[0.045] focus-visible:ring-2 focus-visible:ring-ring/45 data-disabled:cursor-not-allowed data-disabled:opacity-50 data-popup-open:bg-black/[0.055] dark:hover:bg-white/[0.07] dark:data-popup-open:bg-white/[0.08]"
      >
        <Bot className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="max-w-36 truncate font-medium">{agentLabel}</span>
        <span className="text-muted-foreground/55" aria-hidden="true">
          ·
        </span>
        <Brain className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="shrink-0 text-muted-foreground">{reasoningLabel}</span>
        <span className="text-muted-foreground/55" aria-hidden="true">
          ·
        </span>
        <Globe className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="shrink-0 text-muted-foreground">{webSearchLabel}</span>
        {activeRuntimeTools.length > 0 && (
          <>
            <span className="text-muted-foreground/55" aria-hidden="true">
              ·
            </span>
            {/* 临时工具是"一次性提权",选中态必须比其他控件更醒目 */}
            <span className="flex shrink-0 items-center gap-1 rounded-md bg-amber-500/15 px-1.5 py-0.5 font-medium text-amber-700 dark:text-amber-300">
              <Wrench className="size-3 shrink-0" />
              {runtimeToolsLabel}
            </span>
          </>
        )}
        <ChevronDown className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-[popup-open]/runtime:rotate-180" />
      </Menu.Trigger>

      <Menu.Portal>
        <Menu.Positioner
          side="top"
          align="start"
          sideOffset={8}
          className="isolate z-50 outline-hidden"
        >
          <Menu.Popup
            className={`w-[min(17rem,calc(100vw-2rem))] ${popupSurfaceClass}`}
          >
            <Menu.SubmenuRoot>
              <Menu.SubmenuTrigger
                label="Agent"
                openOnHover
                delay={80}
                closeDelay={120}
                className={summaryItemClass}
              >
                <span className="font-medium">Agent</span>
                <span className="ml-auto max-w-32 truncate text-muted-foreground">
                  {agentLabel}
                </span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </Menu.SubmenuTrigger>
              <Menu.Portal>
                <Menu.Positioner
                  side="inline-end"
                  align="start"
                  sideOffset={6}
                  className="isolate z-50 outline-hidden"
                >
                  <Menu.Popup
                    className={`max-h-[min(26rem,var(--available-height))] w-[min(16rem,calc(100vw-2rem))] overflow-y-auto ${popupSurfaceClass}`}
                  >
                    <Menu.RadioGroup
                      value={agentValue}
                      onValueChange={(value) =>
                        onAgentCardChange(
                          value === DEFAULT_AGENT ? undefined : String(value),
                        )
                      }
                    >
                      <Menu.GroupLabel className="flex items-center gap-2 px-2.5 pt-2 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">
                        <Bot className="size-3.5" />
                        Agent
                      </Menu.GroupLabel>
                      <Menu.RadioItem
                        value={DEFAULT_AGENT}
                        className={radioItemClass}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          默认 Agent
                        </span>
                        <Menu.RadioItemIndicator className="flex size-4 shrink-0 items-center justify-center">
                          <Check className="size-3.5" />
                        </Menu.RadioItemIndicator>
                      </Menu.RadioItem>
                      {cards?.map((card) => (
                        <Menu.RadioItem
                          key={card.id}
                          value={card.id}
                          className={radioItemClass}
                        >
                          <span className="min-w-0 flex-1 truncate">
                            {card.name}
                          </span>
                          <Menu.RadioItemIndicator className="flex size-4 shrink-0 items-center justify-center">
                            <Check className="size-3.5" />
                          </Menu.RadioItemIndicator>
                        </Menu.RadioItem>
                      ))}
                    </Menu.RadioGroup>
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.SubmenuRoot>

            <Menu.SubmenuRoot>
              <Menu.SubmenuTrigger
                label="推理强度"
                openOnHover
                delay={80}
                closeDelay={120}
                className={summaryItemClass}
              >
                <span className="font-medium">推理强度</span>
                <span className="ml-auto truncate text-muted-foreground">
                  {reasoningLabel}
                </span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </Menu.SubmenuTrigger>
              <Menu.Portal>
                <Menu.Positioner
                  side="inline-end"
                  align="start"
                  sideOffset={6}
                  className="isolate z-50 outline-hidden"
                >
                  <Menu.Popup
                    className={`w-[min(14rem,calc(100vw-2rem))] ${popupSurfaceClass}`}
                  >
                    <Menu.RadioGroup
                      value={thinking}
                      onValueChange={(value) => onThinkingChange(String(value))}
                    >
                      <Menu.GroupLabel className="flex items-center gap-2 px-2.5 pt-2 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">
                        <Brain className="size-3.5" />
                        推理强度
                      </Menu.GroupLabel>
                      {thinkingLevels?.levels.map((level) => (
                        <Menu.RadioItem
                          key={level.value}
                          value={level.value}
                          className={radioItemClass}
                        >
                          <span className="min-w-0 flex-1 truncate">
                            {level.label}
                          </span>
                          <Menu.RadioItemIndicator className="flex size-4 shrink-0 items-center justify-center">
                            <Check className="size-3.5" />
                          </Menu.RadioItemIndicator>
                        </Menu.RadioItem>
                      ))}
                    </Menu.RadioGroup>
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.SubmenuRoot>

            <Menu.SubmenuRoot>
              <Menu.SubmenuTrigger
                label="联网搜索"
                openOnHover
                delay={80}
                closeDelay={120}
                className={summaryItemClass}
              >
                <span className="font-medium">联网搜索</span>
                <span className="ml-auto truncate text-muted-foreground">
                  {webSearchLabel}
                </span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </Menu.SubmenuTrigger>
              <Menu.Portal>
                <Menu.Positioner
                  side="inline-end"
                  align="start"
                  sideOffset={6}
                  className="isolate z-50 outline-hidden"
                >
                  <Menu.Popup
                    className={`w-[min(17rem,calc(100vw-2rem))] ${popupSurfaceClass}`}
                  >
                    <Menu.RadioGroup
                      value={webSearchValue}
                      onValueChange={(value) =>
                        onWebSearchChange(
                          value === WEB_SEARCH_AUTO ? undefined : String(value),
                        )
                      }
                    >
                      <Menu.GroupLabel className="flex items-center gap-2 px-2.5 pt-2 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">
                        <Globe className="size-3.5" />
                        联网搜索
                      </Menu.GroupLabel>
                      <Menu.RadioItem
                        value={WEB_SEARCH_AUTO}
                        disabled={!webSearchOptions?.ready}
                        className={radioItemClass}
                      >
                        <span className="min-w-0 flex-1 truncate">自动</span>
                        <span className="shrink-0 text-[11px] text-muted-foreground">
                          {webSearchOptions?.ready ? "按管理端配置" : "未配置"}
                        </span>
                        <Menu.RadioItemIndicator className="flex size-4 shrink-0 items-center justify-center">
                          <Check className="size-3.5" />
                        </Menu.RadioItemIndicator>
                      </Menu.RadioItem>
                      {webSearchOptions?.options.map((option) => (
                        <Menu.RadioItem
                          key={option.value}
                          value={option.value}
                          disabled={!option.ready}
                          className={radioItemClass}
                        >
                          <span className="min-w-0 flex-1 truncate">
                            {option.label}
                          </span>
                          <span className="shrink-0 text-[11px] text-muted-foreground">
                            {option.hint}
                          </span>
                          <Menu.RadioItemIndicator className="flex size-4 shrink-0 items-center justify-center">
                            <Check className="size-3.5" />
                          </Menu.RadioItemIndicator>
                        </Menu.RadioItem>
                      ))}
                    </Menu.RadioGroup>
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.SubmenuRoot>

            <Menu.SubmenuRoot>
              <Menu.SubmenuTrigger
                label="本轮工具"
                openOnHover
                delay={80}
                closeDelay={120}
                disabled={candidates.length === 0}
                className={summaryItemClass}
              >
                <span className="font-medium">本轮工具</span>
                <span className="ml-auto truncate text-muted-foreground">
                  {candidates.length === 0
                    ? "无可选"
                    : activeRuntimeTools.length === 0
                      ? "未开启"
                      : `已选 ${activeRuntimeTools.length}`}
                </span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </Menu.SubmenuTrigger>
              <Menu.Portal>
                <Menu.Positioner
                  side="inline-end"
                  align="start"
                  sideOffset={6}
                  className="isolate z-50 outline-hidden"
                >
                  <Menu.Popup
                    className={`max-h-[min(26rem,var(--available-height))] w-[min(19rem,calc(100vw-2rem))] overflow-y-auto ${popupSurfaceClass}`}
                  >
                    <Menu.GroupLabel className="flex items-center gap-2 px-2.5 pt-2 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground">
                      <Wrench className="size-3.5" />
                      本轮工具
                    </Menu.GroupLabel>
                    {/* 口径由后端下发,保证各端一致:这是一次性开关,不改 Agent 配置 */}
                    <p className="px-2.5 pb-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-300">
                      {runtimeToolOptions?.notice ??
                        "仅本轮生效，不会改变 Agent 配置"}
                    </p>
                    {candidates.map((tool) => (
                      <Menu.CheckboxItem
                        key={tool.name}
                        checked={activeRuntimeTools.includes(tool.name)}
                        onCheckedChange={(checked) =>
                          toggleRuntimeTool(tool.name, checked)
                        }
                        className={`${radioItemClass} items-start`}
                      >
                        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="truncate">
                            {tool.label}
                            <span className="ml-1.5 text-[11px] text-muted-foreground">
                              {tool.category}
                            </span>
                          </span>
                          {tool.hint && (
                            <span className="line-clamp-2 text-[11px] leading-snug text-muted-foreground">
                              {tool.hint}
                            </span>
                          )}
                        </span>
                        <Menu.CheckboxItemIndicator className="mt-0.5 flex size-4 shrink-0 items-center justify-center">
                          <Check className="size-3.5" />
                        </Menu.CheckboxItemIndicator>
                      </Menu.CheckboxItem>
                    ))}
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.SubmenuRoot>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
