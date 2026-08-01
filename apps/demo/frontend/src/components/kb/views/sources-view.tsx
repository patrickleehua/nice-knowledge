"use client";

// sources 视图:文档库(上传 + 检索筛选 + 队列状态 + 批量操作)。
//
// 相对旧版的改动(P1):
// 1. 上传改为「整区拖拽放置 + 上传前确认托盘」:上传只保存原文件,
//    完成后在待处理区持久分类,再由用户明确排入解析队列;
// 2. 文档列表补齐主流列表该有的东西:搜索、状态筛选、排序、全选、批量条、
//    筛选状态进 URL(刷新/分享不丢,也承接底部发布状态条的深链 ?status=);
// 3. 行内常驻 4-6 个图标按钮收进「⋯」溢出菜单,主操作(打开)留给整行;
// 4. 原生 checkbox 换成 Checkbox 组件;
// 5. 「修订历史」弹窗里不相干的"文档有效期"拆成独立入口。
//
// 深链契约不变:?view=sources&doc={docId}&start={n}&end={m}

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Ban,
  CalendarClock,
  Folder,
  FolderUp,
  History,
  Images,
  Inbox,
  LoaderCircle,
  MoreHorizontal,
  Pause,
  Play,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  Upload,
  UploadCloud,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  DocumentIntakePanel,
  type DocumentTypeOption,
} from "@/components/kb/document-intake-panel";
import { DocDetail } from "@/components/kb/doc-detail";
import {
  canReclassifyDocument,
  reclassificationStatus,
  retryTargetDocType,
  shouldPollReclassification,
} from "@/components/kb/reclassification";
import {
  documentUploadPreflight,
  SUPPORTED_DOCUMENT_SUFFIXES,
} from "@/components/kb/upload-preflight.mjs";
import {
  BulkActionBar,
  EmptyState,
  SearchInput,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { useCurrentOrg } from "@/lib/auth";
import { elapsedSecondsSince, formatDuration } from "@/lib/duration";
import { cn, errMsg } from "@/lib/utils";
import { useUrlState } from "@/lib/use-url-state";
import {
  DOC_TYPES,
  type BatchResult,
  type DocumentClassificationItem,
  type DocumentClassificationResult,
  type DocumentIngestionQueueItem,
  type DocumentIngestionQueueResult,
  type DocumentReclassificationAccepted,
  type DocumentPurgePreview,
  type DocumentWithdrawalImpact,
  type DocIngestDuration,
  type EntityType,
  type IngestSettings,
  type KbDocumentOperation,
  type SourceDocument,
} from "@/lib/types";
import {
  ACTIVE_STATUSES,
  DocStatusBadge,
  PAUSABLE_STATUSES,
  RESUMABLE_STATUSES,
  RETRYABLE_STATUSES,
  useDocIngestDurations,
  useDocumentIngestionStatus,
} from "@/components/kb/workbench/kb-data";
import { useEndReached } from "@/components/kb/workbench/use-virtual-rows";

const DOC_TYPE_ITEMS: Record<string, string> = Object.fromEntries(
  DOC_TYPES.map((t) => [t.value, t.label]),
);
const CONCURRENCY_OPTIONS = [1, 2, 3, 4, 6, 8];
const DOCUMENT_PAGE_SIZE = 100;
const SUPPORTED_DOCUMENT_ACCEPT = SUPPORTED_DOCUMENT_SUFFIXES.join(",");
const CONCURRENCY_ITEMS: Record<string, string> = Object.fromEntries(
  CONCURRENCY_OPTIONS.map((n) => [String(n), String(n)]),
);

// 摄入阶段中文名:图片多的文档会在"图片理解"停留几十分钟,必须让用户看到
// 在做什么、做到第几张,否则一条不动的进度条与卡死无从区分。
const INGEST_STAGE_LABELS: Record<string, string> = {
  parse: "解析版面",
  image: "图片理解",
  chunk: "切片与向量化",
  extract: "信息抽取",
  entity_extract: "实体抽取",
  document: "整体",
};

function ingestProgressLabel(d: SourceDocument): string {
  const stage = d.progress_stage
    ? (INGEST_STAGE_LABELS[d.progress_stage] ?? d.progress_stage)
    : "处理中";
  // 分母为 1 的阶段(解析/切片)只有整体完成度,报 1/1 反而像卡住
  const counts =
    d.progress_total > 1 ? ` ${d.progress_done}/${d.progress_total}` : "";
  return `${stage}${counts} · ${d.progress}%`;
}

/**
 * 终态行常驻显示耗时的状态集。失败/取消显示的是"到失败为止的墙钟",
 * 同样有参考价值(比如判断是不是刚起步就挂了);数据来自整页一次的
 * 批量接口,刷新页面后依然在。
 */
const TIMED_TERMINAL_STATUSES = new Set([
  "completed",
  "awaiting_review",
  "failed",
  "canceled",
  "paused",
]);

/**
 * 处理中的秒表:每秒刷新一次本地时间戳,让"已运行"自己走动。
 * 数字走动不需要接口配合(起点由 timing.started_at 给定),所以这里只动 state,
 * 不动网络;跑完即不再安排定时器,免得列表里挂一堆空转的 interval。
 */
function useElapsedTicker(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  // 停表期间 now 会陈旧,但那段时间显示的是后端给的总耗时,用不到它;
  // 重新开跑的第一秒里陈旧值只会被钳到 0,不会跳出一个荒唐的数
  return now;
}

/**
 * @param force 无视"只查正在跑的"这条成本纪律,主动拉一次。
 *   只给按需打开的详情用(弹窗一次一份),列表行绝不能传 true。
 */
function useIngestDurationLabel(
  doc: SourceDocument | null,
  force = false,
): string | null {
  const { data } = useDocumentIngestionStatus(
    doc?.id ?? "",
    Boolean(doc) && (force || doc?.status === "parsing"),
  );
  const timing = data?.timing;
  const now = useElapsedTicker(timing?.running === true);
  if (!timing) return null;
  // 仍在跑:从起点本地续算;已结束:直接用后端算好的端到端墙钟
  const seconds = timing.running
    ? elapsedSecondsSince(timing.started_at, now)
    : timing.elapsed_seconds;
  const text = formatDuration(seconds);
  if (!text) return null;
  return timing.running ? `已运行 ${text}` : `耗时 ${text}`;
}

/**
 * 行内耗时。单独成组件是为了让 useQuery 挂在"这一行"上——只有解析中的行
 * (以及刚跑完、缓存里还留着 running 的那一行)才真的发请求,见
 * useDocumentIngestionStatus 的成本说明。
 */
function IngestDurationLine({
  doc,
  inline,
}: {
  doc: SourceDocument;
  /** true:接在进度文案后面;false:自成一行(终态文档) */
  inline?: boolean;
}) {
  const label = useIngestDurationLabel(doc);
  if (!label) return null;
  if (inline) return <span> · {label}</span>;
  return <p className="mt-1 text-[11px] text-muted-foreground">{label}</p>;
}

/**
 * 终态行的常驻耗时:数据来自整页一次的批量接口(useDocIngestDurations),
 * 行上不挂任何 query——这正是"刷新后耗时就没了"的修复方式:不靠会话内
 * 缓存,靠一次批量请求把整页的墙钟拿回来。
 * running 兜底成"已运行"(如已暂停但仍有未收尾的 run),数字随批量轮询更新。
 */
function TerminalDurationLine({
  item,
}: {
  item: DocIngestDuration | undefined;
}) {
  const text = formatDuration(item?.elapsed_seconds);
  if (!text) return null;
  const label = item?.running ? `已运行 ${text}` : `耗时 ${text}`;
  return (
    <span className="hidden shrink-0 text-xs text-muted-foreground lg:inline">
      {label}
    </span>
  );
}

const DOC_ACTION_TOASTS: Record<string, string> = {
  reingest: "已重新入队",
  cancel: "已取消",
  pause: "已暂停，已完成的阶段会保留",
  resume: "已继续，跳过已完成阶段",
};
const BATCH_ACTION_TOASTS: Record<string, string> = {
  retry: "已重试",
  cancel: "已取消",
  pause: "已暂停",
  resume: "已继续",
};

const STATUS_FILTERS: Record<
  string,
  { label: string; match: (d: SourceDocument) => boolean }
> = {
  staged: { label: "待处理", match: (d) => d.status === "staged" },
  active: { label: "处理中", match: (d) => ACTIVE_STATUSES.has(d.status) },
  // 抽取出事实、等人工审核完才会收回 completed;此前筛不出来,只能靠肉眼找
  awaiting_review: {
    label: "待审核",
    match: (d) => d.status === "awaiting_review",
  },
  completed: { label: "已完成", match: (d) => d.status === "completed" },
  failed: { label: "失败", match: (d) => d.status === "failed" },
  canceled: { label: "已取消", match: (d) => d.status === "canceled" },
  paused: { label: "已暂停", match: (d) => d.status === "paused" },
};
const STATUS_ITEMS: Record<string, string> = {
  __all__: "全部状态",
  ...Object.fromEntries(
    Object.entries(STATUS_FILTERS).map(([k, v]) => [k, v.label]),
  ),
};
const SORT_ITEMS: Record<string, string> = {
  new: "最新上传",
  old: "最早上传",
  name: "文件名",
};
const ALL = "__all__";

interface DocumentRevision {
  id: string;
  doc_id: string;
  revision_no: number;
  sha256: string;
  status: string;
  structured_json_key: string | null;
  markdown_key: string | null;
  error: string | null;
  tombstoned_at: string | null;
  tombstone_reason: string | null;
  created_at: string | null;
}

interface StagedFile {
  file: File;
  relPath?: string;
}

interface UploadProgressState {
  total: number;
  completed: number;
  succeeded: number;
  failed: number;
  currentFilename: string | null;
  cancelRequested: boolean;
}

function latestRevision(revisions: DocumentRevision[] | undefined) {
  return revisions?.reduce<DocumentRevision | null>(
    (latest, revision) =>
      !latest || revision.revision_no > latest.revision_no ? revision : latest,
    null,
  );
}

function formatTimestamp(value: string | null) {
  if (!value) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function revisionTone(status: string) {
  if (status === "tombstoned" || status === "failed")
    return "destructive" as const;
  if (status === "active") return "success" as const;
  if (status === "staged" || status === "parsing") return "primary" as const;
  return "muted" as const;
}

function lifecycleLabel(status: SourceDocument["lifecycle_status"]) {
  return {
    active: null,
    withdrawal_pending: "撤回处理中",
    withdrawn: "已撤回",
    reingestion_pending: "重新摄入中",
    purge_pending: "永久清理中",
    purged: "已永久清理",
  }[status];
}

function lifecycleTone(status: SourceDocument["lifecycle_status"]) {
  if (
    status === "withdrawal_pending" ||
    status === "reingestion_pending" ||
    status === "purge_pending"
  )
    return "warning" as const;
  if (status === "withdrawn" || status === "purged")
    return "destructive" as const;
  return "muted" as const;
}

const PURGE_BLOCKER_LABELS: Record<string, string> = {
  DOCUMENT_NOT_WITHDRAWN: "文档尚未完成撤回",
  RETENTION_STATE_UNAVAILABLE: "无法确认审计保留状态",
  RETENTION_PERIOD_ACTIVE: "仍在审计保留期内",
  LEGAL_HOLD_ACTIVE: "存在法律保留",
  REFERENCE_REGISTRY_UNAVAILABLE: "引用登记暂不可用",
  KNOWLEDGE_SNAPSHOT_REFERENCE: "知识快照仍在引用",
  RETRIEVAL_SNAPSHOT_REFERENCE: "检索快照仍在引用",
  BUSINESS_ARTIFACT_REFERENCE: "业务产物仍在引用",
  FEEDBACK_OR_CITATION_REFERENCE: "反馈或引用仍在使用",
  PINNED_ENTITY_REFERENCE: "人工固定实体仍在引用",
  MANUAL_OR_PENDING_FACT_REFERENCE: "人工或待复核事实仍在引用",
  MEDIA_REFERENCE: "媒体资产仍在引用",
  SHARED_OBJECT_KEY_REFERENCE: "对象仍由其他资料共享",
  CROSS_REVISION_CONTENT_REFERENCE: "其他修订仍在引用内容",
};

// 管理员强制清理可跳过的文档 blocker(与后端 document_purge.FORCE_BYPASSABLE_BLOCKER_CODES 对齐);
// 法律保留/共享对象/未撤回/跨修订共享内容等完整性门禁不在此列,强制也拦
const DOC_FORCE_BYPASSABLE_BLOCKERS = new Set([
  "RETENTION_PERIOD_ACTIVE",
  "KNOWLEDGE_SNAPSHOT_REFERENCE",
  "RETRIEVAL_SNAPSHOT_REFERENCE",
  "BUSINESS_ARTIFACT_REFERENCE",
  "FEEDBACK_OR_CITATION_REFERENCE",
  "PINNED_ENTITY_REFERENCE",
  "MANUAL_OR_PENDING_FACT_REFERENCE",
  "MEDIA_REFERENCE",
]);

const PURGE_COUNT_LABELS: Record<string, string> = {
  objects: "源与派生对象",
  revisions: "修订",
  chunks: "文本切片",
  media: "媒体",
  evidence: "证据",
  fact_claims: "独占事实",
  ingest_runs: "摄入记录",
  shared_fact_claims: "共享事实",
  shared_entities: "共享实体",
  shared_relations: "共享关系",
  exclusive_entities_for_gc: "独占实体（转独立 GC）",
  shared_object_keys: "共享对象",
};

const OPERATION_PHASE_LABELS: Record<string, string> = {
  accepted: "受理",
  dispatch: "调度",
  dispatch_retry_scheduled: "等待重新调度",
  dispatch_failed: "调度失败",
  ingestion: "重新摄入",
  snapshot_build: "构建知识快照",
  snapshot_rebuild: "重建知识快照",
  snapshot_activation: "激活知识快照",
  settlement: "结算知识支持",
  support_settlement: "结算知识支持",
  projection_gc: "清理旧投影",
  retry_scheduled: "等待重试",
  dead_letter: "死信处理",
  planned: "等待执行",
  plan_revalidation: "重新校验清理计划",
  load_manifest: "载入清理清单",
  object_deletion: "删除对象内容",
  metadata_deletion: "删除资料元数据",
  verification: "校验清理结果",
  completed: "完成",
};

export function shouldPollDocumentLifecycle(document: SourceDocument) {
  const operationStatus = document.latest_operation?.status;
  return (
    ACTIVE_STATUSES.has(document.status) ||
    shouldPollReclassification(document) ||
    operationStatus === "processing" ||
    operationStatus === "pending" ||
    (!document.latest_operation &&
      (document.lifecycle_status === "withdrawal_pending" ||
        document.lifecycle_status === "reingestion_pending" ||
        document.lifecycle_status === "purge_pending"))
  );
}

export function canRetryWithdrawalOperation(document: SourceDocument) {
  return Boolean(
    document.latest_operation?.operation_type === "withdrawal" &&
    document.latest_operation.retryable &&
    (document.latest_operation.status === "failed" ||
      document.latest_operation.status === "dead_letter"),
  );
}

export function canPreviewDocumentPurge(document: SourceDocument) {
  return (
    document.lifecycle_status === "withdrawn" &&
    document.latest_operation?.status !== "pending" &&
    document.latest_operation?.status !== "processing"
  );
}

export function canReingestWithdrawnDocument(document: SourceDocument) {
  const operationStatus = document.latest_operation?.status;
  return (
    document.lifecycle_status === "withdrawn" &&
    !ACTIVE_STATUSES.has(document.status) &&
    document.status !== "paused" &&
    document.status !== "awaiting_review" &&
    operationStatus !== "pending" &&
    operationStatus !== "processing"
  );
}

function operationStatusText(operation: KbDocumentOperation) {
  const action = {
    withdrawal: "撤回发布",
    reingestion: "重新摄入",
    purge: "永久清理",
  }[operation.operation_type];
  if (operation.status === "pending" || operation.status === "processing") {
    const phase = operation.phase
      ? (OPERATION_PHASE_LABELS[operation.phase] ?? operation.phase)
      : action;
    return `正在${phase}`;
  }
  if (operation.status === "completed") return `${action}已完成`;
  if (operation.status === "dead_letter") {
    return `${action}已进入死信${operation.error_message ? `：${operation.error_message}` : ""}`;
  }
  return `${action}失败${operation.error_message ? `：${operation.error_message}` : ""}`;
}

function reportBatch(r: BatchResult, verb: string) {
  if (r.done.length) toast.success(`${verb} ${r.done.length} 项`);
  for (const s of r.skipped.slice(0, 3)) toast.warning(`跳过:${s.reason}`);
  if (r.skipped.length > 3) {
    toast.warning(`另有 ${r.skipped.length - 3} 项被跳过`);
  }
}

function toggle(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function toLocalDateTimeInput(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
}

/** 递归读取拖入的文件夹条目,保留相对路径(与 webkitdirectory 上传口径一致) */
async function readDroppedEntries(items: DataTransferItemList) {
  const staged: StagedFile[] = [];

  async function walk(entry: FileSystemEntry, prefix: string): Promise<void> {
    if (entry.isFile) {
      const file = await new Promise<File | null>((resolve) =>
        (entry as FileSystemFileEntry).file(
          (f) => resolve(f),
          () => resolve(null),
        ),
      );
      if (file && !file.name.startsWith(".")) {
        staged.push({
          file,
          relPath: prefix ? `${prefix}/${file.name}` : undefined,
        });
      }
      return;
    }
    if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      const children = await new Promise<FileSystemEntry[]>((resolve) =>
        reader.readEntries(
          (entries) => resolve(entries),
          () => resolve([]),
        ),
      );
      const nextPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
      for (const child of children) await walk(child, nextPrefix);
    }
  }

  const entries = [...items]
    .map((item) => item.webkitGetAsEntry?.() ?? null)
    .filter((entry): entry is FileSystemEntry => entry !== null);
  for (const entry of entries) await walk(entry, "");
  return staged;
}

export function SourcesView({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const currentOrg = useCurrentOrg();
  const canPurge =
    currentOrg?.role === "org_admin" || currentOrg?.role === "platform_admin";
  const { get, set } = useUrlState();
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const revisionFileRef = useRef<HTMLInputElement>(null);

  const [staged, setStaged] = useState<StagedFile[]>([]);
  const [selectedIntakeDocs, setSelectedIntakeDocs] = useState<Set<string>>(
    new Set(),
  );
  const [classificationDrafts, setClassificationDrafts] = useState<
    Record<string, string>
  >({});
  const [batchClassification, setBatchClassification] = useState<string | null>(
    null,
  );
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const [selectedDocs, setSelectedDocs] = useState<Set<string>>(new Set());
  const [historyDoc, setHistoryDoc] = useState<SourceDocument | null>(null);
  const [expiryDoc, setExpiryDoc] = useState<SourceDocument | null>(null);
  const [expiryValue, setExpiryValue] = useState("");
  const [withdrawDoc, setWithdrawDoc] = useState<SourceDocument | null>(null);
  const [withdrawImpact, setWithdrawImpact] =
    useState<DocumentWithdrawalImpact | null>(null);
  const [reingestDoc, setReingestDoc] = useState<SourceDocument | null>(null);
  const [checkingWithdrawId, setCheckingWithdrawId] = useState<string | null>(
    null,
  );
  const [purgeDoc, setPurgeDoc] = useState<SourceDocument | null>(null);
  const [purgePreview, setPurgePreview] = useState<DocumentPurgePreview | null>(
    null,
  );
  const [purgeReason, setPurgeReason] = useState("");
  const [purgeConfirmed, setPurgeConfirmed] = useState(false);
  // 强制清理:blocker 全部属于可跳过类时管理员可勾选跳过
  const [purgeForce, setPurgeForce] = useState(false);
  const [checkingPurgeId, setCheckingPurgeId] = useState<string | null>(null);
  const [reclassifyTarget, setReclassifyTarget] = useState("");
  const [reclassifyDoc, setReclassifyDoc] = useState<SourceDocument | null>(
    null,
  );
  const [uploadProgress, setUploadProgress] =
    useState<UploadProgressState | null>(null);
  const uploadCancelRequested = useRef(false);

  // 列表状态一律进 URL:刷新/分享不丢,底部发布状态条 ?status=failed 深链直达
  const detailDocId = get("doc");
  const keyword = get("q") ?? "";
  const statusFilter = get("status") ?? "";
  const sortKey = get("sort") ?? "new";
  const requestedReclassificationId = get("reclassify");
  const handledReclassificationId = useRef<string | null>(null);

  const setDocParam = (docId: string | null) =>
    set({ doc: docId }, { reset: ["start", "end"] });

  /**
   * 跳到图片视图并锁定这份文档。两边的视图内状态一并清掉:资料页的
   * 搜索词/状态/排序/锚点在图片页没有意义(还会以同名参数串味),而残留的
   * 图片筛选会让人刚进来就看到"没有图片",分不清是筛没了还是这份文档真没图。
   */
  const openDocumentImages = (docId: string) =>
    set(
      { view: "images", doc: docId },
      {
        reset: [
          "q",
          "status",
          "sort",
          "start",
          "end",
          "p",
          "enrichment",
          "review",
          "issue",
          "failure",
        ],
      },
    );

  const documentsQuery = useInfiniteQuery({
    queryKey: ["kb-docs", kbId, "paginated"],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        kb_id: kbId,
        limit: String(DOCUMENT_PAGE_SIZE),
        offset: String(pageParam),
      });
      return api.get<SourceDocument[]>(`/kb/documents?${params.toString()}`);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === DOCUMENT_PAGE_SIZE
        ? allPages.reduce((count, page) => count + page.length, 0)
        : undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.some(shouldPollDocumentLifecycle),
      )
        ? 2000
        : false,
  });
  const docs = documentsQuery.data?.pages.flatMap((page) => page);
  useEffect(() => {
    if (
      !requestedReclassificationId ||
      handledReclassificationId.current === requestedReclassificationId
    ) {
      return;
    }
    const requestedDocument = docs?.find(
      (document) => document.id === requestedReclassificationId,
    );
    if (!requestedDocument) return;
    handledReclassificationId.current = requestedReclassificationId;
    set({ reclassify: null });
    if (canReclassifyDocument(requestedDocument)) {
      setReclassifyDoc(requestedDocument);
      setReclassifyTarget(retryTargetDocType(requestedDocument) ?? "");
    } else {
      toast.error("这份文档当前不能重新抽取");
    }
  }, [docs, requestedReclassificationId, set]);
  const settingsQuery = useQuery({
    queryKey: ["ingest-settings"],
    queryFn: () => api.get<IngestSettings>("/kb/ingest/settings"),
    refetchInterval: 5000,
  });
  const settings = settingsQuery.data;
  const entityTypesQuery = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
  });
  const documentTypeOptions: DocumentTypeOption[] = [
    ...DOC_TYPES,
    ...(entityTypesQuery.data ?? [])
      .filter(
        (entityType) =>
          !entityType.is_builtin &&
          !DOC_TYPES.some((item) => item.value === entityType.type_key),
      )
      .map((entityType) => ({
        value: entityType.type_key,
        label: entityType.display_name,
      })),
  ];
  // doc_type / target_doc_type 都是开放字符串:查不到展示名就回落 key 原值
  const docTypeLabel = (typeKey: string) =>
    documentTypeOptions.find((option) => option.value === typeKey)?.label ??
    typeKey;
  const historyQuery = useQuery({
    queryKey: ["kb-doc-revisions", historyDoc?.id],
    queryFn: () =>
      api.get<DocumentRevision[]>(`/kb/documents/${historyDoc?.id}/revisions`),
    enabled: Boolean(historyDoc),
  });
  // 列表行不为终态文档发耗时请求(上百行会把连接池打满),想看某份跑了多久
  // 就从这里展开——一次只查一份,成本可控
  const historyDurationLabel = useIngestDurationLabel(historyDoc, true);

  const preflightUploads = (selected: File[]) => {
    if (!settings) {
      toast.error("上传限制尚未加载，请稍后重试");
      return null;
    }
    const uploadMaxFileBytes = settings.upload_max_file_bytes;
    const uploadMaxBatchFiles = settings.upload_max_batch_files;
    const result = documentUploadPreflight(
      selected,
      uploadMaxFileBytes,
      uploadMaxBatchFiles,
    );
    if (result.batchExceeded) {
      toast.error(
        `一次最多上传 ${uploadMaxBatchFiles} 个文件，当前 ${result.eligible} 个，请分批选择`,
      );
      return result;
    }
    const reasons = [
      result.unsupported > 0 ? `${result.unsupported} 个格式不支持` : null,
      result.empty > 0 ? `${result.empty} 个空文件` : null,
      result.oversized > 0
        ? `${result.oversized} 个超过 ${Math.floor(uploadMaxFileBytes / (1024 * 1024))} MiB`
        : null,
    ].filter(Boolean);
    if (reasons.length > 0) toast.warning(`已跳过：${reasons.join("，")}`);
    return result;
  };

  /** 选中/拖入文件后先进上传确认托盘；分类发生在文件保存成功之后。 */
  const stageFiles = (candidates: StagedFile[]) => {
    const accepted =
      preflightUploads(candidates.map((item) => item.file))?.accepted ?? [];
    if (!accepted.length) return;
    const acceptedSet = new Set(accepted);
    setStaged((current) => [
      ...current,
      ...candidates.filter((item) => acceptedSet.has(item.file)),
    ]);
  };

  const uploadRevision = useMutation({
    mutationFn: async ({ doc, file }: { doc: SourceDocument; file: File }) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm<DocumentRevision>(
        `/kb/documents/${doc.id}/revisions`,
        form,
      );
    },
    onSuccess: (revision, { doc }) => {
      toast.success(
        `Revision ${revision.revision_no} 已上传；请在待处理文件中确认类型并手动排队`,
      );
      setHistoryDoc((current) =>
        current?.id === doc.id ? { ...current, status: "staged" } : current,
      );
      queryClient.invalidateQueries({ queryKey: ["kb-doc-revisions", doc.id] });
      queryClient.invalidateQueries({ queryKey: ["kb-docs", kbId] });
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const updateExpiry = useMutation({
    mutationFn: (expiresAt: string | null) => {
      if (!expiryDoc) throw new Error("未选择文档");
      return api.patch<SourceDocument>(`/kb/documents/${expiryDoc.id}`, {
        expires_at: expiresAt,
      });
    },
    onSuccess: (updated) => {
      setExpiryDoc(updated);
      setExpiryValue(toLocalDateTimeInput(updated.expires_at));
      toast.success(
        updated.expires_at ? "文档有效期已更新" : "文档已设为长期有效",
      );
      queryClient.invalidateQueries({ queryKey: ["kb-docs", kbId] });
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const invalidateDocs = () =>
    queryClient.invalidateQueries({ queryKey: ["kb-docs", kbId] });

  const classifyDocuments = useMutation({
    mutationFn: (items: DocumentClassificationItem[]) =>
      api.post<DocumentClassificationResult>("/kb/documents/classifications", {
        items,
      }),
    onSuccess: async (result) => {
      if (result.updated.length > 0) {
        toast.success(`${result.updated.length} 份文件的类型已保存`);
      }
      for (const skipped of result.skipped.slice(0, 3)) {
        toast.warning(`分类未保存：${skipped.reason}`);
      }
      if (result.skipped.length > 3) {
        toast.warning(`另有 ${result.skipped.length - 3} 份文件分类未保存`);
      }
      const settledIds = new Set([
        ...result.updated.map((item) => item.document_id),
        ...result.skipped.map((item) => item.document_id),
      ]);
      await invalidateDocs();
      setBatchClassification(null);
      setClassificationDrafts((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([documentId]) => !settledIds.has(documentId),
          ),
        ),
      );
    },
    onError: (error, items) => {
      const attemptedIds = new Set(items.map((item) => item.document_id));
      setClassificationDrafts((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([documentId]) => !attemptedIds.has(documentId),
          ),
        ),
      );
      setBatchClassification(null);
      toast.error(errMsg(error));
    },
  });

  const enqueueDocuments = useMutation({
    mutationFn: (items: DocumentIngestionQueueItem[]) =>
      api.post<DocumentIngestionQueueResult>("/kb/documents/ingestion-queue", {
        items,
      }),
    onSuccess: async (result) => {
      if (result.queued.length > 0) {
        toast.success(`${result.queued.length} 份文件已排入解析队列`);
      }
      for (const skipped of result.skipped.slice(0, 3)) {
        toast.warning(`跳过：${skipped.reason}`);
      }
      if (result.skipped.length > 3) {
        toast.warning(`另有 ${result.skipped.length - 3} 份文件被跳过`);
      }
      const queuedIds = new Set(result.queued.map((item) => item.document_id));
      setSelectedIntakeDocs((current) => {
        const next = new Set(current);
        for (const documentId of queuedIds) next.delete(documentId);
        return next;
      });
      setClassificationDrafts((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([documentId]) => !queuedIds.has(documentId),
          ),
        ),
      );
      setBatchClassification(null);
      await invalidateDocs();
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  // 目标类型开放:内置 DocType + 任意已注册实体类型 key(后端 _resolve_doc_type)
  const reclassifyDocument = useMutation({
    mutationFn: ({
      document,
      targetDocType,
    }: {
      document: SourceDocument;
      targetDocType: string;
    }) =>
      api.post<DocumentReclassificationAccepted>(
        `/kb/documents/${document.id}/reclassify`,
        { target_doc_type: targetDocType },
      ),
    onSuccess: async () => {
      setReclassifyDoc(null);
      setReclassifyTarget("");
      toast.success("二次抽取已排队；完成后仍需审核并发布新快照");
      await invalidateDocs();
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const upload = useMutation({
    mutationFn: async (files: StagedFile[]) => {
      let ok = 0;
      const failed: { filename: string; reason: string }[] = [];
      for (const { file, relPath } of files) {
        if (uploadCancelRequested.current) break;
        setUploadProgress((current) =>
          current ? { ...current, currentFilename: file.name } : current,
        );
        const form = new FormData();
        form.append("file", file);
        const params = new URLSearchParams({ kb_id: kbId });
        if (relPath) params.set("rel_path", relPath);
        try {
          await api.postForm<SourceDocument>(`/kb/documents?${params}`, form);
          ok += 1;
        } catch (err) {
          failed.push({ filename: file.name, reason: errMsg(err) });
        } finally {
          setUploadProgress((current) =>
            current
              ? {
                  ...current,
                  completed: current.completed + 1,
                  succeeded: ok,
                  failed: failed.length,
                }
              : current,
          );
        }
      }
      return {
        ok,
        failed,
        canceled: Math.max(files.length - ok - failed.length, 0),
      };
    },
    onMutate: (files) => {
      uploadCancelRequested.current = false;
      setUploadProgress({
        total: files.length,
        completed: 0,
        succeeded: 0,
        failed: 0,
        currentFilename: files[0]?.file.name ?? null,
        cancelRequested: false,
      });
    },
    onSuccess: ({ ok, failed, canceled }) => {
      if (ok > 0) {
        toast.success(`已上传 ${ok} 份；请在待处理文件中分类并手动排队`);
      }
      if (canceled > 0) toast.info(`已停止队列，${canceled} 份文件未上传`);
      if (failed.length > 0) {
        const details = failed
          .slice(0, 3)
          .map(({ filename, reason }) => `${filename}: ${reason}`)
          .join("；");
        toast.error(
          `上传失败 ${failed.length} 份${details ? `：${details}` : ""}`,
        );
      }
      setStaged([]);
      invalidateDocs();
    },
    onSettled: () => {
      setUploadProgress((current) =>
        current ? { ...current, currentFilename: null } : current,
      );
    },
  });

  const setConcurrency = useMutation({
    mutationFn: (n: number) =>
      api.put<IngestSettings>("/kb/ingest/settings", { max_concurrency: n }),
    onSuccess: (s) => {
      toast.success(`全局摄入并行度已设为 ${s.max_concurrency}`);
      queryClient.invalidateQueries({ queryKey: ["ingest-settings"] });
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const docAction = useMutation({
    mutationFn: ({
      id,
      action,
    }: {
      id: string;
      action: "reingest" | "cancel" | "pause" | "resume";
    }) => api.post<SourceDocument>(`/kb/documents/${id}/${action}`),
    onSuccess: (_, { action }) => {
      toast.success(DOC_ACTION_TOASTS[action]);
      invalidateDocs();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const reingestWithdrawnDocument = useMutation({
    mutationFn: (document: SourceDocument) =>
      api.post<SourceDocument>(`/kb/documents/${document.id}/reingest`),
    onSuccess: async (_, document) => {
      setReingestDoc(null);
      toast.success("重新摄入已受理；新修订发布完成前文档仍保持撤回");
      await Promise.all([
        invalidateDocs(),
        queryClient.invalidateQueries({
          queryKey: ["kb-doc-revisions", document.id],
        }),
        queryClient.invalidateQueries({ queryKey: ["kb-fact-claims", kbId] }),
        queryClient.invalidateQueries({ queryKey: ["kb-snapshots", kbId] }),
      ]);
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const docBatch = useMutation({
    mutationFn: (action: "retry" | "cancel" | "pause" | "resume") =>
      api.post<BatchResult>("/kb/documents/batch", {
        ids: [...selectedDocs],
        action,
      }),
    onSuccess: (r, action) => {
      reportBatch(r, BATCH_ACTION_TOASTS[action]);
      setSelectedDocs(new Set());
      invalidateDocs();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const withdrawDocument = useMutation({
    mutationFn: (doc: SourceDocument) => {
      const params = new URLSearchParams({
        reason: "withdrawn from knowledge base via sources view",
      });
      return api.delete<KbDocumentOperation>(
        `/kb/documents/${doc.id}?${params.toString()}`,
      );
    },
    onSuccess: (operation, doc) => {
      setWithdrawDoc(null);
      setWithdrawImpact(null);
      toast.success(
        operation.status === "completed"
          ? `文档已撤回 · operation ${operation.id.slice(0, 8)}`
          : `撤回已受理 · operation ${operation.id.slice(0, 8)}`,
      );
      invalidateDocs();
      queryClient.invalidateQueries({ queryKey: ["kb-doc-revisions", doc.id] });
      queryClient.invalidateQueries({ queryKey: ["kb-fact-claims", kbId] });
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const retryWithdrawalOperation = useMutation({
    mutationFn: (operation: KbDocumentOperation) =>
      api.post<KbDocumentOperation>(
        `/kb/document-operations/${operation.id}/retry`,
      ),
    onSuccess: (operation) => {
      toast.success(`已重新调度 · operation ${operation.id.slice(0, 8)}`);
      invalidateDocs();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const loadPurgePreview = (doc: SourceDocument) =>
    queryClient.fetchQuery({
      queryKey: ["kb-document-purge-preview", doc.id],
      queryFn: () =>
        api.get<DocumentPurgePreview>(`/kb/documents/${doc.id}/purge-preview`),
      staleTime: 0,
    });

  const preparePurge = async (doc: SourceDocument) => {
    if (!canPurge || !canPreviewDocumentPurge(doc)) return;
    setCheckingPurgeId(doc.id);
    try {
      const preview = await loadPurgePreview(doc);
      setPurgeDoc(doc);
      setPurgePreview(preview);
      setPurgeReason("");
      setPurgeConfirmed(false);
      setPurgeForce(false);
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setCheckingPurgeId(null);
    }
  };

  const refreshPurgePreview = async (reportError = true) => {
    if (!purgeDoc) return false;
    setCheckingPurgeId(purgeDoc.id);
    setPurgeConfirmed(false);
    setPurgeForce(false);
    try {
      await queryClient.invalidateQueries({
        queryKey: ["kb-document-purge-preview", purgeDoc.id],
        refetchType: "none",
      });
      setPurgePreview(await loadPurgePreview(purgeDoc));
      return true;
    } catch (err) {
      if (reportError) toast.error(errMsg(err));
      return false;
    } finally {
      setCheckingPurgeId(null);
    }
  };

  const purgeDocument = useMutation({
    mutationFn: ({
      doc,
      preview,
      reason,
      force,
    }: {
      doc: SourceDocument;
      preview: DocumentPurgePreview;
      reason: string;
      force: boolean;
    }) =>
      api.post<KbDocumentOperation>(`/kb/documents/${doc.id}/purge`, {
        expected_plan_hash: preview.plan_hash,
        reason: reason.trim(),
        confirm_irreversible: true,
        force,
      }),
    onSuccess: (operation) => {
      setPurgeDoc(null);
      setPurgePreview(null);
      setPurgeReason("");
      setPurgeConfirmed(false);
      setPurgeForce(false);
      toast.success(`永久清理已受理 · operation ${operation.id.slice(0, 8)}`);
      invalidateDocs();
    },
    onError: async (err) => {
      const message = errMsg(err);
      if (!purgeDoc) {
        toast.error(message);
        return;
      }
      const refreshed = await refreshPurgePreview(false);
      if (!refreshed) {
        toast.error(message);
        return;
      }
      toast.warning(
        err instanceof ApiError && err.status === 409
          ? "清理条件已变化，请查看最新预检结果并重新确认"
          : `永久清理未受理：${message}。已重新预检，请确认后再次提交`,
      );
    },
  });

  const prepareWithdraw = async (doc: SourceDocument) => {
    if (doc.lifecycle_status !== "active") {
      toast.info(
        doc.lifecycle_status === "purged" ? "该文档已永久清理" : "该文档已撤回",
      );
      return;
    }
    setCheckingWithdrawId(doc.id);
    try {
      const impact = await queryClient.fetchQuery({
        queryKey: ["kb-document-withdrawal-impact", doc.id],
        queryFn: () =>
          api.get<DocumentWithdrawalImpact>(
            `/kb/documents/${doc.id}/withdrawal-impact`,
          ),
        staleTime: 0,
      });
      setWithdrawImpact(impact);
      setWithdrawDoc(doc);
    } catch (err) {
      toast.error(errMsg(err));
    } finally {
      setCheckingWithdrawId(null);
    }
  };

  // ---- 筛选 / 排序(对已加载页做,未加载页如实提示) ----
  const keywordLower = keyword.trim().toLowerCase();
  const statusMatch = STATUS_FILTERS[statusFilter]?.match;
  const filtered = (docs ?? []).filter((doc) => {
    if (statusMatch && !statusMatch(doc)) return false;
    if (!keywordLower) return true;
    return (
      doc.filename.toLowerCase().includes(keywordLower) ||
      (doc.rel_path ?? "").toLowerCase().includes(keywordLower)
    );
  });
  const sorted = [...filtered].sort((a, b) => {
    if (sortKey === "name") return a.filename.localeCompare(b.filename, "zh");
    const at = new Date(a.created_at).getTime();
    const bt = new Date(b.created_at).getTime();
    return sortKey === "old" ? at - bt : bt - at;
  });

  const intakeDocuments = sorted.filter(
    (document) =>
      document.status === "staged" && document.lifecycle_status === "active",
  );
  const workflowDocuments = sorted.filter(
    (document) => document.status !== "staged",
  );
  const rows: (
    | { kind: "folder"; key: string; folder: string }
    | { kind: "doc"; key: string; doc: SourceDocument; indented: boolean }
  )[] = [];

  if (sortKey === "name") {
    for (const document of workflowDocuments) {
      rows.push({
        kind: "doc",
        key: document.id,
        doc: document,
        indented: false,
      });
    }
  } else {
    const groups = new Map<string, SourceDocument[]>();
    for (const document of workflowDocuments) {
      const folder = document.rel_path?.includes("/")
        ? document.rel_path.slice(0, document.rel_path.lastIndexOf("/"))
        : "";
      groups.set(folder, [...(groups.get(folder) ?? []), document]);
    }
    for (const [folder, items] of groups) {
      if (folder) {
        rows.push({
          kind: "folder",
          key: `folder:${folder}`,
          folder,
        });
      }
      for (const document of items) {
        rows.push({
          kind: "doc",
          key: document.id,
          doc: document,
          indented: Boolean(folder),
        });
      }
    }
  }

  const listRef = useRef<HTMLDivElement>(null);
  const fetchNextPage = documentsQuery.fetchNextPage;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 48,
    overscan: 8,
  });
  const virtualItems = virtualizer.getVirtualItems();
  useEndReached({
    lastIndex: virtualItems.length
      ? virtualItems[virtualItems.length - 1].index
      : -1,
    count: rows.length,
    hasMore: documentsQuery.hasNextPage,
    loading: documentsQuery.isFetchingNextPage,
    onEndReached: useCallback(() => void fetchNextPage(), [fetchNextPage]),
  });

  const activeCount =
    docs?.filter((d) => ACTIVE_STATUSES.has(d.status)).length ?? 0;
  // 终态行的耗时:当前列表(跟随分页/筛选)一次批量请求;解析中的行不在
  // 此列——它们走 useDocumentIngestionStatus 的本地秒表,别重复请求
  const { data: ingestDurations } = useDocIngestDurations(
    kbId,
    workflowDocuments
      .filter((d) => TIMED_TERMINAL_STATUSES.has(d.status))
      .map((d) => d.id),
    activeCount > 0,
  );
  const allVisibleSelected =
    workflowDocuments.length > 0 &&
    workflowDocuments.every((d) => selectedDocs.has(d.id));
  const filtering = Boolean(keywordLower || statusFilter);
  const intakeAssignments = Object.fromEntries(
    intakeDocuments.map((document) => [
      document.id,
      classificationDrafts[document.id] ??
        (document.doc_type !== "unclassified" ? document.doc_type : ""),
    ]),
  );

  const applyBatchClassification = (docType: string) => {
    const items = intakeDocuments
      .filter((document) => selectedIntakeDocs.has(document.id))
      .map((document) => ({
        document_id: document.id,
        doc_type: docType,
      }));
    if (items.length === 0) return;
    setBatchClassification(docType);
    setClassificationDrafts((current) => {
      const next = { ...current };
      for (const item of items) {
        next[item.document_id] = item.doc_type;
      }
      return next;
    });
    classifyDocuments.mutate(items);
  };

  const applyDocumentClassification = (documentId: string, docType: string) => {
    setClassificationDrafts((current) => ({
      ...current,
      [documentId]: docType,
    }));
    classifyDocuments.mutate([{ document_id: documentId, doc_type: docType }]);
  };

  const enqueueSelectedIntakeDocuments = () => {
    const selectedDocuments = intakeDocuments.filter((document) =>
      selectedIntakeDocs.has(document.id),
    );
    if (selectedDocuments.some((document) => !intakeAssignments[document.id])) {
      toast.error("请先为每份已选文件指定划分 / 抽取类型");
      return;
    }
    enqueueDocuments.mutate(
      selectedDocuments.map((document) => ({
        document_id: document.id,
      })),
    );
  };

  // 详情模式:?doc= 存在时中央视图切换为文档详情(预览+切片),带上下篇导航
  if (detailDocId) {
    const start = Number(get("start"));
    const end = Number(get("end"));
    const initialAnchor =
      Number.isInteger(start) && start > 0
        ? {
            startLine: start,
            endLine: Number.isInteger(end) && end >= start ? end : undefined,
          }
        : null;
    const siblings = sorted.length ? sorted : (docs ?? []);
    const index = siblings.findIndex((doc) => doc.id === detailDocId);
    return (
      <DocDetail
        key={detailDocId}
        kbId={kbId}
        docId={detailDocId}
        initialAnchor={initialAnchor}
        onBack={() => setDocParam(null)}
        onPrev={
          index > 0 ? () => setDocParam(siblings[index - 1].id) : undefined
        }
        onNext={
          index >= 0 && index < siblings.length - 1
            ? () => setDocParam(siblings[index + 1].id)
            : undefined
        }
        position={
          index >= 0
            ? { current: index + 1, total: siblings.length }
            : undefined
        }
      />
    );
  }

  const onDrop = async (event: React.DragEvent) => {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (upload.isPending) return;
    // 优先按目录条目读取(可拖入整个文件夹并保留 rel_path);
    // 浏览器不支持时 readDroppedEntries 返回空,回退到平铺文件列表
    const items = event.dataTransfer.items;
    const dropped = items?.length ? await readDroppedEntries(items) : [];
    if (dropped.length) {
      stageFiles(dropped);
      return;
    }
    const files = [...event.dataTransfer.files].filter(
      (file) => !file.name.startsWith("."),
    );
    if (files.length) stageFiles(files.map((file) => ({ file })));
  };

  return (
    <div
      className="relative flex h-full min-h-0 flex-col gap-3"
      onDragEnter={(event) => {
        if (![...event.dataTransfer.types].includes("Files")) return;
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          event.preventDefault();
        }
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={onDrop}
    >
      {/* 工具条:搜索 / 状态 / 排序 / 摄入并行度 */}
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput
          value={keyword}
          onChange={(next) => set({ q: next || null })}
          label="搜索文档"
          placeholder="搜索文件名或目录"
          className="w-full sm:w-64"
        />
        <Select
          items={STATUS_ITEMS}
          value={statusFilter || ALL}
          onValueChange={(value) =>
            set({ status: String(value) === ALL ? null : String(value) })
          }
        >
          <SelectTrigger size="sm" aria-label="按状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(STATUS_ITEMS).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          items={SORT_ITEMS}
          value={sortKey}
          onValueChange={(value) =>
            set({ sort: String(value) === "new" ? null : String(value) })
          }
        >
          <SelectTrigger size="sm" aria-label="排序方式">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(SORT_ITEMS).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            disabled={upload.isPending || !settings}
            onClick={() => fileRef.current?.click()}
          >
            <Upload className="size-4" />
            上传文件
          </Button>
          {activeCount > 0 && (
            <ToneBadge tone="primary" className="rounded-full">
              {activeCount} 处理中
            </ToneBadge>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="outline"
                  size="icon"
                  className="size-8"
                  aria-label="更多资料操作"
                />
              }
            >
              <MoreHorizontal className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem
                disabled={upload.isPending || !settings}
                onClick={() => folderRef.current?.click()}
              >
                <FolderUp />
                上传文件夹
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <div className="px-2 py-1.5" title="平台级设置，影响所有知识库">
                <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <SlidersHorizontal className="size-3.5" />
                  解析并行度
                </p>
                <Select
                  items={CONCURRENCY_ITEMS}
                  value={String(settings?.max_concurrency ?? 2)}
                  onValueChange={(v) => setConcurrency.mutate(Number(v))}
                >
                  <SelectTrigger size="sm" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {CONCURRENCY_OPTIONS.map((n) => (
                      <SelectItem key={n} value={String(n)}>
                        {n}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        multiple
        accept={SUPPORTED_DOCUMENT_ACCEPT}
        className="hidden"
        onChange={(e) => {
          stageFiles([...(e.target.files ?? [])].map((file) => ({ file })));
          e.target.value = "";
        }}
      />
      <input
        ref={folderRef}
        type="file"
        multiple
        accept={SUPPORTED_DOCUMENT_ACCEPT}
        // @ts-expect-error 非标准属性,Chromium/Firefox/Safari 均支持
        webkitdirectory=""
        className="hidden"
        onChange={(e) => {
          stageFiles(
            [...(e.target.files ?? [])]
              .filter((file) => !file.name.startsWith("."))
              .map((file) => ({
                file,
                relPath:
                  (file as File & { webkitRelativePath?: string })
                    .webkitRelativePath || undefined,
              })),
          );
          e.target.value = "";
        }}
      />

      {staged.length > 0 && (
        <div className="rounded-lg border border-border bg-card">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <span className="text-sm font-medium">
              待上传 {staged.length} 份
            </span>
            <Button
              size="xs"
              className="ml-auto"
              disabled={upload.isPending}
              onClick={() => upload.mutate(staged)}
            >
              {upload.isPending && <LoaderCircle className="animate-spin" />}
              开始上传
            </Button>
            <Button
              size="xs"
              variant="ghost"
              disabled={upload.isPending}
              onClick={() => setStaged([])}
            >
              清空
            </Button>
          </div>
          <div className="max-h-32 divide-y divide-border overflow-y-auto px-3">
            {staged.map((item, index) => (
              <div
                key={`${item.relPath ?? item.file.name}-${index}`}
                className="flex items-center gap-2 py-1.5 text-xs"
              >
                <span
                  className="min-w-0 flex-1 truncate"
                  title={item.relPath ?? item.file.name}
                >
                  {item.relPath ?? item.file.name}
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {(item.file.size / 1024).toFixed(0)} KB
                </span>
                <button
                  type="button"
                  aria-label={`移除 ${item.file.name}`}
                  className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:text-destructive"
                  onClick={() =>
                    setStaged((current) =>
                      current.filter((_, i) => i !== index),
                    )
                  }
                >
                  <Ban className="size-3" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {uploadProgress && (
        <div className="space-y-2 rounded-md border border-border p-2.5 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="min-w-0 truncate">
              {upload.isPending
                ? `正在上传：${uploadProgress.currentFilename ?? "收尾中"}`
                : uploadProgress.cancelRequested
                  ? "本次上传已停止"
                  : "本次上传完成"}
            </span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {uploadProgress.completed}/{uploadProgress.total}
            </span>
          </div>
          <progress
            className="h-1.5 w-full accent-primary"
            max={uploadProgress.total}
            value={uploadProgress.completed}
            aria-label="上传进度"
          />
          <div className="flex items-center justify-between gap-2 text-muted-foreground">
            <span>
              成功 {uploadProgress.succeeded} · 失败 {uploadProgress.failed}
            </span>
            {upload.isPending ? (
              <Button
                size="xs"
                variant="outline"
                disabled={uploadProgress.cancelRequested}
                onClick={() => {
                  uploadCancelRequested.current = true;
                  setUploadProgress((current) =>
                    current ? { ...current, cancelRequested: true } : current,
                  );
                }}
              >
                <Ban className="size-3" />
                {uploadProgress.cancelRequested
                  ? "将在当前文件后停止"
                  : "停止后续上传"}
              </Button>
            ) : (
              <Button
                size="xs"
                variant="ghost"
                onClick={() => setUploadProgress(null)}
              >
                关闭
              </Button>
            )}
          </div>
        </div>
      )}

      {intakeDocuments.length > 0 && (
        <DocumentIntakePanel
          documents={intakeDocuments}
          typeOptions={documentTypeOptions}
          selected={selectedIntakeDocs}
          assignments={intakeAssignments}
          batchType={batchClassification}
          classificationPending={classifyDocuments.isPending}
          enqueuePending={enqueueDocuments.isPending}
          onToggle={(documentId) =>
            setSelectedIntakeDocs((current) => toggle(current, documentId))
          }
          onToggleAll={(checked) =>
            setSelectedIntakeDocs(
              checked
                ? new Set(intakeDocuments.map((document) => document.id))
                : new Set(),
            )
          }
          onBatchTypeChange={applyBatchClassification}
          onAssignmentChange={applyDocumentClassification}
          onEnqueue={enqueueSelectedIntakeDocuments}
        />
      )}

      {settingsQuery.isError && (
        <p className="text-xs text-destructive">
          上传配置加载失败：{errMsg(settingsQuery.error)}
        </p>
      )}

      <BulkActionBar
        count={selectedDocs.size}
        onClear={() => setSelectedDocs(new Set())}
      >
        <Button
          size="xs"
          variant="outline"
          disabled={docBatch.isPending}
          onClick={() => docBatch.mutate("retry")}
        >
          <RotateCcw className="size-3" />
          批量重试
        </Button>
        <Button
          size="xs"
          variant="outline"
          disabled={docBatch.isPending}
          onClick={() => docBatch.mutate("cancel")}
        >
          <Ban className="size-3" />
          批量取消
        </Button>
      </BulkActionBar>

      {/* 列表头:全选 + 计数 */}
      {workflowDocuments.length > 0 && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-1.5 pb-1.5 text-xs text-muted-foreground">
          <Checkbox
            checked={allVisibleSelected}
            aria-label="全选当前列表"
            onCheckedChange={(checked) =>
              setSelectedDocs(
                checked === true
                  ? new Set(workflowDocuments.map((doc) => doc.id))
                  : new Set(),
              )
            }
          />
          <span className="tabular-nums">
            {filtering
              ? `匹配 ${workflowDocuments.length} / 已加载 ${docs?.length ?? 0}`
              : `全部文件 ${workflowDocuments.length}`}
          </span>
          {filtering && documentsQuery.hasNextPage && <span>还有更多结果</span>}
        </div>
      )}

      {documentsQuery.isError && (
        <p className="text-xs text-destructive">
          {errMsg(documentsQuery.error)}
        </p>
      )}
      {documentsQuery.isPending && (
        <p className="text-xs text-muted-foreground">正在加载文档…</p>
      )}
      {!documentsQuery.isPending &&
        !documentsQuery.isError &&
        sorted.length === 0 && (
          <EmptyState
            icon={Inbox}
            title={filtering ? "没有匹配的文档" : "还没有文档"}
            description={filtering ? "换个关键词，或清除筛选。" : undefined}
            action={
              filtering ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => set({ q: null, status: null })}
                >
                  清除筛选
                </Button>
              ) : undefined
            }
          />
        )}

      {/* 列表自带滚动容器,虚拟渲染只挂载视窗内的行;滚到接近底部自动取下一页 */}
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto pr-1">
        <div
          className="relative w-full"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualItems.map((item) => {
            const row = rows[item.index];
            return (
              <div
                key={row.key}
                ref={virtualizer.measureElement}
                data-index={item.index}
                className="absolute top-0 left-0 w-full"
                style={{ transform: `translateY(${item.start}px)` }}
              >
                {row.kind === "folder" ? (
                  <div className="flex items-center gap-1 border-b border-border bg-muted/30 px-2 py-1.5 text-xs font-medium text-muted-foreground">
                    <Folder className="size-3" />
                    {row.folder}
                  </div>
                ) : (
                  (() => {
                    const d = row.doc;
                    const reclassifyStatus = reclassificationStatus(
                      d,
                      docTypeLabel,
                    );
                    return (
                      <div
                        className={cn(
                          "group border-b border-border px-2 py-2 transition-colors hover:bg-muted/35",
                          row.indented && "pl-5",
                          selectedDocs.has(d.id) && "bg-primary/5",
                        )}
                      >
                        <div className="flex items-center gap-2 text-sm">
                          <Checkbox
                            checked={selectedDocs.has(d.id)}
                            aria-label={`选择 ${d.filename}`}
                            onCheckedChange={() =>
                              setSelectedDocs((s) => toggle(s, d.id))
                            }
                          />
                          <button
                            type="button"
                            className="min-w-0 flex-1 truncate text-left transition-colors hover:text-primary hover:underline"
                            title={`${d.filename}(点击查看预览与切片)`}
                            onClick={() => setDocParam(d.id)}
                          >
                            {d.filename}
                          </button>
                          <span
                            className="hidden w-24 shrink-0 truncate text-xs text-muted-foreground md:block"
                            title={
                              documentTypeOptions.find(
                                (option) => option.value === d.doc_type,
                              )?.label ??
                              DOC_TYPE_ITEMS[d.doc_type] ??
                              d.doc_type
                            }
                          >
                            {documentTypeOptions.find(
                              (option) => option.value === d.doc_type,
                            )?.label ??
                              DOC_TYPE_ITEMS[d.doc_type] ??
                              (d.doc_type === "unclassified"
                                ? "未分类"
                                : d.doc_type)}
                          </span>
                          {(d.lifecycle_status === "active" ||
                            d.status !== "completed") && (
                            <DocStatusBadge status={d.status} />
                          )}
                          {lifecycleLabel(d.lifecycle_status) && (
                            <ToneBadge tone={lifecycleTone(d.lifecycle_status)}>
                              {lifecycleLabel(d.lifecycle_status)}
                            </ToneBadge>
                          )}

                          {/* 高频动作(重试/取消)在行内,其余收进溢出菜单 */}
                          {d.lifecycle_status === "active" &&
                            RETRYABLE_STATUSES.has(d.status) && (
                              <Button
                                variant="ghost"
                                size="icon"
                                className="size-7"
                                aria-label={`重试 ${d.filename}`}
                                title="重试"
                                onClick={() =>
                                  docAction.mutate({
                                    id: d.id,
                                    action: "reingest",
                                  })
                                }
                              >
                                <RotateCcw className="size-3.5" />
                              </Button>
                            )}
                          {PAUSABLE_STATUSES.has(d.status) && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7"
                              aria-label={`暂停 ${d.filename}`}
                              title="暂停(已完成的阶段保留,可继续)"
                              onClick={() =>
                                docAction.mutate({ id: d.id, action: "pause" })
                              }
                            >
                              <Pause className="size-3.5" />
                            </Button>
                          )}
                          {RESUMABLE_STATUSES.has(d.status) && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7"
                              aria-label={`继续 ${d.filename}`}
                              title="继续(跳过已完成阶段)"
                              onClick={() =>
                                docAction.mutate({ id: d.id, action: "resume" })
                              }
                            >
                              <Play className="size-3.5 text-primary" />
                            </Button>
                          )}
                          {ACTIVE_STATUSES.has(d.status) && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-7"
                              aria-label={`取消 ${d.filename}`}
                              title="取消"
                              onClick={() =>
                                docAction.mutate({ id: d.id, action: "cancel" })
                              }
                            >
                              <Ban className="size-3.5 text-destructive" />
                            </Button>
                          )}
                          {TIMED_TERMINAL_STATUSES.has(d.status) && (
                            <TerminalDurationLine
                              item={ingestDurations?.get(d.id)}
                            />
                          )}
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              render={
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="size-7 opacity-60 transition-opacity group-hover:opacity-100 data-popup-open:opacity-100"
                                  aria-label={`${d.filename} 的更多操作`}
                                />
                              }
                            >
                              {checkingWithdrawId === d.id ||
                              checkingPurgeId === d.id ? (
                                <LoaderCircle className="size-3.5 animate-spin" />
                              ) : (
                                <MoreHorizontal className="size-3.5" />
                              )}
                            </DropdownMenuTrigger>
                            <DropdownMenuContent>
                              {/* 图片页不再靠"文档下拉"筛选(文件一多就翻不动),
                                  改成从这里带着 doc 参数进去,直接落到这份文档 */}
                              <DropdownMenuItem
                                onClick={() => openDocumentImages(d.id)}
                              >
                                <Images />
                                查看图片
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onClick={() => setHistoryDoc(d)}
                              >
                                <History />
                                修订历史
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onClick={() => {
                                  setExpiryDoc(d);
                                  setExpiryValue(
                                    toLocalDateTimeInput(d.expires_at),
                                  );
                                }}
                              >
                                <CalendarClock />
                                有效期设置
                              </DropdownMenuItem>
                              {canReingestWithdrawnDocument(d) && (
                                <DropdownMenuItem
                                  disabled={reingestWithdrawnDocument.isPending}
                                  onClick={() => setReingestDoc(d)}
                                >
                                  <RotateCcw />
                                  重新摄入
                                </DropdownMenuItem>
                              )}
                              {canReclassifyDocument(d) && (
                                <DropdownMenuItem
                                  disabled={reclassifyDocument.isPending}
                                  onClick={() => {
                                    setReclassifyDoc(d);
                                    setReclassifyTarget(
                                      retryTargetDocType(d) ?? "",
                                    );
                                  }}
                                >
                                  <RotateCcw />
                                  {d.latest_reclassification?.status ===
                                  "failed"
                                    ? "重试二次抽取"
                                    : "重新抽取为…"}
                                </DropdownMenuItem>
                              )}
                              {canRetryWithdrawalOperation(d) && (
                                <DropdownMenuItem
                                  disabled={retryWithdrawalOperation.isPending}
                                  onClick={() =>
                                    retryWithdrawalOperation.mutate(
                                      d.latest_operation!,
                                    )
                                  }
                                >
                                  <RotateCcw />
                                  重试撤回发布
                                </DropdownMenuItem>
                              )}
                              {canPurge && canPreviewDocumentPurge(d) && (
                                <DropdownMenuItem
                                  variant="destructive"
                                  disabled={
                                    checkingPurgeId === d.id ||
                                    purgeDocument.isPending
                                  }
                                  onClick={() => void preparePurge(d)}
                                >
                                  {checkingPurgeId === d.id ? (
                                    <LoaderCircle className="animate-spin" />
                                  ) : (
                                    <Trash2 />
                                  )}
                                  {d.latest_operation?.operation_type ===
                                    "purge" &&
                                  (d.latest_operation.status === "failed" ||
                                    d.latest_operation.status === "dead_letter")
                                    ? "重新预检并永久清理"
                                    : "永久清理"}
                                </DropdownMenuItem>
                              )}
                              {canPurge &&
                                d.lifecycle_status === "purge_pending" && (
                                  <DropdownMenuItem disabled>
                                    <LoaderCircle className="animate-spin" />
                                    永久清理中
                                  </DropdownMenuItem>
                                )}
                              {canPurge && d.lifecycle_status === "purged" && (
                                <DropdownMenuItem disabled>
                                  <Trash2 />
                                  已永久清理
                                </DropdownMenuItem>
                              )}
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                variant="destructive"
                                disabled={
                                  d.lifecycle_status !== "active" ||
                                  checkingWithdrawId === d.id ||
                                  withdrawDocument.isPending
                                }
                                onClick={() => void prepareWithdraw(d)}
                              >
                                <Trash2 />
                                {d.lifecycle_status === "active"
                                  ? "撤回文档"
                                  : lifecycleLabel(d.lifecycle_status)}
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                        {d.status === "parsing" && (
                          <div className="mt-1.5 space-y-1">
                            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                              <div
                                className="h-full rounded-full bg-primary transition-all"
                                style={{ width: `${Math.max(d.progress, 4)}%` }}
                              />
                            </div>
                            <p className="text-[11px] text-muted-foreground">
                              {ingestProgressLabel(d)}
                              <IngestDurationLine doc={d} inline />
                            </p>
                          </div>
                        )}
                        {reclassifyStatus && (
                          <p
                            className={cn(
                              "mt-1 text-[11px] text-muted-foreground",
                              reclassifyStatus.failed &&
                                "text-destructive",
                            )}
                          >
                            {reclassifyStatus.text}
                          </p>
                        )}
                        {d.latest_operation &&
                          d.lifecycle_status !== "active" &&
                          d.latest_operation.status !== "completed" && (
                            <p
                              className={cn(
                                "mt-1 text-[11px] text-muted-foreground",
                                (d.latest_operation.status === "failed" ||
                                  d.latest_operation.status ===
                                    "dead_letter") &&
                                  "text-destructive",
                              )}
                            >
                              {operationStatusText(d.latest_operation)}
                            </p>
                          )}
                        {d.error && (
                          <p
                            className="mt-1 truncate text-xs text-destructive"
                            title={d.error}
                          >
                            {d.error}
                          </p>
                        )}
                      </div>
                    );
                  })()
                )}
              </div>
            );
          })}
        </div>
        {documentsQuery.isFetchingNextPage && (
          <p className="flex items-center justify-center gap-2 py-3 text-xs text-muted-foreground">
            <LoaderCircle className="size-3.5 animate-spin" />
            正在加载更多文档
          </p>
        )}
      </div>

      {/* 拖拽放置提示层 */}
      {dragging && (
        <div className="pointer-events-none absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-primary bg-background/85">
          <UploadCloud className="size-8 text-primary" />
          <p className="text-sm font-medium">松开即可加入上传列表</p>
          <p className="text-xs text-muted-foreground">
            仅保存文件，上传后再选择类型
          </p>
        </div>
      )}

      <AlertDialog
        open={reclassifyDoc !== null}
        onOpenChange={(open) => !open && setReclassifyDoc(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>重新抽取这份文档？</AlertDialogTitle>
            <AlertDialogDescription>
              系统会复用「{reclassifyDoc?.filename}
              」最新修订的解析产物，不会重新上传、改写源文件或改变当前已发布快照。新抽取出的事实仍需审核并发布新快照后才会生效。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <label className="space-y-1.5 px-1 text-xs font-medium">
            目标抽取类型
            <select
              value={reclassifyTarget}
              disabled={
                reclassifyDocument.isPending ||
                !!retryTargetDocType(reclassifyDoc ?? ({} as SourceDocument))
              }
              onChange={(event) => setReclassifyTarget(event.target.value)}
              className="h-9 w-full rounded-lg border border-input bg-background px-2.5 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
            >
              <option value="">选择类型…</option>
              {documentTypeOptions
                .filter((option) => option.value !== "unclassified")
                .map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
            </select>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={reclassifyDocument.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={reclassifyDocument.isPending || !reclassifyTarget}
              onClick={(event) => {
                event.preventDefault();
                if (reclassifyDoc && reclassifyTarget)
                  reclassifyDocument.mutate({
                    document: reclassifyDoc,
                    targetDocType: reclassifyTarget,
                  });
              }}
            >
              {reclassifyDocument.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              {reclassifyDoc?.latest_reclassification?.status === "failed"
                ? "重试抽取"
                : "开始抽取"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 修订历史 */}
      <Dialog
        open={historyDoc !== null}
        onOpenChange={(open) => !open && setHistoryDoc(null)}
      >
        <DialogContent className="max-h-[80vh] overflow-hidden sm:max-w-lg">
          <DialogHeader>
            <DialogTitle className="truncate pr-8">
              {historyDoc?.filename}
            </DialogTitle>
            <DialogDescription>修订历史与解析产物</DialogDescription>
          </DialogHeader>
          <div className="flex items-center justify-between gap-3 border-b border-border pb-3">
            <p className="min-w-0 truncate text-xs text-muted-foreground">
              最新 Revision{" "}
              {latestRevision(historyQuery.data)?.revision_no ?? "-"}
              {historyDurationLabel ? ` · 摄入${historyDurationLabel}` : ""}
            </p>
            <input
              ref={revisionFileRef}
              type="file"
              accept={SUPPORTED_DOCUMENT_ACCEPT}
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file && historyDoc) {
                  const accepted = preflightUploads([file])?.accepted ?? [];
                  if (accepted[0]) {
                    uploadRevision.mutate({
                      doc: historyDoc,
                      file: accepted[0],
                    });
                  }
                }
                event.target.value = "";
              }}
            />
            <Button
              size="sm"
              variant="outline"
              disabled={
                uploadRevision.isPending ||
                historyQuery.isPending ||
                !settings ||
                historyDoc?.status === "uploaded" ||
                historyDoc?.status === "parsing" ||
                latestRevision(historyQuery.data)?.status === "tombstoned"
              }
              onClick={() => revisionFileRef.current?.click()}
            >
              {uploadRevision.isPending ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <Upload />
              )}
              上传新修订
            </Button>
          </div>
          <div className="max-h-[60vh] overflow-y-auto pr-1">
            {historyQuery.isPending && (
              <div className="flex h-24 items-center justify-center text-muted-foreground">
                <LoaderCircle className="size-4 animate-spin" />
              </div>
            )}
            {historyQuery.isError && (
              <p className="py-6 text-center text-sm text-destructive">
                {errMsg(historyQuery.error)}
              </p>
            )}
            {historyQuery.data?.length === 0 && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                暂无修订
              </p>
            )}
            <ol className="space-y-0">
              {[...(historyQuery.data ?? [])]
                .sort((a, b) => b.revision_no - a.revision_no)
                .map((revision, index) => (
                  <li
                    key={revision.id}
                    className="relative grid grid-cols-[18px_1fr] gap-2 pb-4"
                  >
                    {index < (historyQuery.data?.length ?? 0) - 1 && (
                      <span className="absolute top-4 left-[8px] h-full w-px bg-border" />
                    )}
                    <span className="relative mt-1.5 size-2.5 rounded-full border-2 border-background bg-primary ring-1 ring-border" />
                    <div className="min-w-0 rounded-md border border-border p-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="font-medium">
                            Revision {revision.revision_no}
                          </span>
                          <ToneBadge tone={revisionTone(revision.status)}>
                            {revision.status}
                          </ToneBadge>
                        </div>
                        <time className="shrink-0 text-xs text-muted-foreground">
                          {formatTimestamp(revision.created_at)}
                        </time>
                      </div>
                      <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                        SHA {revision.sha256.slice(0, 12)}
                      </p>
                      {(revision.structured_json_key ||
                        revision.markdown_key) && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {[
                            revision.structured_json_key && "结构化 JSON",
                            revision.markdown_key && "Markdown",
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                      )}
                      {revision.tombstoned_at && (
                        <p className="mt-1 text-xs text-destructive">
                          已撤回于 {formatTimestamp(revision.tombstoned_at)}
                          {revision.tombstone_reason
                            ? ` · ${revision.tombstone_reason}`
                            : ""}
                        </p>
                      )}
                      {revision.error && (
                        <p className="mt-1 text-xs text-destructive">
                          {revision.error}
                        </p>
                      )}
                    </div>
                  </li>
                ))}
            </ol>
          </div>
        </DialogContent>
      </Dialog>

      {/* 有效期(从修订历史弹窗里拆出来,二者本无关系) */}
      <Dialog
        open={expiryDoc !== null}
        onOpenChange={(open) => !open && setExpiryDoc(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="truncate pr-8">文档有效期</DialogTitle>
            <DialogDescription className="truncate">
              {expiryDoc?.filename}
            </DialogDescription>
          </DialogHeader>
          <label className="block text-xs font-medium">
            过期时间(留空表示长期有效)
            <Input
              className="mt-1"
              type="datetime-local"
              value={expiryValue}
              onChange={(event) => setExpiryValue(event.target.value)}
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setExpiryDoc(null)}>
              取消
            </Button>
            <Button
              disabled={!expiryDoc || updateExpiry.isPending}
              onClick={() =>
                updateExpiry.mutate(
                  expiryValue ? new Date(expiryValue).toISOString() : null,
                )
              }
            >
              {updateExpiry.isPending ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <CalendarClock />
              )}
              {expiryValue ? "保存有效期" : "设为长期有效"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={reingestDoc !== null}
        onOpenChange={(open) => {
          if (!open && !reingestWithdrawnDocument.isPending) {
            setReingestDoc(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>重新摄入这份文档？</AlertDialogTitle>
            <AlertDialogDescription>
              系统会使用「{reingestDoc?.filename}
              」保留的原始资料创建新的不可变修订并重新发布。新修订成功发布前，文档仍保持不可检索；旧修订的撤回标记和审计记录不会被恢复或删除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={reingestWithdrawnDocument.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={
                reingestWithdrawnDocument.isPending || reingestDoc === null
              }
              onClick={(event) => {
                event.preventDefault();
                if (reingestDoc) {
                  reingestWithdrawnDocument.mutate(reingestDoc);
                }
              }}
            >
              {reingestWithdrawnDocument.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              确认重新摄入
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={withdrawDoc !== null}
        onOpenChange={(open) => {
          if (!open && !withdrawDocument.isPending) {
            setWithdrawDoc(null);
            setWithdrawImpact(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>撤回这份文档?</AlertDialogTitle>
            <AlertDialogDescription>
              {withdrawDoc?.filename} 将立即停止所有活动检索和业务消费，随后异步
              发布不含该文档的新知识快照。原始资料会保留，永久清理是独立操作。
            </AlertDialogDescription>
            {withdrawImpact && (
              <div className="grid grid-cols-2 gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                <span>涉及修订 {withdrawImpact.revision_count}</span>
                <span>将隔离切片 {withdrawImpact.chunk_count}</span>
                <span>将隔离图片 {withdrawImpact.image_count}</span>
                <span>待复核事实 {withdrawImpact.orphaned_fact_count}</span>
                <span>将隔离事实 {withdrawImpact.exclusive_fact_count}</span>
                <span>共享事实保留 {withdrawImpact.shared_fact_count}</span>
                <span>将隐藏实体 {withdrawImpact.exclusive_entity_count}</span>
                <span>共享实体保留 {withdrawImpact.shared_entity_count}</span>
                <span>
                  将移除关系 {withdrawImpact.exclusive_relation_count}
                </span>
                <span>共享关系保留 {withdrawImpact.shared_relation_count}</span>
              </div>
            )}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={withdrawDocument.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={withdrawDocument.isPending || !withdrawDoc}
              onClick={() =>
                withdrawDoc && withdrawDocument.mutate(withdrawDoc)
              }
            >
              {withdrawDocument.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              确认撤回
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={purgeDoc !== null}
        onOpenChange={(open) => {
          if (
            !open &&
            !purgeDocument.isPending &&
            checkingPurgeId !== purgeDoc?.id
          ) {
            setPurgeDoc(null);
            setPurgePreview(null);
            setPurgeReason("");
            setPurgeConfirmed(false);
          }
        }}
      >
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>永久清理文档</DialogTitle>
            <DialogDescription>
              {purgeDoc?.filename}{" "}
              已从活动知识中撤回。永久清理只删除该文档的独占
              内容且无法恢复；共享事实、实体和关系继续保留，失去支持的独占实体也只
              会转交独立的引用感知
              GC。任何快照、业务、人工固定或媒体引用都会阻止 整次清理受理。
            </DialogDescription>
          </DialogHeader>

          {purgePreview && (
            <div className="space-y-4">
              {purgePreview.retention_deadline && (
                <p className="text-xs text-muted-foreground">
                  审计保留截止：
                  {formatTimestamp(purgePreview.retention_deadline)}
                </p>
              )}

              {purgePreview.blockers.length > 0 && (
                <section
                  aria-label="永久清理阻塞项"
                  className="space-y-2 rounded-md border border-destructive/30 bg-destructive/5 p-3"
                >
                  <p className="text-sm font-medium text-destructive">
                    当前不可永久清理
                  </p>
                  <ul className="space-y-1 text-sm">
                    {purgePreview.blockers.map((blocker) => (
                      <li key={blocker.code}>
                        {PURGE_BLOCKER_LABELS[blocker.code] ?? blocker.code}：
                        {blocker.count} 项
                        {blocker.retry_at
                          ? `，最早可于 ${formatTimestamp(blocker.retry_at)} 重试`
                          : ""}
                      </li>
                    ))}
                  </ul>
                  {/* 拦截项全为可跳过类时,管理员可勾选强制清理不必等保留期 */}
                  {canPurge &&
                    purgePreview.blockers.every((blocker) =>
                      DOC_FORCE_BYPASSABLE_BLOCKERS.has(blocker.code),
                    ) && (
                      <label
                        htmlFor="confirm-purge-force"
                        className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm"
                      >
                        <Checkbox
                          id="confirm-purge-force"
                          checked={purgeForce}
                          onCheckedChange={(checked) =>
                            setPurgeForce(checked === true)
                          }
                        />
                        <span>
                          跳过以上 {purgePreview.blockers.length}{" "}
                          类拦截立即清理(含保留期等待);引用这些内容的快照与外部业务对象会出现死链,
                          删除后无法恢复。
                        </span>
                      </label>
                    )}
                </section>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <section className="rounded-md border p-3">
                  <h4 className="text-sm font-medium">将删除</h4>
                  <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                    {Object.entries(purgePreview.delete_counts).map(
                      ([key, count]) => (
                        <li key={key}>
                          {PURGE_COUNT_LABELS[key] ?? key}：{count}
                        </li>
                      ),
                    )}
                  </ul>
                </section>
                <section className="rounded-md border p-3">
                  <h4 className="text-sm font-medium">继续保留</h4>
                  <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                    {Object.entries(purgePreview.retain_counts).map(
                      ([key, count]) => (
                        <li key={key}>
                          {PURGE_COUNT_LABELS[key] ?? key}：{count}
                        </li>
                      ),
                    )}
                  </ul>
                </section>
              </div>

              {(purgePreview.eligible || purgeForce) && (
                <div className="space-y-3">
                  <div className="space-y-1.5">
                    <label
                      htmlFor="purge-reason"
                      className="text-sm font-medium"
                    >
                      清理理由
                    </label>
                    <Textarea
                      id="purge-reason"
                      aria-label="永久清理理由"
                      maxLength={500}
                      value={purgeReason}
                      placeholder="说明永久清理的业务或合规原因"
                      onChange={(event) => setPurgeReason(event.target.value)}
                    />
                  </div>
                  <label
                    htmlFor="confirm-purge-irreversible"
                    className="flex items-start gap-2 rounded-md border border-destructive/30 p-3 text-sm"
                  >
                    <Checkbox
                      id="confirm-purge-irreversible"
                      checked={purgeConfirmed}
                      onCheckedChange={(checked) =>
                        setPurgeConfirmed(checked === true)
                      }
                    />
                    <span>
                      我确认该操作不可逆，原始文件、解析产物和独占内容将被永久
                      删除。
                    </span>
                  </label>
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              disabled={
                purgeDocument.isPending || checkingPurgeId === purgeDoc?.id
              }
              onClick={() => void refreshPurgePreview()}
            >
              {checkingPurgeId === purgeDoc?.id && (
                <LoaderCircle className="animate-spin" />
              )}
              重新检查
            </Button>
            <Button
              variant="destructive"
              disabled={
                purgeDocument.isPending ||
                !purgeDoc ||
                !purgePreview ||
                (!purgePreview.eligible && !purgeForce) ||
                !purgeReason.trim() ||
                !purgeConfirmed
              }
              onClick={() => {
                if (!purgeDoc || !purgePreview) return;
                purgeDocument.mutate({
                  doc: purgeDoc,
                  preview: purgePreview,
                  reason: purgeReason,
                  force: purgeForce,
                });
              }}
            >
              {purgeDocument.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              {purgeForce ? "强制永久清理" : "确认永久清理"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
