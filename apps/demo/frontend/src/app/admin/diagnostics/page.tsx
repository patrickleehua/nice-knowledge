"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Bot,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Database,
  Network,
  RefreshCw,
  Server,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import {
  ErrorState,
  PageHeader,
  type Tone,
  ToneBadge,
} from "@/components/shared";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type {
  AgentDiagnosticDto,
  CapabilitySlotDto,
  CustomScheduleDiagnosticDto,
  HeartbeatDto,
  McpDiagnosticDto,
  ModelCapability,
  OperationsDiagnosticsDto,
  OrgHealthDiagnosticDto,
  ProviderProbeDto,
  SystemScheduleDiagnosticDto,
} from "@/lib/types";
const SLOT_LABEL: Record<string, string> = {
  "llm.default": "对话模型",
  "kb.image.caption": "视觉模型",
  "kb.embedding": "嵌入模型",
  "kb.search.rerank": "重排序模型",
};

const CAPABILITY_LABEL: Record<ModelCapability, string> = {
  generation: "生成",
  vision: "视觉",
  web_search: "联网",
  reasoning: "推理",
  function_call: "工具",
  embedding: "嵌入",
  rerank: "重排",
};

const PROBE_LABEL: Record<string, string> = {
  embedding: "嵌入调用",
  rerank: "重排调用",
  ocr: "OCR（本地 Docling）",
  caption: "图片描述调用",
};

const PROBE_STATUS: Record<string, { label: string; tone: Tone }> = {
  healthy: { label: "正常", tone: "success" },
  degraded: { label: "异常", tone: "warning" },
  unavailable: { label: "不可用", tone: "destructive" },
  disabled: { label: "未启用", tone: "muted" },
};

const HEARTBEAT_ROLE: Record<HeartbeatDto["role"], string> = {
  api: "API 服务",
  worker: "任务 Worker",
  beat: "任务调度器",
};

const AGENT_ISSUE_LABEL: Record<string, string> = {
  agent_active_version_missing: "缺少激活版本",
  agent_tool_missing: "绑定的工具已不存在",
  agent_skill_missing: "绑定的 Skill 已不存在",
  agent_mcp_missing: "绑定的 MCP 已不存在",
  agent_mcp_disabled: "绑定的 MCP 已停用",
};

interface DiagnosticIssue {
  key: string;
  title: string;
  detail: string;
  href: string;
  severity: "error" | "warning";
}

function seconds(value: number | null): string {
  if (value === null) return "从未";
  if (value < 60) return `${Math.round(value)} 秒前`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟前`;
  if (value < 86400) return `${Math.round(value / 3600)} 小时前`;
  return `${Math.round(value / 86400)} 天前`;
}

function interval(value: number): string {
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟`;
  if (value < 86400) return `${Math.round(value / 3600)} 小时`;
  return `${Math.round(value / 86400)} 天`;
}

function collectIssues(data: OperationsDiagnosticsDto): DiagnosticIssue[] {
  const issues: DiagnosticIssue[] = [];

  for (const slot of data.capability_slots.filter((item) => !item.ready)) {
    issues.push({
      key: `slot-${slot.task}`,
      title: `${SLOT_LABEL[slot.task] ?? slot.task}不可用`,
      detail:
        slot.configured && slot.is_active
          ? "路由存在，但当前提供商或模型能力已不满足要求"
          : "尚未配置可用的系统模型路由",
      href: "/admin/models",
      severity: "error",
    });
  }
  for (const heartbeat of data.heartbeats) {
    if (heartbeat.required && heartbeat.status !== "healthy") {
      issues.push({
        key: `heartbeat-${heartbeat.role}`,
        title: `${HEARTBEAT_ROLE[heartbeat.role]}未正常上报`,
        detail:
          heartbeat.status === "expired"
            ? `最近心跳在 ${seconds(heartbeat.age_seconds)}`
            : "当前派发模式要求该进程运行，但尚无心跳",
        href: "/admin/diagnostics",
        severity: "error",
      });
    }
  }
  for (const provider of data.providers) {
    if (provider.status === "degraded" || provider.status === "unavailable") {
      issues.push({
        key: `provider-${provider.capability}`,
        title: `${PROBE_LABEL[provider.capability] ?? provider.capability}未通过`,
        detail: provider.error_code ?? "最近一次真实调用失败",
        href: "/admin/providers",
        severity: provider.status === "unavailable" ? "error" : "warning",
      });
    }
  }
  if (data.providers.length === 0) {
    issues.push({
      key: "provider-unchecked",
      title: "模型能力尚未探测",
      detail: "没有可用的周期或手动真实调用记录",
      href: "/admin/diagnostics",
      severity: "warning",
    });
  }
  for (const item of data.skills.invalid) {
    issues.push({
      key: `skill-${item.slug}`,
      title: `Skill「${item.slug}」不可加载`,
      detail:
        item.code === "skill_manifest_missing"
          ? "技能包缺少 SKILL.md"
          : "SKILL.md 无法解析",
      href: "/admin/skills",
      severity: "warning",
    });
  }
  for (const server of data.mcp_servers) {
    if (server.status === "offline") {
      issues.push({
        key: `mcp-${server.server_id}`,
        title: `MCP「${server.name}」离线`,
        detail: server.bound_agents
          ? `已有 ${server.bound_agents} 个 Agent 绑定`
          : "服务器已启用，但最近连接失败",
        href: "/admin/mcp",
        severity: server.bound_agents ? "error" : "warning",
      });
    } else if (server.status === "unchecked" && server.bound_agents > 0) {
      issues.push({
        key: `mcp-unchecked-${server.server_id}`,
        title: `MCP「${server.name}」尚未检测`,
        detail: `已有 ${server.bound_agents} 个 Agent 绑定，建议执行全面检测`,
        href: "/admin/mcp",
        severity: "warning",
      });
    }
  }
  const affectedAgents = data.agents.filter((item) => item.issues.length > 0);
  if (affectedAgents.length > 0) {
    const issueCount = affectedAgents.reduce(
      (total, item) => total + item.issues.length,
      0,
    );
    issues.push({
      key: "agents",
      title: `${affectedAgents.length} 个租户存在 Agent 配置问题`,
      detail: Array.from(
        new Set(
          affectedAgents.flatMap((agent) =>
            agent.issues.map(
              (item) => AGENT_ISSUE_LABEL[item.code] ?? item.code,
            ),
          ),
        ),
      )
        .slice(0, 3)
        .join("、")
        .concat(`（共 ${issueCount} 项）`),
      href: "/admin/agents",
      severity: "warning",
    });
  }
  for (const schedule of data.schedules.system) {
    if (schedule.status === "unavailable") {
      issues.push({
        key: `schedule-${schedule.name}`,
        title: `${schedule.label}未运行`,
        detail: "任务已配置，但 Celery Beat 心跳异常",
        href: "/admin/diagnostics",
        severity: "error",
      });
    } else if (schedule.status === "manual_only") {
      issues.push({
        key: `schedule-${schedule.name}`,
        title: `${schedule.label}没有周期执行器`,
        detail: "当前 inline 模式只能手动触发该任务",
        href: "/admin/diagnostics",
        severity: "warning",
      });
    }
  }
  const affectedSchedules = data.schedules.custom.filter(
    (item) => item.overdue_count > 0 || item.failed_runs_24h > 0,
  );
  if (affectedSchedules.length > 0) {
    const overdue = affectedSchedules.reduce(
      (total, item) => total + item.overdue_count,
      0,
    );
    const failed = affectedSchedules.reduce(
      (total, item) => total + item.failed_runs_24h,
      0,
    );
    issues.push({
      key: "icron",
      title: `${affectedSchedules.length} 个租户的 Agent 定时任务异常`,
      detail: `${overdue} 个已逾期，24 小时内失败 ${failed} 次`,
      href: "/app/icron",
      severity: overdue > 0 ? "error" : "warning",
    });
  }
  const affectedPipelines = data.organizations.filter(
    (item) => item.remediation.length > 0,
  );
  if (affectedPipelines.length > 0) {
    const remediationCount = affectedPipelines.reduce(
      (total, item) => total + item.remediation.length,
      0,
    );
    issues.push({
      key: "kb-pipelines",
      title: `${affectedPipelines.length} 个租户的知识采集链路需要处理`,
      detail: `共 ${remediationCount} 项水位超过阈值，展开下方租户查看详情`,
      href: "/app/kb",
      severity: "error",
    });
  }
  return issues;
}

function DetailSectionTitle({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-2 flex flex-wrap items-end gap-2">
      <div className="min-w-0 flex-1">
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
      </div>
      {action}
    </div>
  );
}

function SlotCard({ slot }: { slot: CapabilitySlotDto }) {
  const staleRoute = slot.configured && slot.is_active && !slot.ready;
  return (
    <section
      className={cn(
        "flex flex-col gap-2 rounded-lg border p-4",
        slot.ready ? "border-border bg-card" : "border-warning/40 bg-warning/5",
      )}
    >
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium">
          {SLOT_LABEL[slot.task] ?? slot.task}
        </h3>
        {slot.ready ? (
          <ToneBadge tone="success">就绪</ToneBadge>
        ) : staleRoute ? (
          <ToneBadge tone="warning">路由失效</ToneBadge>
        ) : !slot.configured ? (
          <ToneBadge tone="warning">未配置</ToneBadge>
        ) : (
          <ToneBadge tone="muted">已停用</ToneBadge>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {slot.required_capabilities.map((capability) => (
          <ToneBadge key={capability} tone="muted">
            {CAPABILITY_LABEL[capability] ?? capability}
          </ToneBadge>
        ))}
      </div>
      {slot.ready ? (
        <p className="font-mono text-xs text-muted-foreground">
          {slot.resolved_provider}/{slot.resolved_model}
          {slot.resolved_model !== slot.primary_model && (
            <span className="ml-1 font-sans text-warning">
              （主模型不可用，已降级）
            </span>
          )}
        </p>
      ) : staleRoute ? (
        <p className="text-xs text-warning">
          {slot.primary_provider}/{slot.primary_model} 当前不满足所需能力
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          {slot.configured
            ? "路由已停用，依赖功能当前不可用"
            : "尚未配置，依赖功能当前不可用"}
        </p>
      )}
    </section>
  );
}

function ProbeRow({ probe }: { probe: ProviderProbeDto }) {
  const status = PROBE_STATUS[probe.status] ?? {
    label: probe.status,
    tone: "muted" as Tone,
  };
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(14rem,1fr)_auto_6rem_7rem] sm:items-center">
      <div className="min-w-0">
        <div className="text-sm">
          {PROBE_LABEL[probe.capability] ?? probe.capability}
        </div>
        <div className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
          {probe.provider
            ? `${probe.provider}/${probe.model ?? "?"}`
            : "未配置"}
        </div>
        {(probe.error_code || probe.consecutive_failures > 0) && (
          <p className="mt-1 text-xs text-destructive">
            {probe.error_code}
            {probe.consecutive_failures > 0 &&
              ` · 连续失败 ${probe.consecutive_failures} 次`}
          </p>
        )}
      </div>
      <ToneBadge tone={status.tone}>{status.label}</ToneBadge>
      <span className="text-xs text-muted-foreground">
        {probe.latency_ms === null ? "—" : `${Math.round(probe.latency_ms)}ms`}
      </span>
      <span className="text-xs text-muted-foreground">
        {seconds(probe.probe_age_seconds)}
      </span>
    </div>
  );
}

function HeartbeatRow({ heartbeat }: { heartbeat: HeartbeatDto }) {
  const healthy = heartbeat.status === "healthy";
  const optional = !heartbeat.required;
  const label = healthy
    ? "正常"
    : optional && heartbeat.status === "missing"
      ? "当前模式不需要"
      : heartbeat.status === "expired"
        ? "已过期"
        : "未上报";
  const tone: Tone = healthy ? "success" : optional ? "muted" : "destructive";
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(10rem,1fr)_minmax(10rem,1fr)_auto_7rem] sm:items-center">
      <div className="flex items-center gap-2 text-sm">
        {healthy ? (
          <CheckCircle2 className="size-3.5 shrink-0 text-success" />
        ) : optional ? (
          <Clock3 className="size-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <XCircle className="size-3.5 shrink-0 text-destructive" />
        )}
        {HEARTBEAT_ROLE[heartbeat.role]}
        {heartbeat.required && <span className="text-destructive">*</span>}
      </div>
      <span className="truncate font-mono text-xs text-muted-foreground">
        {heartbeat.instance ?? "—"}
        {heartbeat.version ? ` · ${heartbeat.version}` : ""}
      </span>
      <ToneBadge tone={tone}>{label}</ToneBadge>
      <span className="text-xs text-muted-foreground">
        {seconds(heartbeat.age_seconds)}
      </span>
    </div>
  );
}

function ScheduleRow({ schedule }: { schedule: SystemScheduleDiagnosticDto }) {
  const state =
    schedule.status === "healthy"
      ? { label: "运行中", tone: "success" as Tone }
      : schedule.status === "unavailable"
        ? { label: "执行器异常", tone: "destructive" as Tone }
        : schedule.status === "manual_only"
          ? { label: "仅手动", tone: "warning" as Tone }
          : { label: "已关闭", tone: "muted" as Tone };
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(12rem,1fr)_6rem_7rem_auto] sm:items-center">
      <div>
        <div className="text-sm">{schedule.label}</div>
        <div className="font-mono text-xs text-muted-foreground">
          {schedule.task}
        </div>
      </div>
      <span className="text-xs text-muted-foreground">
        {schedule.runner === "api"
          ? "API"
          : schedule.runner === "beat"
            ? "Beat"
            : schedule.runner === "manual"
              ? "手动"
              : "—"}
      </span>
      <span className="text-xs text-muted-foreground">
        {schedule.enabled ? interval(schedule.interval_seconds) : "—"}
      </span>
      <ToneBadge tone={state.tone}>{state.label}</ToneBadge>
    </div>
  );
}

function OverviewCard({
  icon,
  title,
  value,
  detail,
  tone,
  onClick,
}: {
  icon: ReactNode;
  title: string;
  value: string;
  detail: string;
  tone: Tone;
  onClick: () => void;
}) {
  const status =
    tone === "success"
      ? "正常"
      : tone === "destructive"
        ? "异常"
        : tone === "warning"
          ? "需关注"
          : "概览";
  return (
    <button
      type="button"
      aria-label={`查看${title}详情`}
      onClick={onClick}
      className="group flex min-h-48 w-full flex-col rounded-xl border border-border bg-card p-5 text-left transition-[border-color,box-shadow,transform] hover:-translate-y-0.5 hover:border-primary/25 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <div className="flex w-full items-start gap-3">
        <span
          className={cn(
            "flex size-9 shrink-0 items-center justify-center rounded-lg",
            tone === "success"
              ? "bg-success/10 text-success"
              : tone === "destructive"
                ? "bg-destructive/10 text-destructive"
                : "bg-warning/10 text-warning",
          )}
        >
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="min-w-0 flex-1 truncate text-sm font-medium">
              {title}
            </h3>
            <ToneBadge tone={tone}>{status}</ToneBadge>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {detail}
          </p>
        </div>
      </div>
      <div className="mt-6 text-2xl font-semibold tracking-tight">{value}</div>
      <div className="mt-auto flex w-full items-center gap-1 pt-5 text-xs font-medium text-primary">
        查看详情
        <ChevronRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
      </div>
    </button>
  );
}

function McpRow({ server }: { server: McpDiagnosticDto }) {
  const state =
    server.status === "online"
      ? { label: "在线", tone: "success" as Tone }
      : server.status === "offline"
        ? { label: "离线", tone: "destructive" as Tone }
        : server.status === "unchecked"
          ? { label: "未检测", tone: "warning" as Tone }
          : { label: "已停用", tone: "muted" as Tone };
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(12rem,1fr)_6rem_6rem_6rem_auto] sm:items-center">
      <div>
        <div className="text-sm">{server.name}</div>
        <div className="text-xs text-muted-foreground">{server.transport}</div>
      </div>
      <span className="text-xs text-muted-foreground">
        工具 {server.tools_count}
      </span>
      <span className="text-xs text-muted-foreground">
        Agent {server.bound_agents}
      </span>
      <span className="text-xs text-muted-foreground">
        {server.latency_ms === null ? "—" : `${server.latency_ms}ms`}
      </span>
      <ToneBadge tone={state.tone}>{state.label}</ToneBadge>
    </div>
  );
}

function kbMetrics(health: OrgHealthDiagnosticDto) {
  return [
    {
      label: "出箱队列",
      value: `${health.outbox.pending_count} 待处理`,
      age: health.outbox.oldest_pending_age_seconds,
    },
    {
      label: "文档摄入",
      value: `${health.documents.uploaded_count} 上传 · ${health.documents.processing_count} 处理中 · ${health.documents.failed_count} 失败`,
      age:
        health.documents.oldest_processing_age_seconds ??
        health.documents.oldest_uploaded_age_seconds,
    },
    {
      label: "摄入租约",
      value: `${health.ingest_leases.count} 个`,
      age: health.ingest_leases.oldest_age_seconds,
    },
    {
      label: "图片富化",
      value: `${health.image_enrichment.count} 待处理`,
      age: health.image_enrichment.oldest_age_seconds,
    },
    {
      label: "快照构建",
      value: `${health.snapshot_builds.count} 进行中`,
      age: health.snapshot_builds.oldest_age_seconds,
    },
    {
      label: "媒体一致性",
      value: `${health.object_metadata_inconsistencies.count + health.media_projection_failures.count} 个异常`,
      age:
        health.object_metadata_inconsistencies.oldest_age_seconds ??
        health.media_projection_failures.oldest_age_seconds,
    },
    {
      label: "检索投影",
      value: `${health.vectorless_chunks} 缺向量 · ${health.pending_claims} 待审核`,
      age: null,
    },
  ];
}

function KnowledgePipeline({
  org,
}: {
  org: OperationsDiagnosticsDto["organizations"][number];
}) {
  const healthy = org.remediation.length === 0;
  return (
    <details className="group rounded-lg border border-border bg-card">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3">
        <Database className="size-4 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {org.org_name}
        </span>
        <span className="text-xs text-muted-foreground">
          {org.health.outbox.pending_count +
            org.health.documents.uploaded_count +
            org.health.documents.processing_count +
            org.health.image_enrichment.count}{" "}
          项处理中
        </span>
        <ToneBadge tone={healthy ? "success" : "destructive"}>
          {healthy ? "链路正常" : `${org.remediation.length} 项异常`}
        </ToneBadge>
      </summary>
      <div className="border-t border-border p-4">
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {kbMetrics(org.health).map((metric) => (
            <div key={metric.label} className="rounded-md bg-muted/45 p-3">
              <div className="text-xs text-muted-foreground">
                {metric.label}
              </div>
              <div className="mt-1 text-sm font-medium">{metric.value}</div>
              {metric.age !== null && (
                <div className="mt-1 text-xs text-muted-foreground">
                  最老记录 {seconds(metric.age)}
                </div>
              )}
            </div>
          ))}
        </div>
        {org.remediation.length > 0 && (
          <ul className="mt-3 space-y-1 rounded-md border border-destructive/25 bg-destructive/5 p-3 text-xs text-destructive">
            {org.remediation.map((item) => (
              <li key={item}>• {item}</li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}

function agentTotals(items: AgentDiagnosticDto[]) {
  return items.reduce(
    (total, item) => ({
      enabled: total.enabled + item.enabled_cards,
      ready: total.ready + item.ready_cards,
      running: total.running + item.running_sessions,
      issues: total.issues + item.issues.length,
    }),
    { enabled: 0, ready: 0, running: 0, issues: 0 },
  );
}

function customScheduleTotals(items: CustomScheduleDiagnosticDto[]) {
  return items.reduce(
    (total, item) => ({
      active: total.active + item.active_count,
      overdue: total.overdue + item.overdue_count,
      failed: total.failed + item.failed_runs_24h,
    }),
    { active: 0, overdue: 0, failed: 0 },
  );
}

type DiagnosticPanelId =
  | "issues"
  | "models"
  | "runtime"
  | "schedules"
  | "ecosystem"
  | "agents"
  | "knowledge";

const PANEL_META: Record<
  DiagnosticPanelId,
  { title: string; description: string }
> = {
  issues: {
    title: "问题清单",
    description: "按严重程度汇总当前需要处置或关注的运行状态",
  },
  models: {
    title: "模型能力与真实探测",
    description: "核对系统路由、所需能力与最近一次上游真实调用",
  },
  runtime: {
    title: "运行进程",
    description: "按当前派发模式核对 API、Worker 与调度器心跳",
  },
  schedules: {
    title: "系统周期任务",
    description: "查看每项后台任务的周期、执行器与实际可用状态",
  },
  ecosystem: {
    title: "Skills 与 MCP",
    description: "检查技能包清单、MCP 连接缓存及 Agent 绑定影响",
  },
  agents: {
    title: "Agents 与用户定时任务",
    description: "查看租户 Agent 激活版本、会话和 iCron 任务状态",
  },
  knowledge: {
    title: "知识库采集链路",
    description: "按租户查看出箱、摄入、富化、快照与检索投影水位",
  },
};

function MetricTile({
  label,
  value,
  detail,
  tone = "muted",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: Tone;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 text-xs text-muted-foreground">
          {label}
        </span>
        {tone !== "muted" && (
          <span
            className={cn(
              "size-1.5 rounded-full",
              tone === "success"
                ? "bg-success"
                : tone === "destructive"
                  ? "bg-destructive"
                  : "bg-warning",
            )}
          />
        )}
      </div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
      {detail && (
        <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>
      )}
    </div>
  );
}

function AgentDiagnosticRow({ agent }: { agent: AgentDiagnosticDto }) {
  return (
    <div className="flex flex-wrap items-start gap-3 px-4 py-3">
      <div className="min-w-40 flex-1">
        <div className="text-sm">{agent.org_name}</div>
        <div className="text-xs text-muted-foreground">
          {agent.ready_cards}/{agent.enabled_cards} 就绪 ·{" "}
          {agent.running_sessions} 个会话运行中
        </div>
        {agent.issues.length > 0 && (
          <div className="mt-1 text-xs text-warning">
            {agent.issues
              .map(
                (item) =>
                  `${AGENT_ISSUE_LABEL[item.code] ?? item.code}（${item.card}）`,
              )
              .join("、")}
          </div>
        )}
      </div>
      <ToneBadge tone={agent.issues.length ? "warning" : "success"}>
        {agent.issues.length ? `${agent.issues.length} 个问题` : "正常"}
      </ToneBadge>
    </div>
  );
}

function CustomScheduleRow({
  schedule,
}: {
  schedule: CustomScheduleDiagnosticDto;
}) {
  const unhealthy = schedule.overdue_count > 0 || schedule.failed_runs_24h > 0;
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[minmax(12rem,1fr)_6rem_6rem_auto] sm:items-center">
      <div>
        <div className="text-sm">{schedule.org_name}</div>
        <div className="text-xs text-muted-foreground">
          暂停 {schedule.paused_count} · 已归档 {schedule.archived_count}
        </div>
      </div>
      <span className="text-xs text-muted-foreground">
        启用 {schedule.active_count}
      </span>
      <span className="text-xs text-muted-foreground">
        失败 {schedule.failed_runs_24h}
      </span>
      <ToneBadge tone={unhealthy ? "destructive" : "success"}>
        {schedule.overdue_count > 0
          ? `${schedule.overdue_count} 个逾期`
          : unhealthy
            ? "近期失败"
            : "正常"}
      </ToneBadge>
    </div>
  );
}

function IssueRow({ issue }: { issue: DiagnosticIssue }) {
  const content = (
    <>
      {issue.severity === "error" ? (
        <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      ) : (
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
      )}
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium">{issue.title}</span>
        <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
          {issue.detail}
        </span>
      </span>
      {issue.href !== "/admin/diagnostics" && (
        <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      )}
    </>
  );
  const className =
    "flex items-start gap-3 rounded-lg border border-border bg-card p-3.5";
  return issue.href === "/admin/diagnostics" ? (
    <div className={className}>{content}</div>
  ) : (
    <Link
      href={issue.href}
      className={cn(
        className,
        "transition-colors hover:border-primary/25 hover:bg-muted/30",
      )}
    >
      {content}
    </Link>
  );
}

function DiagnosticDetailDialog({
  panel,
  onOpenChange,
  data,
  issues,
  showAllPipelines,
  onShowAllPipelinesChange,
}: {
  panel: DiagnosticPanelId | null;
  onOpenChange: (open: boolean) => void;
  data: OperationsDiagnosticsDto;
  issues: DiagnosticIssue[];
  showAllPipelines: boolean;
  onShowAllPipelinesChange: (show: boolean) => void;
}) {
  const meta = panel ? PANEL_META[panel] : PANEL_META.issues;
  const agents = agentTotals(data.agents);
  const customTasks = customScheduleTotals(data.schedules.custom);
  const affectedAgents = data.agents.filter((item) => item.issues.length > 0);
  const affectedCustomTasks = data.schedules.custom.filter(
    (item) => item.overdue_count > 0 || item.failed_runs_24h > 0,
  );
  const affectedPipelines = data.organizations.filter(
    (item) => item.remediation.length > 0,
  );
  const orderedPipelines = [...data.organizations].sort(
    (left, right) =>
      Number(right.remediation.length > 0) -
      Number(left.remediation.length > 0),
  );
  const visiblePipelines = showAllPipelines
    ? orderedPipelines
    : affectedPipelines;
  const requiredHeartbeats = data.heartbeats.filter((item) => item.required);
  const healthyRequiredHeartbeats = requiredHeartbeats.filter(
    (item) => item.status === "healthy",
  ).length;
  const enabledSchedules = data.schedules.system.filter((item) => item.enabled);
  const healthySchedules = enabledSchedules.filter(
    (item) => item.status === "healthy",
  ).length;
  const providerHealthy = data.providers.filter(
    (item) => item.status === "healthy",
  ).length;
  const mcpOnline = data.mcp_servers.filter(
    (item) => item.status === "online",
  ).length;
  const mcpOffline = data.mcp_servers.filter(
    (item) => item.status === "offline",
  ).length;

  return (
    <Dialog open={panel !== null} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[88vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl lg:max-w-5xl"
      >
        <DialogClose
          render={
            <Button
              variant="ghost"
              size="icon-sm"
              className="absolute top-2.5 right-2.5 z-10"
            />
          }
        >
          <X className="size-4" />
          <span className="sr-only">关闭详情</span>
        </DialogClose>
        <DialogHeader className="border-b border-border px-5 py-4 pr-14 sm:px-6 sm:py-5">
          <DialogTitle>{meta.title}</DialogTitle>
          <DialogDescription>{meta.description}</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto bg-muted/15 p-4 sm:p-6">
          {panel === "issues" && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricTile
                  label="需要处置"
                  value={`${issues.filter((item) => item.severity === "error").length} 项`}
                  tone={
                    issues.some((item) => item.severity === "error")
                      ? "destructive"
                      : "success"
                  }
                />
                <MetricTile
                  label="配置提醒"
                  value={`${issues.filter((item) => item.severity === "warning").length} 项`}
                  tone={
                    issues.some((item) => item.severity === "warning")
                      ? "warning"
                      : "success"
                  }
                />
                <MetricTile
                  label="诊断快照"
                  value={new Date(data.generated_at).toLocaleTimeString()}
                  detail={new Date(data.generated_at).toLocaleDateString()}
                />
              </div>
              {issues.length > 0 ? (
                <div className="space-y-2">
                  {issues.map((issue) => (
                    <IssueRow key={issue.key} issue={issue} />
                  ))}
                </div>
              ) : (
                <div className="rounded-lg border border-success/30 bg-success/5 px-4 py-10 text-center">
                  <CheckCircle2 className="mx-auto size-6 text-success" />
                  <p className="mt-2 text-sm font-medium">
                    当前没有需要处理的问题
                  </p>
                </div>
              )}
            </div>
          )}

          {panel === "models" && (
            <div className="space-y-5">
              <div>
                <DetailSectionTitle
                  title="系统能力路由"
                  description={`${data.capability_slots.filter((item) => item.ready).length}/${data.capability_slots.length} 个能力槽位就绪`}
                  action={
                    <Link
                      href="/admin/models"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      管理模型路由
                    </Link>
                  }
                />
                <div className="grid gap-3 sm:grid-cols-2">
                  {data.capability_slots.map((slot) => (
                    <SlotCard key={slot.task} slot={slot} />
                  ))}
                </div>
              </div>
              <div>
                <DetailSectionTitle
                  title="真实调用探测"
                  description={`${providerHealthy}/${data.providers.length} 项最近探测正常；全面检测会刷新结果`}
                />
                <div className="divide-y divide-border rounded-lg border border-border bg-card">
                  {data.providers.length > 0 ? (
                    data.providers.map((item) => (
                      <ProbeRow key={item.capability} probe={item} />
                    ))
                  ) : (
                    <div className="px-4 py-10 text-center text-sm text-muted-foreground">
                      还没有真实调用记录，执行一次「全面检测」
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {panel === "runtime" && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricTile
                  label="必需进程"
                  value={`${healthyRequiredHeartbeats}/${requiredHeartbeats.length} 正常`}
                  tone={
                    healthyRequiredHeartbeats === requiredHeartbeats.length
                      ? "success"
                      : "destructive"
                  }
                />
                <MetricTile
                  label="派发模式"
                  value={
                    data.runtime.dispatch_mode === "celery"
                      ? "Celery"
                      : "API 内联"
                  }
                />
                <MetricTile
                  label="心跳过期阈值"
                  value={interval(data.runtime.heartbeat_expiry_seconds)}
                />
              </div>
              <div className="divide-y divide-border rounded-lg border border-border bg-card">
                {data.heartbeats.map((item) => (
                  <HeartbeatRow key={item.role} heartbeat={item} />
                ))}
              </div>
            </div>
          )}

          {panel === "schedules" && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricTile
                  label="已配置周期任务"
                  value={`${enabledSchedules.length} 项`}
                />
                <MetricTile
                  label="执行器正常"
                  value={`${healthySchedules} 项`}
                  tone={
                    healthySchedules === enabledSchedules.length
                      ? "success"
                      : "warning"
                  }
                />
                <MetricTile
                  label="仅手动或异常"
                  value={`${enabledSchedules.length - healthySchedules} 项`}
                  tone={
                    enabledSchedules.length === healthySchedules
                      ? "success"
                      : "warning"
                  }
                />
              </div>
              <div className="divide-y divide-border rounded-lg border border-border bg-card">
                {data.schedules.system.map((item) => (
                  <ScheduleRow key={item.name} schedule={item} />
                ))}
              </div>
            </div>
          )}

          {panel === "ecosystem" && (
            <div className="space-y-5">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricTile
                  label="Skills"
                  value={`${data.skills.total} 个`}
                  detail={`${data.skills.invalid.length} 个不可加载`}
                  tone={data.skills.invalid.length ? "warning" : "success"}
                />
                <MetricTile
                  label="MCP 在线"
                  value={`${mcpOnline}/${data.mcp_servers.length} 台`}
                  tone={mcpOffline ? "destructive" : "success"}
                />
                <MetricTile
                  label="Agent 绑定受影响"
                  value={`${data.mcp_servers.reduce((total, item) => total + (item.status === "offline" ? item.bound_agents : 0), 0)} 个`}
                  tone={
                    data.mcp_servers.some(
                      (item) =>
                        item.status === "offline" && item.bound_agents > 0,
                    )
                      ? "destructive"
                      : "success"
                  }
                />
              </div>
              <div>
                <DetailSectionTitle
                  title="Skill 清单"
                  description="扫描技能根目录下的所有包，而非只显示可解析项"
                  action={
                    <Link
                      href="/admin/skills"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      技能管理
                    </Link>
                  }
                />
                {data.skills.invalid.length > 0 ? (
                  <div className="divide-y divide-border rounded-lg border border-border bg-card">
                    {data.skills.invalid.map((item) => (
                      <div
                        key={item.slug}
                        className="flex items-center gap-3 px-4 py-3"
                      >
                        <Sparkles className="size-4 text-warning" />
                        <span className="min-w-0 flex-1 text-sm">
                          {item.slug}
                        </span>
                        <ToneBadge tone="warning">
                          {item.code === "skill_manifest_missing"
                            ? "缺少 SKILL.md"
                            : "清单不可解析"}
                        </ToneBadge>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-lg border border-border bg-card px-4 py-8 text-center text-sm text-muted-foreground">
                    所有技能包均可读取
                  </div>
                )}
              </div>
              <div>
                <DetailSectionTitle
                  title="MCP 连接"
                  description="普通刷新读取连接缓存；全面检测才会重新连接"
                  action={
                    <Link
                      href="/admin/mcp"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      MCP 管理
                    </Link>
                  }
                />
                <div className="divide-y divide-border rounded-lg border border-border bg-card">
                  {data.mcp_servers.length > 0 ? (
                    data.mcp_servers.map((server) => (
                      <McpRow key={server.server_id} server={server} />
                    ))
                  ) : (
                    <div className="px-4 py-10 text-center text-sm text-muted-foreground">
                      尚未配置 MCP 服务器
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {panel === "agents" && (
            <div className="space-y-5">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <MetricTile
                  label="Agent 就绪"
                  value={`${agents.ready}/${agents.enabled}`}
                  tone={agents.issues ? "warning" : "success"}
                />
                <MetricTile label="运行会话" value={`${agents.running} 个`} />
                <MetricTile
                  label="用户定时任务"
                  value={`${customTasks.active} 个启用`}
                />
                <MetricTile
                  label="逾期 / 近期失败"
                  value={`${customTasks.overdue} / ${customTasks.failed}`}
                  tone={
                    customTasks.overdue
                      ? "destructive"
                      : customTasks.failed
                        ? "warning"
                        : "success"
                  }
                />
              </div>
              <div>
                <DetailSectionTitle
                  title="Agent 配置异常"
                  description={`仅列出受影响租户；其余 ${data.agents.length - affectedAgents.length} 个租户正常`}
                  action={
                    <Link
                      href="/admin/agents"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      Agent 管理
                    </Link>
                  }
                />
                <div className="divide-y divide-border rounded-lg border border-border bg-card">
                  {affectedAgents.length > 0 ? (
                    affectedAgents.map((agent) => (
                      <AgentDiagnosticRow key={agent.org_id} agent={agent} />
                    ))
                  ) : (
                    <div className="px-4 py-10 text-center text-sm text-muted-foreground">
                      {data.agents.length} 个租户的 Agent 配置均正常
                    </div>
                  )}
                </div>
              </div>
              <div>
                <DetailSectionTitle
                  title="用户定时任务异常"
                  description="仅列出逾期或 24 小时内执行失败的租户"
                  action={
                    <Link
                      href="/app/icron"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      定时任务
                    </Link>
                  }
                />
                <div className="divide-y divide-border rounded-lg border border-border bg-card">
                  {affectedCustomTasks.length > 0 ? (
                    affectedCustomTasks.map((schedule) => (
                      <CustomScheduleRow
                        key={schedule.org_id}
                        schedule={schedule}
                      />
                    ))
                  ) : (
                    <div className="px-4 py-10 text-center text-sm text-muted-foreground">
                      当前没有逾期或近期失败的用户定时任务
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {panel === "knowledge" && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricTile
                  label="纳管租户"
                  value={`${data.organizations.length} 个`}
                />
                <MetricTile
                  label="链路正常"
                  value={`${data.organizations.length - affectedPipelines.length} 个`}
                  tone={affectedPipelines.length === 0 ? "success" : "warning"}
                />
                <MetricTile
                  label="需要处理"
                  value={`${affectedPipelines.length} 个`}
                  detail={`${affectedPipelines.reduce((total, item) => total + item.remediation.length, 0)} 项水位异常`}
                  tone={affectedPipelines.length ? "destructive" : "success"}
                />
              </div>
              <DetailSectionTitle
                title={
                  showAllPipelines
                    ? "全部租户链路"
                    : `异常租户链路（${affectedPipelines.length}）`
                }
                description="异常租户优先排列；展开租户可查看每条流水线水位"
                action={
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        onShowAllPipelinesChange(!showAllPipelines)
                      }
                    >
                      {showAllPipelines
                        ? "只看异常"
                        : `查看全部 ${data.organizations.length} 个`}
                    </Button>
                    <Link
                      href="/app/kb"
                      className={cn(
                        buttonVariants({ variant: "outline", size: "sm" }),
                      )}
                    >
                      知识库
                    </Link>
                  </div>
                }
              />
              <div className="space-y-2">
                {visiblePipelines.map((org) => (
                  <KnowledgePipeline key={org.org_id} org={org} />
                ))}
                {visiblePipelines.length === 0 && (
                  <div className="rounded-lg border border-success/30 bg-success/5 px-4 py-10 text-center text-sm text-muted-foreground">
                    当前所有租户的知识采集链路均正常
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function AdminDiagnosticsPage() {
  const [activePanel, setActivePanel] = useState<DiagnosticPanelId | null>(
    null,
  );
  const [showAllPipelines, setShowAllPipelines] = useState(false);
  const queryClient = useQueryClient();
  const diagnostics = useQuery({
    queryKey: ["admin-diagnostics"],
    queryFn: () =>
      api.get<OperationsDiagnosticsDto>("/admin/operations/diagnostics"),
  });

  const probe = useMutation({
    mutationFn: () =>
      api.post<OperationsDiagnosticsDto>("/admin/operations/probe", {}),
    onSuccess: (data) => {
      queryClient.setQueryData(["admin-diagnostics"], data);
      const failures =
        data.providers.filter((item) =>
          ["degraded", "unavailable"].includes(item.status),
        ).length +
        data.mcp_servers.filter((item) => item.status === "offline").length;
      if (failures > 0) {
        toast.warning(`${failures} 项外部能力检测未通过`);
      } else {
        toast.success("模型能力与 MCP 检测通过");
      }
    },
    onError: (error) => toast.error(errMsg(error, "检测失败")),
  });

  const data = diagnostics.data;
  const issues = data ? collectIssues(data) : [];
  const errors = issues.filter((item) => item.severity === "error").length;
  const warnings = issues.length - errors;
  const agents = data ? agentTotals(data.agents) : agentTotals([]);
  const customTasks = data
    ? customScheduleTotals(data.schedules.custom)
    : customScheduleTotals([]);
  const mcpOffline =
    data?.mcp_servers.filter((item) => item.status === "offline").length ?? 0;
  const affectedAgents =
    data?.agents.filter((item) => item.issues.length > 0) ?? [];
  const affectedCustomTasks =
    data?.schedules.custom.filter(
      (item) => item.overdue_count > 0 || item.failed_runs_24h > 0,
    ) ?? [];
  const affectedPipelines =
    data?.organizations.filter((item) => item.remediation.length > 0) ?? [];
  const remediationCount = affectedPipelines.reduce(
    (total, item) => total + item.remediation.length,
    0,
  );
  const requiredHeartbeats =
    data?.heartbeats.filter((item) => item.required) ?? [];
  const healthyRequiredHeartbeats = requiredHeartbeats.filter(
    (item) => item.status === "healthy",
  ).length;
  const enabledSchedules =
    data?.schedules.system.filter((item) => item.enabled) ?? [];
  const healthySchedules = enabledSchedules.filter(
    (item) => item.status === "healthy",
  ).length;
  const readySlots =
    data?.capability_slots.filter((item) => item.ready).length ?? 0;
  const healthyProbes =
    data?.providers.filter((item) => item.status === "healthy").length ?? 0;
  const mcpOnline =
    data?.mcp_servers.filter((item) => item.status === "online").length ?? 0;

  const modelTone: Tone =
    data?.capability_slots.some((item) => !item.ready) ||
    data?.providers.some((item) => item.status === "unavailable")
      ? "destructive"
      : !data?.providers.length ||
          data.providers.some((item) => item.status === "degraded")
        ? "warning"
        : "success";
  const runtimeTone: Tone = requiredHeartbeats.some(
    (item) => item.status !== "healthy",
  )
    ? "destructive"
    : "success";
  const scheduleTone: Tone = enabledSchedules.some(
    (item) => item.status === "unavailable",
  )
    ? "destructive"
    : enabledSchedules.some((item) => item.status === "manual_only")
      ? "warning"
      : "success";
  const ecosystemTone: Tone = data?.mcp_servers.some(
    (item) => item.status === "offline" && item.bound_agents > 0,
  )
    ? "destructive"
    : data?.skills.invalid.length ||
        mcpOffline > 0 ||
        data?.mcp_servers.some(
          (item) => item.status === "unchecked" && item.bound_agents > 0,
        )
      ? "warning"
      : "success";
  const agentsTone: Tone = customTasks.overdue
    ? "destructive"
    : agents.issues || customTasks.failed
      ? "warning"
      : "success";
  const knowledgeTone: Tone = affectedPipelines.length
    ? "destructive"
    : "success";
  const healthyDomains = [
    modelTone,
    runtimeTone,
    scheduleTone,
    ecosystemTone,
    agentsTone,
    knowledgeTone,
  ].filter((tone) => tone === "success").length;
  const affectedTenantCount = new Set([
    ...affectedAgents.map((item) => item.org_id),
    ...affectedCustomTasks.map((item) => item.org_id),
    ...affectedPipelines.map((item) => item.org_id),
  ]).size;

  const openPanel = (panel: DiagnosticPanelId) => {
    if (panel === "knowledge") setShowAllPipelines(false);
    setActivePanel(panel);
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="系统诊断"
        description="统一查看模型能力、运行进程、Agent 生态、调度任务和知识采集链路"
        actions={
          <Button disabled={probe.isPending} onClick={() => probe.mutate()}>
            <RefreshCw
              className={cn("size-4", probe.isPending && "animate-spin")}
            />
            {probe.isPending ? "检测中…" : "全面检测"}
          </Button>
        }
      />

      {diagnostics.error ? (
        <ErrorState
          error={diagnostics.error}
          onRetry={() => diagnostics.refetch()}
        />
      ) : diagnostics.isPending || !data ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((item) => (
            <Skeleton key={item} className="h-28 rounded-lg" />
          ))}
        </div>
      ) : (
        <>
          <section
            className={cn(
              "overflow-hidden rounded-xl border bg-card",
              errors > 0
                ? "border-destructive/35"
                : warnings > 0
                  ? "border-warning/40"
                  : "border-success/30",
            )}
          >
            <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:p-6">
              <div
                className={cn(
                  "flex size-11 shrink-0 items-center justify-center rounded-xl",
                  errors > 0
                    ? "bg-destructive/10 text-destructive"
                    : warnings > 0
                      ? "bg-warning/10 text-warning"
                      : "bg-success/10 text-success",
                )}
              >
                {errors > 0 ? (
                  <XCircle className="size-5" />
                ) : warnings > 0 ? (
                  <AlertTriangle className="size-5" />
                ) : (
                  <CheckCircle2 className="size-5" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  系统健康概览
                </div>
                <h2 className="mt-1 text-lg font-semibold tracking-tight">
                  {errors > 0
                    ? "系统存在需要处置的问题"
                    : warnings > 0
                      ? "系统可运行，仍有配置提醒"
                      : "所有纳管链路状态正常"}
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  {data.runtime.dispatch_mode === "celery"
                    ? "Celery 生产派发"
                    : "API 内联派发"}{" "}
                  · 服务版本 {data.runtime.service_version} · 快照{" "}
                  {new Date(data.generated_at).toLocaleString()}
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => openPanel("issues")}
                className="shrink-0"
              >
                {issues.length > 0 ? "查看问题清单" : "查看诊断摘要"}
                <ChevronRight className="size-4" />
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-px border-t border-border bg-border lg:grid-cols-4">
              <div className="bg-card px-5 py-4">
                <div className="text-xs text-muted-foreground">需要处置</div>
                <div className="mt-1 text-xl font-semibold">{errors}</div>
              </div>
              <div className="bg-card px-5 py-4">
                <div className="text-xs text-muted-foreground">配置提醒</div>
                <div className="mt-1 text-xl font-semibold">{warnings}</div>
              </div>
              <div className="bg-card px-5 py-4">
                <div className="text-xs text-muted-foreground">健康领域</div>
                <div className="mt-1 text-xl font-semibold">
                  {healthyDomains}/6
                </div>
              </div>
              <div className="bg-card px-5 py-4">
                <div className="text-xs text-muted-foreground">受影响租户</div>
                <div className="mt-1 text-xl font-semibold">
                  {affectedTenantCount}
                </div>
              </div>
            </div>
          </section>

          <section aria-labelledby="diagnostic-domains">
            <div className="mb-3">
              <h2
                id="diagnostic-domains"
                className="text-base font-semibold tracking-tight"
              >
                诊断领域
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">
                首屏只展示关键水位；点击卡片进入对应诊断工作区
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              <OverviewCard
                icon={<Sparkles className="size-4" />}
                title="模型能力"
                value={`${readySlots}/${data.capability_slots.length} 路由就绪`}
                detail={
                  data.providers.length
                    ? `${healthyProbes}/${data.providers.length} 项真实探测正常`
                    : "尚无真实调用探测记录"
                }
                tone={modelTone}
                onClick={() => openPanel("models")}
              />
              <OverviewCard
                icon={<Server className="size-4" />}
                title="运行进程"
                value={`${healthyRequiredHeartbeats}/${requiredHeartbeats.length} 必需进程正常`}
                detail={
                  data.runtime.dispatch_mode === "celery"
                    ? "Celery 模式要求 Worker 与 Beat 持续上报"
                    : "API 内联模式不要求 Worker 与 Beat"
                }
                tone={runtimeTone}
                onClick={() => openPanel("runtime")}
              />
              <OverviewCard
                icon={<CalendarClock className="size-4" />}
                title="系统周期任务"
                value={`${healthySchedules}/${enabledSchedules.length} 执行器正常`}
                detail={`${enabledSchedules.filter((item) => item.status === "manual_only").length} 项仅手动 · ${enabledSchedules.filter((item) => item.status === "unavailable").length} 项异常`}
                tone={scheduleTone}
                onClick={() => openPanel("schedules")}
              />
              <OverviewCard
                icon={<Network className="size-4" />}
                title="Skills 与 MCP"
                value={`${data.skills.total} Skills · ${data.mcp_servers.length} MCP`}
                detail={`${data.skills.invalid.length} 个技能包异常 · ${mcpOnline} 台 MCP 在线`}
                tone={ecosystemTone}
                onClick={() => openPanel("ecosystem")}
              />
              <OverviewCard
                icon={<Bot className="size-4" />}
                title="Agents 与定时任务"
                value={`${agents.ready}/${agents.enabled} Agent 就绪`}
                detail={`${affectedAgents.length} 个租户配置异常 · ${customTasks.overdue} 个任务逾期`}
                tone={agentsTone}
                onClick={() => openPanel("agents")}
              />
              <OverviewCard
                icon={<Database className="size-4" />}
                title="知识采集链路"
                value={`${data.organizations.length - affectedPipelines.length}/${data.organizations.length} 租户正常`}
                detail={`${affectedPipelines.length} 个租户需处理 · ${remediationCount} 项水位异常`}
                tone={knowledgeTone}
                onClick={() => openPanel("knowledge")}
              />
            </div>
          </section>

          <DiagnosticDetailDialog
            panel={activePanel}
            onOpenChange={(open) => {
              if (!open) setActivePanel(null);
            }}
            data={data}
            issues={issues}
            showAllPipelines={showAllPipelines}
            onShowAllPipelinesChange={setShowAllPipelines}
          />

          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Activity className="size-3.5" />
            普通刷新只读取配置、心跳与持久化状态；「全面检测」才会调用模型上游和
            MCP
          </p>
        </>
      )}
    </div>
  );
}
