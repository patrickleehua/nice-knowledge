"use client";

// 知识库管理页:标题/新建 → 生命周期分段筛选(活动/已归档) → 卡片网格。
// 卡片悬浮浮现快捷操作(资料/设置);生命周期能力内敛为横幅与角标(到期提醒/恢复),
// 清理执行入口在各库危险操作区。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Coins,
  Database,
  FileText,
  FolderOpen,
  Hotel,
  Layers,
  MapPin,
  Plus,
  RotateCcw,
  Route,
  Settings,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { LayerBadge } from "@/components/kb/badges";
import {
  formatMonthDay,
  PurgeDueBanner,
  WorkerDisabledBanner,
} from "@/components/kb/lifecycle-console";
import {
  KbRuntimePanel,
  KbRuntimeSummary,
  type KbStatusFilter,
  matchesKbStatusFilter,
} from "@/components/kb/status-board";
import {
  EmptyState,
  FormField,
  PageHeader,
  Spinner,
  ToneBadge,
} from "@/components/shared";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import { type AuthOrg, useCurrentOrg } from "@/lib/auth";
import {
  KB_STATUS_BOARD_QUERY_KEY,
  kbStatusBoardApi,
  kbStatusBoardRefetchInterval,
  type KbStatusBoardItem,
} from "@/lib/kb-status-board";
import {
  kbLifecycleApi,
  kbLifecycleErrorMessage,
  type KnowledgeBaseBoardItem,
} from "@/lib/kb-lifecycle";
import { KB_TYPE_PRESETS, type KnowledgeBase } from "@/lib/types";
import { cn } from "@/lib/utils";

const KB_TYPE_ITEMS: Record<string, string> = {
  ...Object.fromEntries(KB_TYPE_PRESETS.map((t) => [t.value, t.label])),
  __custom__: "自定义类型…",
};

/** 类型 → 图标与配色(卡片头像用;色相区分类型,暗色模式各给显式变体) */
const KB_TYPE_VISUALS: Record<string, { icon: LucideIcon; className: string }> =
  {
    destination: {
      icon: MapPin,
      className: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
    },
    hotel: {
      icon: Hotel,
      className: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
    },
    cost: {
      icon: Coins,
      className: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    },
    route: {
      icon: Route,
      className: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    },
    document: {
      icon: FileText,
      className: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
    },
    mixed: {
      icon: Layers,
      className: "bg-primary/10 text-primary",
    },
  };

const FALLBACK_TYPE_VISUAL = {
  icon: Database,
  className: "bg-muted text-muted-foreground",
};

function kbTypeLabel(value: string): string {
  return KB_TYPE_PRESETS.find((t) => t.value === value)?.label ?? value;
}

// org 由调用方从 useCurrentOrg 传入:直接读 localStorage 会在 SSR 期抛 ReferenceError
function kbLayer(kb: KnowledgeBase, org: AuthOrg | null): string {
  if (org && kb.org_id === org.id) return "tenant";
  // 平台 org 的库对租户显示为平台层,其余为共享库
  return kb.org_id === "00000000-0000-0000-0000-000000000001"
    ? "platform"
    : "shared";
}

export default function KbListPage() {
  const queryClient = useQueryClient();
  const currentOrg = useCurrentOrg();
  const canManageLifecycle =
    currentOrg?.role === "org_admin" || currentOrg?.role === "platform_admin";

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [kbType, setKbType] = useState("mixed");
  const [customType, setCustomType] = useState("");
  const [description, setDescription] = useState("");
  const [lifecycleFilter, setLifecycleFilter] = useState<"active" | "archived">(
    "active",
  );
  const [statusFilter, setStatusFilter] = useState<KbStatusFilter>("all");

  const { data: bases, isLoading } = useQuery({
    queryKey: ["kb-bases", lifecycleFilter],
    queryFn: () => kbLifecycleApi.list(lifecycleFilter),
  });
  const visibleBases = bases?.filter(
    (base) => base.lifecycle_status !== "purged",
  );

  const statusBoardQuery = useQuery({
    queryKey: KB_STATUS_BOARD_QUERY_KEY,
    queryFn: kbStatusBoardApi.active,
    enabled: lifecycleFilter === "active",
    staleTime: 0,
    refetchInterval: kbStatusBoardRefetchInterval,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  const statusBoard = statusBoardQuery.data;
  const statusByKbId = new Map(
    (statusBoard?.items ?? []).map((item) => [item.kb_id, item]),
  );
  // 只筛选原列表，不按轮询状态重排，避免状态变化时卡片跳位。
  const displayedBases =
    lifecycleFilter === "active" && statusFilter !== "all"
      ? visibleBases?.filter((base) => {
          const status = statusByKbId.get(base.id);
          return status ? matchesKbStatusFilter(status, statusFilter) : false;
        })
      : visibleBases;
  const statusMayBeStale =
    statusBoardQuery.isError && statusBoardQuery.data !== undefined;

  // 生命周期看板:给卡片补保留期角标、驱动到期横幅与 worker 警示(仅管理员可调)
  const { data: board } = useQuery({
    queryKey: ["kb-lifecycle-board"],
    queryFn: () => kbLifecycleApi.board(),
    enabled: canManageLifecycle,
  });
  const boardByKbId = new Map(
    (board?.items ?? []).map((item) => [item.kb_id, item]),
  );
  const purgeDueItems = (board?.items ?? []).filter((item) => item.purge_due);
  const workerDisabled = board?.purge_worker_enabled === false;
  // worker 关闭时仍在排队的库清理数(取自看板 latest_operation,不额外发请求)
  const pendingPurges = (board?.items ?? []).filter(
    (item) =>
      item.latest_operation?.status === "pending" ||
      item.latest_operation?.status === "processing",
  ).length;

  const createMutation = useMutation({
    mutationFn: () =>
      api.post<KnowledgeBase>("/kb/bases", {
        name,
        kb_type: kbType === "__custom__" ? customType : kbType,
        description: description || null,
      }),
    onSuccess: () => {
      toast.success("知识库已创建");
      setOpen(false);
      setName("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["kb-bases"] });
      queryClient.invalidateQueries({ queryKey: KB_STATUS_BOARD_QUERY_KEY });
    },
    onError: (err) =>
      toast.error(err instanceof ApiError ? err.message : "创建失败"),
  });
  const restoreMutation = useMutation({
    mutationFn: (kbId: string) => kbLifecycleApi.restore(kbId),
    onSuccess: () => {
      toast.success("知识库已恢复；原分享和旅行计划关联不会自动恢复");
      queryClient.invalidateQueries({ queryKey: ["kb-bases"] });
      queryClient.invalidateQueries({ queryKey: KB_STATUS_BOARD_QUERY_KEY });
    },
    onError: (error) => toast.error(kbLifecycleErrorMessage(error)),
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="知识库"
        description="按库隔离维护知识资产,可分享给其他组织;类型可自定义"
        actions={
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button />}>
              <Plus className="size-4" />
              新建知识库
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建知识库</DialogTitle>
              </DialogHeader>
              <div className="space-y-4">
                <FormField label="名称" htmlFor="kb-name" required>
                  <Input
                    id="kb-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="如:欧洲目的地基础库"
                  />
                </FormField>
                <FormField label="类型" htmlFor="kb-type">
                  <Select
                    items={KB_TYPE_ITEMS}
                    value={kbType}
                    onValueChange={(v) => setKbType(v as string)}
                  >
                    <SelectTrigger id="kb-type" className="h-9 w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {KB_TYPE_PRESETS.map((t) => (
                        <SelectItem key={t.value} value={t.value}>
                          {t.label}
                        </SelectItem>
                      ))}
                      <SelectItem value="__custom__">自定义类型…</SelectItem>
                    </SelectContent>
                  </Select>
                  {kbType === "__custom__" && (
                    <Input
                      value={customType}
                      onChange={(e) => setCustomType(e.target.value)}
                      placeholder="输入类型标识,如 visa / faq"
                    />
                  )}
                </FormField>
                <FormField label="描述(可选)" htmlFor="kb-description">
                  <Textarea
                    id="kb-description"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    rows={3}
                  />
                </FormField>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setOpen(false)}>
                  取消
                </Button>
                <Button
                  disabled={!name.trim() || createMutation.isPending}
                  onClick={() => createMutation.mutate()}
                >
                  {createMutation.isPending && <Spinner size={3.5} />}
                  创建
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        }
      />

      {canManageLifecycle && workerDisabled && (
        <WorkerDisabledBanner pendingPurges={pendingPurges} />
      )}
      {canManageLifecycle && <PurgeDueBanner items={purgeDueItems} />}

      {/* 生命周期分段筛选:从页头下移到内容区,活动/已归档一目了然 */}
      <div
        role="group"
        aria-label="知识库生命周期筛选"
        className="inline-flex rounded-lg border border-border bg-muted/40 p-0.5"
      >
        {(
          [
            { value: "active", label: "活动知识库" },
            { value: "archived", label: "已归档" },
          ] as const
        ).map((item) => (
          <button
            key={item.value}
            type="button"
            aria-pressed={lifecycleFilter === item.value}
            onClick={() => setLifecycleFilter(item.value)}
            className={cn(
              "rounded-md px-3 py-1 text-sm transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              lifecycleFilter === item.value
                ? "bg-background font-medium text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      {lifecycleFilter === "active" && (
        <KbRuntimeSummary
          board={statusBoard}
          filter={statusFilter}
          onFilterChange={setStatusFilter}
          stale={statusMayBeStale}
          loading={statusBoardQuery.isPending}
          error={statusBoardQuery.isError}
        />
      )}

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-40 w-full rounded-xl" />
          ))}
        </div>
      ) : !visibleBases?.length ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={Database}
              title={
                lifecycleFilter === "archived"
                  ? "没有已归档知识库"
                  : "还没有知识库"
              }
              description={
                lifecycleFilter === "archived"
                  ? "归档后的知识库会出现在这里，可由所有者管理员恢复。"
                  : "先建一个,再上传资料开始清洗。"
              }
            />
          </CardContent>
        </Card>
      ) : !displayedBases?.length ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={Database}
              title="没有符合当前状态的知识库"
              description="状态更新后结果会自动刷新，也可以查看全部活动知识库。"
              action={
                <Button
                  variant="outline"
                  onClick={() => setStatusFilter("all")}
                >
                  查看全部
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {displayedBases.map((kb) => (
            <KbCard
              key={kb.id}
              kb={kb}
              currentOrg={currentOrg}
              canManageLifecycle={canManageLifecycle}
              lifecycle={boardByKbId.get(kb.id)}
              forceArchivedView={lifecycleFilter === "archived"}
              runtime={statusByKbId.get(kb.id)}
              runtimeStale={statusMayBeStale}
              runtimeLoading={statusBoardQuery.isPending}
              restoring={
                restoreMutation.isPending && restoreMutation.variables === kb.id
              }
              onRestore={() => restoreMutation.mutate(kb.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function KbCard({
  kb,
  currentOrg,
  canManageLifecycle,
  lifecycle,
  forceArchivedView,
  runtime,
  runtimeStale,
  runtimeLoading,
  restoring,
  onRestore,
}: {
  kb: KnowledgeBase;
  currentOrg: AuthOrg | null;
  canManageLifecycle: boolean;
  lifecycle?: KnowledgeBaseBoardItem;
  forceArchivedView: boolean;
  runtime?: KbStatusBoardItem;
  runtimeStale: boolean;
  runtimeLoading: boolean;
  restoring: boolean;
  onRestore: () => void;
}) {
  const archived = forceArchivedView || kb.lifecycle_status === "archived";
  const own = currentOrg?.id === kb.org_id;
  const visual = KB_TYPE_VISUALS[kb.kb_type] ?? FALLBACK_TYPE_VISUAL;
  const Icon = visual.icon;
  return (
    <Card
      data-testid="kb-card"
      className="group h-full py-0 transition-all hover:-translate-y-0.5 hover:border-ring/40 hover:shadow-lg"
    >
      <CardContent className="flex h-full flex-col gap-3 p-5">
        <Link
          href={
            archived
              ? `/org/kb/${kb.id}?view=settings&tab=danger`
              : `/org/kb/${kb.id}`
          }
          className="flex min-w-0 items-start gap-3"
        >
          <span
            className={cn(
              "flex size-10 shrink-0 items-center justify-center rounded-lg",
              visual.className,
            )}
          >
            <Icon className="size-5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate font-semibold group-hover:text-primary">
              {kb.name}
            </span>
            <span className="text-xs text-muted-foreground">
              {kbTypeLabel(kb.kb_type)}
            </span>
          </span>
        </Link>
        <p className="line-clamp-2 flex-1 text-sm text-muted-foreground">
          {kb.description || "暂无描述"}
        </p>
        {!archived && (
          <KbRuntimePanel
            kbId={kb.id}
            kbName={kb.name}
            item={runtime}
            stale={runtimeStale}
            loading={runtimeLoading}
          />
        )}
        <div className="flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-3">
          <LayerBadge layer={kbLayer(kb, currentOrg)} />
          {archived && <ToneBadge tone="warning">已归档</ToneBadge>}
          {/* 归档库带保留期角标:到期红色提示可清理,未到期给出期限 */}
          {archived && own && lifecycle?.purge_due && (
            <ToneBadge tone="destructive">保留期已到期</ToneBadge>
          )}
          {archived &&
            own &&
            !lifecycle?.purge_due &&
            lifecycle?.retention_due_at && (
              <ToneBadge tone="muted">
                {formatMonthDay(lifecycle.retention_due_at)} 到期
              </ToneBadge>
            )}
          {archived && canManageLifecycle && own ? (
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={restoring}
              onClick={onRestore}
            >
              {restoring ? <Spinner size={3.5} /> : <RotateCcw />}
              恢复知识库
            </Button>
          ) : (
            // 悬浮快捷操作:hover/键盘聚焦时浮现,直达资料上传与设置
            <span
              className={cn(
                "ml-auto flex items-center gap-0.5 transition-opacity",
                "sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100",
              )}
            >
              {/* 直接用 Link + buttonVariants:Button 的 render prop 会丢掉 href */}
              <Link
                href={`/org/kb/${kb.id}?view=sources`}
                aria-label={`${kb.name} 资料`}
                title="上传/管理资料"
                className={buttonVariants({
                  variant: "ghost",
                  size: "icon-sm",
                })}
              >
                <FolderOpen className="size-4" />
              </Link>
              <Link
                href={`/org/kb/${kb.id}?view=settings`}
                aria-label={`${kb.name} 设置`}
                title="库设置"
                className={buttonVariants({
                  variant: "ghost",
                  size: "icon-sm",
                })}
              >
                <Settings className="size-4" />
              </Link>
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
