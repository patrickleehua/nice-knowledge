"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Columns2, FileText, Loader2, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { EmptyState, ToneBadge } from "@/components/shared";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { KbLintReport, KbPage } from "@/lib/types";
import { findPageByTitle, kbPagesQueryKey, type WikiPage } from "./data";
import { WikiMarkdownView } from "./markdown-view";
import { extractWikilinks } from "./wikilink";

type ReviewAction = "publish" | "reject";

function MarkdownPane({
  label,
  content,
  emptyText,
  pages,
  onOpenPage,
  onCreatePage,
}: {
  label: string;
  content: string;
  emptyText: string;
  pages: KbPage[] | undefined;
  onOpenPage: (pageId: string) => void;
  onCreatePage: (title: string) => void;
}) {
  return (
    <section className="min-w-0 border-t border-border pt-3">
      <p className="mb-3 text-xs font-medium text-muted-foreground">{label}</p>
      {content.trim() ? (
        <WikiMarkdownView
          content={content}
          pages={pages}
          onOpenPage={onOpenPage}
          onCreatePage={onCreatePage}
        />
      ) : (
        <EmptyState title={emptyText} className="py-10" />
      )}
    </section>
  );
}

export function DraftReview({
  kbId,
  page,
  pages,
  onOpenPage,
  onCreatePage,
  onReviewed,
}: {
  kbId: string;
  page: WikiPage;
  pages: WikiPage[] | undefined;
  onOpenPage: (pageId: string) => void;
  onCreatePage: (title: string) => void;
  onReviewed: (page: WikiPage) => void;
}) {
  const queryClient = useQueryClient();
  const [confirmAction, setConfirmAction] = useState<ReviewAction | null>(null);
  const published = page.content ?? "";
  const suggested = page.draft_content ?? "";
  const lint = useQuery({
    queryKey: ["kb-lint", kbId],
    queryFn: () => api.get<KbLintReport>(`/kb/bases/${kbId}/lint`),
    staleTime: 30_000,
  });
  const missingDraftLinks = extractWikilinks(suggested).filter(
    (title) => !findPageByTitle(pages, title),
  );
  const blockingLintIssues = (lint.data?.issues ?? []).filter(
    (issue) =>
      issue.severity === "error" &&
      issue.page_id === page.id &&
      issue.type !== "broken_link",
  );
  const publishBlocked =
    lint.isPending ||
    lint.isError ||
    missingDraftLinks.length > 0 ||
    blockingLintIssues.length > 0;

  const requestPublish = () => {
    if (lint.isPending) {
      toast.info("正在执行发布前结构体检");
      return;
    }
    if (lint.isError) {
      toast.error(`发布前体检失败：${errMsg(lint.error)}`);
      return;
    }
    if (missingDraftLinks.length > 0) {
      toast.error(
        `草稿包含 ${missingDraftLinks.length} 个断链，请先创建或修正目标页面`,
      );
      return;
    }
    if (blockingLintIssues.length > 0) {
      toast.error(
        `当前页面仍有 ${blockingLintIssues.length} 个结构错误，修复后再发布`,
      );
      return;
    }
    setConfirmAction("publish");
  };

  const review = useMutation({
    mutationFn: (action: ReviewAction) =>
      api.post<WikiPage>(`/kb/pages/${page.id}/draft/${action}`),
    onSuccess: (updated, action) => {
      queryClient.setQueryData<WikiPage[]>(kbPagesQueryKey(kbId), (old) =>
        old?.map((item) => (item.id === updated.id ? updated : item)),
      );
      setConfirmAction(null);
      onReviewed(updated);
      toast.success(action === "publish" ? "草稿已发布" : "草稿已丢弃");
    },
    onError: (error) => toast.error(errMsg(error, "草稿处理失败")),
  });

  return (
    <div className="border-y border-border bg-muted/20 py-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 px-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <ToneBadge tone="warning" className="gap-1">
            <Sparkles className="size-3" />
            AI 草稿待审核
          </ToneBadge>
          <span className="text-xs text-muted-foreground">
            发布前不会覆盖当前正文
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            disabled={review.isPending}
            onClick={() => setConfirmAction("reject")}
          >
            <X className="size-3.5" />
            丢弃草稿
          </Button>
          <Button
            size="sm"
            disabled={review.isPending || lint.isPending}
            onClick={requestPublish}
          >
            {review.isPending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Check className="size-3.5" />
            )}
            发布草稿
          </Button>
        </div>
      </div>
      <div className="mb-3 px-3 text-xs text-muted-foreground">
        {lint.isPending
          ? "正在执行发布前结构体检…"
          : publishBlocked
            ? `发布门禁未通过：${missingDraftLinks.length} 个草稿断链，${blockingLintIssues.length} 个结构错误${lint.isError ? "，体检服务不可用" : ""}`
            : "发布前结构体检已通过"}
      </div>

      <Tabs defaultValue="compare" className="gap-3 px-3">
        <TabsList aria-label="Wiki 草稿审核视图">
          <TabsTrigger value="compare">
            <Columns2 className="size-3.5" />
            对照
          </TabsTrigger>
          <TabsTrigger value="published">
            <FileText className="size-3.5" />
            当前正文
          </TabsTrigger>
          <TabsTrigger value="suggested">
            <Sparkles className="size-3.5" />
            AI 草稿
          </TabsTrigger>
        </TabsList>
        <TabsContent value="compare">
          <div className="grid gap-5 md:grid-cols-2">
            <MarkdownPane
              label="当前正文"
              content={published}
              emptyText="尚无已发布正文"
              pages={pages}
              onOpenPage={onOpenPage}
              onCreatePage={onCreatePage}
            />
            <MarkdownPane
              label="AI 草稿"
              content={suggested}
              emptyText="草稿内容为空"
              pages={pages}
              onOpenPage={onOpenPage}
              onCreatePage={onCreatePage}
            />
          </div>
        </TabsContent>
        <TabsContent value="published">
          <MarkdownPane
            label="当前正文"
            content={published}
            emptyText="尚无已发布正文"
            pages={pages}
            onOpenPage={onOpenPage}
            onCreatePage={onCreatePage}
          />
        </TabsContent>
        <TabsContent value="suggested">
          <MarkdownPane
            label="AI 草稿"
            content={suggested}
            emptyText="草稿内容为空"
            pages={pages}
            onOpenPage={onOpenPage}
            onCreatePage={onCreatePage}
          />
        </TabsContent>
      </Tabs>

      <AlertDialog
        open={confirmAction !== null}
        onOpenChange={(open) => {
          if (!open && !review.isPending) setConfirmAction(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmAction === "publish"
                ? "发布 AI 草稿？"
                : "丢弃 AI 草稿？"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmAction === "publish"
                ? "草稿将替换当前正文；页面来源保持不变。"
                : "当前正文不会变化，待审核草稿将被永久清除。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={review.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              variant={confirmAction === "reject" ? "destructive" : "default"}
              disabled={!confirmAction || review.isPending}
              onClick={() => {
                if (confirmAction) review.mutate(confirmAction);
              }}
            >
              {review.isPending && <Loader2 className="size-4 animate-spin" />}
              {confirmAction === "publish" ? "确认发布" : "确认丢弃"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
