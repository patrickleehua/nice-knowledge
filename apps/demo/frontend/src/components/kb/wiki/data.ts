"use client";

// wiki 页列表共享 query:左栏 PageList、wiki 视图、wikilink 解析共用同一缓存,
// queryKey 与既有约定("kb-pages")保持一致,不产生额外请求。

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { KbPage } from "@/lib/types";

export const kbPagesQueryKey = (kbId: string) => ["kb-pages", kbId] as const;

export interface WikiPage extends KbPage {
  snapshot_id?: string | null;
  draft_content?: string | null;
  draft_status?: string | null;
}

export function useKbPages(kbId: string) {
  return useQuery({
    queryKey: kbPagesQueryKey(kbId),
    queryFn: () => api.get<WikiPage[]>(`/kb/pages?kb_id=${kbId}&limit=500`),
  });
}

/** 按标题解析 wiki 页(wikilink 跳转用):精确匹配优先,忽略大小写兜底 */
export function findPageByTitle(
  pages: KbPage[] | undefined,
  title: string,
): KbPage | undefined {
  if (!pages) return undefined;
  const exact = pages.find((p) => p.title === title);
  if (exact) return exact;
  const lower = title.toLowerCase();
  return pages.find((p) => p.title.toLowerCase() === lower);
}
