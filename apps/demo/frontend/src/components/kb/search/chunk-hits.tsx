"use client";

// 文档片段区(KB-4 检索页下区):chunk 命中列表,heading_path 面包屑 +
// 查询词高亮摘要 + 页码/行号锚点 meta + 「查看原文」深链到 /org/kb 工作台。
// 定位消费(view=sources&doc=&start=&end=)由工作台侧并行线实现,这里只负责正确生成。

import { FileText, SquareArrowOutUpRight } from "lucide-react";
import Link from "next/link";
import { LayerBadge } from "@/components/kb/badges";
import { KnowledgeCitationCard } from "@/components/kb/knowledge-citation-card";
import { Highlight } from "@/components/kb/search/highlight";
import {
  SearchCitationDetails,
  SearchScoreDetails,
} from "@/components/kb/search/hit-metadata";
import { sourceTarget } from "@/components/kb/search/deep-link";
import { ToneBadge } from "@/components/shared";
import { buttonVariants } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCurrentOrg, type Role } from "@/lib/auth";
import { cn } from "@/lib/utils";
import type { SearchHit } from "@/lib/types";

// 与 proxy.ts AREA_ROLES["/org"] 对齐:operator 可进租户管理端(知识库对全角色可见)
const ORG_AREA_ROLES: Role[] = ["platform_admin", "org_admin", "operator"];

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function SourceLink({
  href,
  canAccess,
  hasLineAnchor,
}: {
  href: string;
  canAccess: boolean;
  hasLineAnchor: boolean;
}) {
  const label = hasLineAnchor ? "定位原文" : "打开来源文档";
  if (canAccess) {
    return (
      <Link
        href={href}
        className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
      >
        <SquareArrowOutUpRight />
        {label}
      </Link>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger
        aria-disabled="true"
        className={cn(
          buttonVariants({ variant: "outline", size: "sm" }),
          "cursor-not-allowed opacity-50 hover:bg-background hover:text-inherit",
        )}
      >
        <SquareArrowOutUpRight />
        {label}
      </TooltipTrigger>
      <TooltipContent>需管理员权限{label}</TooltipContent>
    </Tooltip>
  );
}

function ChunkItem({
  hit,
  tokens,
  canAccess,
}: {
  hit: SearchHit;
  tokens: string[];
  canAccess: boolean;
}) {
  const headingPath = str(hit.data.heading_path);
  const content = str(hit.data.content) ?? "(空片段)";
  const target = sourceTarget(hit);

  return (
    <div className="space-y-1.5 rounded-lg border border-border bg-card p-3">
      {headingPath && (
        <p
          className="truncate text-xs text-muted-foreground"
          title={headingPath}
        >
          {headingPath}
        </p>
      )}
      <p className="line-clamp-2 text-sm leading-relaxed">
        <Highlight text={content.slice(0, 400)} tokens={tokens} />
      </p>
      <div className="flex flex-wrap items-center gap-2 pt-0.5">
        <LayerBadge layer={hit.layer} />
        {hit.data.stale === true && (
          <ToneBadge tone="warning">可能过期</ToneBadge>
        )}
        <SearchScoreDetails hit={hit} />
        {target && (
          <span className="ml-auto">
            <SourceLink
              href={target.href}
              canAccess={canAccess}
              hasLineAnchor={target.hasLineAnchor}
            />
          </span>
        )}
      </div>
      <SearchCitationDetails citation={hit.citation} />
      {(hit.media_refs ?? []).map((media) => (
        <KnowledgeCitationCard
          key={media.asset_id}
          media={media}
          sourceFilename={
            str(hit.data.source_filename) ?? str(hit.data.filename)
          }
          description={str(hit.data.caption) ?? media.alt_text}
          compact
        />
      ))}
    </div>
  );
}

export function ChunkHits({
  hits,
  tokens,
}: {
  hits: SearchHit[];
  tokens: string[];
}) {
  // 当前角色取自 auth store(cookie/localStorage);SSR 期为 null,水合后重渲染
  const role = useCurrentOrg()?.role;
  const canAccess = role != null && ORG_AREA_ROLES.includes(role);

  return (
    <TooltipProvider>
      <section className="space-y-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <FileText className="size-4 text-muted-foreground" />
          文档片段
          <span className="font-mono text-xs font-normal text-muted-foreground">
            {hits.length}
          </span>
        </h2>
        <div className="space-y-2">
          {hits.map((hit, i) => (
            <ChunkItem
              // source_ref 为 filename#chunkN,同名文档可能撞 key,优先用 chunk id
              key={str(hit.data.id) ?? `${hit.source}-${i}`}
              hit={hit}
              tokens={tokens}
              canAccess={canAccess}
            />
          ))}
        </div>
      </section>
    </TooltipProvider>
  );
}
