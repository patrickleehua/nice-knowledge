// 知识实体区(KB-4 检索页上区):结构化命中按 kind 分组的紧凑卡片网格。
// 区块高度封顶 23rem 内部独立滚动(llm_wiki 图片区同款手法)。

import {
  Bed,
  BookOpen,
  Boxes,
  Map,
  MapPin,
  Receipt,
  Route,
  type LucideIcon,
} from "lucide-react";
import { LayerBadge } from "@/components/kb/badges";
import {
  SearchCitationDetails,
  SearchScoreDetails,
} from "@/components/kb/search/hit-metadata";
import { ToneBadge } from "@/components/shared";
import type { SearchHit } from "@/lib/types";

// 后端 kind 现值为 route_template(search.py),规格口径为 route,两者归一渲染
const KIND_META: Record<string, { label: string; icon: LucideIcon }> = {
  destination: { label: "目的地", icon: Map },
  poi: { label: "景点", icon: MapPin },
  hotel: { label: "酒店", icon: Bed },
  cost: { label: "成本参考", icon: Receipt },
  route: { label: "成熟线路", icon: Route },
  page: { label: "知识页", icon: BookOpen },
};

const KIND_ORDER = ["destination", "poi", "hotel", "cost", "route", "page"];

function normalizeKind(kind: string): string {
  return kind === "route_template" ? "route" : kind;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function num(v: unknown): number | null {
  return typeof v === "number" ? v : null;
}

/** 各 kind 的关键字段摘要(缺失字段静默跳过,不编造) */
function summary(kind: string, data: Record<string, unknown>): string {
  const parts: string[] = [];
  switch (kind) {
    case "destination":
      if (str(data.country)) parts.push(String(data.country));
      break;
    case "poi":
      if (str(data.city)) parts.push(String(data.city));
      if (str(data.poi_type)) parts.push(String(data.poi_type));
      if (num(data.visit_minutes) !== null)
        parts.push(`${data.visit_minutes} 分钟`);
      if (str(data.ticket_info)) parts.push(String(data.ticket_info));
      break;
    case "hotel":
      if (str(data.city)) parts.push(String(data.city));
      if (str(data.star)) parts.push(String(data.star));
      if (num(data.price_ref) !== null)
        parts.push(
          `参考价 ${data.price_ref} ${str(data.currency) ?? ""}`.trim(),
        );
      break;
    case "cost": {
      if (str(data.category)) parts.push(String(data.category));
      // 后端字段为 unit_cost(search.py),兼容规格口径 unit_price
      const price = num(data.unit_price) ?? num(data.unit_cost);
      if (price !== null)
        parts.push(
          `${price} ${str(data.currency) ?? ""}${str(data.unit) ? ` / ${data.unit}` : ""}`.trim(),
        );
      if (str(data.valid_to)) parts.push(`有效期至 ${data.valid_to}`);
      break;
    }
    case "route":
      if (num(data.days) !== null) parts.push(`${data.days} 天`);
      break;
    case "page":
      if (str(data.page_type)) parts.push(String(data.page_type));
      break;
    default:
      // M3a 自定义类型:attributes 键值对通用摘要(跳过标识与已单独渲染的字段)
      for (const [k, v] of Object.entries(data)) {
        if (parts.length >= 4) break;
        if (GENERIC_SKIP_KEYS.has(k) || v === null || v === undefined || v === "") {
          continue;
        }
        const text = typeof v === "object" ? JSON.stringify(v) : String(v);
        parts.push(`${k}: ${text.length > 32 ? `${text.slice(0, 32)}…` : text}`);
      }
      break;
  }
  return parts.join(" · ");
}

// 通用摘要跳过的字段:标识类 + 标题类 + 已单独渲染的 stale
const GENERIC_SKIP_KEYS = new Set([
  "name",
  "id",
  "entity_type_key",
  "title",
  "stale",
]);

function EntityCard({ hit }: { hit: SearchHit }) {
  const kind = normalizeKind(hit.kind);
  const meta = KIND_META[kind];
  const Icon = meta?.icon ?? Boxes;
  const name = str(hit.data.name) ?? str(hit.data.title) ?? "(未命名)";
  const detail = summary(kind, hit.data);
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3">
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate text-sm font-medium" title={name}>
          {name}
        </span>
        {hit.data.stale === true && (
          <ToneBadge tone="warning" className="ml-auto shrink-0">
            可能过期
          </ToneBadge>
        )}
      </div>
      {detail && (
        <p className="line-clamp-2 text-xs text-muted-foreground">{detail}</p>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-0.5">
        <LayerBadge layer={hit.layer} />
        <div className="ml-auto">
          <SearchScoreDetails hit={hit} />
        </div>
      </div>
      <SearchCitationDetails citation={hit.citation} />
    </div>
  );
}

export function EntityHits({ hits }: { hits: SearchHit[] }) {
  // 未知 kind(M3a 自定义类型)按出现顺序追加在内置分组之后,不再丢弃
  const extraKinds = [
    ...new Set(hits.map((h) => normalizeKind(h.kind))),
  ].filter((k) => !KIND_ORDER.includes(k));
  const groups = [...KIND_ORDER, ...extraKinds]
    .map((kind) => ({
      kind,
      hits: hits.filter((h) => normalizeKind(h.kind) === kind),
    }))
    .filter((g) => g.hits.length > 0);

  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <Boxes className="size-4 text-muted-foreground" />
        知识实体
        <span className="font-mono text-xs font-normal text-muted-foreground">
          {hits.length}
        </span>
      </h2>
      <div className="max-h-[23rem] space-y-4 overflow-y-auto pr-1">
        {groups.map((group) => (
          <div key={group.kind} className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">
              {KIND_META[group.kind]?.label ?? group.kind} · {group.hits.length}
            </p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {group.hits.map((hit) => (
                <EntityCard key={hit.source} hit={hit} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
