"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  LoaderCircle,
  RotateCcw,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { LifecycleFlow } from "@/components/kb/lifecycle-flow";
import { ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useCurrentOrg } from "@/lib/auth";
import {
  FORCE_BYPASSABLE_BLOCKER_CODES,
  isDeletionPlanStale,
  kbLifecycleApi,
  kbLifecycleErrorMessage,
  type KnowledgeBaseDeletionAction,
  type KnowledgeBaseDeletionPreview,
  type KnowledgeBaseLifecycleOperation,
  type KnowledgeBaseLifecycleStatus,
} from "@/lib/kb-lifecycle";

// 流转图节点 → 对应操作卡:点击节点滚动到能把库带向(或带离)该状态的操作卡
const STAGE_ACTION_CARD: Record<KnowledgeBaseLifecycleStatus, string> = {
  active: "restore",
  archived: "archive",
  purge_pending: "purge",
  purged: "purge",
};

const COUNT_LABELS: Record<string, string> = {
  documents: "文档",
  revisions: "修订",
  snapshots: "知识快照",
  projections: "派生投影",
  entities: "实体",
  fact_claims: "事实",
  media: "媒体",
  shares: "活动分享",
  external_references: "外部业务引用",
  active_tasks: "活动任务",
  business_artifacts: "业务产物",
  feedback_references: "反馈与引用",
  pinned_entities: "人工固定实体",
  object_keys: "对象存储项",
  shared_object_keys: "共享对象",
};

const BLOCKER_LABELS: Record<string, string> = {
  RETENTION_PERIOD_ACTIVE: "资料仍在审计保留期内",
  LEGAL_HOLD_ACTIVE: "资料处于法律保留状态",
  ACTIVE_TASK_REFERENCE: "仍有活动任务",
  EXTERNAL_SCOPE_REFERENCE: "宿主业务对象仍在引用",
  KNOWLEDGE_BASE_REFERENCE: "其他知识库仍在引用",
  BUSINESS_ARTIFACT_REFERENCE: "业务产物仍在引用",
  KNOWLEDGE_SNAPSHOT_REFERENCE: "受保护知识快照仍在引用",
  FEEDBACK_OR_CITATION_REFERENCE: "反馈或引用记录仍在使用",
  PINNED_ENTITY_REFERENCE: "人工固定实体仍在引用",
  MANUAL_FACT_REFERENCE: "人工事实仍在引用",
  REFERENCE_REGISTRY_UNAVAILABLE: "引用登记暂不可用",
  OBJECT_INVENTORY_UNAVAILABLE: "对象清单暂不可用",
  SHARED_OBJECT_REFERENCE: "对象仍由其他资料共享",
};

const PHASE_LABELS: Record<KnowledgeBaseLifecycleOperation["phase"], string> = {
  revalidate_and_quiesce: "重新校验并冻结消费",
  delete_objects: "删除独占对象",
  cleanup_metadata: "清理依赖元数据",
  finalize_audit_shell: "完成最小审计壳",
  completed: "永久清理完成",
};

const STATUS_LABELS: Record<KnowledgeBaseLifecycleOperation["status"], string> =
  {
    pending: "等待执行",
    processing: "处理中",
    failed: "执行失败",
    dead_letter: "已进入死信",
    completed: "已完成",
  };

type ConfirmedAction = Exclude<KnowledgeBaseDeletionAction, "restore">;

const ACTION_COPY: Record<
  ConfirmedAction,
  { title: string; description: string; confirmLabel: string }
> = {
  archive: {
    title: "归档知识库",
    description:
      "归档会立即停止摄入、发布、检索和业务消费，并撤销分享；它不会物理删除资料，且在永久清理开始前可以恢复。",
    confirmLabel: "确认归档",
  },
  empty_delete: {
    title: "删除空知识库",
    description:
      "该操作仅删除从未使用且没有任何资料或引用的空知识库，完成后知识库本身也不再保留。",
    confirmLabel: "确认删除空知识库",
  },
  purge: {
    title: "永久清理知识库",
    description:
      "永久清理不可撤销。系统会按持久清单删除独占内容和派生数据，受保护引用及共享对象不会被级联破坏。",
    confirmLabel: "确认永久清理",
  },
};

function operationIsLive(operation: KnowledgeBaseLifecycleOperation | null) {
  return operation?.status === "pending" || operation?.status === "processing";
}

function OperationStatus({
  operation,
  onRetry,
  retrying,
}: {
  operation: KnowledgeBaseLifecycleOperation;
  onRetry: () => void;
  retrying: boolean;
}) {
  const failed =
    operation.status === "failed" || operation.status === "dead_letter";
  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">永久清理进度</span>
        <ToneBadge
          tone={
            failed
              ? "destructive"
              : operation.status === "completed"
                ? "success"
                : "warning"
          }
        >
          {STATUS_LABELS[operation.status]}
        </ToneBadge>
      </div>
      <p className="text-sm">
        当前阶段：{PHASE_LABELS[operation.phase] ?? operation.phase}
      </p>
      <p className="text-xs text-muted-foreground">
        operation {operation.id} · 已尝试 {operation.attempt_count} 次
      </p>
      {operation.error_message && (
        <p className="text-sm text-destructive">
          {operation.last_error_code ? `${operation.last_error_code}：` : null}
          {operation.error_message}
        </p>
      )}
      {failed && operation.retryable && (
        <Button
          size="sm"
          variant="outline"
          disabled={retrying}
          onClick={onRetry}
        >
          {retrying ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}
          重试永久清理
        </Button>
      )}
    </div>
  );
}

function ImpactSummary({ preview }: { preview: KnowledgeBaseDeletionPreview }) {
  const counts = Object.entries(preview.impact_counts).filter(
    ([, count]) => count > 0,
  );
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {counts.map(([key, count]) => (
          <div
            key={key}
            className="rounded-md border border-border bg-muted/20 px-3 py-2"
          >
            <p className="text-xs text-muted-foreground">
              {COUNT_LABELS[key] ?? key}
            </p>
            <p className="font-medium tabular-nums">{count}</p>
          </div>
        ))}
        {counts.length === 0 && (
          <p className="text-sm text-muted-foreground">
            未发现资料、引用或对象存储内容
          </p>
        )}
      </div>
      {preview.blockers.length > 0 && (
        <div className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-3">
          <p className="text-sm font-medium">当前阻塞条件</p>
          <ul className="space-y-2 text-sm">
            {preview.blockers.map((blocker) => (
              <li key={blocker.code}>
                <span className="font-medium">
                  {BLOCKER_LABELS[blocker.code] ?? blocker.code}
                  {blocker.count > 0 ? `（${blocker.count}）` : ""}
                </span>
                {blocker.identifiers.length > 0 && (
                  <span className="ml-1 text-muted-foreground">
                    {blocker.identifiers.join("、")}
                  </span>
                )}
                {blocker.resolution_hint && (
                  <p className="text-xs text-muted-foreground">
                    解决提示：{blocker.resolution_hint}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function KbDangerZone({ kbId }: { kbId: string }) {
  const currentOrg = useCurrentOrg();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<ConfirmedAction | null>(null);
  const [confirmationName, setConfirmationName] = useState("");
  const [reason, setReason] = useState("");
  const [externalUnlinkConfirmed, setExternalUnlinkConfirmed] = useState(false);
  // 强制清理:跳过保留期与引用类拦截;需管理员额外勾选确认
  const [forceIntent, setForceIntent] = useState(false);
  const [forceConfirmed, setForceConfirmed] = useState(false);
  const [submittedOperationId, setSubmittedOperationId] = useState<
    string | null
  >(null);
  // 操作卡容器:流转图节点点击后滚动到对应操作卡
  const actionsRef = useRef<HTMLDivElement>(null);
  const isAdmin =
    currentOrg?.role === "org_admin" || currentOrg?.role === "platform_admin";

  const previewQuery = useQuery({
    queryKey: ["kb-deletion-preview", kbId],
    queryFn: () => kbLifecycleApi.preview(kbId),
    enabled: isAdmin,
    // 预检要逐个 stat 对象存储(对象存储在公网时单次往返数百毫秒),整体约 1 秒。
    // 缓存 30 秒:远小于服务端计划有效期(expires_at),切走再回来不必重复等待;
    // 期间若影响真的变了,提交时 plan_hash 校验会返回 409 并自动重新预检。
    staleTime: 30_000,
  });
  const preview = previewQuery.data;
  const owner = Boolean(
    preview && currentOrg && preview.owner_org_id === currentOrg.id,
  );
  const operationId =
    submittedOperationId ?? preview?.latest_operation?.id ?? null;
  const operationQuery = useQuery({
    queryKey: ["kb-lifecycle-operation", operationId],
    queryFn: () => kbLifecycleApi.operation(operationId!),
    enabled: Boolean(operationId),
    initialData:
      preview?.latest_operation?.id === operationId
        ? preview.latest_operation
        : undefined,
    refetchInterval: (query) =>
      operationIsLive(query.state.data ?? null) ? 2000 : false,
  });
  const operation = operationQuery.data ?? preview?.latest_operation ?? null;

  const refreshLifecycle = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["kb-bases"] }),
      queryClient.invalidateQueries({
        queryKey: ["kb-deletion-preview", kbId],
      }),
    ]);
  };

  const closeConfirmation = () => {
    setAction(null);
    setConfirmationName("");
    setReason("");
    setExternalUnlinkConfirmed(false);
    setForceIntent(false);
    setForceConfirmed(false);
  };

  const execute = useMutation({
    mutationFn: async ({
      selected,
      currentPreview,
    }: {
      selected: ConfirmedAction;
      currentPreview: KnowledgeBaseDeletionPreview;
    }) => {
      if (!currentPreview.plan_hash) {
        throw new Error("删除清单不完整，请重新预检");
      }
      if (selected === "archive") {
        return kbLifecycleApi.archive(kbId, {
          expected_plan_hash: currentPreview.plan_hash,
          reason: reason.trim(),
          acknowledge_external_unlink: externalUnlinkConfirmed,
        });
      }
      if (selected === "empty_delete") {
        return kbLifecycleApi.deleteEmpty(kbId, currentPreview.plan_hash);
      }
      return kbLifecycleApi.purge(
        kbId,
        {
          expected_plan_hash: currentPreview.plan_hash,
          confirmation_name: confirmationName,
          reason: reason.trim(),
          force: forceIntent,
        },
        crypto.randomUUID(),
      );
    },
    onSuccess: async (result, { selected }) => {
      closeConfirmation();
      if (selected === "purge") {
        const operationResult = result as KnowledgeBaseLifecycleOperation;
        setSubmittedOperationId(operationResult.id);
        toast.success("永久清理已受理，请以服务端 operation 状态为准");
        await refreshLifecycle();
        return;
      }
      toast.success(selected === "archive" ? "知识库已归档" : "空知识库已删除");
      await refreshLifecycle();
      router.push("/org/kb");
    },
    onError: async (error) => {
      toast.error(kbLifecycleErrorMessage(error));
      if (isDeletionPlanStale(error)) {
        await queryClient.invalidateQueries({
          queryKey: ["kb-deletion-preview", kbId],
        });
      }
    },
  });

  const restore = useMutation({
    mutationFn: () => kbLifecycleApi.restore(kbId),
    onSuccess: async () => {
      toast.success("知识库已恢复；原分享和项目关联不会自动恢复");
      await refreshLifecycle();
    },
    onError: (error) => toast.error(kbLifecycleErrorMessage(error)),
  });

  const retryOperation = useMutation({
    mutationFn: (id: string) => kbLifecycleApi.retryOperation(id),
    onSuccess: (retried) => {
      setSubmittedOperationId(retried.id);
      toast.success("永久清理已重新调度");
      queryClient.invalidateQueries({
        queryKey: ["kb-lifecycle-operation", retried.id],
      });
    },
    onError: (error) => toast.error(kbLifecycleErrorMessage(error)),
  });

  if (!isAdmin) return null;

  const allowed = new Set(preview?.allowed_actions ?? []);
  const selectedCopy = action ? ACTION_COPY[action] : null;
  const nameConfirmed =
    Boolean(preview) && confirmationName === preview?.kb_name;
  const externalRefCount = preview?.impact_counts.external_references ?? 0;
  const externalUnlinkAcknowledged =
    externalRefCount === 0 || externalUnlinkConfirmed;
  const reasonConfirmed = action === "empty_delete" || reason.trim().length > 0;
  const canSubmit =
    Boolean(action && preview) &&
    Boolean(preview?.plan_hash) &&
    nameConfirmed &&
    externalUnlinkAcknowledged &&
    reasonConfirmed &&
    (!forceIntent || forceConfirmed) &&
    !execute.isPending;

  // 强制清理入口:archived 且 blocker 全部属于可跳过类(保留期/引用)时可用;
  // 存在法律保留/共享对象/活动任务等完整性门禁则连强制入口都不给
  const forceBypassableBlockers = (preview?.blockers ?? []).filter((blocker) =>
    FORCE_BYPASSABLE_BLOCKER_CODES.has(blocker.code),
  );
  const canForcePurge =
    preview?.lifecycle_status === "archived" &&
    !allowed.has("purge") &&
    (preview?.blockers.length ?? 0) > 0 &&
    forceBypassableBlockers.length === (preview?.blockers.length ?? 0);

  return (
    <Card className="max-w-5xl border-destructive/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm text-destructive">
          <ShieldAlert className="size-4" />
          危险操作
        </CardTitle>
        {/* 预检结果缓存 30 秒,想强制拿最新影响时手动刷新 */}
        {preview && owner && (
          <CardAction>
            <Button
              size="sm"
              variant="outline"
              disabled={previewQuery.isFetching}
              onClick={() => previewQuery.refetch()}
            >
              {previewQuery.isFetching ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <RotateCcw />
              )}
              重新预检
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="space-y-5">
        {previewQuery.isPending && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" />
            正在预检知识库影响
          </p>
        )}
        {previewQuery.isError && (
          <div className="space-y-2">
            <p className="text-sm text-destructive">
              {kbLifecycleErrorMessage(previewQuery.error)}
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => previewQuery.refetch()}
            >
              重新预检
            </Button>
          </div>
        )}
        {preview && !owner && (
          <p className="text-sm text-muted-foreground">
            共享或平台只读知识库不能由当前组织归档、删除或永久清理。
          </p>
        )}
        {preview && owner && (
          <>
            {/* 生命周期流转图:让管理员先看清当前所处状态与后续不可逆步骤 */}
            <LifecycleFlow
              current={preview.lifecycle_status}
              onStageClick={(stage) => {
                const target = actionsRef.current?.querySelector(
                  `[data-action-card="${STAGE_ACTION_CARD[stage]}"]`,
                );
                (target ?? actionsRef.current)?.scrollIntoView?.({
                  behavior: "smooth",
                  block: "center",
                });
              }}
              className="rounded-lg border border-border/60 bg-muted/10 p-3"
            />
            <div className="flex flex-wrap items-center gap-2">
              <ToneBadge
                tone={
                  preview.lifecycle_status === "active"
                    ? "success"
                    : preview.lifecycle_status === "archived"
                      ? "warning"
                      : "destructive"
                }
              >
                {preview.lifecycle_status}
              </ToneBadge>
              <span className="text-xs text-muted-foreground">
                {preview.expires_at
                  ? `计划有效至 ${new Date(preview.expires_at).toLocaleString("zh-CN")}`
                  : "当前未生成可执行计划"}
              </span>
              {!preview.complete && (
                <ToneBadge tone="destructive">预检不完整</ToneBadge>
              )}
            </div>
            <ImpactSummary preview={preview} />
            {(preview.impact_counts.external_references ?? 0) > 0 && (
              <div className="space-y-1 text-sm">
                <p className="font-medium">宿主业务对象引用</p>
                <p className="text-muted-foreground">
                  本库被宿主的 {preview.impact_counts.external_references}{" "}
                  个业务对象引用。SDK 不认识那些表,只统计数量;解除关联由宿主自行完成。
                </p>
              </div>
            )}
            {operation && (
              <OperationStatus
                operation={operation}
                retrying={retryOperation.isPending}
                onRetry={() => retryOperation.mutate(operation.id)}
              />
            )}
            <div ref={actionsRef} className="grid gap-3 md:grid-cols-2">
              {allowed.has("empty_delete") && (
                <div
                  data-action-card="empty_delete"
                  className="space-y-2 rounded-md border border-destructive/30 p-3"
                >
                  <p className="font-medium">删除空知识库</p>
                  <p className="text-xs text-muted-foreground">
                    仅适用于从未使用、没有资料和引用的误建空库。
                  </p>
                  <Button
                    variant="destructive"
                    onClick={() => setAction("empty_delete")}
                  >
                    <Trash2 />
                    删除空知识库
                  </Button>
                </div>
              )}
              {allowed.has("archive") && (
                <div
                  data-action-card="archive"
                  className="space-y-2 rounded-md border border-border p-3"
                >
                  <p className="font-medium">归档知识库</p>
                  <p className="text-xs text-muted-foreground">
                    可逆地停止当前消费，不删除历史审计和资料。
                  </p>
                  <Button
                    variant="outline"
                    onClick={() => setAction("archive")}
                  >
                    <Archive />
                    归档知识库
                  </Button>
                </div>
              )}
              {allowed.has("restore") && (
                <div
                  data-action-card="restore"
                  className="space-y-2 rounded-md border border-border p-3"
                >
                  <p className="font-medium">恢复知识库</p>
                  <p className="text-xs text-muted-foreground">
                    恢复活动资格，但不会重建归档时撤销的分享或项目关联。
                  </p>
                  <Button
                    variant="outline"
                    disabled={restore.isPending}
                    onClick={() => restore.mutate()}
                  >
                    {restore.isPending ? (
                      <LoaderCircle className="animate-spin" />
                    ) : (
                      <RotateCcw />
                    )}
                    恢复知识库
                  </Button>
                </div>
              )}
              {allowed.has("purge") && (
                <div
                  data-action-card="purge"
                  className="space-y-2 rounded-md border border-destructive/30 p-3"
                >
                  <p className="font-medium">永久清理</p>
                  <p className="text-xs text-muted-foreground">
                    仅归档库且没有 blocker 时可执行，开始后不可恢复。
                  </p>
                  <Button
                    variant="destructive"
                    onClick={() => setAction("purge")}
                  >
                    <Trash2 />
                    永久清理
                  </Button>
                </div>
              )}
              {canForcePurge && (
                <div
                  data-action-card="purge"
                  className="space-y-2 rounded-md border border-destructive/50 bg-destructive/5 p-3"
                >
                  <p className="font-medium text-destructive">强制永久清理</p>
                  <p className="text-xs text-muted-foreground">
                    跳过保留期等待与 {forceBypassableBlockers.length}{" "}
                    类引用拦截立即清理；引用这些资料的历史检索与外部业务对象将出现死链。
                    法律保留、共享对象与进行中任务仍会拦截。
                  </p>
                  <Button
                    variant="destructive"
                    onClick={() => {
                      setForceIntent(true);
                      setAction("purge");
                    }}
                  >
                    <Trash2 />
                    强制永久清理
                  </Button>
                </div>
              )}
            </div>
          </>
        )}
      </CardContent>

      <Dialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open && !execute.isPending) closeConfirmation();
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{selectedCopy?.title}</DialogTitle>
            <DialogDescription>{selectedCopy?.description}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            {action !== "empty_delete" && (
              <label className="block space-y-1 text-sm font-medium">
                操作理由
                <Textarea
                  aria-label="操作理由"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="说明归档或永久清理原因"
                />
              </label>
            )}
            <label className="block space-y-1 text-sm font-medium">
              输入知识库名称「{preview?.kb_name}」确认
              <Input
                aria-label="知识库名称确认"
                value={confirmationName}
                onChange={(event) => setConfirmationName(event.target.value)}
                autoComplete="off"
              />
            </label>
            {externalRefCount > 0 && (
              <label className="flex items-start gap-2 text-sm">
                <Checkbox
                  checked={externalUnlinkConfirmed}
                  onCheckedChange={(checked) =>
                    setExternalUnlinkConfirmed(checked === true)
                  }
                  aria-label="确认解除外部业务引用"
                />
                <span>
                  我确认由宿主解除以上 {externalRefCount}{" "}
                  处外部业务引用；已发布的业务产物不会被改写。
                </span>
              </label>
            )}
            {action === "purge" && forceIntent && (
              <label className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm">
                <Checkbox
                  checked={forceConfirmed}
                  onCheckedChange={(checked) =>
                    setForceConfirmed(checked === true)
                  }
                  aria-label="确认强制清理"
                />
                <span>
                  我了解本次将<b>跳过保留期等待</b>并跳过{" "}
                  {forceBypassableBlockers
                    .map((b) => BLOCKER_LABELS[b.code] ?? b.code)
                    .join("、")}{" "}
                  共 {forceBypassableBlockers.length}{" "}
                  类拦截；引用这些资料的历史检索与外部业务对象将出现死链，删除后无法恢复。
                </span>
              </label>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={execute.isPending}
              onClick={closeConfirmation}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={!canSubmit}
              onClick={() =>
                action &&
                preview &&
                execute.mutate({ selected: action, currentPreview: preview })
              }
            >
              {execute.isPending && <LoaderCircle className="animate-spin" />}
              {selectedCopy?.confirmLabel}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
