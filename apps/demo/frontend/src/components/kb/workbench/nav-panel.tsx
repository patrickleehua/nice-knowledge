"use client";

// 工作台左栏（sources / wiki）：
// - 两类列表共用分组标题、行高、缩进、选中态与过滤反馈；
// - 分组折叠是个人浏览偏好，按知识库保存在浏览器；
// - Wiki 页面顺序是团队信息结构，仅允许同类型内拖拽并保存到知识库；
// - 页面重命名沿用 Wiki 的可写约束，快照投影仍通过审核链路修改。

import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Folder,
  GripVertical,
  Loader2,
  MoreHorizontal,
  Pencil,
  Search,
  X,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState, type CSSProperties, type FormEvent } from "react";
import { toast } from "sonner";
import {
  kbPagesQueryKey,
  useKbPages,
  type WikiPage,
} from "@/components/kb/wiki/data";
import { PAGE_TYPE_ORDER, pageTypeMeta } from "@/components/kb/wiki/page-types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { useCurrentOrg } from "@/lib/auth";
import { errMsg } from "@/lib/utils";
import { useStoredList } from "@/lib/use-local-storage";
import { useUrlState } from "@/lib/use-url-state";
import { cn } from "@/lib/utils";
import type { KnowledgeBase, SourceDocument } from "@/lib/types";
import {
  ACTIVE_STATUSES,
  KB_DOCUMENTS_SOFT_LIMIT,
  useKbDocuments,
} from "./kb-data";
import type { KbViewMeta } from "./views";
import {
  buildPageOrder,
  sortByNavigationOrder,
  wikiPageNavigationKey,
} from "./wiki-navigation";

const WRITER_ROLES = new Set(["platform_admin", "org_admin", "operator"]);

function PanelHint({ text }: { text: string }) {
  return <p className="px-3 py-2 text-xs text-muted-foreground">{text}</p>;
}

/** 左栏顶部的即时过滤框（纯前端过滤已加载项，不额外发请求）。 */
function PanelFilter({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
  label: string;
}) {
  return (
    <div className="relative shrink-0 border-b border-border/70 px-2 py-2">
      <Search className="pointer-events-none absolute top-1/2 left-4 size-3.5 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={label}
        placeholder={placeholder}
        className="h-8 pr-8 pl-7 text-xs"
      />
      {value && (
        <button
          type="button"
          aria-label="清除过滤"
          onClick={() => onChange("")}
          className="absolute top-1/2 right-4 -translate-y-1/2 rounded-sm p-0.5 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

function TreeGroupHeader({
  icon: Icon,
  label,
  count,
  collapsed,
  onToggle,
}: {
  icon: LucideIcon;
  label: string;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={!collapsed}
      onClick={onToggle}
      className="group flex h-8 w-full items-center gap-1 rounded-md px-1 text-left text-xs font-medium text-muted-foreground transition-colors hover:bg-sidebar-accent/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {collapsed ? (
        <ChevronRight className="size-3.5 shrink-0" />
      ) : (
        <ChevronDown className="size-3.5 shrink-0" />
      )}
      <Icon className="size-3.5 shrink-0" />
      <span className="truncate" title={label}>
        {label}
      </span>
      <span className="ml-auto pr-1 text-[11px] font-normal tabular-nums">
        {count}
      </span>
    </button>
  );
}

function statusDotClass(status: SourceDocument["status"]): string {
  if (ACTIVE_STATUSES.has(status)) return "bg-primary";
  if (status === "failed") return "bg-destructive";
  if (status === "completed") return "bg-success";
  return "bg-muted-foreground/40";
}

/** sources：文档目录树（文件夹分组、状态点标注；点击打开文档详情预览）。 */
function SourcesTree({ kbId }: { kbId: string }) {
  const { data: docs, isPending } = useKbDocuments(kbId);
  const { get, set } = useUrlState();
  const activeDocId = get("doc");
  const [filter, setFilter] = useState("");
  const collapsedFolders = useStoredList(`kb-source-folders:${kbId}`, 100);

  const matched = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    if (!keyword) return docs ?? [];
    return (docs ?? []).filter(
      (doc) =>
        doc.filename.toLowerCase().includes(keyword) ||
        (doc.rel_path ?? "").toLowerCase().includes(keyword),
    );
  }, [docs, filter]);

  const openDoc = (docId: string) => {
    set({ view: "sources", doc: docId }, { reset: ["start", "end"] });
  };

  if (isPending) return <PanelHint text="正在加载文档…" />;
  if (!docs?.length) return <PanelHint text="暂无文档，先上传资料" />;

  const groups = new Map<string, SourceDocument[]>();
  for (const doc of matched) {
    const folder = doc.rel_path?.includes("/")
      ? doc.rel_path.slice(0, doc.rel_path.lastIndexOf("/"))
      : "";
    groups.set(folder, [...(groups.get(folder) ?? []), doc]);
  }

  const searching = filter.trim().length > 0;

  return (
    <div className="flex h-full flex-col">
      <PanelFilter
        value={filter}
        onChange={setFilter}
        label="过滤文档"
        placeholder="过滤文档名或目录"
      />
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
        {matched.length === 0 && (
          <PanelHint text={`没有匹配「${filter.trim()}」的文档`} />
        )}
        {[...groups.entries()].map(([folder, items]) => {
          const collapsed =
            Boolean(folder) &&
            !searching &&
            collapsedFolders.items.includes(folder);
          return (
            <div key={folder || "__root__"}>
              {folder && (
                <TreeGroupHeader
                  icon={Folder}
                  label={folder}
                  count={items.length}
                  collapsed={collapsed}
                  onToggle={() =>
                    collapsed
                      ? collapsedFolders.remove(folder)
                      : collapsedFolders.push(folder)
                  }
                />
              )}
              {!collapsed &&
                items.map((doc) => (
                  <button
                    key={doc.id}
                    type="button"
                    data-panel-navigation
                    onClick={() => openDoc(doc.id)}
                    aria-current={activeDocId === doc.id ? "page" : undefined}
                    className={cn(
                      "flex h-8 w-full items-center gap-2 rounded-md px-2 text-left text-xs transition-colors hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      folder && "ml-4 w-[calc(100%-1rem)]",
                      activeDocId === doc.id &&
                        "bg-sidebar-accent font-medium text-accent-foreground",
                    )}
                    title={doc.filename}
                  >
                    <span
                      className={cn(
                        "size-1.5 shrink-0 rounded-full",
                        statusDotClass(doc.status),
                      )}
                    />
                    <span className="truncate">{doc.filename}</span>
                  </button>
                ))}
            </div>
          );
        })}
        {docs.length >= KB_DOCUMENTS_SOFT_LIMIT && (
          <p className="px-2 pt-2 text-[11px] text-muted-foreground">
            目录树仅列出最近 {KB_DOCUMENTS_SOFT_LIMIT}{" "}
            份文档，更早的请在右侧列表中检索。
          </p>
        )}
      </div>
    </div>
  );
}

function SortablePageRow({
  page,
  active,
  showDragHandle,
  sortable,
  canRename,
  renamePending,
  onOpen,
  onRename,
}: {
  page: WikiPage;
  active: boolean;
  showDragHandle: boolean;
  sortable: boolean;
  canRename: boolean;
  renamePending: boolean;
  onOpen: () => void;
  onRename: (title: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(page.title);
  const {
    attributes,
    listeners,
    setActivatorNodeRef,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: page.id, disabled: !sortable });

  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 1 : undefined,
  };

  const submitRename = async (event: FormEvent) => {
    event.preventDefault();
    const next = title.trim();
    if (!next || next === page.title) {
      setTitle(page.title);
      setEditing(false);
      return;
    }
    try {
      await onRename(next);
      setEditing(false);
    } catch {
      // Mutation 已展示错误，保留输入便于直接修正后重试。
    }
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        "group relative ml-4 flex h-8 w-[calc(100%-1rem)] items-center rounded-md transition-colors hover:bg-sidebar-accent focus-within:bg-sidebar-accent",
        active && "bg-sidebar-accent font-medium text-accent-foreground",
        isDragging && "bg-sidebar-accent shadow-sm ring-1 ring-border",
      )}
    >
      {showDragHandle && (
        <button
          ref={setActivatorNodeRef}
          type="button"
          disabled={!sortable}
          aria-label={`拖动“${page.title}”排序`}
          title="拖动排序；也可按空格后用方向键移动"
          className="ml-0.5 flex size-6 shrink-0 cursor-grab items-center justify-center rounded text-muted-foreground/70 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:cursor-grabbing disabled:cursor-wait disabled:opacity-0"
          {...attributes}
          {...listeners}
        >
          <GripVertical className="size-3.5" />
        </button>
      )}

      {editing ? (
        <form
          className="flex min-w-0 flex-1 items-center gap-1 px-1"
          onSubmit={submitRename}
        >
          <Input
            autoFocus
            value={title}
            aria-label="页面新名称"
            className="h-6 min-w-0 flex-1 px-1.5 text-xs"
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setTitle(page.title);
                setEditing(false);
              }
            }}
          />
          <button
            type="submit"
            disabled={!title.trim() || renamePending}
            aria-label="保存名称"
            className="flex size-6 shrink-0 items-center justify-center rounded text-primary hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            {renamePending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Check className="size-3.5" />
            )}
          </button>
        </form>
      ) : (
        <>
          <button
            type="button"
            data-panel-navigation
            onClick={onOpen}
            aria-current={active ? "page" : undefined}
            className={cn(
              "min-w-0 flex-1 truncate px-2 text-left text-xs focus-visible:outline-none",
              !showDragHandle && "pl-2",
            )}
            title={page.title}
          >
            {page.title}
          </button>
          {canRename && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <button
                    type="button"
                    aria-label={`“${page.title}”的页面操作`}
                    className="mr-0.5 flex size-6 shrink-0 items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 hover:bg-background/70 hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                }
              >
                <MoreHorizontal className="size-3.5" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="right">
                <DropdownMenuItem
                  onClick={() => {
                    setTitle(page.title);
                    setEditing(true);
                  }}
                >
                  <Pencil className="size-3.5" />
                  重命名
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </>
      )}
    </div>
  );
}

function PageGroup({
  type,
  items,
  activePageId,
  collapsed,
  searching,
  canOrganize,
  orderPending,
  renamingPageId,
  onToggle,
  onOpen,
  onReorder,
  onRename,
}: {
  type: string;
  items: WikiPage[];
  activePageId: string | null;
  collapsed: boolean;
  searching: boolean;
  canOrganize: boolean;
  orderPending: boolean;
  renamingPageId: string | null;
  onToggle: () => void;
  onOpen: (pageId: string) => void;
  onReorder: (items: WikiPage[]) => void;
  onRename: (page: WikiPage, title: string) => Promise<void>;
}) {
  const meta = pageTypeMeta(type);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );
  const showDragHandle = canOrganize && items.length > 1 && !searching;
  const sortable = showDragHandle && !orderPending;

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const oldIndex = items.findIndex((page) => page.id === active.id);
    const newIndex = items.findIndex((page) => page.id === over.id);
    if (oldIndex === -1 || newIndex === -1) return;
    onReorder(arrayMove(items, oldIndex, newIndex));
  };

  return (
    <div>
      <TreeGroupHeader
        icon={meta.icon}
        label={meta.label}
        count={items.length}
        collapsed={collapsed}
        onToggle={onToggle}
      />
      {!collapsed && (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={items.map((page) => page.id)}
            strategy={verticalListSortingStrategy}
          >
            {items.map((page) => (
              <SortablePageRow
                key={page.id}
                page={page}
                active={activePageId === page.id}
                showDragHandle={showDragHandle}
                sortable={sortable}
                canRename={
                  canOrganize &&
                  !page.snapshot_id &&
                  !(
                    page.draft_status === "pending_review" &&
                    page.draft_content != null
                  )
                }
                renamePending={renamingPageId === page.id}
                onOpen={() => onOpen(page.id)}
                onRename={(title) => onRename(page, title)}
              />
            ))}
          </SortableContext>
        </DndContext>
      )}
    </div>
  );
}

/** wiki：可折叠、可排序的分组页面树。 */
function PageList({
  kbId,
  kb,
}: {
  kbId: string;
  kb: KnowledgeBase | undefined;
}) {
  const { data: pages, isPending } = useKbPages(kbId);
  const queryClient = useQueryClient();
  const currentOrg = useCurrentOrg();
  const { get, set } = useUrlState();
  const activePageId = get("page");
  const [filter, setFilter] = useState("");
  const collapsedTypes = useStoredList(`kb-wiki-groups:${kbId}`, 100);

  const canOrganize = Boolean(
    kb &&
    currentOrg &&
    kb.org_id === currentOrg.id &&
    WRITER_ROLES.has(currentOrg.role),
  );

  const orderedPages = useMemo(() => {
    if (!pages) return [];
    return sortByNavigationOrder(
      pages,
      kb?.wiki_navigation?.page_order ?? [],
      wikiPageNavigationKey,
    );
  }, [kb?.wiki_navigation?.page_order, pages]);

  const matched = useMemo(() => {
    const keyword = filter.trim().toLowerCase();
    if (!keyword) return orderedPages;
    return orderedPages.filter((page) =>
      page.title.toLowerCase().includes(keyword),
    );
  }, [filter, orderedPages]);

  const groups = new Map<string, WikiPage[]>();
  for (const page of matched) {
    groups.set(page.page_type, [...(groups.get(page.page_type) ?? []), page]);
  }
  const orderedTypes = [...groups.keys()].sort((a, b) => {
    const aIndex = PAGE_TYPE_ORDER.indexOf(a);
    const bIndex = PAGE_TYPE_ORDER.indexOf(b);
    if (aIndex !== -1 || bIndex !== -1) {
      return (
        (aIndex === -1 ? Number.MAX_SAFE_INTEGER : aIndex) -
        (bIndex === -1 ? Number.MAX_SAFE_INTEGER : bIndex)
      );
    }
    return a.localeCompare(b);
  });

  const saveOrder = useMutation({
    mutationFn: ({
      pageOrder,
    }: {
      pageOrder: string[];
      previous: KnowledgeBase[] | undefined;
    }) =>
      api.patch<KnowledgeBase>(`/kb/bases/${kbId}`, {
        wiki_navigation: { page_order: pageOrder },
      }),
    onError: (error, variables) => {
      queryClient.setQueryData(["kb-bases"], variables.previous);
      toast.error(errMsg(error, "页面排序保存失败"));
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<KnowledgeBase[]>(["kb-bases"], (old) =>
        old?.map((base) => (base.id === updated.id ? updated : base)),
      );
    },
  });

  const rename = useMutation({
    mutationFn: ({ page, title }: { page: WikiPage; title: string }) =>
      api.patch<WikiPage>(`/kb/pages/${page.id}`, { title }),
    onSuccess: (updated) => {
      queryClient.setQueryData<WikiPage[]>(kbPagesQueryKey(kbId), (old) =>
        old?.map((page) => (page.id === updated.id ? updated : page)),
      );
      toast.success("页面已重命名");
    },
    onError: (error) => toast.error(errMsg(error, "重命名失败")),
  });

  const openPage = (pageId: string) => set({ view: "wiki", page: pageId });

  const reorderType = (type: string, nextItems: WikiPage[]) => {
    const pageOrder = buildPageOrder(
      orderedTypes,
      groups,
      type,
      nextItems,
      wikiPageNavigationKey,
    );
    const previous = queryClient.getQueryData<KnowledgeBase[]>(["kb-bases"]);

    // dnd-kit 在 onDragEnd 后立即清除 transform。必须在同一个事件里同步
    // 提交新顺序，否则会先按旧数组回弹，再等异步 mutation 跳到新位置。
    void queryClient.cancelQueries({ queryKey: ["kb-bases"] });
    queryClient.setQueryData<KnowledgeBase[]>(["kb-bases"], (old) =>
      old?.map((base) =>
        base.id === kbId
          ? { ...base, wiki_navigation: { page_order: pageOrder } }
          : base,
      ),
    );
    saveOrder.mutate({ pageOrder, previous });
  };

  if (isPending) return <PanelHint text="正在加载页面…" />;
  if (!pages?.length) return <PanelHint text="还没有 Wiki 页面" />;

  const searching = filter.trim().length > 0;

  return (
    <div className="flex h-full flex-col">
      <PanelFilter
        value={filter}
        onChange={setFilter}
        label="过滤页面"
        placeholder="过滤页面标题"
      />
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
        {matched.length === 0 && (
          <PanelHint text={`没有匹配「${filter.trim()}」的页面`} />
        )}
        {orderedTypes.map((type) => {
          const items = groups.get(type) ?? [];
          const collapsed = !searching && collapsedTypes.items.includes(type);
          return (
            <PageGroup
              key={type}
              type={type}
              items={items}
              activePageId={activePageId}
              collapsed={collapsed}
              searching={searching}
              canOrganize={canOrganize}
              orderPending={saveOrder.isPending}
              renamingPageId={
                rename.isPending ? (rename.variables?.page.id ?? null) : null
              }
              onToggle={() =>
                collapsed
                  ? collapsedTypes.remove(type)
                  : collapsedTypes.push(type)
              }
              onOpen={openPage}
              onReorder={(nextItems) => reorderType(type, nextItems)}
              onRename={async (page, title) => {
                await rename.mutateAsync({ page, title });
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

export const SIDEBAR_TITLES: Record<
  NonNullable<KbViewMeta["sidebar"]>,
  string
> = {
  documents: "文档目录",
  pages: "Wiki 页面",
};

export function NavPanel({
  kbId,
  kb,
  sidebar,
}: {
  kbId: string;
  kb: KnowledgeBase | undefined;
  sidebar: NonNullable<KbViewMeta["sidebar"]>;
}) {
  if (sidebar === "documents") return <SourcesTree kbId={kbId} />;
  return <PageList kbId={kbId} kb={kb} />;
}
