"use client";

// wiki 结构体检面板(KB-5C):右侧抽屉,调 GET /kb/bases/{kb_id}/lint。
// issues 按 severity 分组(error/warning/info),每条:类型 chip + 页面名可点跳转
// + message + suggestions chips(按标题解析,存在的页可点跳转);顶部 stats 摘要。
// 后端并行线未合并时接口 404 → 如实显示「体检服务待上线」空态,不 mock 假数据。

import { useQuery } from "@tanstack/react-query";
import {
  CircleAlert,
  Info,
  Loader2,
  RotateCcw,
  Stethoscope,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { EmptyState, ToneBadge, type Tone } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { KbLintIssue, KbLintReport, KbPage } from "@/lib/types";
import { findPageByTitle } from "./data";

const SEVERITY_META: Record<
  string,
  { label: string; icon: LucideIcon; tone: Tone; iconClass: string }
> = {
  error: {
    label: "错误",
    icon: CircleAlert,
    tone: "destructive",
    iconClass: "text-destructive",
  },
  warning: {
    label: "警告",
    icon: TriangleAlert,
    tone: "warning",
    iconClass: "text-warning",
  },
  info: { label: "提示", icon: Info, tone: "info", iconClass: "text-primary" },
};

const SEVERITY_ORDER = ["error", "warning", "info"];

/** stats 键的中文映射(未知键原样展示,契约开放) */
const STAT_LABELS: Record<string, string> = {
  pages: "页面",
  issues: "问题",
  errors: "错误",
  warnings: "警告",
  infos: "提示",
  orphans: "孤立页",
  broken_links: "断链",
};

function groupBySeverity(issues: KbLintIssue[]): [string, KbLintIssue[]][] {
  const groups = new Map<string, KbLintIssue[]>();
  for (const issue of issues) {
    groups.set(issue.severity, [...(groups.get(issue.severity) ?? []), issue]);
  }
  return [...groups.entries()].sort(
    ([a], [b]) =>
      (SEVERITY_ORDER.indexOf(a) + 1 || 99) -
      (SEVERITY_ORDER.indexOf(b) + 1 || 99),
  );
}

export function LintPanel({
  kbId,
  open,
  pages,
  onOpenChange,
  onOpenPage,
}: {
  kbId: string;
  open: boolean;
  /** 当前库页列表,用于把 suggestions 标题解析成可跳转的页 */
  pages: KbPage[] | undefined;
  onOpenChange: (open: boolean) => void;
  onOpenPage: (pageId: string, options?: { edit?: boolean }) => void;
}) {
  const { data, error, isPending, isFetching, refetch } = useQuery({
    queryKey: ["kb-lint", kbId],
    queryFn: () => api.get<KbLintReport>(`/kb/bases/${kbId}/lint`),
    enabled: open,
    // 每次主动打开都取最新结果:修完一条再打开时应该看到它已经消失,而不是缓存旧报告
    staleTime: 0,
    refetchOnMount: "always",
    // 404 = 后端体检服务未上线,是明确业务态,不重试
    retry: (count, err) =>
      !(err instanceof ApiError && err.status === 404) && count < 2,
  });

  const notReady = error instanceof ApiError && error.status === 404;

  /** 问题条目跳转 = 去修,直接进编辑态;建议词条跳转 = 去看,保持只读 */
  const jumpTo = (pageId: string, edit: boolean) => {
    onOpenPage(pageId, { edit });
    onOpenChange(false);
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="gap-0 data-[side=right]:sm:max-w-md"
        aria-describedby={undefined}
      >
        <SheetHeader className="border-b border-border">
          <SheetTitle className="flex items-center gap-2 text-sm">
            <Stethoscope className="size-4 text-primary" />
            wiki 结构体检
            <Button
              variant="ghost"
              size="icon"
              className="size-6"
              aria-label="重新体检"
              disabled={isFetching}
              onClick={() => refetch()}
            >
              <RotateCcw className="size-3.5" />
            </Button>
          </SheetTitle>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {isPending ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="size-5 animate-spin text-primary" />
            </div>
          ) : notReady ? (
            <EmptyState
              icon={Stethoscope}
              title="体检服务待上线"
              description="结构 lint 接口尚未部署,后端合并后即可在这里查看孤立页、断链等结构问题。"
            />
          ) : error || !data ? (
            <EmptyState
              icon={CircleAlert}
              title="体检失败"
              description={errMsg(error)}
              action={
                <Button variant="outline" size="sm" onClick={() => refetch()}>
                  重试
                </Button>
              }
            />
          ) : (
            <div className="space-y-4">
              {/* stats 摘要 */}
              {Object.keys(data.stats).length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(data.stats).map(([key, value]) => (
                    <span
                      key={key}
                      className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground"
                    >
                      {STAT_LABELS[key] ?? key}
                      <span className="font-mono font-medium text-foreground tabular-nums">
                        {value}
                      </span>
                    </span>
                  ))}
                </div>
              )}

              {data.issues.length === 0 ? (
                <EmptyState
                  icon={Info}
                  title="没有发现结构问题"
                  description="wiki 页结构健康,继续保持。"
                />
              ) : (
                groupBySeverity(data.issues).map(([severity, issues]) => {
                  const meta = SEVERITY_META[severity] ?? SEVERITY_META.info;
                  const SeverityIcon = meta.icon;
                  return (
                    <section key={severity} className="space-y-2">
                      <h3 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                        <SeverityIcon
                          className={`size-3.5 ${meta.iconClass}`}
                        />
                        {meta.label}
                        <span className="tabular-nums">{issues.length}</span>
                      </h3>
                      {issues.map((issue, i) => (
                        <div
                          key={`${issue.page_id}-${issue.type}-${i}`}
                          className="space-y-1.5 rounded-md border border-border p-2.5"
                        >
                          <div className="flex flex-wrap items-center gap-1.5">
                            <ToneBadge tone={meta.tone} className="font-mono">
                              {issue.type}
                            </ToneBadge>
                            <button
                              type="button"
                              className="max-w-full cursor-pointer truncate text-xs font-medium underline-offset-2 hover:text-primary hover:underline"
                              title={`${issue.page_title}(点击打开并进入编辑)`}
                              onClick={() => jumpTo(issue.page_id, true)}
                            >
                              {issue.page_title}
                            </button>
                          </div>
                          <p className="text-xs text-muted-foreground">
                            {issue.message}
                          </p>
                          {issue.suggestions.length > 0 && (
                            <div className="flex flex-wrap gap-1">
                              {issue.suggestions.map((s) => {
                                const target = findPageByTitle(pages, s);
                                return target ? (
                                  <button
                                    key={s}
                                    type="button"
                                    className="cursor-pointer rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary transition-colors hover:bg-primary/20"
                                    onClick={() => jumpTo(target.id, false)}
                                  >
                                    {s}
                                  </button>
                                ) : (
                                  <span
                                    key={s}
                                    className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                                  >
                                    {s}
                                  </span>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      ))}
                    </section>
                  );
                })
              )}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
