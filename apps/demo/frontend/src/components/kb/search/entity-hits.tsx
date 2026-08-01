"use client";

// 知识实体区(检索页上区):结构化命中按类型分组的紧凑卡片网格。
// 区块高度封顶 23rem 内部独立滚动。
//
// SDK 化改造(MIGRATION-PLAN §5.8):TF 按五个旅游 kind 各写一段字段摘要。
// 后端现在的 `hit.kind` 就是 `entity_type_key`(自由字符串,见
// nicekit/kb/search.py:1307)或内置的 `page`,因此这里统一走**通用 attributes
// 摘要**:展示名查 `/kb/entity-types` 注册表,查不到回落 type_key 原值。

import { useQuery } from "@tanstack/react-query";
import { BookOpen, Boxes, type LucideIcon } from "lucide-react";
import { LayerBadge } from "@/components/kb/badges";
import {
  SearchCitationDetails,
  SearchScoreDetails,
} from "@/components/kb/search/hit-metadata";
import { ToneBadge } from "@/components/shared";
import { api } from "@/lib/api";
import type { EntityType, SearchHit } from "@/lib/types";

/** 内置 kind(非实体类型):wiki 页。其余一律按实体类型注册表解析。 */
const BUILTIN_KIND_META: Record<string, { label: string; icon: LucideIcon }> = {
  page: { label: "知识页", icon: BookOpen },
};

// 通用摘要跳过的字段:标识类 + 标题类 + 已单独渲染的字段
const GENERIC_SKIP_KEYS = new Set([
  "name",
  "id",
  "entity_type_key",
  "title",
  "stale",
  "snapshot_id",
  "must_include_hit",
  "content",
]);

const MAX_SUMMARY_PARTS = 4;
const MAX_VALUE_CHARS = 32;

/**
 * 通用 attributes 摘要:按字段顺序取前几项 `键: 值`。
 * 缺失字段静默跳过,不编造;长值截断。
 */
export function genericEntitySummary(data: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(data)) {
    if (parts.length >= MAX_SUMMARY_PARTS) break;
    if (
      GENERIC_SKIP_KEYS.has(key) ||
      value === null ||
      value === undefined ||
      value === ""
    )
      continue;
    const text =
      typeof value === "object" ? JSON.stringify(value) : String(value);
    parts.push(
      `${key}: ${text.length > MAX_VALUE_CHARS ? `${text.slice(0, MAX_VALUE_CHARS)}…` : text}`,
    );
  }
  return parts.join(" · ");
}

function summary(kind: string, data: Record<string, unknown>): string {
  // wiki 页的正文不适合当键值对摊开,只给页面类型
  if (kind === "page") {
    return typeof data.page_type === "string" ? data.page_type : "";
  }
  return genericEntitySummary(data);
}

function EntityCard({ hit, label }: { hit: SearchHit; label: string }) {
  const Icon = BUILTIN_KIND_META[hit.kind]?.icon ?? Boxes;
  const name =
    (typeof hit.data.name === "string" && hit.data.name) ||
    (typeof hit.data.title === "string" && hit.data.title) ||
    "(未命名)";
  const detail = summary(hit.kind, hit.data);
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3">
      <div className="flex items-start gap-2">
        <Icon
          className="mt-0.5 size-4 shrink-0 text-muted-foreground"
          aria-label={label}
        />
        <span className="min-w-0 truncate text-sm font-medium" title={name}>
          {name}
        </span>
        {hit.data.stale === true && (
          <ToneBadge tone="warning" className="ml-auto shrink-0">
            可能过期
          </ToneBadge>
        )}
      </div>
      {detail && (
        <p className="line-clamp-2 text-xs text-muted-foreground">{detail}</p>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-0.5">
        <LayerBadge layer={hit.layer} />
        <div className="ml-auto">
          <SearchScoreDetails hit={hit} />
        </div>
      </div>
      <SearchCitationDetails citation={hit.citation} />
    </div>
  );
}

export function EntityHits({ hits }: { hits: SearchHit[] }) {
  // 类型展示名来自注册表;失败/未命中时回落 type_key,不隐藏命中
  const entityTypes = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
    staleTime: 5 * 60 * 1000,
  });
  const labelOf = (kind: string) =>
    BUILTIN_KIND_META[kind]?.label ??
    entityTypes.data?.find((type) => type.type_key === kind)?.display_name ??
    kind;

  // 分组顺序 = 命中出现顺序(检索排序已经表达了相关性,不再另定优先级)
  const kinds = [...new Set(hits.map((hit) => hit.kind))];
  const groups = kinds
    .map((kind) => ({ kind, hits: hits.filter((hit) => hit.kind === kind) }))
    .filter((group) => group.hits.length > 0);

  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <Boxes className="size-4 text-muted-foreground" />
        知识实体
        <span className="font-mono text-xs font-normal text-muted-foreground">
          {hits.length}
        </span>
      </h2>
      <div className="max-h-[23rem] space-y-4 overflow-y-auto pr-1">
        {groups.map((group) => (
          <div key={group.kind} className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">
              {labelOf(group.kind)} · {group.hits.length}
            </p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {group.hits.map((hit) => (
                <EntityCard
                  key={hit.source}
                  hit={hit}
                  label={labelOf(hit.kind)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
