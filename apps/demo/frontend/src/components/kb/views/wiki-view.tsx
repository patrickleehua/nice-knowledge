"use client";

// wiki 视图(KB-5C 重做,对标 llm_wiki 阅读/编辑体验):
// URL 契约 ?view=wiki&page={id} 驱动选页(左栏 PageList 点击 / wikilink 跳转 / lint 跳转
// 共用),中央 = 页详情 read/edit 双模;无选中时引导空态 + 新建。
// 顶部工具条:结构体检(右侧抽屉)+ 新建页面(Dialog,创建后直达 edit 模式)。

import { BookOpen, FileQuestion, Plus, Stethoscope } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { CreatePageDialog } from "@/components/kb/wiki/create-page-dialog";
import { useKbPages } from "@/components/kb/wiki/data";
import { LintPanel } from "@/components/kb/wiki/lint-panel";
import { PageDetail } from "@/components/kb/wiki/page-detail";
import { EmptyState } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { KbPage } from "@/lib/types";

export function WikiView({ kbId }: { kbId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const pageId = searchParams.get("page");

  const { data: pages, isLoading } = useKbPages(kbId);

  const [lintOpen, setLintOpen] = useState(false);
  const [create, setCreate] = useState<{
    open: boolean;
    prefillTitle?: string;
  }>({ open: false });
  // 新建后直达 edit 模式:记录目标页 id,切走即失效
  const [editOnOpenId, setEditOnOpenId] = useState<string | null>(null);

  // options.edit:从结构检查跳过来时直接进编辑态——点过去就是为了改它
  const openPage = (id: string | null, options?: { edit?: boolean }) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("view", "wiki");
    if (id) params.set("page", id);
    else params.delete("page");
    if (id && options?.edit) setEditOnOpenId(id);
    else if (id !== editOnOpenId) setEditOnOpenId(null);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };

  const onCreated = (page: KbPage) => {
    setEditOnOpenId(page.id);
    const params = new URLSearchParams(searchParams.toString());
    params.set("view", "wiki");
    params.set("page", page.id);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };

  const selected = pageId ? pages?.find((p) => p.id === pageId) : undefined;

  return (
    <div className="flex min-h-full flex-col gap-4">
      {/* 顶部工具条:页面切换器 + 检查 + 新建。
          切换器让中央区自成闭环——左栏在移动端不渲染,此前无从选页。 */}
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {pages && pages.length > 0 && (
          <Select
            items={Object.fromEntries(pages.map((p) => [p.id, p.title]))}
            value={selected?.id ?? null}
            onValueChange={(value) => openPage(String(value))}
          >
            <SelectTrigger size="sm" className="w-56" aria-label="切换页面">
              <SelectValue placeholder="选择页面…" />
            </SelectTrigger>
            <SelectContent>
              {pages.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-8"
            onClick={() => setLintOpen(true)}
          >
            <Stethoscope className="size-4" />
            检查链接与结构
          </Button>
          <Button
            size="sm"
            className="h-8"
            onClick={() => setCreate({ open: true })}
          >
            <Plus className="size-4" />
            新建页面
          </Button>
        </div>
      </div>

      {/* 中央:页详情 / 引导空态 */}
      {isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : selected ? (
        <PageDetail
          key={selected.id}
          kbId={kbId}
          page={selected}
          pages={pages}
          initialEdit={editOnOpenId === selected.id}
          onOpenPage={(id) => openPage(id)}
          onCreateFromLink={(title) =>
            setCreate({ open: true, prefillTitle: title })
          }
          onDeleted={() => openPage(null)}
        />
      ) : pageId ? (
        <EmptyState
          icon={FileQuestion}
          title="页面不存在或已删除"
          description="用上方的页面切换器重新选择,或新建一页。"
          action={
            <Button variant="outline" size="sm" onClick={() => openPage(null)}>
              返回引导页
            </Button>
          }
        />
      ) : (
        <EmptyState
          icon={BookOpen}
          title={
            pages?.length ? "用上方切换器选择一页开始阅读" : "还没有 wiki 页"
          }
          description="签证规则、常见问答、内部约定都适合放在这里;正文里用 [[标题]] 建立页面间双链。"
          action={
            <Button onClick={() => setCreate({ open: true })}>
              <Plus className="size-4" />
              新建页面
            </Button>
          }
        />
      )}

      <CreatePageDialog
        kbId={kbId}
        open={create.open}
        prefillTitle={create.prefillTitle}
        onOpenChange={(open) =>
          setCreate((s) => ({
            open,
            prefillTitle: open ? s.prefillTitle : undefined,
          }))
        }
        onCreated={onCreated}
      />
      <LintPanel
        kbId={kbId}
        open={lintOpen}
        pages={pages}
        onOpenChange={setLintOpen}
        onOpenPage={(id, options) => openPage(id, options)}
      />
    </div>
  );
}
