"use client";

// 系统 Prompt 资源浏览(TripBoss AutoPrompt 式):左列表(分类 chips + 搜索)+ 右详情。
// 资源随源码发布、只读;列表接口不含正文,选中后再拉详情,避免一次拉全量大文本。

import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { useMemo, useState } from "react";
import {
  EmptyState,
  ErrorState,
  SearchInput,
  ToneBadge,
} from "@/components/shared";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { PromptResourceDetailDto, PromptResourceDto } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CopyTextButton } from "./copy-text-button";

// 分类中文映射;未知分类原样展示,保证后端新增分类时前端不至于显示空白
const CATEGORY_LABELS: Record<string, string> = {
  global: "全局段",
  agent: "角色段",
  memory: "记忆",
};
const categoryLabel = (category: string) => CATEGORY_LABELS[category] ?? category;

const fmt = new Intl.NumberFormat("zh-CN");

export function PromptResourcesTab() {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ["admin-prompt-resources"],
    queryFn: () => api.get<PromptResourceDto[]>("/admin/prompt-resources"),
  });
  // useMemo 包住兜底空数组:避免每次渲染生成新引用导致下游 useMemo 失效
  const resources = useMemo(() => listQuery.data ?? [], [listQuery.data]);

  const categories = useMemo(
    () =>
      [...new Set(resources.map((item) => item.category).filter(Boolean))].sort(),
    [resources],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return resources.filter((item) => {
      const matchCategory = category === "all" || item.category === category;
      const matchSearch =
        !q ||
        [item.id, item.title, item.description, item.source].some((value) =>
          (value ?? "").toLowerCase().includes(q),
        );
      return matchCategory && matchSearch;
    });
  }, [category, resources, search]);

  // 选中项被筛掉时自动回落到过滤结果的第一项,右侧详情始终与左侧列表一致
  const active =
    filtered.find((item) => item.id === selectedId) ?? filtered[0] ?? null;

  const detailQuery = useQuery({
    queryKey: ["admin-prompt-resource", active?.id],
    queryFn: () =>
      api.get<PromptResourceDetailDto>(
        `/admin/prompt-resources/${encodeURIComponent(active?.id ?? "")}`,
      ),
    enabled: Boolean(active),
  });

  if (listQuery.isLoading) {
    return <Skeleton className="h-72 w-full" />;
  }
  if (listQuery.error) {
    return (
      <ErrorState error={listQuery.error} onRetry={() => listQuery.refetch()} />
    );
  }

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        系统 Prompt 资源随源码发布,此处只读;修改需编辑
        <code className="mx-1 font-mono text-foreground">
          backend/app/services/agent/prompts/resources/
        </code>
        并重启后端。
      </p>

      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        {/* 左:搜索 + 分类过滤 + 资源列表 */}
        <Card className="self-start">
          <CardContent className="space-y-3 pt-4">
            <SearchInput
              value={search}
              onChange={setSearch}
              label="搜索 Prompt 资源"
              placeholder="搜索 id / 标题 / 描述 / source"
            />
            <div className="flex flex-wrap gap-1.5" aria-label="按分类过滤">
              {["all", ...categories].map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setCategory(item)}
                  className={cn(
                    "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                    category === item
                      ? "border-primary/50 bg-primary/10 font-medium text-primary"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {item === "all" ? "全部" : categoryLabel(item)}
                </button>
              ))}
            </div>
            <div className="max-h-136 space-y-1.5 overflow-y-auto pr-1">
              {filtered.length === 0 ? (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  没有匹配的资源
                </p>
              ) : (
                filtered.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedId(item.id)}
                    className={cn(
                      "block w-full rounded-md border px-3 py-2 text-left transition-colors",
                      active?.id === item.id
                        ? "border-primary/50 bg-primary/5"
                        : "border-border hover:bg-accent/50",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">
                        {item.title || item.id}
                      </span>
                      <ToneBadge tone="muted">
                        {categoryLabel(item.category)}
                      </ToneBadge>
                    </div>
                    <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
                      {item.id}
                    </p>
                    {item.description && (
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                        {item.description}
                      </p>
                    )}
                  </button>
                ))
              )}
            </div>
          </CardContent>
        </Card>

        {/* 右:选中资源详情 */}
        <Card className="min-w-0">
          <CardContent className="pt-4">
            {!active ? (
              <EmptyState
                icon={FileText}
                title="请选择一个 Prompt 资源"
                description="左侧列表点击任意资源查看详情。"
              />
            ) : (
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-base font-semibold">
                      {active.title || active.id}
                    </h2>
                    <ToneBadge tone="info">
                      {categoryLabel(active.category)}
                    </ToneBadge>
                    <ToneBadge tone="muted">只读</ToneBadge>
                  </div>
                  <p className="font-mono text-xs text-muted-foreground">
                    {active.id}
                    {active.source ? ` · ${active.source}` : ""}
                  </p>
                  {active.description && (
                    <p className="text-sm leading-6 text-muted-foreground">
                      {active.description}
                    </p>
                  )}
                  {active.variables.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {active.variables.map((name) => (
                        <span
                          key={name}
                          className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-xs text-primary"
                        >
                          {`{{${name}}}`}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-muted-foreground">
                      正文 · {fmt.format(active.content_chars)} 字符
                    </span>
                    <CopyTextButton
                      text={detailQuery.data?.content ?? ""}
                      disabled={detailQuery.data?.id !== active.id}
                      label="复制正文"
                    />
                  </div>
                  {detailQuery.isLoading ? (
                    <Skeleton className="h-56 w-full" />
                  ) : detailQuery.error ? (
                    <ErrorState
                      error={detailQuery.error}
                      onRetry={() => detailQuery.refetch()}
                    />
                  ) : (
                    <pre className="max-h-120 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
                      {detailQuery.data?.content}
                    </pre>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
