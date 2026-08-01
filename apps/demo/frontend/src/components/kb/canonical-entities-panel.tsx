"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ArrowRight,
  GitMerge,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Tags,
  X,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, FormField, ToneBadge } from "@/components/shared";
import { Badge } from "@/components/ui/badge";
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
import { errMsg } from "@/lib/utils";
import type { CanonicalEntity, EntityType } from "@/lib/types";

// 归一实体的 entity_type 是自由字符串(后端 CanonicalEntityOut.entity_type:str),
// 取值就是已注册的实体类型 key。词表走 /kb/entity-types 下发,查不到回落原值 ——
// TF 那份写死的五个旅游类型在 SDK 里没有对应表。
const ALL_TYPES = "__all__";
const FALLBACK_TYPE_KEY = "concept";

/**
 * 实体类型词表:`{type_key: display_name}`。注册表拉不到时给一个只含
 * `concept`(SDK 唯一内置兜底类型)的最小词表,页面照常可用。
 */
function useEntityTypeOptions(): Record<string, string> {
  const types = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
    staleTime: 5 * 60 * 1000,
  });
  if (!types.data?.length) return { [FALLBACK_TYPE_KEY]: FALLBACK_TYPE_KEY };
  return Object.fromEntries(
    types.data.map((type) => [type.type_key, type.display_name]),
  );
}

type EntityFormState = {
  entity: CanonicalEntity | null;
  name: string;
  entityType: string;
  metadata: string;
};

type MergeState = {
  source: CanonicalEntity;
  targetId: string;
};

/** 后端合并候选(只读建议,human-in-the-loop):确认后走现有 merge API。 */
type MergeSuggestion = {
  source_entity: CanonicalEntity;
  target_entity: CanonicalEntity;
  confidence: number;
  reasons: string[];
};

const REASON_LABELS: Record<string, string> = {
  canonical_name_match: "标准名一致",
  shared_alias: "共享别名",
  canonical_is_alias: "名称互为别名",
  name_substring: "名称包含",
  token_overlap: "词元重叠",
  edit_distance: "拼写相近",
  semantic_similarity: "语义相近",
};

function reasonLabel(reason: string): string {
  const separator = reason.indexOf(":");
  const kind = separator === -1 ? reason : reason.slice(0, separator);
  const detail = separator === -1 ? "" : reason.slice(separator + 1);
  const label = REASON_LABELS[kind] ?? kind;
  return detail ? `${label} ${detail}` : label;
}

function canonicalEntitiesKey(kbId: string) {
  return ["kb-canonical-entities", kbId] as const;
}

/** 查不到展示名就回显 type_key 原值(宿主刚注册的类型也不会渲染成空白)。 */
function entityTypeLabel(
  entityType: string,
  labels: Record<string, string>,
): string {
  return labels[entityType] ?? entityType;
}

function governanceStatus(entity: CanonicalEntity) {
  if (entity.merged_into_entity_id) {
    return { label: "已合并", tone: "muted" as const };
  }
  if (entity.support_status === "unsupported") {
    return { label: "无活动支持", tone: "warning" as const };
  }
  return { label: "有活动支持", tone: "success" as const };
}

function listCanonicalEntities(
  kbId: string,
  query: string,
  entityType: string,
  limit = 500,
  offset = 0,
): Promise<CanonicalEntity[]> {
  const params = new URLSearchParams({ kb_id: kbId });
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (query) params.set("q", query);
  if (entityType !== ALL_TYPES) params.set("entity_type", entityType);
  return api.get(`/kb/canonical-entities?${params.toString()}`);
}

function listMergeSuggestions(
  kbId: string,
  entityType: string,
): Promise<MergeSuggestion[]> {
  const params = new URLSearchParams({ limit: "20" });
  if (entityType !== ALL_TYPES) params.set("entity_type", entityType);
  return api.get(
    `/kb/bases/${kbId}/canonical-entities/merge-suggestions?${params.toString()}`,
  );
}

function parseMetadata(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("元数据必须是 JSON 对象");
  }
  return parsed as Record<string, unknown>;
}

function EntityFormDialog({
  kbId,
  form,
  onFormChange,
  onOpenChange,
}: {
  kbId: string;
  form: EntityFormState | null;
  onFormChange: (form: EntityFormState) => void;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const typeOptions = useEntityTypeOptions();
  const isEditing = form?.entity !== null;
  let metadataError = "";
  if (form) {
    try {
      parseMetadata(form.metadata);
    } catch (error) {
      metadataError = error instanceof Error ? error.message : "JSON 格式无效";
    }
  }

  const save = useMutation({
    mutationFn: () => {
      if (!form) throw new Error("表单未打开");
      const payload = {
        canonical_name: form.name.trim(),
        metadata: parseMetadata(form.metadata),
      };
      if (form.entity) {
        return api.patch<CanonicalEntity>(
          `/kb/canonical-entities/${form.entity.id}`,
          payload,
        );
      }
      return api.post<CanonicalEntity>("/kb/canonical-entities", {
        ...payload,
        kb_id: kbId,
        entity_type: form.entityType,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: canonicalEntitiesKey(kbId),
      });
      toast.success(isEditing ? "实体已更新" : "实体已创建");
      onOpenChange(false);
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  return (
    <Dialog open={form !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isEditing ? "编辑归一实体" : "创建归一实体"}
          </DialogTitle>
        </DialogHeader>
        {form && (
          <div className="space-y-3">
            <FormField label="标准名称" htmlFor="canonical-name" required>
              <Input
                id="canonical-name"
                autoFocus
                value={form.name}
                onChange={(event) =>
                  onFormChange({ ...form, name: event.target.value })
                }
              />
            </FormField>
            <FormField label="实体类型" htmlFor="canonical-type" required>
              <Select
                items={typeOptions}
                value={form.entityType}
                disabled={isEditing}
                onValueChange={(value) =>
                  onFormChange({ ...form, entityType: String(value) })
                }
              >
                <SelectTrigger id="canonical-type" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(typeOptions).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField
              label="元数据 (JSON)"
              htmlFor="canonical-metadata"
              description={metadataError || undefined}
            >
              <Textarea
                id="canonical-metadata"
                rows={6}
                className={metadataError ? "border-destructive" : undefined}
                value={form.metadata}
                onChange={(event) =>
                  onFormChange({ ...form, metadata: event.target.value })
                }
              />
            </FormField>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            disabled={
              !form?.name.trim() ||
              !form.entityType ||
              Boolean(metadataError) ||
              save.isPending
            }
            onClick={() => save.mutate()}
          >
            {save.isPending && <LoaderCircle className="animate-spin" />}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function CanonicalEntitiesPanel({ kbId }: { kbId: string }) {
  const typeOptions = useEntityTypeOptions();
  const queryClient = useQueryClient();
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [entityType, setEntityType] = useState(ALL_TYPES);
  const [form, setForm] = useState<EntityFormState | null>(null);
  const [aliasDrafts, setAliasDrafts] = useState<Record<string, string>>({});
  const [localeDrafts, setLocaleDrafts] = useState<Record<string, string>>({});
  const [mergeState, setMergeState] = useState<MergeState | null>(null);
  const [mergeConfirmOpen, setMergeConfirmOpen] = useState(false);
  const [mergeTargetQuery, setMergeTargetQuery] = useState("");
  const [suggestionToMerge, setSuggestionToMerge] =
    useState<MergeSuggestion | null>(null);

  const entitiesQuery = useInfiniteQuery({
    queryKey: [...canonicalEntitiesKey(kbId), "list", query, entityType],
    queryFn: ({ pageParam }) =>
      listCanonicalEntities(kbId, query, entityType, 100, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === 100
        ? allPages.reduce((count, page) => count + page.length, 0)
        : undefined,
  });
  const entities = entitiesQuery.data?.pages.flatMap((page) => page) ?? [];

  const suggestionsQuery = useQuery({
    queryKey: [...canonicalEntitiesKey(kbId), "merge-suggestions", entityType],
    queryFn: () => listMergeSuggestions(kbId, entityType),
  });
  const suggestions = suggestionsQuery.data ?? [];

  const mergeCandidatesQuery = useQuery({
    queryKey: [
      ...canonicalEntitiesKey(kbId),
      "merge-candidates",
      mergeState?.source.entity_type,
      mergeTargetQuery,
    ],
    queryFn: () =>
      listCanonicalEntities(
        kbId,
        mergeTargetQuery.trim(),
        mergeState!.source.entity_type,
      ),
    enabled: mergeState !== null,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: canonicalEntitiesKey(kbId) });

  const addAlias = useMutation({
    mutationFn: ({ entityId }: { entityId: string }) => {
      const alias = aliasDrafts[entityId]?.trim() ?? "";
      const locale = localeDrafts[entityId]?.trim() ?? "";
      return api.post(`/kb/canonical-entities/${entityId}/aliases`, {
        alias,
        locale: locale || "und",
      });
    },
    onSuccess: async (_data, { entityId }) => {
      await invalidate();
      setAliasDrafts((current) => ({ ...current, [entityId]: "" }));
      setLocaleDrafts((current) => ({ ...current, [entityId]: "" }));
      toast.success("别名已添加");
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const deleteAlias = useMutation({
    mutationFn: (aliasId: string) =>
      api.delete(`/kb/entity-aliases/${aliasId}`),
    onSuccess: async () => {
      await invalidate();
      toast.success("别名已删除");
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const merge = useMutation({
    mutationFn: ({ source, targetId }: MergeState) =>
      api.post(`/kb/canonical-entities/${source.id}/merge`, {
        target_entity_id: targetId,
      }),
    onSuccess: async () => {
      await invalidate();
      toast.success("实体已合并");
      setMergeConfirmOpen(false);
      setMergeState(null);
      setMergeTargetQuery("");
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const mergeCandidates = (mergeCandidatesQuery.data ?? []).filter(
    (entity) =>
      entity.id !== mergeState?.source.id && !entity.merged_into_entity_id,
  );
  const mergeTarget = mergeCandidates.find(
    (entity) => entity.id === mergeState?.targetId,
  );
  const typeItems = { [ALL_TYPES]: "全部类型", ...typeOptions };

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setQuery(queryDraft.trim());
  }

  function openCreate() {
    setForm({
      entity: null,
      name: "",
      entityType:
        entityType === ALL_TYPES
          ? (Object.keys(typeOptions)[0] ?? FALLBACK_TYPE_KEY)
          : entityType,
      metadata: "{}",
    });
  }

  function openEdit(entity: CanonicalEntity) {
    setForm({
      entity,
      name: entity.canonical_name,
      entityType: entity.entity_type,
      metadata: JSON.stringify(entity.metadata, null, 2),
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 border-b border-border pb-4 md:flex-row md:items-end md:justify-between">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">实体归一</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            已加载 {entities.length} 个实体
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus />
          创建实体
        </Button>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row">
        <form className="flex min-w-0 flex-1 gap-2" onSubmit={submitSearch}>
          <Input
            className="min-w-0"
            aria-label="搜索实体名称或别名"
            placeholder="搜索名称或别名"
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
          />
          {query && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="清除搜索"
              title="清除搜索"
              onClick={() => {
                setQueryDraft("");
                setQuery("");
              }}
            >
              <X />
            </Button>
          )}
          <Button
            type="submit"
            variant="outline"
            size="icon"
            aria-label="搜索"
            title="搜索"
          >
            <Search />
          </Button>
        </form>
        <Select
          items={typeItems}
          value={entityType}
          onValueChange={(value) => setEntityType(String(value))}
        >
          <SelectTrigger className="w-full sm:w-40" aria-label="实体类型">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_TYPES}>全部类型</SelectItem>
            {Object.entries(typeOptions).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {entitiesQuery.isPending ? (
        <div className="flex min-h-40 items-center justify-center text-muted-foreground">
          <LoaderCircle className="animate-spin" />
        </div>
      ) : entitiesQuery.isError ? (
        <div className="rounded-md border border-destructive/30 p-4 text-sm text-destructive">
          {errMsg(entitiesQuery.error)}
        </div>
      ) : entities.length === 0 ? (
        <div className="flex min-h-40 flex-col items-center justify-center gap-2 rounded-md border border-dashed text-sm text-muted-foreground">
          <Tags className="size-5" />
          暂无归一实体
        </div>
      ) : (
        <div className="divide-y divide-border rounded-md border border-border">
          {entities.map((entity) => {
            const aliasDraft = aliasDrafts[entity.id] ?? "";
            const localeDraft = localeDrafts[entity.id] ?? "";
            const governance = governanceStatus(entity);
            const merged = Boolean(entity.merged_into_entity_id);
            return (
              <section key={entity.id} className="p-3 sm:p-4">
                <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <h3 className="break-words text-sm font-medium">
                        {entity.canonical_name}
                      </h3>
                      <Badge variant="secondary">
                        {entityTypeLabel(entity.entity_type, typeOptions)}
                      </Badge>
                      <ToneBadge tone={governance.tone}>
                        {governance.label}
                      </ToneBadge>
                      {entity.is_pinned && (
                        <ToneBadge tone="primary">人工固定</ToneBadge>
                      )}
                    </div>
                    {merged && (
                      <p className="mt-2 text-xs text-muted-foreground">
                        已重定向至实体 {entity.merged_into_entity_id}
                        {entity.merge_reason
                          ? `，原因：${entity.merge_reason}`
                          : ""}
                      </p>
                    )}
                    {!merged &&
                      entity.support_status === "unsupported" &&
                      entity.support_status_reason && (
                        <p className="mt-2 text-xs text-muted-foreground">
                          无活动支持原因：{entity.support_status_reason}
                        </p>
                      )}
                    {entity.is_pinned && entity.pin_reason && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        固定原因：{entity.pin_reason}
                      </p>
                    )}
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {entity.aliases.length === 0 && (
                        <span className="text-xs text-muted-foreground">
                          暂无别名
                        </span>
                      )}
                      {entity.aliases.map((alias) => (
                        <Badge
                          key={alias.id}
                          variant="outline"
                          className="h-auto max-w-full py-1"
                        >
                          <span className="break-all whitespace-normal">
                            {alias.alias}
                          </span>
                          {alias.locale && (
                            <span className="text-muted-foreground">
                              {alias.locale}
                            </span>
                          )}
                          {alias.source !== "canonical" && (
                            <button
                              type="button"
                              className="rounded-sm text-muted-foreground hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              aria-label={`删除别名 ${alias.alias}`}
                              title="删除别名"
                              disabled={merged || deleteAlias.isPending}
                              onClick={() => deleteAlias.mutate(alias.id)}
                            >
                              <X className="size-3" />
                            </button>
                          )}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1 self-end sm:self-auto">
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`编辑 ${entity.canonical_name}`}
                      title="编辑实体"
                      disabled={merged}
                      onClick={() => openEdit(entity)}
                    >
                      <Pencil />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`合并 ${entity.canonical_name}`}
                      title="合并实体"
                      disabled={merged}
                      onClick={() => {
                        setMergeConfirmOpen(false);
                        setMergeTargetQuery("");
                        setMergeState({ source: entity, targetId: "" });
                      }}
                    >
                      <GitMerge />
                    </Button>
                  </div>
                </div>

                <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                  <Input
                    className="min-w-0 flex-1"
                    aria-label={`为 ${entity.canonical_name} 添加别名`}
                    placeholder="新增别名"
                    disabled={merged}
                    value={aliasDraft}
                    onChange={(event) =>
                      setAliasDrafts((current) => ({
                        ...current,
                        [entity.id]: event.target.value,
                      }))
                    }
                    onKeyDown={(event) => {
                      if (
                        event.key === "Enter" &&
                        aliasDraft.trim() &&
                        !addAlias.isPending
                      ) {
                        addAlias.mutate({ entityId: entity.id });
                      }
                    }}
                  />
                  <Input
                    className="w-full sm:w-28"
                    aria-label="别名语言"
                    placeholder="语言"
                    disabled={merged}
                    value={localeDraft}
                    onChange={(event) =>
                      setLocaleDrafts((current) => ({
                        ...current,
                        [entity.id]: event.target.value,
                      }))
                    }
                  />
                  <Button
                    variant="outline"
                    disabled={
                      merged || !aliasDraft.trim() || addAlias.isPending
                    }
                    onClick={() => addAlias.mutate({ entityId: entity.id })}
                  >
                    <Plus />
                    添加别名
                  </Button>
                </div>
              </section>
            );
          })}
        </div>
      )}
      {entitiesQuery.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={entitiesQuery.isFetchingNextPage}
            onClick={() => entitiesQuery.fetchNextPage()}
          >
            {entitiesQuery.isFetchingNextPage ? "加载中…" : "加载更多实体"}
          </Button>
        </div>
      )}

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-2 border-b border-border pb-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-1.5 text-base font-semibold">
              <Sparkles className="size-4 text-muted-foreground" />
              合并建议
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              按别名、名称与语义相似度推荐的疑似重复实体,合并前需人工确认
            </p>
          </div>
          <Button
            variant="outline"
            size="icon"
            aria-label="刷新合并建议"
            title="刷新合并建议"
            disabled={suggestionsQuery.isFetching}
            onClick={() => suggestionsQuery.refetch()}
          >
            <RefreshCw
              className={
                suggestionsQuery.isFetching ? "animate-spin" : undefined
              }
            />
          </Button>
        </div>
        {suggestionsQuery.isPending ? (
          <div className="flex min-h-24 items-center justify-center text-muted-foreground">
            <LoaderCircle className="animate-spin" />
          </div>
        ) : suggestionsQuery.isError ? (
          <div className="rounded-md border border-destructive/30 p-4 text-sm text-destructive">
            {errMsg(suggestionsQuery.error)}
          </div>
        ) : suggestions.length === 0 ? (
          <div className="flex min-h-24 items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground">
            暂无疑似重复实体
          </div>
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {suggestions.map((suggestion) => (
              <div
                key={`${suggestion.source_entity.id}-${suggestion.target_entity.id}`}
                className="flex flex-col gap-3 p-3 sm:flex-row sm:items-start sm:justify-between sm:p-4"
              >
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
                    <span className="break-words font-medium">
                      {suggestion.source_entity.canonical_name}
                    </span>
                    <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="break-words font-medium">
                      {suggestion.target_entity.canonical_name}
                    </span>
                    <Badge variant="secondary">
                      {entityTypeLabel(
                        suggestion.source_entity.entity_type,
                        typeOptions,
                      )}
                    </Badge>
                    <Badge
                      variant={
                        suggestion.confidence >= 0.9 ? "default" : "outline"
                      }
                    >
                      置信 {Math.round(suggestion.confidence * 100)}%
                    </Badge>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {suggestion.reasons.map((reason) => (
                      <Badge
                        key={reason}
                        variant="outline"
                        className="h-auto max-w-full py-0.5 text-xs"
                      >
                        <span className="break-all whitespace-normal">
                          {reasonLabel(reason)}
                        </span>
                      </Badge>
                    ))}
                  </div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0 self-end sm:self-auto"
                  disabled={merge.isPending}
                  onClick={() => setSuggestionToMerge(suggestion)}
                >
                  <GitMerge />
                  合并
                </Button>
              </div>
            ))}
          </div>
        )}
      </section>

      <ConfirmDialog
        open={suggestionToMerge !== null}
        onOpenChange={(open) => {
          if (!open) setSuggestionToMerge(null);
        }}
        title="确认合并实体?"
        description={
          suggestionToMerge
            ? `“${suggestionToMerge.source_entity.canonical_name}”将合并到“${suggestionToMerge.target_entity.canonical_name}”，来源、别名和引用会转移到保留实体。`
            : undefined
        }
        confirmLabel="确认合并"
        destructive
        onConfirm={() => {
          if (!suggestionToMerge) return;
          return merge.mutateAsync({
            source: suggestionToMerge.source_entity,
            targetId: suggestionToMerge.target_entity.id,
          });
        }}
      />

      <Dialog
        open={mergeState !== null && !mergeConfirmOpen}
        onOpenChange={(open) => {
          if (!open && !mergeConfirmOpen) {
            setMergeState(null);
            setMergeTargetQuery("");
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>选择保留实体</DialogTitle>
          </DialogHeader>
          {mergeState && (
            <div className="space-y-3">
              <div className="rounded-md border border-border p-3">
                <p className="text-xs text-muted-foreground">待合并实体</p>
                <p className="mt-1 break-words text-sm font-medium">
                  {mergeState.source.canonical_name}
                </p>
              </div>
              <FormField label="保留实体" htmlFor="merge-target" required>
                <Input
                  className="mb-2"
                  aria-label="搜索合并目标"
                  placeholder="搜索标准名称或别名"
                  value={mergeTargetQuery}
                  onChange={(event) => {
                    setMergeTargetQuery(event.target.value);
                    setMergeState({ ...mergeState, targetId: "" });
                  }}
                />
                <Select
                  items={Object.fromEntries(
                    mergeCandidates.map((entity) => [
                      entity.id,
                      entity.canonical_name,
                    ]),
                  )}
                  value={mergeState.targetId || null}
                  onValueChange={(value) =>
                    setMergeState({ ...mergeState, targetId: String(value) })
                  }
                >
                  <SelectTrigger id="merge-target" className="w-full">
                    <SelectValue placeholder="选择同类型实体" />
                  </SelectTrigger>
                  <SelectContent>
                    {mergeCandidates.map((entity) => (
                      <SelectItem key={entity.id} value={entity.id}>
                        {entity.canonical_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              {mergeCandidatesQuery.isPending && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <LoaderCircle className="animate-spin" />
                  加载实体
                </div>
              )}
              {!mergeCandidatesQuery.isPending &&
                mergeCandidates.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    暂无可合并的同类型实体
                  </p>
                )}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setMergeState(null);
                setMergeTargetQuery("");
              }}
            >
              取消
            </Button>
            <Button
              disabled={!mergeTarget}
              onClick={() => {
                if (mergeTarget) setMergeConfirmOpen(true);
              }}
            >
              <GitMerge />
              继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={mergeConfirmOpen}
        onOpenChange={(open) => {
          setMergeConfirmOpen(open);
          if (!open) {
            setMergeState(null);
            setMergeTargetQuery("");
          }
        }}
        title="确认合并实体?"
        description={
          mergeState && mergeTarget
            ? `“${mergeState.source.canonical_name}”将合并到“${mergeTarget.canonical_name}”，来源、别名和引用会转移到保留实体。`
            : undefined
        }
        confirmLabel="确认合并"
        destructive
        onConfirm={() => {
          if (!mergeState || !mergeTarget) return;
          return merge.mutateAsync(mergeState);
        }}
      />

      <EntityFormDialog
        kbId={kbId}
        form={form}
        onFormChange={setForm}
        onOpenChange={(open) => !open && setForm(null)}
      />
    </div>
  );
}
