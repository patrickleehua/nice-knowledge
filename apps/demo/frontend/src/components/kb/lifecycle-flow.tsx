"use client";

// 知识库生命周期状态流转图:active → archived → purge_pending → purged。
// 两种使用模式:
// - current 模式(单库危险操作区):当前节点高亮 ring+主色,已过节点常规,未到节点淡化,
//   让管理员在执行归档/恢复/清理前看清自己处在哪一步、下一步会发生什么、哪些步骤不可逆;
// - counts 模式(org 生命周期指标页):无当前状态,全部节点常规态,节点下带各状态库数量徽章,作图例用。
// 纯 div/flex + 内联 SVG 箭头实现,不引入新依赖;配色只用语义 token,暗色模式安全;窄屏纵向堆叠。

import { Lock } from "lucide-react";
import type { KnowledgeBaseLifecycleStatus } from "@/lib/kb-lifecycle";
import { cn } from "@/lib/utils";

export interface LifecycleFlowProps {
  /** 当前生命周期状态(单库危险操作区传入;指标页不传) */
  current?: KnowledgeBaseLifecycleStatus;
  /** 各状态知识库数量(指标页传入 kb_status_counts;按状态 key 取值) */
  counts?: Record<string, number>;
  /** 节点点击回调:传了节点才渲染为按钮(危险区用于滚动到对应操作卡) */
  onStageClick?: (stage: KnowledgeBaseLifecycleStatus) => void;
  className?: string;
}

/** 状态机节点顺序(与后端 lifecycle_status 对齐) */
const STAGE_ORDER: KnowledgeBaseLifecycleStatus[] = [
  "active",
  "archived",
  "purge_pending",
  "purged",
];

const STAGE_META: Record<
  KnowledgeBaseLifecycleStatus,
  { label: string; hint: string }
> = {
  active: { label: "活动", hint: "正常摄入与检索" },
  archived: { label: "已归档", hint: "停止消费,可恢复" },
  purge_pending: { label: "清理中", hint: "分阶段执行删除" },
  purged: { label: "已清理", hint: "仅保留审计壳" },
};

/** 节点间转移:forward 为正向动作短语;back 为回边短语;irreversible 标记不可逆段 */
const TRANSITIONS: {
  forward: string;
  back?: string;
  irreversible?: boolean;
}[] = [
  { forward: "归档", back: "恢复" },
  { forward: "提交清理", irreversible: true },
  { forward: "执行完成" },
];

/** 每个状态下的可达转移说明,用于容器 aria-label(让读屏用户与测试都能取到完整语义) */
const STAGE_TRANSITION_TEXT: Record<KnowledgeBaseLifecycleStatus, string> = {
  active:
    "可执行转移:归档(可逆,撤销分享并解除项目关联);从未使用的空库可直接硬删除。",
  archived:
    "可执行转移:恢复为活动(不还原分享),或提交永久清理(不可逆,需预检与库名确认)。",
  purge_pending: "清理已提交,系统按阶段执行中,完成后进入已清理,过程不可逆。",
  purged: "已永久清理,仅保留审计壳,无可执行转移。",
};

/** 正向箭头:sm 及以上横向指右,窄屏纵向指下。
 *  注意:不能用 `rotate-90 sm:rotate-0` 做响应式旋转——本项目 Tailwind v4
 *  工具链下 `sm:` 变体与 `rotate-*` 组合不生成规则(浏览器实测),
 *  故直接渲染两套朝向的 SVG,按断点显隐。 */
function ForwardArrow({ className }: { className?: string }) {
  return (
    <>
      <svg
        viewBox="0 0 48 12"
        className={cn("hidden h-3 w-12 sm:block", className)}
        aria-hidden="true"
      >
        <line
          x1="1"
          y1="6"
          x2="40"
          y2="6"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <path d="M40 2 L47 6 L40 10 Z" fill="currentColor" />
      </svg>
      <svg
        viewBox="0 0 12 48"
        className={cn("h-12 w-3 sm:hidden", className)}
        aria-hidden="true"
      >
        <line
          x1="6"
          y1="1"
          x2="6"
          y2="40"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <path d="M2 40 L6 47 L10 40 Z" fill="currentColor" />
      </svg>
    </>
  );
}

/** 回边箭头(archived → active 的「恢复」):sm 及以上指左,窄屏指上 */
function BackArrow({ className }: { className?: string }) {
  return (
    <>
      <svg
        viewBox="0 0 48 12"
        className={cn("hidden h-3 w-12 sm:block", className)}
        aria-hidden="true"
      >
        <line
          x1="8"
          y1="6"
          x2="47"
          y2="6"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeDasharray="3 2"
        />
        <path d="M8 2 L1 6 L8 10 Z" fill="currentColor" />
      </svg>
      <svg
        viewBox="0 0 12 48"
        className={cn("h-12 w-3 sm:hidden", className)}
        aria-hidden="true"
      >
        <line
          x1="6"
          y1="8"
          x2="6"
          y2="47"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeDasharray="3 2"
        />
        <path d="M2 8 L6 1 L10 8 Z" fill="currentColor" />
      </svg>
    </>
  );
}

/** 节点间连接段:正向动作短语 + 箭头;第一段带「恢复」回边;不可逆段带锁形与微标 */
function Connector({
  transition,
  dimmed,
}: {
  transition: (typeof TRANSITIONS)[number];
  dimmed: boolean;
}) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-col items-center justify-center gap-0.5 px-1 py-1 text-xs",
        transition.irreversible ? "text-destructive" : "text-muted-foreground",
        dimmed && "opacity-40",
      )}
    >
      <span className="flex items-center gap-1 whitespace-nowrap">
        {transition.irreversible && (
          <Lock className="size-3" aria-hidden="true" />
        )}
        {transition.forward}
        {transition.irreversible && (
          <span
            data-testid="irreversible-badge"
            className="rounded border border-destructive/40 px-1 text-[10px] leading-4"
          >
            不可逆
          </span>
        )}
      </span>
      <ForwardArrow />
      {transition.back && (
        <>
          <BackArrow />
          <span className="whitespace-nowrap">{transition.back} ↩</span>
        </>
      )}
    </div>
  );
}

export function LifecycleFlow({
  current,
  counts,
  onStageClick,
  className,
}: LifecycleFlowProps) {
  const currentIndex = current ? STAGE_ORDER.indexOf(current) : -1;

  // 容器 aria-label:current 模式列出当前状态与可达转移;counts 模式列出各状态数量
  const ariaLabel = current
    ? `知识库生命周期流转:当前状态 ${STAGE_META[current].label}。${STAGE_TRANSITION_TEXT[current]}`
    : counts
      ? `知识库生命周期图例:${STAGE_ORDER.map(
          (stage) => `${STAGE_META[stage].label} ${counts[stage] ?? 0} 个`,
        ).join("、")}`
      : "知识库生命周期流转图";

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={cn(
        "flex flex-col items-center sm:flex-row sm:flex-wrap sm:justify-center",
        className,
      )}
    >
      {STAGE_ORDER.map((stage, index) => {
        const meta = STAGE_META[stage];
        const state =
          currentIndex < 0
            ? undefined
            : index === currentIndex
              ? "current"
              : index < currentIndex
                ? "past"
                : "future";
        const irreversibleStage = stage === "purged";
        const nodeContent = (
          <>
            <span className="flex items-center justify-center gap-1 text-sm font-medium">
              {irreversibleStage && (
                <Lock className="size-3 text-destructive" aria-hidden="true" />
              )}
              {meta.label}
              {irreversibleStage && (
                <span
                  data-testid="irreversible-badge"
                  className="rounded border border-destructive/40 px-1 text-[10px] leading-4 text-destructive"
                >
                  不可逆
                </span>
              )}
            </span>
            <span className="block text-[10px] text-muted-foreground">
              {stage}
            </span>
            <span className="block text-[10px] text-muted-foreground">
              {meta.hint}
            </span>
            {counts && (
              <span className="mt-1 inline-block rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular-nums">
                {counts[stage] ?? 0}
              </span>
            )}
          </>
        );
        const nodeClassName = cn(
          "min-w-28 rounded-lg border border-border bg-card px-3 py-2 text-center",
          stage === "purge_pending" && "border-warning/60",
          irreversibleStage && "border-destructive/50",
          state === "current" &&
            "border-primary bg-primary/5 ring-2 ring-primary",
          state === "future" && "opacity-50",
          onStageClick &&
            "cursor-pointer transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-primary",
        );
        const nodeAriaLabel = `${meta.label}(${stage})${
          state === "current" ? ",当前状态" : ""
        }${counts ? `,共 ${counts[stage] ?? 0} 个` : ""}`;
        const node = onStageClick ? (
          <button
            type="button"
            data-stage={stage}
            data-state={state}
            aria-current={state === "current" ? "step" : undefined}
            aria-label={nodeAriaLabel}
            className={nodeClassName}
            onClick={() => onStageClick(stage)}
          >
            {nodeContent}
          </button>
        ) : (
          <div
            data-stage={stage}
            data-state={state}
            aria-current={state === "current" ? "step" : undefined}
            aria-label={nodeAriaLabel}
            className={nodeClassName}
          >
            {nodeContent}
          </div>
        );
        return (
          <div key={stage} className="contents">
            {/* active 节点下方带「空库硬删除」虚线支线;仅从未使用的空库可走 */}
            {stage === "active" ? (
              <div className="flex shrink-0 flex-col items-center">
                {node}
                {/* 支线仅在 counts 模式或当前正处于 active 时保持常规,其余状态下淡化 */}
                <div
                  className={cn(
                    "flex flex-col items-center",
                    currentIndex >= 0 && current !== "active" && "opacity-40",
                  )}
                >
                  <div
                    className="h-3 border-l border-dashed border-muted-foreground/40"
                    aria-hidden="true"
                  />
                  <span className="rounded border border-dashed border-muted-foreground/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    空库硬删除(仅从未使用)
                  </span>
                </div>
              </div>
            ) : (
              node
            )}
            {index < TRANSITIONS.length && (
              <Connector
                transition={TRANSITIONS[index]}
                dimmed={currentIndex >= 0 && index > currentIndex}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
