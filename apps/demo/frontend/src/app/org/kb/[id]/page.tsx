"use client";

// 知识库详情页 = 知识工作台:顶部视图 Tab | (按需)左栏 + 中央视图 | 底部发布状态条。
// URL 状态契约:?view= 驱动(默认 overview),router.replace 同步,刷新/分享不丢;
// 各视图自身的筛选/分页也一律进 query string(见 lib/use-url-state.ts)。

import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useParams } from "next/navigation";
import { Suspense, useCallback, useState } from "react";
import {
  KbSearchCommand,
  useSearchHotkey,
} from "@/components/kb/search/search-command";
import { usePendingExtractions } from "@/components/kb/workbench/kb-data";
import { NavPanel, SIDEBAR_TITLES } from "@/components/kb/workbench/nav-panel";
import { ReleaseBar } from "@/components/kb/workbench/release-bar";
import { ViewTabs } from "@/components/kb/workbench/view-tabs";
import {
  WorkbenchShell,
  type WorkbenchSidebar,
} from "@/components/kb/workbench/workbench-shell";
import {
  isKbViewKey,
  kbView,
  type KbViewKey,
} from "@/components/kb/workbench/views";
import { EntitiesView } from "@/components/kb/views/entities-view";
import { GraphView } from "@/components/kb/views/graph-view";
import { ImagesView } from "@/components/kb/views/images-view";
import { OverviewView } from "@/components/kb/views/overview-view";
import { ReleaseView } from "@/components/kb/views/release-view";
import { ReviewView } from "@/components/kb/views/review-view";
import { SettingsView } from "@/components/kb/views/settings-view";
import { SourcesView } from "@/components/kb/views/sources-view";
import { WikiView } from "@/components/kb/views/wiki-view";
import { ShellHeader } from "@/components/shell/app-shell";
import { Breadcrumbs } from "@/components/shared";
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
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import {
  UnsavedGuardProvider,
  useUnsavedGuardCheck,
} from "@/lib/unsaved-guard";
import { useUrlState } from "@/lib/use-url-state";
import type { KnowledgeBase } from "@/lib/types";

export default function KbDetailPage() {
  return (
    <Suspense fallback={<Skeleton className="h-40 w-full" />}>
      <UnsavedGuardProvider>
        <KbWorkbench />
      </UnsavedGuardProvider>
    </Suspense>
  );
}

/** 切视图时要清掉的视图内状态,避免 ?doc= / ?page= / 筛选参数串味 */
const VIEW_SCOPED_PARAMS = [
  "doc",
  "start",
  "end",
  "page",
  "status",
  "queue",
  "review",
  "p",
  "tab",
  "dest",
  "kind",
  "q",
  "sort",
  "enrichment",
  "issue",
  "failure",
  "reclassify",
];

function KbWorkbench() {
  const { id } = useParams<{ id: string }>();
  const { get, set } = useUrlState();
  const [searchOpen, setSearchOpen] = useState(false);
  useSearchHotkey(useCallback(() => setSearchOpen(true), []));

  const checkUnsaved = useUnsavedGuardCheck();
  // 被未保存改动拦下的目标视图,等用户在确认框里决定
  const [pendingNav, setPendingNav] = useState<{
    view: KbViewKey;
    params?: Record<string, string>;
    label: string;
  } | null>(null);

  const rawView = get("view");
  const view: KbViewKey = isKbViewKey(rawView) ? rawView : "overview";

  const applyView = (next: KbViewKey, params?: Record<string, string>) =>
    set({ view: next, ...params }, { reset: VIEW_SCOPED_PARAMS });

  const setView = (next: KbViewKey, params?: Record<string, string>) => {
    if (next === view) {
      applyView(next, params);
      return;
    }
    const label = checkUnsaved();
    if (label) {
      setPendingNav({ view: next, params, label });
      return;
    }
    applyView(next, params);
  };

  const { data: bases } = useQuery({
    queryKey: ["kb-bases"],
    queryFn: () => api.get<KnowledgeBase[]>("/kb/bases"),
  });
  const kb = bases?.find((b) => b.id === id);

  // Tab 徽章与 review 视图共用同一轮询 query
  const { data: pending } = usePendingExtractions(id);

  const sidebarKind = kbView(view).sidebar;
  const sidebar: WorkbenchSidebar | undefined = sidebarKind
    ? {
        title: SIDEBAR_TITLES[sidebarKind],
        content: <NavPanel kbId={id} kb={kb} sidebar={sidebarKind} />,
      }
    : undefined;

  return (
    <>
      <ShellHeader>
        <div className="flex h-full min-w-0 items-center gap-3">
          <Breadcrumbs
            items={[
              { label: "知识库", href: "/org/kb" },
              { label: kb?.name ?? "知识库详情" },
            ]}
          />
          <Button
            variant="outline"
            size="sm"
            className="ml-auto h-8 shrink-0 gap-2 text-muted-foreground"
            onClick={() => setSearchOpen(true)}
          >
            <Search className="size-3.5" />
            <span className="hidden sm:inline">检索本库</span>
            <kbd className="hidden rounded border border-border px-1 text-[10px] md:inline-block">
              ⌘K
            </kbd>
          </Button>
        </div>
      </ShellHeader>

      {/* 响应式抵消 AppShell main 的 p-3/sm:p-6，让工作台占满头部以下视口(dvh 与 AppShell 一致) */}
      <div className="-m-3 h-[calc(100dvh-3.5rem)] sm:-m-6">
        <WorkbenchShell
          tabs={
            <ViewTabs
              view={view}
              onViewChange={setView}
              reviewCount={pending?.length ?? 0}
            />
          }
          sidebar={sidebar}
          statusBar={<ReleaseBar kbId={id} kb={kb} onNavigate={setView} />}
        >
          {view === "overview" && (
            <OverviewView kbId={id} kb={kb} onNavigate={setView} />
          )}
          {view === "sources" && <SourcesView kbId={id} />}
          {view === "images" && <ImagesView kbId={id} />}
          {view === "entities" && <EntitiesView kbId={id} />}
          {view === "wiki" && <WikiView kbId={id} />}
          {view === "graph" && <GraphView kbId={id} />}
          {view === "review" && <ReviewView kbId={id} />}
          {view === "release" && <ReleaseView kbId={id} />}
          {view === "settings" && <SettingsView kbId={id} />}
        </WorkbenchShell>
      </div>

      <KbSearchCommand
        open={searchOpen}
        onOpenChange={setSearchOpen}
        kbIds={[id]}
        scopeLabel={kb?.name}
      />

      {/* 有未保存改动时拦下视图切换,由用户决定是否放弃 */}
      <AlertDialog
        open={pendingNav !== null}
        onOpenChange={(open) => !open && setPendingNav(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>放弃未保存的改动?</AlertDialogTitle>
            <AlertDialogDescription>
              「{pendingNav?.label}
              」还有未保存的修改，离开当前视图会丢失这些改动。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>留在本页</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                if (pendingNav) applyView(pendingNav.view, pendingNav.params);
                setPendingNav(null);
              }}
            >
              放弃并离开
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
