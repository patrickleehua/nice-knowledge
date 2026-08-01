"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArchiveRestore,
  CheckCircle2,
  CircleCheckBig,
  LoaderCircle,
  PackageOpen,
  PackagePlus,
  Rocket,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, ToneBadge, type Tone } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type { KnowledgeBase } from "@/lib/types";

type SnapshotStatus = "building" | "ready" | "active" | "retired" | "failed";

interface KnowledgeSnapshot {
  id: string;
  kb_id: string;
  revision_set_hash: string;
  embedding_fingerprint: {
    provider: string;
    model: string;
    dim: number;
  };
  config_fingerprint: string;
  revision_manifest: Record<string, unknown>[];
  config_manifest: Record<string, unknown>;
  build_stats: Record<string, unknown>;
  status: SnapshotStatus;
  ready_at: string | null;
  activated_at: string | null;
  retired_at: string | null;
  failed_at: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  rollback_capability: {
    allowed: boolean;
    code: string | null;
    message: string | null;
  };
}

const STATUS_META: Record<
  SnapshotStatus,
  { label: string; tone: Tone; icon: typeof LoaderCircle }
> = {
  building: { label: "构建中", tone: "primary", icon: LoaderCircle },
  ready: { label: "待发布", tone: "primary", icon: CheckCircle2 },
  active: { label: "生效中", tone: "primary", icon: Rocket },
  retired: { label: "已退役", tone: "muted", icon: ArchiveRestore },
  failed: { label: "构建失败", tone: "destructive", icon: TriangleAlert },
};

/** 历史默认只展开最近几个,其余折叠 */
const HISTORY_PREVIEW = 5;

type ReleasePhase = "idle" | "building" | "ready" | "active" | "failed";

function formatTimestamp(value: string | null) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function statNumber(snapshot: KnowledgeSnapshot, key: string) {
  const value = snapshot.build_stats[key];
  return typeof value === "number" ? value : null;
}

function shortId(value: string) {
  return value.slice(0, 8);
}

function BuildActivity({ count }: { count: number }) {
  return (
    <div
      aria-live="polite"
      className="border-t border-border bg-primary/[0.025] px-5 py-4 sm:px-6"
    >
      <div className="flex items-center gap-3">
        <span className="relative flex size-9 shrink-0 items-center justify-center">
          <span className="absolute inset-1 animate-ping rounded-full bg-primary/15 motion-reduce:hidden" />
          <span className="relative flex size-8 items-center justify-center rounded-full bg-primary/10 text-primary">
            <LoaderCircle className="size-4 animate-spin motion-reduce:animate-none" />
          </span>
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium">
              {count > 1 ? `${count} 个版本正在构建` : "正在构建知识版本"}
            </p>
            <span className="shrink-0 text-xs text-muted-foreground">
              状态自动刷新
            </span>
          </div>
          <div
            role="progressbar"
            aria-label="版本构建进度"
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-primary/10"
          >
            <span className="release-build-progress block h-full w-1/3 rounded-full bg-primary" />
          </div>
        </div>
      </div>
    </div>
  );
}

export function SnapshotReleaseCard({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const [showAllHistory, setShowAllHistory] = useState(false);

  const { data: bases } = useQuery({
    queryKey: ["kb-bases"],
    queryFn: () => api.get<KnowledgeBase[]>("/kb/bases"),
  });
  const kb = bases?.find((base) => base.id === kbId);

  const snapshotsQuery = useQuery({
    queryKey: ["kb-snapshots", kbId],
    queryFn: () => api.get<KnowledgeSnapshot[]>(`/kb/bases/${kbId}/snapshots`),
    refetchInterval: (query) =>
      query.state.data?.some((snapshot) => snapshot.status === "building")
        ? 3000
        : false,
  });
  const snapshots = snapshotsQuery.data ?? [];

  const refreshReleaseState = async () => {
    await queryClient.invalidateQueries({
      predicate: ({ queryKey }) =>
        typeof queryKey[0] === "string" && queryKey[0].startsWith("kb-"),
    });
  };

  const build = useMutation({
    mutationFn: () =>
      api.post<KnowledgeSnapshot>(`/kb/bases/${kbId}/snapshots`, {
        reason: "manual snapshot build via knowledge base release view",
      }),
    onSuccess: async (snapshot) => {
      await refreshReleaseState();
      if (snapshot.status === "failed") {
        toast.error(snapshot.error ?? "快照构建失败");
      } else if (snapshot.status === "ready") {
        toast.success("快照已就绪，等待激活");
      } else if (snapshot.status === "active") {
        toast.info("当前内容已由活动快照发布");
      } else if (snapshot.status === "retired") {
        toast.info("内容与历史快照一致，可直接回滚");
      } else {
        toast.success("快照构建已开始");
      }
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const activate = useMutation({
    mutationFn: (snapshot: KnowledgeSnapshot) =>
      api.post<KnowledgeSnapshot>(
        `/kb/bases/${kbId}/snapshots/${snapshot.id}/activate`,
        { reason: "activated via knowledge base release view" },
      ),
    onSuccess: async (snapshot) => {
      await refreshReleaseState();
      toast.success(`快照 ${shortId(snapshot.id)} 已激活`);
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  const rollback = useMutation({
    mutationFn: (snapshot: KnowledgeSnapshot) =>
      api.post<KnowledgeSnapshot>(
        `/kb/bases/${kbId}/snapshots/${snapshot.id}/rollback`,
        { reason: "rolled back via knowledge base release view" },
      ),
    onSuccess: async (snapshot) => {
      await refreshReleaseState();
      toast.success(`已回滚到快照 ${shortId(snapshot.id)}`);
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.code) {
        await refreshReleaseState();
        toast.error(error.message);
        return;
      }
      toast.error(errMsg(error));
    },
  });

  const pending = build.isPending || activate.isPending || rollback.isPending;
  const building = snapshots.filter((s) => s.status === "building");
  const ready = snapshots.filter((s) => s.status === "ready");
  const retired = snapshots.filter((s) => s.status === "retired");
  const failed = snapshots.filter((s) => s.status === "failed");
  const activeSnapshot =
    snapshots.find((s) => s.id === kb?.active_snapshot_id) ??
    snapshots.find((s) => s.status === "active");
  // 最新的就绪快照即「下一个可上线的版本」
  const nextReady = ready[0];
  const latestSnapshot = snapshots[0];

  const phase: ReleasePhase = nextReady
    ? "ready"
    : building.length > 0
      ? "building"
      : latestSnapshot?.status === "failed"
        ? "failed"
        : activeSnapshot
          ? "active"
          : "idle";

  const summary = {
    idle: {
      label: "尚未发布",
      title: "发布首个知识版本",
      description: "构建并确认发布后，知识即可被业务使用。",
      tone: "primary" as const,
      icon: PackagePlus,
    },
    building: {
      label: "构建中",
      title: "正在生成新版本",
      description: "完成后会自动进入待发布状态。",
      tone: "primary" as const,
      icon: LoaderCircle,
    },
    ready: {
      label: "待发布",
      title: "新版本已准备就绪",
      description: "确认后将立即切换为线上版本。",
      tone: "primary" as const,
      icon: CircleCheckBig,
    },
    active: {
      label: "已发布",
      title: "知识库已发布",
      description: "当前版本正在供检索与下游消费使用。",
      tone: "primary" as const,
      icon: Rocket,
    },
    failed: {
      label: "构建异常",
      title: "新版本构建失败",
      description: activeSnapshot
        ? "线上版本未受影响，可查看失败原因后重试。"
        : "查看失败原因后重新构建。",
      tone: "destructive" as const,
      icon: TriangleAlert,
    },
  }[phase];
  const SummaryIcon = summary.icon;

  const visibleHistory = showAllHistory
    ? snapshots
    : snapshots.slice(0, HISTORY_PREVIEW);

  if (snapshotsQuery.isPending) {
    return (
      <Card className="gap-0 py-0">
        <div
          aria-live="polite"
          className="flex min-h-44 items-center justify-center gap-2 text-sm text-muted-foreground"
        >
          <LoaderCircle className="size-4 animate-spin motion-reduce:animate-none" />
          正在读取发布状态
        </div>
      </Card>
    );
  }

  if (snapshotsQuery.isError) {
    return (
      <Card className="gap-0 py-0">
        <div className="flex min-h-44 flex-col items-center justify-center gap-4 px-5 py-8 text-center">
          <span className="flex size-10 items-center justify-center rounded-full bg-destructive/10 text-destructive">
            <TriangleAlert className="size-5" />
          </span>
          <div>
            <h3 className="font-medium">发布状态加载失败</h3>
            <p className="mt-1 max-w-lg text-sm text-muted-foreground">
              {errMsg(snapshotsQuery.error)}
            </p>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => snapshotsQuery.refetch()}
          >
            重新加载
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <Card className="gap-0 py-0">
      <section className="grid gap-5 px-5 py-5 sm:px-6 sm:py-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
        <div className="flex min-w-0 gap-3.5 sm:gap-4">
          <span
            className={cn(
              "flex size-11 shrink-0 items-center justify-center rounded-xl",
              summary.tone === "primary" && "bg-primary/10 text-primary",
              summary.tone === "destructive" &&
                "bg-destructive/10 text-destructive",
            )}
          >
            <SummaryIcon
              className={cn(
                "size-5",
                phase === "building" &&
                  "animate-spin motion-reduce:animate-none",
              )}
            />
          </span>

          <div className="min-w-0">
            <ToneBadge tone={summary.tone}>{summary.label}</ToneBadge>
            <h3 className="mt-2 text-lg font-semibold tracking-tight">
              {summary.title}
            </h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {summary.description}
            </p>

            {(activeSnapshot || nextReady || retired.length > 0) && (
              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                {activeSnapshot && (
                  <span className="flex items-center gap-1.5">
                    <CircleCheckBig className="size-3.5 text-primary" />
                    线上版本
                    <span className="font-mono text-foreground">
                      {shortId(activeSnapshot.id)}
                    </span>
                  </span>
                )}
                {nextReady && (
                  <span className="flex items-center gap-1.5">
                    <CheckCircle2 className="size-3.5 text-primary" />
                    待发布
                    <span className="font-mono text-foreground">
                      {shortId(nextReady.id)}
                    </span>
                  </span>
                )}
                {retired.length > 0 && <span>{retired.length} 个历史版本</span>}
              </div>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center pl-14 sm:pl-15 lg:pl-0">
          {nextReady ? (
            <ConfirmDialog
              trigger={
                <Button size="lg" disabled={pending}>
                  <Rocket />
                  发布此版本
                </Button>
              }
              title="发布这个知识版本?"
              description={`版本 ${shortId(nextReady.id)} 将立即供检索与下游消费使用。`}
              confirmLabel="确认发布"
              onConfirm={() => activate.mutateAsync(nextReady)}
            />
          ) : phase !== "building" ? (
            <Button
              size="lg"
              disabled={pending || building.length > 0}
              onClick={() => build.mutate()}
            >
              {build.isPending ? (
                <LoaderCircle className="animate-spin motion-reduce:animate-none" />
              ) : (
                <PackagePlus />
              )}
              {build.isPending
                ? "正在构建"
                : phase === "active"
                  ? "构建新版本"
                  : phase === "failed"
                    ? "重新构建"
                    : "构建首个版本"}
            </Button>
          ) : null}
        </div>
      </section>

      {phase === "building" && <BuildActivity count={building.length} />}

      <section className="border-t border-border">
        <div className="flex min-h-12 items-center justify-between gap-3 px-5 sm:px-6">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium">版本记录</h3>
            <span className="text-xs tabular-nums text-muted-foreground">
              {snapshots.length}
            </span>
            {failed.length > 0 && (
              <ToneBadge tone="destructive">{failed.length} 次失败</ToneBadge>
            )}
          </div>
          {snapshots.length > HISTORY_PREVIEW && (
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setShowAllHistory((current) => !current)}
            >
              {showAllHistory ? "收起" : `查看全部 ${snapshots.length} 个`}
            </Button>
          )}
        </div>

        {snapshots.length === 0 ? (
          <div className="flex items-center justify-center gap-2 border-t border-border px-5 py-10 text-sm text-muted-foreground">
            <PackageOpen className="size-4" />
            尚无版本记录
          </div>
        ) : (
          <ul className="divide-y divide-border border-t border-border">
            {visibleHistory.map((snapshot) => {
              const isActivePointer =
                activeSnapshot?.id === snapshot.id ||
                kb?.active_snapshot_id === snapshot.id;
              const status = isActivePointer ? "active" : snapshot.status;
              const meta = STATUS_META[status];
              const StatusIcon = meta.icon;
              const revisionCount =
                statNumber(snapshot, "revision_count") ??
                snapshot.revision_manifest.length;
              const claimCount = statNumber(snapshot, "confirmed_claim_count");
              const evidenceCount = statNumber(snapshot, "evidence_count");
              const technicalDetail = `${snapshot.embedding_fingerprint.provider}/${snapshot.embedding_fingerprint.model} · ${snapshot.embedding_fingerprint.dim}d · rev ${snapshot.revision_set_hash.slice(0, 10)}`;

              return (
                <li
                  key={snapshot.id}
                  className={cn(
                    "grid gap-3 px-5 py-3.5 transition-colors hover:bg-muted/30 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-6",
                    isActivePointer && "bg-primary/[0.025]",
                  )}
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <ToneBadge tone={meta.tone}>
                        <StatusIcon
                          className={cn(
                            "size-3",
                            status === "building" &&
                              "animate-spin motion-reduce:animate-none",
                          )}
                        />
                        {meta.label}
                      </ToneBadge>
                      <span
                        className="font-mono text-xs text-foreground"
                        title={technicalDetail}
                      >
                        {shortId(snapshot.id)}
                      </span>
                      <time
                        dateTime={snapshot.created_at ?? undefined}
                        className="text-xs text-muted-foreground"
                      >
                        {formatTimestamp(snapshot.created_at)}
                      </time>
                    </div>
                    <p className="mt-1.5 truncate text-xs text-muted-foreground">
                      {revisionCount} 份修订
                      {claimCount !== null
                        ? ` · ${claimCount} 条已确认事实`
                        : ""}
                      {evidenceCount !== null
                        ? ` · ${evidenceCount} 条证据`
                        : ""}
                    </p>
                    {snapshot.error && (
                      <p className="mt-1.5 line-clamp-2 text-xs text-destructive">
                        {snapshot.error}
                      </p>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    {status === "ready" && (
                      <ConfirmDialog
                        trigger={
                          <Button size="sm" disabled={pending}>
                            <Rocket />
                            发布
                          </Button>
                        }
                        title="发布这个知识版本?"
                        description={`版本 ${shortId(snapshot.id)} 将立即供检索与下游消费使用。`}
                        confirmLabel="确认发布"
                        onConfirm={() => activate.mutateAsync(snapshot)}
                      />
                    )}
                    {status === "retired" &&
                      snapshot.rollback_capability.allowed && (
                        <ConfirmDialog
                          trigger={
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={pending}
                            >
                              <ArchiveRestore />
                              回滚
                            </Button>
                          }
                          title="回滚到这个知识版本?"
                          description={`线上版本将切换到 ${shortId(snapshot.id)}，当前版本会进入历史记录。已撤回资料不会被恢复。`}
                          confirmLabel="确认回滚"
                          destructive
                          onConfirm={() => rollback.mutateAsync(snapshot)}
                        />
                      )}
                    {status === "retired" &&
                      !snapshot.rollback_capability.allowed &&
                      snapshot.rollback_capability.message && (
                        <p className="flex max-w-sm items-start gap-1.5 text-xs leading-5 text-muted-foreground">
                          <ShieldAlert className="mt-0.5 size-3.5 shrink-0" />
                          <span>{snapshot.rollback_capability.message}</span>
                        </p>
                      )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </Card>
  );
}
