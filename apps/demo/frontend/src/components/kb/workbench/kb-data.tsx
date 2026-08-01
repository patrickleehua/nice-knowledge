"use client";

// 工作台共享数据层:文档状态元数据 + 文档/待审核轮询 query。
// 文档轮询收窄到 2s,且仅在存在活跃任务(uploaded/parsing)时轮询;
// sources 视图、活动面板、左栏文档树共用同一 queryKey,react-query 自动去重。

import {
  keepPreviousData,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ToneBadge, type Tone } from "@/components/shared";
import { api } from "@/lib/api";
import { DOC_IDS_URL_MAX, fetchDocIngestDurations } from "@/lib/kb-ingest";
import type {
  DocIngestDuration,
  DocumentIngestionStatus,
  FactClaimQueueItem,
  FactClaimReview,
  KbIngestStats,
  SearchHit,
  SourceDocument,
} from "@/lib/types";

/**
 * 摄入文档状态 → tone + 文案。
 * 只覆盖 SourceDocument["status"] 的取值域;旧版这里还混进了 fact-claim 的
 * suggested/confirmed/rejected/pending,同一个词在不同视图含义不同,已清掉。
 */
export const DOC_STATUS: Record<
  SourceDocument["status"],
  { label: string; tone: Tone }
> = {
  staged: { label: "待分类", tone: "warning" },
  uploaded: { label: "排队中", tone: "muted" },
  parsing: { label: "解析中", tone: "primary" },
  awaiting_review: { label: "待审核", tone: "warning" },
  completed: { label: "已摄入", tone: "primary" },
  failed: { label: "失败", tone: "destructive" },
  canceled: { label: "已取消", tone: "muted" },
  paused: { label: "已暂停", tone: "warning" },
};

export function DocStatusBadge({ status }: { status: string }) {
  const meta = DOC_STATUS[status as SourceDocument["status"]];
  return (
    <ToneBadge tone={meta?.tone ?? "muted"}>{meta?.label ?? status}</ToneBadge>
  );
}

export const ACTIVE_STATUSES = new Set(["uploaded", "parsing"]);
export const RETRYABLE_STATUSES = new Set(["failed", "canceled"]);
/** 解析中可暂停;已暂停可继续(续跑复用已完成阶段,不重复采集) */
export const PAUSABLE_STATUSES = new Set(["parsing"]);
export const RESUMABLE_STATUSES = new Set(["paused"]);

/**
 * 概览性文档列表的软上限。后端 /kb/documents 默认 limit=100,此前前端不传 limit,
 * 于是文档树 / 活动面板 / 图片来源筛选都在 100 条处被静默截断且分母算错。
 * 这里显式传 limit,并把上限导出给调用方,达上限时如实提示"仅统计前 N 条"。
 */
export const KB_DOCUMENTS_SOFT_LIMIT = 500;

/** 文档列表:仅存在活跃任务时以 2s 间隔轮询,空闲时停表 */
export function useKbDocuments(kbId: string) {
  return useQuery({
    queryKey: ["kb-docs", kbId],
    queryFn: () =>
      api.get<SourceDocument[]>(
        `/kb/documents?kb_id=${kbId}&limit=${KB_DOCUMENTS_SOFT_LIMIT}`,
      ),
    refetchInterval: (query) =>
      query.state.data?.some((d) => ACTIVE_STATUSES.has(d.status))
        ? 2000
        : false,
  });
}

/**
 * 单份文档的摄入耗时。
 *
 * 成本约束是这个 hook 的全部设计动机:文档列表可能上百行,一行一个
 * ingestion-status 请求会直接打满浏览器的并发连接。所以是否发请求由调用方
 * 按行判断(`active`),此外只多补一种情况:
 * 缓存里那条记录还是 running —— 文档状态刚翻到终态、耗时却还停在中途,
 * 再补拉一次才能拿到最终的 finished_at / elapsed_seconds,然后自动停表。
 *
 * 其余(本次会话没看着它跑过的终态文档)一律 enabled=false:不发请求,
 * 也不渲染耗时,而不是为了一行文字去换一次往返。
 */
export function useDocumentIngestionStatus(docId: string, active: boolean) {
  const queryKey = ["kb-doc-ingestion-status", docId];
  const cached =
    useQueryClient().getQueryData<DocumentIngestionStatus>(queryKey);
  return useQuery({
    queryKey,
    queryFn: () =>
      api.get<DocumentIngestionStatus>(
        `/kb/documents/${docId}/ingestion-status`,
      ),
    enabled: Boolean(docId) && (active || cached?.timing.running === true),
    // 秒表由前端本地走动,接口只需要偶尔校准一次;跑完即停表
    refetchInterval: (query) =>
      active || query.state.data?.timing.running ? 5000 : false,
  });
}

/**
 * 整页文档的批量摄入耗时,供终态行常驻显示"耗时 X"。
 *
 * 与 useDocumentIngestionStatus 的分工:解析中的行继续用后者(带本地秒表
 * 与 5s 校准),终态行用这里的批量数据——一页一个请求,而不是一行一个,
 * 刷新页面后终态耗时也不丢。docIds 超过 DOC_IDS_URL_MAX 时退化为按 kb
 * 全量拉(后端截到最近 500 份),避免 query string 撞请求行长度上限。
 *
 * 轮询节奏:有活跃摄入任务(live)时跟随文档列表的 2s 节奏一起重取;
 * 上一次响应里还有 running 条目时同样续 2s,直到它落到终态拿到最终墙钟;
 * 其余时间完全停表。docIds 变化(翻页/筛选)天然换 queryKey 触发重取,
 * placeholderData 保住旧数据,翻页瞬间不闪空。
 */
export function useDocIngestDurations(
  kbId: string,
  docIds: string[],
  live: boolean,
) {
  const scope = docIds.length > DOC_IDS_URL_MAX ? null : [...docIds].sort();
  return useQuery({
    queryKey: ["kb-doc-ingest-durations", kbId, scope ?? "all"],
    queryFn: () => fetchDocIngestDurations(kbId, scope ?? undefined),
    enabled: Boolean(kbId) && docIds.length > 0,
    placeholderData: keepPreviousData,
    refetchInterval: (query) =>
      live || query.state.data?.items.some((item) => item.running)
        ? 2000
        : false,
    select: (data) =>
      new Map<string, DocIngestDuration>(
        data.items.map((item) => [item.doc_id, item]),
      ),
  });
}

/**
 * 全库摄入耗时基线。均值只在有文档跑完时才会变,所以仅在存在活跃任务时
 * 以 30s 间隔轮询,空闲期完全停表——这页可能整天开着不动。
 */
export function useKbIngestStats(kbId: string, live: boolean) {
  return useQuery({
    queryKey: ["kb-ingest-stats", kbId],
    queryFn: () => api.get<KbIngestStats>(`/kb/bases/${kbId}/ingest-stats`),
    refetchInterval: live ? 30_000 : false,
  });
}

/** FactClaim 审核队列；默认 suggested 供图标栏与概览统计复用。 */
export function usePendingExtractions(
  kbId: string,
  reviewStatus: "suggested" | "orphaned" = "suggested",
) {
  return useQuery({
    queryKey: ["kb-fact-claims", kbId, reviewStatus],
    queryFn: async (): Promise<FactClaimQueueItem[]> => {
      const claims = await api.get<FactClaimReview[]>(
        `/kb/fact-claims?kb_id=${kbId}&review_status=${reviewStatus}&limit=200`,
      );
      return claims.map((claim) => ({
        ...claim,
        entity_type: claim.predicate,
      }));
    },
    refetchInterval: 5000,
  });
}

const FACT_CLAIM_PAGE_SIZE = 200;

/** 审核主视图分页读取完整队列；导航徽标仍使用轻量首屏查询。 */
export function useFactClaimPages(
  kbId: string,
  reviewStatus: "suggested" | "orphaned",
) {
  return useInfiniteQuery({
    queryKey: ["kb-fact-claims", kbId, reviewStatus, "paginated"],
    queryFn: async ({ pageParam }): Promise<FactClaimQueueItem[]> => {
      const claims = await api.get<FactClaimReview[]>(
        `/kb/fact-claims?kb_id=${kbId}&review_status=${reviewStatus}&limit=${FACT_CLAIM_PAGE_SIZE}&offset=${pageParam}`,
      );
      return claims.map((claim) => ({
        ...claim,
        entity_type: claim.predicate,
      }));
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === FACT_CLAIM_PAGE_SIZE
        ? allPages.reduce((count, page) => count + page.length, 0)
        : undefined,
    refetchInterval: 5000,
  });
}

/** 统一知识检索；kbIds 不传表示搜索全部可见知识库。 */
/**
 * 单次检索能取到的最大条数。与后端 search 服务层护栏 _MAX_TOP_K 对齐
 * (services/kb/search.py,也在 search_execution_manifest 里以 max_top_k 对外声明)。
 * 前端据此停止「加载更多」,并如实告诉用户为什么没有更多了。
 */
export const SEARCH_TOP_K_LIMIT = 100;
/** 「加载更多」每次追加的条数 */
export const SEARCH_TOP_K_STEP = 20;

export function useKbSearch(query: string, topK = 20, kbIds?: string[]) {
  const normalizedQuery = query.trim();
  return useQuery({
    queryKey: ["kb-search", normalizedQuery, topK, kbIds],
    queryFn: () =>
      api.post<SearchHit[]>("/kb/search", {
        query: normalizedQuery,
        top_k: topK,
        kb_ids: kbIds,
      }),
    enabled: normalizedQuery.length > 0,
    // 「加载更多」只是 topK 换 key;保留上一页数据,避免整列表退回骨架屏
    placeholderData: keepPreviousData,
  });
}
