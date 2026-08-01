"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  LoaderCircle,
  MapPin,
  Pencil,
  Plus,
  Route,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  EmptyState,
  FormField,
} from "@/components/shared";
import { CustomEntitiesSection } from "@/components/kb/custom-entities-section";
import { routeDiagnosticPresentation } from "@/components/kb/route-ingestion";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import { useUrlState } from "@/lib/use-url-state";
import { cn } from "@/lib/utils";
import type {
  CostEntry,
  Destination,
  EntityType,
  HotelEntry,
  PoiEntry,
  RouteEntry,
  RouteKnowledgeDiagnostics,
} from "@/lib/types";

type EntityRow = Record<string, unknown>;
const ENTITY_PAGE_SIZE = 200;

function useEntityPages<T>(queryKey: string, apiPath: string, kbId: string) {
  return useInfiniteQuery({
    queryKey: [queryKey, kbId, "paginated"],
    queryFn: ({ pageParam }) =>
      api.get<T[]>(
        `/kb/${apiPath}?kb_id=${kbId}&limit=${ENTITY_PAGE_SIZE}&offset=${pageParam}`,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === ENTITY_PAGE_SIZE
        ? allPages.reduce((count, page) => count + page.length, 0)
        : undefined,
  });
}

/** 单个实体分区的 DataTable(加载/空态内建;操作列含编辑 + 破坏性删除确认) */
function SectionTable({
  label,
  rows,
  columns,
  onNew,
  onEdit,
  onDelete,
  hasNextPage,
}: {
  label: string;
  rows: EntityRow[];
  columns: { key: string; label: string }[];
  onNew: () => void;
  onEdit: (row: EntityRow) => void;
  onDelete: (row: EntityRow) => Promise<unknown>;
  hasNextPage?: boolean;
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const cols = useMemo<ColumnDef<EntityRow, any>[]>(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dataCols: ColumnDef<EntityRow, any>[] = columns.map((c) => ({
      id: c.key,
      header: c.label,
      accessorFn: (row) => row[c.key],
      cell: ({ getValue }) => {
        const v = getValue();
        return v === null || v === undefined || v === "" ? "—" : String(v);
      },
    }));
    return [
      ...dataCols,
      {
        id: "__actions__",
        enableSorting: false,
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => (
          <div className="flex justify-end gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="编辑"
              onClick={() => onEdit(row.original)}
            >
              <Pencil className="size-3.5" />
            </Button>
            <ConfirmDialog
              trigger={
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  aria-label="删除"
                >
                  <Trash2 className="size-3.5 text-destructive" />
                </Button>
              }
              title={`删除「${String(row.original.name ?? "")}」?`}
              description="删除后不可恢复。"
              destructive
              confirmLabel="删除"
              onConfirm={() => onDelete(row.original)}
            />
          </div>
        ),
      },
    ];
  }, [columns, onEdit, onDelete]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        {/* rows 是「已加载且已按目的地过滤」的行数,不是总数——如实标注,别当计数用 */}
        <CardTitle className="text-sm">
          {label}
          <span className="ml-1.5 text-xs font-normal text-muted-foreground tabular-nums">
            已加载 {rows.length}
            {hasNextPage ? "+" : ""}
          </span>
        </CardTitle>
        <Button size="sm" variant="outline" onClick={onNew}>
          <Plus className="size-3.5" />
          新增
        </Button>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={cols}
          data={rows}
          getRowId={(r) => String(r.id)}
          empty={{ title: "暂无数据" }}
        />
      </CardContent>
    </Card>
  );
}

// 每类实体的可编辑字段描述(顺序即表单顺序)
type FieldDef = {
  key: string;
  label: string;
  kind?: "text" | "number" | "textarea";
  required?: boolean;
};

const ENTITY_META: Record<
  string,
  { label: string; api: string; fields: FieldDef[] }
> = {
  destination: {
    label: "目的地",
    api: "destinations",
    fields: [
      { key: "name", label: "名称", required: true },
      { key: "country", label: "国家" },
    ],
  },
  hotel: {
    label: "酒店",
    api: "hotels",
    fields: [
      { key: "name", label: "名称", required: true },
      { key: "city", label: "城市", required: true },
      { key: "star", label: "星级" },
      { key: "room_type", label: "房型" },
      { key: "price_ref", label: "参考价", kind: "number" },
      { key: "currency", label: "币种" },
      { key: "notes", label: "备注", kind: "textarea" },
    ],
  },
  cost: {
    label: "成本项",
    api: "costs",
    fields: [
      { key: "name", label: "名称", required: true },
      { key: "category", label: "类别", required: true },
      { key: "city", label: "城市" },
      { key: "unit", label: "单位" },
      { key: "unit_cost", label: "单价", kind: "number" },
      { key: "currency", label: "币种" },
      { key: "notes", label: "备注", kind: "textarea" },
    ],
  },
  poi: {
    label: "景点",
    api: "pois",
    fields: [
      { key: "name", label: "名称", required: true },
      { key: "city", label: "城市" },
      { key: "poi_type", label: "类型" },
      { key: "ticket_info", label: "门票信息", kind: "textarea" },
      { key: "visit_minutes", label: "建议游览(分钟)", kind: "number" },
      { key: "description", label: "介绍", kind: "textarea" },
    ],
  },
};

interface Editing {
  entity: string; // ENTITY_META key
  row: Record<string, unknown> | null; // null = 新建
}

export function EntitiesTab({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  // 目的地筛选进 URL:刷新与分享不丢(此前是组件内 useState)
  const { get, set } = useUrlState();
  const selectedDest = get("dest") ?? "all";
  const setSelectedDest = (next: string) =>
    set({ dest: next === "all" ? null : next });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [form, setForm] = useState<Record<string, unknown>>({});

  const destinationsQuery = useEntityPages<Destination>(
    "kb-destinations",
    "destinations",
    kbId,
  );
  const hotelsQuery = useEntityPages<HotelEntry>("kb-hotels", "hotels", kbId);
  const costsQuery = useEntityPages<CostEntry>("kb-costs", "costs", kbId);
  const poisQuery = useEntityPages<PoiEntry>("kb-pois", "pois", kbId);
  const routesQuery = useEntityPages<RouteEntry>("kb-routes", "routes", kbId);
  const destinations = destinationsQuery.data?.pages.flatMap((page) => page);
  const hotels = hotelsQuery.data?.pages.flatMap((page) => page);
  const costs = costsQuery.data?.pages.flatMap((page) => page);
  const pois = poisQuery.data?.pages.flatMap((page) => page);
  const routes = routesQuery.data?.pages.flatMap((page) => page);

  const invalidate = () =>
    queryClient
      .invalidateQueries({ queryKey: ["kb-destinations", kbId] })
      .then(() =>
        Promise.all(
          ["kb-hotels", "kb-costs", "kb-pois", "kb-routes"].map((k) =>
            queryClient.invalidateQueries({ queryKey: [k, kbId] }),
          ),
        ),
      );

  const save = useMutation({
    mutationFn: () => {
      if (!editing) throw new Error("no editing state");
      const meta = ENTITY_META[editing.entity];
      const payload: Record<string, unknown> = {};
      for (const f of meta.fields) {
        const v = form[f.key];
        payload[f.key] =
          f.kind === "number"
            ? v === "" || v === undefined || v === null
              ? null
              : Number(v)
            : (v ?? null) || null;
      }
      if (editing.row) {
        return api.patch(`/kb/${meta.api}/${editing.row.id}`, payload);
      }
      return api.post(`/kb/${meta.api}`, {
        ...payload,
        kb_id: kbId,
        destination_id:
          editing.entity !== "destination" && selectedDest !== "all"
            ? selectedDest
            : undefined,
      });
    },
    onSuccess: () => {
      toast.success("已保存");
      setEditing(null);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const remove = useMutation({
    mutationFn: ({ entity, id }: { entity: string; id: string }) =>
      api.delete(`/kb/${ENTITY_META[entity].api}/${id}`),
    onSuccess: () => {
      toast.success("已删除");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  function openEditor(entity: string, row: Record<string, unknown> | null) {
    const init: Record<string, unknown> = {};
    for (const f of ENTITY_META[entity].fields) {
      init[f.key] = row?.[f.key] ?? "";
    }
    setForm(init);
    setEditing({ entity, row });
  }

  const byDest = <T extends { destination_id: string | null }>(rows?: T[]) =>
    selectedDest === "all"
      ? (rows ?? [])
      : (rows ?? []).filter((r) => r.destination_id === selectedDest);

  const filteredHotels = byDest(hotels);
  const filteredCosts = byDest(costs);
  const filteredPois = byDest(pois);

  const sections: {
    entity: string;
    rows: Record<string, unknown>[];
    columns: { key: string; label: string }[];
    query: {
      hasNextPage: boolean;
      isFetchingNextPage: boolean;
      fetchNextPage: () => Promise<unknown>;
    };
  }[] = [
    {
      entity: "hotel",
      rows: filteredHotels as unknown as Record<string, unknown>[],
      columns: [
        { key: "name", label: "名称" },
        { key: "city", label: "城市" },
        { key: "star", label: "星级" },
        { key: "price_ref", label: "参考价" },
      ],
      query: hotelsQuery,
    },
    {
      entity: "cost",
      rows: filteredCosts as unknown as Record<string, unknown>[],
      columns: [
        { key: "name", label: "名称" },
        { key: "category", label: "类别" },
        { key: "city", label: "城市" },
        { key: "unit_cost", label: "单价" },
      ],
      query: costsQuery,
    },
    {
      entity: "poi",
      rows: filteredPois as unknown as Record<string, unknown>[],
      columns: [
        { key: "name", label: "名称" },
        { key: "city", label: "城市" },
        { key: "poi_type", label: "类型" },
        { key: "visit_minutes", label: "游览分钟" },
      ],
      query: poisQuery,
    },
  ];

  const editingMeta = editing ? ENTITY_META[editing.entity] : null;

  // 有自定义类型时才给「自定义」这一档(CustomEntitiesSection 无类型时本就不渲染)
  const { data: entityTypes } = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
  });
  const hasCustomTypes = (entityTypes ?? []).some((t) => !t.is_builtin);

  // 一次只展示一类实体:此前 4 张表格 + 自定义区全部堆在一页,信息密度失控,
  // 且每张表各带一套分页控件。现在类型切换在顶部,下面只有一张表和一个分页。
  const kinds: {
    key: string;
    label: string;
    count: number;
    hasNextPage: boolean;
  }[] = [
    ...sections.map((section) => ({
      key: section.entity,
      label: ENTITY_META[section.entity].label,
      count: section.rows.length,
      hasNextPage: section.query.hasNextPage,
    })),
    {
      key: "route",
      label: "线路知识",
      count: routes?.length ?? 0,
      hasNextPage: routesQuery.hasNextPage,
    },
    ...(hasCustomTypes
      ? [
          {
            key: "custom",
            label: "自定义类型",
            count: 0,
            hasNextPage: false,
          },
        ]
      : []),
  ];
  const rawKind = get("kind");
  const activeKind = kinds.some((k) => k.key === rawKind)
    ? (rawKind as string)
    : "hotel";
  const activeSection = sections.find((s) => s.entity === activeKind);
  const routeDiagnosticsQuery = useQuery({
    queryKey: ["kb-route-diagnostics", kbId],
    queryFn: () =>
      api.get<RouteKnowledgeDiagnostics>(`/kb/bases/${kbId}/route-diagnostics`),
    enabled: activeKind === "route" && routes?.length === 0,
    refetchInterval: (query) =>
      query.state.data?.reason_code === "extraction_in_progress" ? 2000 : false,
  });
  const routeDiagnostic = routeDiagnosticsQuery.data
    ? routeDiagnosticPresentation(routeDiagnosticsQuery.data)
    : null;

  const navigateFromRouteDiagnostic = () => {
    if (!routeDiagnostic?.destination) return;
    const reclassificationId =
      routeDiagnostic.destination === "sources" &&
      routeDiagnosticsQuery.data?.next_action === "reclassify"
        ? routeDiagnosticsQuery.data.eligible_general_document_ids[0]
        : null;
    set(
      {
        view: routeDiagnostic.destination,
        tab: null,
        dest: null,
        kind: null,
        reclassify: reclassificationId,
      },
      { reset: ["q", "status", "sort", "doc", "start", "end"] },
    );
  };

  // 当前这一类的分页(目的地列独立,跟着自己的按钮走)
  const activeQuery =
    activeSection?.query ?? (activeKind === "route" ? routesQuery : undefined);

  return (
    <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
      {/* 左:目的地树 */}
      <Card className="self-start">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm">
            目的地({destinations?.length ?? 0})
          </CardTitle>
          <Button
            size="sm"
            variant="outline"
            onClick={() => openEditor("destination", null)}
          >
            <Plus className="size-3.5" />
          </Button>
        </CardHeader>
        <CardContent className="space-y-0.5">
          <button
            className={cn(
              "w-full rounded-md px-2 py-1.5 text-left text-sm",
              selectedDest === "all"
                ? "bg-primary/10 font-medium text-primary"
                : "hover:bg-muted",
            )}
            onClick={() => setSelectedDest("all")}
          >
            全部
          </button>
          {destinations?.map((d) => {
            const count =
              (hotels?.filter((h) => h.destination_id === d.id).length ?? 0) +
              (costs?.filter((c) => c.destination_id === d.id).length ?? 0) +
              (pois?.filter((p) => p.destination_id === d.id).length ?? 0);
            return (
              <div
                key={d.id}
                className={cn(
                  "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm",
                  selectedDest === d.id
                    ? "bg-primary/10 font-medium text-primary"
                    : "hover:bg-muted",
                )}
              >
                <button
                  className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  onClick={() => setSelectedDest(d.id)}
                >
                  <MapPin className="size-3.5 shrink-0" />
                  <span className="truncate">{d.name}</span>
                  <span className="text-xs text-muted-foreground">
                    ({count})
                  </span>
                </button>
                <span className="flex shrink-0 items-center opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-6"
                    aria-label={`编辑目的地 ${d.name}`}
                    onClick={() =>
                      openEditor(
                        "destination",
                        d as unknown as Record<string, unknown>,
                      )
                    }
                  >
                    <Pencil className="size-3" />
                  </Button>
                  <ConfirmDialog
                    trigger={
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-6"
                        aria-label={`删除目的地 ${d.name}`}
                      >
                        <Trash2 className="size-3 text-destructive" />
                      </Button>
                    }
                    title={`删除目的地「${d.name}」?`}
                    description="删除后不可恢复。挂在该目的地下的酒店、成本项与景点不会被删除，但会失去归属。"
                    destructive
                    confirmLabel="删除"
                    onConfirm={async () => {
                      await remove.mutateAsync({
                        entity: "destination",
                        id: d.id,
                      });
                      if (selectedDest === d.id) setSelectedDest("all");
                    }}
                  />
                </span>
              </div>
            );
          })}
        </CardContent>
      </Card>

      {/* 右:类型切换 + 单张表 + 单一分页 */}
      <div className="space-y-4">
        <div
          role="tablist"
          aria-label="实体类型"
          className="flex flex-wrap items-center gap-1 rounded-lg border border-border p-1"
        >
          {kinds.map((kind) => (
            <button
              key={kind.key}
              type="button"
              role="tab"
              aria-selected={activeKind === kind.key}
              onClick={() =>
                set({ kind: kind.key === "hotel" ? null : kind.key })
              }
              className={cn(
                "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-sm transition-colors",
                activeKind === kind.key
                  ? "bg-accent font-medium text-accent-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {kind.label}
              {kind.key !== "custom" && (
                <span className="text-xs tabular-nums opacity-70">
                  {kind.count}
                  {kind.hasNextPage ? "+" : ""}
                </span>
              )}
            </button>
          ))}
        </div>

        {activeSection && (
          <SectionTable
            label={ENTITY_META[activeSection.entity].label}
            rows={activeSection.rows}
            columns={activeSection.columns}
            onNew={() => openEditor(activeSection.entity, null)}
            onEdit={(row) => openEditor(activeSection.entity, row)}
            onDelete={(row) =>
              remove.mutateAsync({
                entity: activeSection.entity,
                id: row.id as string,
              })
            }
            hasNextPage={activeSection.query.hasNextPage}
          />
        )}

        {activeKind === "route" && (
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">
                线路知识
                <span className="ml-1.5 text-xs font-normal text-muted-foreground tabular-nums">
                  已加载 {routes?.length ?? 0}
                  {routesQuery.hasNextPage ? "+" : ""}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-1.5">
              {routesQuery.isPending && (
                <p className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
                  <LoaderCircle className="size-4 animate-spin" />
                  正在加载线路知识
                </p>
              )}
              {!routesQuery.isPending &&
                routes?.length === 0 &&
                routeDiagnosticsQuery.isPending && (
                  <p className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
                    <LoaderCircle className="size-4 animate-spin" />
                    正在诊断线路生命周期
                  </p>
                )}
              {!routesQuery.isPending &&
                routes?.length === 0 &&
                routeDiagnosticsQuery.isError && (
                  <EmptyState
                    icon={Route}
                    title="线路诊断加载失败"
                    description={errMsg(routeDiagnosticsQuery.error)}
                    action={
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => void routeDiagnosticsQuery.refetch()}
                      >
                        重试诊断
                      </Button>
                    }
                  />
                )}
              {!routesQuery.isPending &&
                routes?.length === 0 &&
                routeDiagnostic && (
                  <EmptyState
                    icon={Route}
                    title={routeDiagnostic.title}
                    description={routeDiagnostic.description}
                    action={
                      routeDiagnostic.actionLabel ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={navigateFromRouteDiagnostic}
                        >
                          {routeDiagnostic.actionLabel}
                        </Button>
                      ) : undefined
                    }
                  />
                )}
              {routesQuery.isError && (
                <p className="py-6 text-center text-sm text-destructive">
                  {errMsg(routesQuery.error)}
                </p>
              )}
              {routes?.map((r) => (
                <div
                  key={r.id}
                  className="flex items-center justify-between rounded border border-border p-2 text-sm"
                >
                  <span className="truncate">{r.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {r.days ? `${r.days} 天` : "—"}
                  </span>
                </div>
              ))}
              {(routes?.length ?? 0) > 0 && (
                <p className="text-xs text-muted-foreground">
                  这里展示当前已发布快照中的线路知识；提升为经营线路资产后，才会进入复用与经营统计。
                </p>
              )}
            </CardContent>
          </Card>
        )}

        {/* M3a:自定义类型实体(schema 驱动) */}
        {activeKind === "custom" && <CustomEntitiesSection kbId={kbId} />}

        {/* 单一分页:只推当前这一类,不再是一页上 5 个含义不同的「加载更多」 */}
        {activeQuery?.hasNextPage && (
          <Button
            className="w-full"
            variant="outline"
            size="sm"
            disabled={activeQuery.isFetchingNextPage}
            onClick={() => void activeQuery.fetchNextPage()}
          >
            {activeQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
          </Button>
        )}
      </div>

      {/* 编辑弹窗(新建/修改共用) */}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editing?.row ? "编辑" : "新增"}
              {editingMeta?.label}
            </DialogTitle>
          </DialogHeader>
          {editing && editingMeta && (
            <div className="space-y-3">
              {editingMeta.fields.map((f) => (
                <FormField
                  key={f.key}
                  label={f.label}
                  htmlFor={`entity-${f.key}`}
                  required={f.required}
                >
                  {f.kind === "textarea" ? (
                    <Textarea
                      id={`entity-${f.key}`}
                      value={String(form[f.key] ?? "")}
                      rows={3}
                      onChange={(e) =>
                        setForm({ ...form, [f.key]: e.target.value })
                      }
                    />
                  ) : (
                    <Input
                      id={`entity-${f.key}`}
                      type={f.kind === "number" ? "number" : "text"}
                      value={String(form[f.key] ?? "")}
                      onChange={(e) =>
                        setForm({ ...form, [f.key]: e.target.value })
                      }
                    />
                  )}
                </FormField>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditing(null)}>
              取消
            </Button>
            <Button
              disabled={
                !editingMeta ||
                save.isPending ||
                editingMeta.fields.some(
                  (f) => f.required && !String(form[f.key] ?? "").trim(),
                )
              }
              onClick={() => save.mutate()}
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
