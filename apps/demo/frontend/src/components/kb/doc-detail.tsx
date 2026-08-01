"use client";

// 文档详情:左 Markdown 预览(约 60%)+ 右 chunk 切片侧栏。
// chunk 卡点击 → 预览定位到该片行区间;编辑走 PATCH /kb/chunks/{id}(同步重嵌),
// 删除两段式确认(第一次点变红色 Confirm pill 带 animate-pulse,再点执行)。
// 深链契约:?view=sources&doc={docId}&start={n}&end={m} 由 sources-view 解析成 initialAnchor 传入。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Pencil,
  Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  MarkdownPreview,
  type PreviewAnchor,
} from "@/components/kb/markdown-preview";
import { DocStatusBadge } from "@/components/kb/workbench/kb-data";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type { KbChunk, SourceDocument } from "@/lib/types";
const DELETE_ARM_MS = 3000;

export function DocDetail({
  kbId,
  docId,
  initialAnchor,
  onBack,
  onPrev,
  onNext,
  position,
}: {
  kbId: string;
  docId: string;
  initialAnchor?: PreviewAnchor | null;
  onBack: () => void;
  /** 上/下一篇:按列表当前的筛选与排序顺序,缺省表示到边界 */
  onPrev?: () => void;
  onNext?: () => void;
  /** 当前文档在列表中的序号,用于「第 x / y 篇」 */
  position?: { current: number; total: number };
}) {
  const queryClient = useQueryClient();
  const [anchor, setAnchor] = useState<PreviewAnchor | null>(
    initialAnchor ?? null,
  );
  const [editing, setEditing] = useState<KbChunk | null>(null);
  const [draft, setDraft] = useState("");
  const [armedDelete, setArmedDelete] = useState<string | null>(null);

  const { data: doc } = useQuery({
    queryKey: ["kb-doc", kbId, docId],
    queryFn: () => api.get<SourceDocument>(`/kb/documents/${docId}`),
  });

  const { data: chunks } = useQuery({
    queryKey: ["kb-chunks", docId],
    queryFn: () => api.get<KbChunk[]>(`/kb/documents/${docId}/chunks`),
  });

  const snapshotsQuery = useQuery({
    queryKey: ["kb-snapshots", kbId],
    queryFn: () => api.get<{ id: string }[]>(`/kb/bases/${kbId}/snapshots`),
  });
  // ChunkOut 暂未返回 snapshot_id/mutable。只要库已进入过快照链，列表中的
  // ready/active/retired 投影都按只读处理；仅从未建过快照的 legacy KB 可编辑。
  const chunksMutable =
    snapshotsQuery.isSuccess && snapshotsQuery.data.length === 0;

  // 两段式删除:武装 3s 内不二次点击则自动解除
  useEffect(() => {
    if (!armedDelete) return;
    const timer = setTimeout(() => setArmedDelete(null), DELETE_ARM_MS);
    return () => clearTimeout(timer);
  }, [armedDelete]);

  const invalidateChunks = () =>
    queryClient.invalidateQueries({ queryKey: ["kb-chunks", docId] });

  const patchChunk = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      api.patch<KbChunk>(`/kb/chunks/${id}`, { content }),
    onSuccess: (updated) => {
      if (updated.has_embedding) toast.success("已保存并重新生成向量");
      else toast.warning("已保存,但向量服务不可用,该片暂无向量");
      setEditing(null);
      invalidateChunks();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const deleteChunk = useMutation({
    mutationFn: (id: string) => api.delete(`/kb/chunks/${id}`),
    onSuccess: () => {
      toast.success("已删除切片");
      setArmedDelete(null);
      invalidateChunks();
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  // 切片一次性取回(无服务端分页),只做虚拟渲染:上千片时也只挂载视窗内的行
  const chunkList = chunks ?? [];
  const chunkScrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: chunkList.length,
    getScrollElement: () => chunkScrollRef.current,
    estimateSize: () => 96,
    overscan: 8,
  });
  const items = virtualizer.getVirtualItems();

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="size-4" />
          返回列表
        </Button>
        <span
          className="min-w-0 truncate text-sm font-medium"
          title={doc?.filename}
        >
          {doc?.filename ?? "文档"}
        </span>
        {doc && <DocStatusBadge status={doc.status} />}
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">
          {chunks?.length ?? 0} 个切片
        </span>
        {(onPrev || onNext) && (
          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="上一篇文档"
              title="上一篇"
              disabled={!onPrev}
              onClick={onPrev}
            >
              <ChevronLeft className="size-4" />
            </Button>
            {position && (
              <span className="text-xs text-muted-foreground tabular-nums">
                {position.current}/{position.total}
              </span>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="下一篇文档"
              title="下一篇"
              disabled={!onNext}
              onClick={onNext}
            >
              <ChevronRight className="size-4" />
            </Button>
          </div>
        )}
      </div>

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[3fr_2fr]">
        <div className="min-h-0 overflow-hidden rounded-lg border border-border bg-card">
          <MarkdownPreview docId={docId} anchor={anchor} className="h-full" />
        </div>

        <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-card">
          <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-3 py-1.5 text-xs font-medium text-muted-foreground">
            <Boxes className="size-3.5" />
            切片(点击定位原文)
          </div>
          {!chunksMutable && (
            <div className="border-b border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              {snapshotsQuery.isPending
                ? "正在确认切片发布状态，编辑操作暂不可用。"
                : snapshotsQuery.isError
                  ? "无法确认切片发布状态，为避免修改快照投影，编辑操作已停用。"
                  : "切片由快照发布管理，仅供查看。需要修改时请上传新修订，完成审核后构建并发布新快照。"}
            </div>
          )}
          <div
            ref={chunkScrollRef}
            className="min-h-0 flex-1 overflow-y-auto p-2"
          >
            {chunks && !chunks.length && (
              <p className="py-8 text-center text-xs text-muted-foreground">
                {doc?.status === "uploaded" || doc?.status === "parsing"
                  ? "文档正在解析，切片尚未生成。"
                  : doc?.status === "failed" || doc?.status === "canceled"
                    ? "摄入未完成，当前没有可查看的切片。"
                    : "暂无已发布切片。摄入产物仍在 staging，完成审核并构建、激活新快照后会在此显示。"}
              </p>
            )}
            <div
              className="relative w-full"
              style={{ height: virtualizer.getTotalSize() }}
            >
              {items.map((item) => {
                const c = chunkList[item.index];
                return (
                  <div
                    key={c.id}
                    ref={virtualizer.measureElement}
                    data-index={item.index}
                    className="absolute top-0 left-0 w-full pb-2"
                    style={{ transform: `translateY(${item.start}px)` }}
                  >
                    <div
                      role="button"
                      tabIndex={0}
                      className="group cursor-pointer rounded-md border border-border p-2 transition-colors hover:border-primary/40 hover:bg-accent/40"
                      onClick={() =>
                        setAnchor({
                          startLine: c.start_line ?? undefined,
                          endLine: c.end_line ?? undefined,
                          headingPath:
                            c.start_line == null
                              ? (c.heading_path ?? undefined)
                              : undefined,
                        })
                      }
                      onKeyDown={(e) => {
                        if (e.key === "Enter") e.currentTarget.click();
                      }}
                    >
                      {c.heading_path && (
                        <div
                          className="mb-1 truncate text-[11px] text-primary"
                          title={c.heading_path}
                        >
                          {c.heading_path}
                        </div>
                      )}
                      <p className="line-clamp-3 text-xs whitespace-pre-wrap text-foreground/90">
                        {c.content}
                      </p>
                      <div className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                        <span className="font-mono">
                          #{c.chunk_index ?? "-"}
                        </span>
                        {c.start_line != null && (
                          <span className="font-mono">
                            L{c.start_line}
                            {c.end_line != null && c.end_line !== c.start_line
                              ? `-${c.end_line}`
                              : ""}
                          </span>
                        )}
                        {c.page != null && <span>页 {c.page}</span>}
                        <span
                          className={cn(
                            "size-1.5 rounded-full",
                            c.has_embedding
                              ? "bg-success"
                              : "bg-muted-foreground/40",
                          )}
                          title={c.has_embedding ? "已生成向量" : "暂无向量"}
                        />
                        <span className="sr-only">
                          {c.has_embedding ? "已生成向量" : "暂无向量"}
                        </span>
                        {chunksMutable && (
                          <span className="ml-auto flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                            <Button
                              variant="ghost"
                              size="icon"
                              className="size-6"
                              aria-label="编辑切片"
                              onClick={(e) => {
                                e.stopPropagation();
                                setEditing(c);
                                setDraft(c.content);
                              }}
                            >
                              <Pencil className="size-3.5" />
                            </Button>
                            {armedDelete === c.id ? (
                              <Button
                                variant="destructive"
                                size="sm"
                                className="h-6 animate-pulse px-2 text-[11px]"
                                disabled={deleteChunk.isPending}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  deleteChunk.mutate(c.id);
                                }}
                              >
                                确认删除
                              </Button>
                            ) : (
                              <Button
                                variant="ghost"
                                size="icon"
                                className="size-6 text-destructive"
                                aria-label="删除切片"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setArmedDelete(c.id);
                                }}
                              >
                                <Trash2 className="size-3.5" />
                              </Button>
                            )}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* 编辑弹窗:保存走 PATCH,后端同步重嵌 */}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="text-sm">
              编辑切片 #{editing?.chunk_index ?? "-"}
            </DialogTitle>
          </DialogHeader>
          {editing?.heading_path && (
            <p className="text-xs text-muted-foreground">
              {editing.heading_path}
            </p>
          )}
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="max-h-[50vh] min-h-48 font-mono text-xs"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setEditing(null)}>
              取消
            </Button>
            <Button
              disabled={!draft.trim() || patchChunk.isPending}
              onClick={() =>
                editing && patchChunk.mutate({ id: editing.id, content: draft })
              }
            >
              保存并重嵌
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
