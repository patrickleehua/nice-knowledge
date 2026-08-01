"use client";

// wiki 页详情(KB-5C,对标 llm_wiki 的 read/edit 双模交互):
// - read(默认):元信息卡(类型图标 chip / origin 徽章 / 更新时间 / wikilink 出链数)
//   + Markdown 渲染(GFM,含 wikilink 跳转)。
// - edit:原生 textarea(等宽字体撑满高度),Cmd/Ctrl+S 立即保存(preventDefault),
//   其余改动 1s 防抖自动保存(保存中/已保存 小字状态);title/page_type 顶部行内可编辑。
// - 删除两段式:arm → 3s 内点红色 pill 确认(手法同 doc-detail)。
// 本组件由 wiki-view 以 key={page.id} 挂载,切页即重置本地草稿;
// read 模式渲染本地草稿(编辑后立即一致,不等缓存回写)。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Clock,
  Link2,
  LockKeyhole,
  Loader2,
  Pencil,
  Sparkles,
  Trash2,
  UserRound,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { EmptyState, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import { useUnsavedGuard } from "@/lib/unsaved-guard";
import type { KbPage } from "@/lib/types";
import { kbPagesQueryKey, type WikiPage } from "./data";
import { DraftReview } from "./draft-review";
import { WikiMarkdownView } from "./markdown-view";
import { PAGE_TYPE_ORDER, pageTypeMeta } from "./page-types";
import { extractWikilinks } from "./wikilink";

const DELETE_ARM_MS = 3000;
const AUTOSAVE_DEBOUNCE_MS = 1000;

interface Draft {
  title: string;
  page_type: string;
  content: string;
}

function draftOf(page: WikiPage): Draft {
  return {
    title: page.title,
    page_type: page.page_type,
    content: page.content ?? "",
  };
}

function sameDraft(a: Draft, b: Draft): boolean {
  return (
    a.title === b.title &&
    a.page_type === b.page_type &&
    a.content === b.content
  );
}

type SaveState = "idle" | "dirty" | "saving" | "saved";

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

export function PageDetail({
  kbId,
  page,
  pages,
  initialEdit,
  onOpenPage,
  onCreateFromLink,
  onDeleted,
}: {
  kbId: string;
  page: WikiPage;
  /** 当前库页列表(共享缓存),供 wikilink 解析 */
  pages: WikiPage[] | undefined;
  /** 新建后直达 edit 模式 */
  initialEdit?: boolean;
  onOpenPage: (pageId: string) => void;
  /** 点击不存在的 wikilink → 预填标题的新建 Dialog */
  onCreateFromLink: (title: string) => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"read" | "edit">(
    initialEdit ? "edit" : "read",
  );
  const [draft, setDraft] = useState<Draft>(() => draftOf(page));
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [armedDelete, setArmedDelete] = useState(false);

  // refs:防抖/快捷键/卸载兜底都要拿最新草稿与已落库快照
  const draftRef = useRef(draft);
  const savedRef = useRef<Draft>(draftOf(page));
  // 串行化保存:in-flight 时不并发 PATCH(防乱序覆盖),成功后若仍 dirty 立即补存
  const inFlightRef = useRef(false);

  const save = useMutation({
    mutationFn: (d: Draft) =>
      api.patch<KbPage>(`/kb/pages/${page.id}`, {
        title: d.title.trim() || page.title,
        page_type: d.page_type.trim().toLowerCase() || "topic",
        content: d.content || null,
      }),
    onSuccess: (updated, variables) => {
      inFlightRef.current = false;
      savedRef.current = variables;
      // 直接回写列表缓存:左栏标题实时更新,且不触发整表 refetch 风暴
      queryClient.setQueryData<WikiPage[]>(kbPagesQueryKey(kbId), (old) =>
        old?.map((p) => (p.id === updated.id ? updated : p)),
      );
      if (sameDraft(draftRef.current, variables)) {
        setSaveState("saved");
        setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      } else {
        // 保存期间又有改动 → 立即补存最新草稿
        flushRef.current();
      }
    },
    onError: (err) => {
      inFlightRef.current = false;
      setSaveState("dirty");
      toast.error(errMsg(err, "保存失败"));
    },
  });

  const saveRef = useRef(save.mutate);

  const flushSave = () => {
    if (sameDraft(draftRef.current, savedRef.current)) return;
    if (inFlightRef.current) return; // 成功回调会补存最新草稿
    inFlightRef.current = true;
    setSaveState("saving");
    saveRef.current(draftRef.current);
  };
  const flushRef = useRef(flushSave);

  // 自动保存虽然会兜底,但草稿尚未落库期间关标签页/切视图仍会丢内容
  useUnsavedGuard(saveState === "dirty" || saveState === "saving", "Wiki 草稿");

  // 每次 render 后同步最新草稿/闭包进 ref(render 期赋值会触发级联渲染告警;
  // 这些 ref 只在 effect cleanup / setTimeout / 事件回调里读,commit 后同步即可)
  useEffect(() => {
    draftRef.current = draft;
    saveRef.current = save.mutate;
    flushRef.current = flushSave;
  });

  // 1s 防抖自动保存(仅 edit 模式)
  useEffect(() => {
    if (mode !== "edit") return;
    if (sameDraft(draft, savedRef.current)) return;
    setSaveState("dirty");
    const timer = setTimeout(() => flushRef.current(), AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [draft, mode]);

  // 卸载兜底:仍有未保存改动时 fire-and-forget 落库(切页/切视图不丢字)
  useEffect(() => {
    return () => {
      const d = draftRef.current;
      if (sameDraft(d, savedRef.current)) return;
      api
        .patch<KbPage>(`/kb/pages/${page.id}`, {
          title: d.title.trim() || page.title,
          page_type: d.page_type.trim().toLowerCase() || "topic",
          content: d.content || null,
        })
        .then(() =>
          queryClient.invalidateQueries({ queryKey: kbPagesQueryKey(kbId) }),
        )
        .catch((error) => {
          const message = `“${page.title}”离开页面时保存失败：${errMsg(error, "请返回页面重试")}`;
          try {
            sessionStorage.setItem(`kb-wiki-save-error:${page.id}`, message);
          } catch {
            // 隐私模式可能禁用 sessionStorage，toast 仍可见。
          }
          toast.error(message);
        });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅卸载时执行,依赖经 ref 取最新值
  }, []);

  useEffect(() => {
    try {
      const key = `kb-wiki-save-error:${page.id}`;
      const message = sessionStorage.getItem(key);
      if (message) {
        sessionStorage.removeItem(key);
        toast.error(message);
      }
    } catch {
      // sessionStorage 不可用时无需额外处理。
    }
  }, [page.id]);

  useEffect(() => {
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (sameDraft(draftRef.current, savedRef.current)) return;
      event.preventDefault();
      event.returnValue = true;
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, []);

  // 两段式删除:武装 3s 内不确认则自动解除
  useEffect(() => {
    if (!armedDelete) return;
    const timer = setTimeout(() => setArmedDelete(false), DELETE_ARM_MS);
    return () => clearTimeout(timer);
  }, [armedDelete]);

  const remove = useMutation({
    mutationFn: () => api.delete(`/kb/pages/${page.id}`),
    onSuccess: () => {
      toast.success("已删除");
      queryClient.setQueryData<WikiPage[]>(kbPagesQueryKey(kbId), (old) =>
        old?.filter((p) => p.id !== page.id),
      );
      onDeleted();
    },
    onError: (err) => toast.error(errMsg(err, "删除失败")),
  });

  const outlinks = useMemo(
    () => extractWikilinks(draft.content),
    [draft.content],
  );

  const typeMeta = pageTypeMeta(draft.page_type);
  const TypeIcon = typeMeta.icon;
  const timeLabel = page.updated_at ?? page.created_at;
  const hasPendingDraft =
    page.draft_status === "pending_review" && page.draft_content != null;
  const isSnapshotManaged = Boolean(page.snapshot_id);

  const saveHint =
    saveState === "saving" ? (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="size-3 animate-spin" />
        保存中…
      </span>
    ) : saveState === "saved" ? (
      <span className="inline-flex items-center gap-1 text-xs text-success">
        <Check className="size-3" />
        已保存 {savedAt}
      </span>
    ) : saveState === "dirty" ? (
      <span className="text-xs text-muted-foreground">未保存改动…</span>
    ) : null;

  const deleteButton = armedDelete ? (
    <Button
      variant="destructive"
      size="sm"
      className="h-7 animate-pulse rounded-full px-3 text-xs"
      disabled={remove.isPending}
      onClick={() => remove.mutate()}
    >
      确认删除
    </Button>
  ) : (
    <Button
      variant="ghost"
      size="icon"
      className="size-7 text-destructive"
      aria-label="删除页面"
      onClick={() => setArmedDelete(true)}
    >
      <Trash2 className="size-4" />
    </Button>
  );

  // ---- edit 模式 ------------------------------------------------------------
  if (mode === "edit" && !isSnapshotManaged && !hasPendingDraft) {
    return (
      <div
        className="flex min-h-full flex-col gap-3"
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
            e.preventDefault();
            flushSave();
          }
        }}
      >
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Input
            value={draft.title}
            aria-label="页面标题"
            className="h-8 min-w-48 flex-1 text-sm font-medium"
            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
          />
          <Input
            value={draft.page_type}
            aria-label="页面类型"
            list="wiki-page-type-presets"
            className="h-8 w-36 font-mono text-xs"
            onChange={(e) => setDraft({ ...draft, page_type: e.target.value })}
          />
          <datalist id="wiki-page-type-presets">
            {PAGE_TYPE_ORDER.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
          {saveHint}
          <Button
            size="sm"
            className="h-8"
            onClick={() => {
              flushSave();
              setMode("read");
            }}
          >
            <Check className="size-4" />
            完成
          </Button>
        </div>
        <textarea
          value={draft.content}
          aria-label="正文 Markdown"
          placeholder={
            "Markdown 正文,支持 GFM 表格与 [[其他页标题]] 双链…\n\nCtrl+S 立即保存,停顿 1 秒自动保存"
          }
          className={cn(
            "min-h-96 w-full flex-1 resize-none rounded-lg border border-input bg-transparent p-3",
            "font-mono text-xs leading-relaxed outline-none",
            "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
            "placeholder:text-muted-foreground",
          )}
          onChange={(e) => setDraft({ ...draft, content: e.target.value })}
        />
      </div>
    );
  }

  // ---- read 模式 ------------------------------------------------------------
  return (
    <div className="flex min-h-full flex-col gap-4">
      <div className="flex shrink-0 items-start justify-between gap-3">
        <h1 className="min-w-0 text-lg leading-tight font-semibold wrap-break-word">
          {draft.title}
        </h1>
        <div className="flex shrink-0 items-center gap-1.5">
          {saveHint}
          {!isSnapshotManaged && !hasPendingDraft && (
            <>
              <Button
                variant="outline"
                size="sm"
                className="h-7"
                onClick={() => setMode("edit")}
              >
                <Pencil className="size-3.5" />
                编辑
              </Button>
              {deleteButton}
            </>
          )}
        </div>
      </div>

      {/* 元信息卡 */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        <ToneBadge tone="teal" className="gap-1">
          <TypeIcon className="size-3" />
          {typeMeta.label}
        </ToneBadge>
        {page.origin === "llm" && (
          <ToneBadge tone="info" className="gap-1">
            <Sparkles className="size-3" />
            AI 生成
          </ToneBadge>
        )}
        {page.origin === "human" && (
          <ToneBadge tone="muted" className="gap-1">
            <UserRound className="size-3" />
            人工
          </ToneBadge>
        )}
        {isSnapshotManaged && (
          <ToneBadge tone="muted" className="gap-1">
            <LockKeyhole className="size-3" />
            快照投影
          </ToneBadge>
        )}
        {timeLabel && (
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3" />
            {fmtTime(timeLabel)}
          </span>
        )}
        <span className="inline-flex items-center gap-1">
          <Link2 className="size-3" />
          出链 {outlinks.length}
        </span>
      </div>

      {hasPendingDraft ? (
        <DraftReview
          kbId={kbId}
          page={page}
          pages={pages}
          onOpenPage={onOpenPage}
          onCreatePage={onCreateFromLink}
          onReviewed={(updated) => {
            if (updated.content !== draftRef.current.content) {
              const next = draftOf(updated);
              setDraft(next);
              draftRef.current = next;
              savedRef.current = next;
              setSaveState("idle");
            }
          }}
        />
      ) : draft.content.trim() ? (
        <WikiMarkdownView
          content={draft.content}
          pages={pages}
          onOpenPage={onOpenPage}
          onCreatePage={onCreateFromLink}
        />
      ) : (
        <EmptyState
          title="空白页"
          description="点击右上角「编辑」开始撰写 Markdown 正文"
          className="py-16"
        />
      )}
    </div>
  );
}
