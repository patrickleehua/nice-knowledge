"use client";

// 长期记忆:agent 从对话沉淀的偏好 / 硬约束 / 决策 / 风险的人工审阅台。
//
// 为什么必须有这一页:记忆由 LLM 抽取,再严的管道也拦不住全部误判,而一条错误
// 记忆会被反复召回、反复影响后续判断。所以这里三件事缺一不可——看得到来源
// (哪次会话沉淀的、是自动抽的还是助手主动记的)、改得动内容、废得掉。
//
// 「删除」是软删:后端置 status=rejected 而非物理删(POST /memory/{id}/reject)。
// 删掉痕迹会让下一轮抽取把同一条脏记忆原样再写一遍——rejected 记录本身就是
// 后端防污染管道的输入(nicekit/agent/memory.py::ingest_candidate)。
//
// scope 词表走 GET /memory/scopes 动态下发(SDK 只内置 org,其余由宿主注册),
// 前端不硬编码枚举。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { BrainCircuit, Pencil, Trash2 } from "lucide-react";
import { Suspense, useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  FormField,
  PageHeader,
  Pagination,
  SearchInput,
  Spinner,
  StatusBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import {
  MEMORY_TYPE_LABELS,
  memoryScopeLabel,
  memoryScopeTarget,
  memorySourceLabel,
  memorySourceSessionId,
  memoryStatusMeta,
  memoryTypeLabel,
  memoryTypeMeta,
  promotionHint,
  type MemoryListOut,
  type MemoryOut,
  type MemoryType,
  type MemoryVocabOut,
} from "@/lib/memory";
import { useUrlNumber, useUrlParam } from "@/lib/use-url-state";
import { errMsg, fmtDateTime } from "@/lib/utils";

/** 词表由后端 GET /memory/scopes 下发,前端不硬编码 scope 枚举。 */
function useMemoryVocab() {
  return useQuery({
    queryKey: ["memory-scopes"],
    queryFn: () => api.get<MemoryVocabOut>("/memory/scopes"),
    staleTime: 5 * 60 * 1000,
  });
}

const ALL = "__all__";
const PAGE_SIZE = 50;

// 后端 /memory/scopes 拿不到时的最小词表:SDK 内置只有 org。
const FALLBACK_SCOPES = ["org"];
const FALLBACK_STATUSES = ["active", "superseded", "rejected"];

const STATUS_LABELS: Record<string, string> = {
  active: "生效中",
  superseded: "已被取代",
  rejected: "已失效",
};

// 编辑弹窗里可改的字段。source 刻意不可改:人工订正的是内容不是出处,
// 把来源改掉会让"这条当初怎么来的"永久丢失,而那正是排污染时最需要的线索。
interface EditDraft {
  id: string;
  title: string;
  content: string;
  memory_type: MemoryType;
  confidence: number;
}

function SourceCell({ item }: { item: MemoryOut }) {
  const sessionId = memorySourceSessionId(item.source);
  return (
    <div className="space-y-0.5">
      <p className="text-xs">{memorySourceLabel(item.source)}</p>
      {sessionId && (
        <p
          className="max-w-48 truncate font-mono text-[11px] text-muted-foreground"
          title={`会话 ${sessionId}`}
        >
          会话 {sessionId}
        </p>
      )}
    </div>
  );
}

function MemoryTable({
  items,
  isLoading,
  error,
  onRetry,
  onEdit,
  onReject,
}: {
  items: MemoryOut[] | undefined;
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  onEdit: (item: MemoryOut) => void;
  onReject: (item: MemoryOut) => Promise<unknown>;
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns: ColumnDef<MemoryOut, any>[] = [
    {
      accessorKey: "title",
      header: "记忆",
      enableSorting: false,
      cell: ({ row }) => {
        const hint = promotionHint(row.original);
        return (
          <div className="max-w-104 space-y-0.5">
            <p className="text-sm font-medium">{row.original.title}</p>
            <p className="line-clamp-2 text-xs whitespace-pre-line text-muted-foreground">
              {row.original.content}
            </p>
            {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
          </div>
        );
      },
    },
    {
      accessorKey: "memory_type",
      header: "类型",
      enableSorting: false,
      cell: ({ row }) => (
        <StatusBadge meta={memoryTypeMeta(row.original.memory_type)} />
      ),
    },
    {
      accessorKey: "scope",
      header: "范围",
      enableSorting: false,
      cell: ({ row }) => (
        <div className="space-y-0.5">
          <p className="text-xs">{memoryScopeLabel(row.original.scope)}</p>
          <p className="max-w-40 truncate text-[11px] text-muted-foreground">
            {memoryScopeTarget(row.original)}
          </p>
        </div>
      ),
    },
    {
      accessorKey: "confidence",
      header: "置信",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-sm tabular-nums">
          {row.original.confidence.toFixed(2)}
        </span>
      ),
    },
    {
      accessorKey: "sightings",
      header: "印证/命中",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-sm tabular-nums">
          {row.original.sightings} / {row.original.hit_count}
        </span>
      ),
    },
    {
      id: "source",
      header: "来源",
      enableSorting: false,
      cell: ({ row }) => <SourceCell item={row.original} />,
    },
    {
      accessorKey: "status",
      header: "状态",
      enableSorting: false,
      cell: ({ row }) => (
        <StatusBadge meta={memoryStatusMeta(row.original.status)} />
      ),
    },
    {
      accessorKey: "updated_at",
      header: "更新时间",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-xs text-muted-foreground">
          {fmtDateTime(row.original.updated_at)}
        </span>
      ),
    },
    {
      id: "__actions__",
      header: () => <span className="sr-only">操作</span>,
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex items-center justify-end gap-0.5">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`编辑 ${row.original.title}`}
            onClick={() => onEdit(row.original)}
          >
            <Pencil className="size-3.5" />
          </Button>
          {row.original.status !== "rejected" && (
            <ConfirmDialog
              trigger={
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`标记失效 ${row.original.title}`}
                >
                  <Trash2 className="size-3.5 text-destructive" />
                </Button>
              }
              title={`标记记忆「${row.original.title}」失效?`}
              description="失效后这条记忆不再被召回注入对话,记录与来源保留以供追溯,并会阻止后续对话把同一条内容重新沉淀进来。"
              confirmLabel="标记失效"
              destructive
              onConfirm={() => onReject(row.original)}
            />
          )}
        </div>
      ),
    },
  ];

  return (
    <DataTable
      columns={columns}
      data={items}
      isLoading={isLoading}
      error={error}
      onRetry={onRetry}
      getRowId={(row) => row.id}
      empty={{
        icon: BrainCircuit,
        title: "还没有长期记忆",
        description:
          "助手会在对话结束后自动沉淀偏好与约束;也可以在对话里让它主动记录",
      }}
    />
  );
}

function MemoryContent() {
  const queryClient = useQueryClient();
  // 筛选与分页全部入 query string:刷新不丢、可分享给同事一起核对同一批记忆
  const [scope, setScope] = useUrlParam("scope", ALL);
  const [scopeRef, setScopeRef] = useUrlParam("scope_ref_id", "");
  const [memoryType, setMemoryType] = useUrlParam("type", ALL);
  const [status, setStatus] = useUrlParam("status", ALL);
  const [keyword, setKeyword] = useUrlParam("q", "");
  const [page, setPage] = useUrlNumber("page", 1);

  const [draft, setDraft] = useState<EditDraft | null>(null);

  const vocab = useMemoryVocab();

  const scopeItems = useMemo(() => {
    const scopes = vocab.data?.scopes?.length
      ? vocab.data.scopes
      : FALLBACK_SCOPES;
    return {
      [ALL]: "全部范围",
      ...Object.fromEntries(scopes.map((s) => [s, memoryScopeLabel(s)])),
    } as Record<string, string>;
  }, [vocab.data]);

  const typeItems = useMemo(() => {
    const types = vocab.data?.types?.length
      ? vocab.data.types
      : Object.keys(MEMORY_TYPE_LABELS);
    return {
      [ALL]: "全部类型",
      ...Object.fromEntries(types.map((t) => [t, memoryTypeLabel(t)])),
    } as Record<string, string>;
  }, [vocab.data]);

  const statusItems = useMemo(() => {
    const statuses = vocab.data?.statuses?.length
      ? vocab.data.statuses
      : FALLBACK_STATUSES;
    return {
      [ALL]: "全部状态",
      ...Object.fromEntries(statuses.map((s) => [s, STATUS_LABELS[s] ?? s])),
    } as Record<string, string>;
  }, [vocab.data]);

  const params = new URLSearchParams({
    page: String(page),
    page_size: String(PAGE_SIZE),
  });
  if (scope !== ALL) params.set("scope", scope);
  if (scopeRef) params.set("scope_ref_id", scopeRef);
  if (memoryType !== ALL) params.set("memory_type", memoryType);
  if (status !== ALL) params.set("memory_status", status);
  if (keyword) params.set("q", keyword);

  const query = useQuery({
    queryKey: ["org-memory", params.toString()],
    queryFn: () => api.get<MemoryListOut>(`/memory?${params.toString()}`),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["org-memory"] });

  const saveMutation = useMutation({
    mutationFn: (form: EditDraft) =>
      api.patch<MemoryOut>(`/memory/${form.id}`, {
        title: form.title.trim(),
        content: form.content.trim(),
        memory_type: form.memory_type,
        confidence: form.confidence,
      }),
    onSuccess: (updated) => {
      toast.success(`已更新记忆「${updated.title}」`);
      setDraft(null);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "保存失败")),
  });

  const rejectMutation = useMutation({
    mutationFn: (item: MemoryOut) =>
      api.post<MemoryOut>(`/memory/${item.id}/reject`, {
        reason: "组织管理员在长期记忆页标记失效",
      }),
    onSuccess: (updated) => {
      toast.success(`已标记记忆「${updated.title}」失效`);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "标记失效失败")),
  });

  const total = query.data?.total ?? 0;
  const totalPages = Math.max(Math.ceil(total / PAGE_SIZE), 1);
  const draftValid =
    draft !== null &&
    draft.title.trim().length > 0 &&
    draft.content.trim().length > 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="长期记忆"
        description="助手从对话沉淀的长期偏好、硬约束与决策;记错了请就地订正或标记失效"
      />

      <div className="flex flex-wrap items-center gap-2">
        <SearchInput
          label="搜索记忆标题或内容"
          placeholder="搜索标题或内容…"
          value={keyword}
          onChange={(next) => setKeyword(next, { reset: ["page"] })}
          className="w-64"
        />
        <Select
          items={scopeItems}
          value={scope}
          onValueChange={(value) => setScope(String(value), { reset: ["page"] })}
        >
          <SelectTrigger size="sm" aria-label="按范围筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(scopeItems).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <SearchInput
          label="按作用域标识筛选"
          placeholder="scope_ref_id…"
          value={scopeRef}
          onChange={(next) => setScopeRef(next, { reset: ["page"] })}
          className="w-48"
        />
        <Select
          items={typeItems}
          value={memoryType}
          onValueChange={(value) =>
            setMemoryType(String(value), { reset: ["page"] })
          }
        >
          <SelectTrigger size="sm" aria-label="按类型筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(typeItems).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          items={statusItems}
          value={status}
          onValueChange={(value) =>
            setStatus(String(value), { reset: ["page"] })
          }
        >
          <SelectTrigger size="sm" aria-label="按状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(statusItems).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="ml-auto text-xs text-muted-foreground tabular-nums">
          共 {total} 条
        </span>
      </div>

      <MemoryTable
        items={query.data?.items}
        isLoading={query.isLoading}
        error={query.error}
        onRetry={() => query.refetch()}
        onEdit={(item) =>
          setDraft({
            id: item.id,
            title: item.title,
            content: item.content,
            memory_type: item.memory_type,
            confidence: item.confidence,
          })
        }
        onReject={(item) => rejectMutation.mutateAsync(item)}
      />

      <Pagination
        page={page}
        totalPages={totalPages}
        total={total}
        onChange={setPage}
      />

      <Dialog
        open={draft !== null}
        onOpenChange={(open) => !open && setDraft(null)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>订正记忆</DialogTitle>
          </DialogHeader>
          {draft && (
            <>
              <div className="space-y-4">
                <FormField label="标题" htmlFor="memory-title" required>
                  <Input
                    id="memory-title"
                    value={draft.title}
                    maxLength={200}
                    onChange={(event) =>
                      setDraft({ ...draft, title: event.target.value })
                    }
                  />
                </FormField>
                <FormField
                  label="内容"
                  htmlFor="memory-content"
                  required
                  description="脱离原对话也要能读懂:写清是谁、在什么条件下、要求什么"
                >
                  <Textarea
                    id="memory-content"
                    rows={5}
                    value={draft.content}
                    maxLength={2000}
                    onChange={(event) =>
                      setDraft({ ...draft, content: event.target.value })
                    }
                  />
                </FormField>
                <div className="grid grid-cols-2 gap-3">
                  <FormField
                    label="类型"
                    htmlFor="memory-type"
                    description="改为「偏好」即视为人工确认,不再等系统自动提升"
                  >
                    <Select
                      items={MEMORY_TYPE_LABELS}
                      value={draft.memory_type}
                      onValueChange={(value) =>
                        setDraft({
                          ...draft,
                          memory_type: String(value) as MemoryType,
                        })
                      }
                    >
                      <SelectTrigger id="memory-type" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {Object.entries(MEMORY_TYPE_LABELS).map(
                          ([value, label]) => (
                            <SelectItem key={value} value={value}>
                              {label}
                            </SelectItem>
                          ),
                        )}
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField
                    label="置信度"
                    htmlFor="memory-confidence"
                    description="0~1;低于 0.5 的记忆不会再被召回"
                  >
                    <Input
                      id="memory-confidence"
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={draft.confidence}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          confidence: Math.min(
                            1,
                            Math.max(0, Number(event.target.value) || 0),
                          ),
                        })
                      }
                    />
                  </FormField>
                </div>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setDraft(null)}>
                  取消
                </Button>
                <Button
                  disabled={!draftValid || saveMutation.isPending}
                  onClick={() => saveMutation.mutate(draft)}
                >
                  {saveMutation.isPending && <Spinner size={3.5} />}
                  保存
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function MemoryPage() {
  // useUrlState 依赖 useSearchParams,必须包在 Suspense 里
  return (
    <Suspense fallback={null}>
      <MemoryContent />
    </Suspense>
  );
}
