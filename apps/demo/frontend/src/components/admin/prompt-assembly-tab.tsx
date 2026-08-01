"use client";

// 组装预览:选一张 agent 卡 → 后端按当前源码资源静态组装出该卡的 system prompt。
// 展示块清单(字符数 + 占比水平条)与组装后全文;仅运行时注入的动态块
// (记忆/会话上下文等)无法静态预览,单独用虚线卡片列出并说明。

import { useQuery } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import { useMemo, useState } from "react";
import { EmptyState, ErrorState } from "@/components/shared";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { AgentCardDto, PromptAssemblyDto } from "@/lib/types";
import { CopyTextButton } from "./copy-text-button";

const fmt = new Intl.NumberFormat("zh-CN");

export function PromptAssemblyTab() {
  const [cardId, setCardId] = useState("");

  const cardsQuery = useQuery({
    queryKey: ["admin-agent-cards"],
    queryFn: () => api.get<AgentCardDto[]>("/admin/agent-cards"),
  });
  // useMemo 包住兜底空数组:避免每次渲染生成新引用导致下游 useMemo 失效
  const cards = useMemo(() => cardsQuery.data ?? [], [cardsQuery.data]);
  // base-ui Select 需要 items 映射(value → 展示文案)才能在 Trigger 里回显选中项
  const cardItems = useMemo(
    () => Object.fromEntries(cards.map((card) => [card.id, card.name])),
    [cards],
  );

  const assemblyQuery = useQuery({
    queryKey: ["admin-prompt-assembly", cardId],
    queryFn: () =>
      api.get<PromptAssemblyDto>(`/admin/prompt-assembly?card_id=${cardId}`),
    enabled: Boolean(cardId),
  });
  const assembly = assemblyQuery.data;
  const totalChars = Math.max(1, assembly?.system_prompt_chars ?? 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">Agent 卡</span>
        <Select
          items={cardItems}
          value={cardId || null}
          onValueChange={(value) => setCardId((value as string) ?? "")}
        >
          <SelectTrigger className="h-9 w-64" aria-label="选择 Agent 卡">
            <SelectValue
              placeholder={
                cardsQuery.isLoading ? "Agent 卡加载中…" : "选择要预览的 Agent 卡"
              }
            />
          </SelectTrigger>
          <SelectContent>
            {cards.map((card) => (
              <SelectItem key={card.id} value={card.id}>
                {card.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {cardsQuery.error ? (
        <ErrorState
          error={cardsQuery.error}
          onRetry={() => cardsQuery.refetch()}
        />
      ) : !cardId ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={Layers}
              title="选择一张 Agent 卡查看组装结果"
              description="按该卡的配置把静态 prompt 段组装成最终 system prompt。"
            />
          </CardContent>
        </Card>
      ) : assemblyQuery.isLoading ? (
        <Skeleton className="h-72 w-full" />
      ) : assemblyQuery.error ? (
        <ErrorState
          error={assemblyQuery.error}
          onRetry={() => assemblyQuery.refetch()}
        />
      ) : assembly ? (
        <div className="space-y-4">
          {/* 块清单:每块一行,水平条按占组装总字符数的比例绘制 */}
          <Card>
            <CardContent className="space-y-3 pt-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-medium">{assembly.card_name}</span>
                <span className="text-xs text-muted-foreground">
                  {assembly.blocks.length} 个静态块 · 共{" "}
                  {fmt.format(assembly.system_prompt_chars)} 字符
                </span>
              </div>
              <div className="space-y-1.5">
                {assembly.blocks.map((block) => {
                  const pct = (block.chars / totalChars) * 100;
                  return (
                    <div
                      key={block.id}
                      className="flex items-center gap-3 text-xs"
                    >
                      <span
                        className="w-44 truncate font-mono"
                        title={block.id}
                      >
                        {block.id}
                      </span>
                      <span className="w-20 text-right text-muted-foreground tabular-nums">
                        {fmt.format(block.chars)} 字符
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full rounded-full bg-primary/70"
                          style={{ width: `${Math.max(pct, 0.5)}%` }}
                        />
                      </div>
                      <span className="w-12 text-right text-muted-foreground tabular-nums">
                        {pct.toFixed(1)}%
                      </span>
                    </div>
                  );
                })}
              </div>
            </CardContent>
          </Card>

          {/* 仅运行时出现的动态块:虚线边框与静态块区分 */}
          {assembly.runtime_only_blocks.length > 0 && (
            <Card>
              <CardContent className="space-y-2 pt-4">
                <p className="text-sm font-medium">仅运行时出现的动态块</p>
                <p className="text-xs text-muted-foreground">
                  以下块依赖会话现场(客户记忆、运行上下文等),在每次运行时动态注入,
                  静态组装预览不含其内容。
                </p>
                <div className="grid gap-2 sm:grid-cols-2">
                  {assembly.runtime_only_blocks.map((block) => (
                    <div
                      key={block.id}
                      className="rounded-md border border-dashed border-border bg-muted/20 p-3"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-medium">
                          {block.title || block.id}
                        </span>
                        <span className="shrink-0 font-mono text-xs text-muted-foreground">
                          {block.id}
                        </span>
                      </div>
                      {block.description && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {block.description}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          {/* 组装后全文 */}
          <Card>
            <CardContent className="space-y-2 pt-4">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-muted-foreground">
                  组装后全文 · {fmt.format(assembly.system_prompt_chars)} 字符
                </span>
                <CopyTextButton text={assembly.text} label="复制全文" />
              </div>
              <pre className="max-h-120 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
                {assembly.text}
              </pre>
            </CardContent>
          </Card>
        </div>
      ) : null}
    </div>
  );
}
