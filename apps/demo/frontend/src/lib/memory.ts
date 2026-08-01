// 长期记忆(/org/settings/memory)的 DTO、文案表与纯展示助手。
// 与后端 nicekit/api/v1/memory.py 的 MemoryOut / MemoryPatch 一一对应。
//
// **scope 是自由字符串**:SDK 只内置 `org`,其余由宿主 register_memory_scopes()
// 注册。前端不硬编码词表,一律走 GET /memory/scopes 下发(见 useMemoryVocab),
// 未知 scope 直接回显原值——写死枚举会让宿主一注册新范围就筛不出来。

// 注意:本文件被 node --test 原生加载(memory.test.mjs),不能有值级别的
// "@/" 别名 import —— node 解析不了 tsconfig paths。因此这里只放纯函数与
// 类型,取词表的 hook 放在页面侧。
import type { StatusMeta } from "@/lib/utils";

/** 记忆范围:内置 `org` + 宿主注册的任意字符串。 */
export type MemoryScope = string;

export type MemoryType =
  | "preference"
  | "preference_candidate"
  | "constraint"
  | "decision"
  | "fact"
  | "risk";

export type MemoryStatus = "active" | "superseded" | "rejected";

export interface MemoryOut {
  id: string;
  scope: MemoryScope;
  scope_ref_id: string | null;
  /** 宿主 ScopeLabelResolver 给出的展示名;SDK 默认不解析,为 null。 */
  scope_ref_label: string | null;
  memory_type: MemoryType;
  title: string;
  content: string;
  /** 来源:`memory_extraction:<session_id>` 或 `memory_write:<session_id>`。 */
  source: string;
  source_message_id: string | null;
  confidence: number;
  status: MemoryStatus;
  hit_count: number;
  sightings: number;
  last_hit_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryListOut {
  items: MemoryOut[];
  total: number;
}

/** GET /memory/scopes:筛选下拉的词表,避免前后端各写一份枚举。 */
export interface MemoryVocabOut {
  scopes: string[];
  types: string[];
  statuses: string[];
}

/** 内置 scope 的展示名;宿主注册的自定义 scope 回落原值。 */
const BUILTIN_SCOPE_LABELS: Record<string, string> = {
  org: "全组织通用",
};

export function memoryScopeLabel(scope: string): string {
  return BUILTIN_SCOPE_LABELS[scope] ?? scope;
}

export const MEMORY_TYPE_LABELS: Record<MemoryType, string> = {
  preference: "偏好",
  preference_candidate: "偏好(待确认)",
  constraint: "硬约束",
  decision: "已确认决策",
  fact: "事实",
  risk: "风险",
};

export function memoryTypeLabel(memoryType: string): string {
  return MEMORY_TYPE_LABELS[memoryType as MemoryType] ?? memoryType;
}

// 类型徽章的色调按"这条记忆有多硬"排:约束/风险最该被一眼看见,
// 未提升的候选压成灰色——它还没被证明是稳定的。
export const MEMORY_TYPE_META: Record<MemoryType, StatusMeta> = {
  constraint: { label: MEMORY_TYPE_LABELS.constraint, tone: "destructive" },
  risk: { label: MEMORY_TYPE_LABELS.risk, tone: "warning" },
  decision: { label: MEMORY_TYPE_LABELS.decision, tone: "success" },
  preference: { label: MEMORY_TYPE_LABELS.preference, tone: "primary" },
  fact: { label: MEMORY_TYPE_LABELS.fact, tone: "teal" },
  preference_candidate: {
    label: MEMORY_TYPE_LABELS.preference_candidate,
    tone: "muted",
  },
};

/** 未知类型(后端加了新值而前端未跟进)兜底成中性徽章,不渲染空白。 */
export function memoryTypeMeta(memoryType: string): StatusMeta {
  return (
    MEMORY_TYPE_META[memoryType as MemoryType] ?? {
      label: memoryType,
      tone: "muted",
    }
  );
}

export const MEMORY_STATUS_META: Record<MemoryStatus, StatusMeta> = {
  active: { label: "生效中", tone: "success" },
  superseded: { label: "已被取代", tone: "muted" },
  rejected: { label: "已失效", tone: "destructive" },
};

export function memoryStatusMeta(status: string): StatusMeta {
  return (
    MEMORY_STATUS_META[status as MemoryStatus] ?? {
      label: status,
      tone: "muted",
    }
  );
}

/** 来源前缀 → 中文说明。来源是排查记忆污染时最需要的线索,不能只显示一串 id。 */
export function memorySourceLabel(source: string): string {
  if (source.startsWith("memory_extraction")) return "对话自动沉淀";
  if (source.startsWith("memory_write")) return "助手主动记录";
  return source;
}

/** 来源里的会话 id(可用于回到原始对话);解析不出来时返回 null。 */
export function memorySourceSessionId(source: string): string | null {
  const index = source.indexOf(":");
  if (index < 0) return null;
  const rest = source.slice(index + 1).trim();
  return rest.length > 0 ? rest : null;
}

/** scope 的展示归属:优先宿主解析出的展示名,回落 ref 原值,org 无归属。 */
export function memoryScopeTarget(item: MemoryOut): string {
  if (item.scope === "org") return "—";
  return item.scope_ref_label ?? item.scope_ref_id ?? "—";
}

/**
 * 候选提升进度文案。与后端常量对齐:同一事实被独立观察到 2 次
 * 且置信达 0.7 才提升为正式偏好(见 nicekit/agent/memory.py::should_promote)。
 */
export const PROMOTION_MIN_SIGHTINGS = 2;
export const PROMOTION_MIN_CONFIDENCE = 0.7;

export function promotionHint(item: MemoryOut): string | null {
  if (item.memory_type !== "preference_candidate") return null;
  const needSightings = Math.max(PROMOTION_MIN_SIGHTINGS - item.sightings, 0);
  if (needSightings > 0) {
    return `再被印证 ${needSightings} 次且置信达 ${PROMOTION_MIN_CONFIDENCE} 后提升为正式偏好`;
  }
  if (item.confidence < PROMOTION_MIN_CONFIDENCE) {
    return `置信需达 ${PROMOTION_MIN_CONFIDENCE} 才提升为正式偏好`;
  }
  return "已满足提升条件,下次印证时转正";
}
