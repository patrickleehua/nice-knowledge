"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpDown,
  Boxes,
  CheckCircle2,
  ChevronDown,
  Eye,
  Globe,
  Lightbulb,
  Plug,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Trash2,
  Type,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, ErrorState, PageHeader, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
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
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type {
  LlmProviderDto,
  ModelCapability,
  ModelCapabilitySource,
  ProviderConnectivityDto,
  ProviderModelCatalogItem,
  ProviderModelMetadata,
} from "@/lib/types";

// 与模型路由页共用的协议色点
const PROTOCOL_DOT: Record<string, string> = {
  openai: "bg-teal",
  anthropic: "bg-warning",
};
const PROTOCOL_LABEL: Record<string, string> = {
  openai: "OpenAI 协议（Responses）",
  anthropic: "Anthropic 协议（Messages）",
};

// 模型类型 chip：标签、图标与色值对齐 Cherry Studio 的 model tag 调色板
// （视觉 #00b96b / 联网 #1677ff / 推理 #6372bd / 工具 #f18737 /
//   重排 #6495ED / 嵌入 #FFA500）。generation 是本平台独有的承重能力，
// Cherry 把「文本」当主类型不放在这排，这里补一个青色 chip。
const CAPABILITY_ITEMS: Array<{
  value: ModelCapability;
  label: string;
  icon: typeof Eye;
  color: string;
}> = [
  { value: "generation", label: "生成", icon: Type, color: "#13c2c2" },
  { value: "vision", label: "视觉", icon: Eye, color: "#00b96b" },
  { value: "web_search", label: "联网", icon: Globe, color: "#1677ff" },
  { value: "reasoning", label: "推理", icon: Lightbulb, color: "#6372bd" },
  { value: "function_call", label: "工具", icon: Wrench, color: "#f18737" },
  { value: "rerank", label: "重排", icon: ArrowUpDown, color: "#6495ED" },
  { value: "embedding", label: "嵌入", icon: Boxes, color: "#FFA500" },
];

// 未选中统一走灰色，与 Cherry 的 CustomTag inactive 一致
const CAPABILITY_INACTIVE_COLOR = "#aaaaaa";

// 后端按模型 ID 推导 vendor slug，这里只负责显示名；未收录的 slug 直接原样展示
const VENDOR_LABEL: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
  alibaba: "阿里通义",
  deepseek: "DeepSeek",
  zhipu: "智谱 GLM",
  bytedance: "字节豆包",
  moonshot: "月之暗面 Kimi",
  tencent: "腾讯混元",
  minimax: "MiniMax",
  stepfun: "阶跃星辰",
  baidu: "百度文心",
  xai: "xAI Grok",
  mistral: "Mistral",
  meta: "Meta Llama",
  microsoft: "Microsoft",
  nvidia: "NVIDIA",
  cohere: "Cohere",
  perplexity: "Perplexity",
  baai: "智源 BAAI",
  jina: "Jina AI",
  voyage: "Voyage AI",
  youdao: "网易有道",
  nomic: "Nomic",
  "01ai": "零一万物",
  baichuan: "百川智能",
  iflytek: "科大讯飞",
  sensetime: "商汤",
  "shanghai-ai-lab": "上海 AI Lab",
  upstage: "Upstage",
  xiaomi: "小米",
  ant: "蚂蚁百灵",
  meituan: "美团",
  skywork: "昆仑天工",
  "black-forest-labs": "Black Forest Labs",
  stability: "Stability AI",
};
const UNKNOWN_VENDOR = "__unknown__";

function vendorLabel(vendor: string): string {
  if (vendor === UNKNOWN_VENDOR) return "未识别厂商";
  return VENDOR_LABEL[vendor] ?? vendor;
}

// 后端只回脱敏错误码（形如 http_error;status=401），这里翻成可行动的中文
const CONNECTIVITY_ERROR_LABEL: Record<string, string> = {
  api_key_missing: "未配置 API 密钥",
  base_url_missing: "未配置 Base URL",
  model_not_in_inventory: "模型不在清单中",
  capability_unclassified: "能力未分类，先标注能力再测试",
  timeout: "请求超时",
  connection_error: "网络不可达",
  rate_limit: "被上游限流",
  http_error: "上游返回错误",
  provider_error: "上游返回异常",
  empty_output: "上游返回空结果",
  schema_error: "上游响应格式异常",
  stream_error: "流式响应中断",
  stream_incomplete: "流式响应不完整",
};

function connectivityErrorLabel(code: string): string {
  const [head, ...rest] = code.split(";");
  const label = CONNECTIVITY_ERROR_LABEL[head] ?? head;
  const status = rest
    .find((part) => part.startsWith("status="))
    ?.slice("status=".length);
  return status ? `${label}（HTTP ${status}）` : label;
}

function ConnectivityBadge({ result }: { result: ProviderConnectivityDto }) {
  const latency =
    result.latency_ms === null ? null : `${Math.round(result.latency_ms)}ms`;
  if (result.ok) {
    return (
      <span className="flex items-center gap-1 text-xs text-success">
        <CheckCircle2 className="size-3.5 shrink-0" />
        连通
        {latency ? ` · ${latency}` : ""}
        {result.scope === "instance" && result.model_count !== null
          ? ` · 目录 ${result.model_count} 个`
          : ""}
        {result.scope === "model" && result.probed_capability
          ? ` · 按${
              CAPABILITY_ITEMS.find(
                (item) => item.value === result.probed_capability,
              )?.label ?? result.probed_capability
            }探测`
          : ""}
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-xs text-destructive">
      <XCircle className="size-3.5 shrink-0" />
      {result.error_code
        ? connectivityErrorLabel(result.error_code)
        : "连通性检测失败"}
      {latency ? ` · ${latency}` : ""}
    </span>
  );
}

const CAPABILITY_SOURCE_LABEL: Record<ModelCapabilitySource, string> = {
  manual: "人工标注",
  provider: "Provider 声明",
  registry: "平台注册表",
  unclassified: "未分类",
};

const CAPABILITY_ORDER = new Map<ModelCapability, number>(
  CAPABILITY_ITEMS.map((item, index) => [item.value, index]),
);

function metadataFor(
  provider: LlmProviderDto,
  model: string,
): ProviderModelMetadata {
  return (
    provider.model_metadata?.[model] ?? {
      capabilities: [],
      input_modalities: [],
      capability_source: "unclassified",
      registry_revision: null,
      provider_metadata: {},
    }
  );
}

function ModelCapabilityEditor({
  model,
  metadata,
  saving,
  testing,
  testResult,
  onSave,
  onRemove,
  onTest,
  onReset,
}: {
  model: string;
  metadata: ProviderModelMetadata;
  saving: boolean;
  testing: boolean;
  testResult?: ProviderConnectivityDto;
  onSave: (capabilities: ModelCapability[]) => void;
  onRemove: () => void;
  onTest: () => void;
  onReset: () => void;
}) {
  const [capabilities, setCapabilities] = useState<ModelCapability[]>(
    metadata.capabilities,
  );
  const dirty =
    capabilities.length !== metadata.capabilities.length ||
    capabilities.some(
      (capability, index) => capability !== metadata.capabilities[index],
    );
  const sourceTone =
    metadata.capability_source === "unclassified"
      ? "warning"
      : metadata.capability_source === "manual"
        ? "primary"
        : "muted";

  function toggle(capability: ModelCapability) {
    setCapabilities((current) =>
      current.includes(capability)
        ? current.filter((item) => item !== capability)
        : [...current, capability].sort(
            (left, right) =>
              (CAPABILITY_ORDER.get(left) ?? 99) -
              (CAPABILITY_ORDER.get(right) ?? 99),
          ),
    );
  }

  return (
    <div
      className={cn(
        "rounded-lg border p-3",
        metadata.capability_source === "unclassified"
          ? "border-warning/40 bg-warning/5"
          : "border-border bg-muted/20",
      )}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-mono text-xs font-medium">{model}</span>
            <ToneBadge tone={sourceTone}>
              {CAPABILITY_SOURCE_LABEL[metadata.capability_source]}
            </ToneBadge>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            输入模态：
            {metadata.input_modalities.length
              ? metadata.input_modalities.join(" + ")
              : "未声明"}
            {metadata.registry_revision
              ? ` · 注册表 ${metadata.registry_revision}`
              : ""}
          </div>
          {metadata.capability_source === "unclassified" && (
            <div className="mt-1 flex items-center gap-1 text-xs text-warning">
              <AlertTriangle className="size-3.5 shrink-0" />
              网关未提供可信能力，完成人工标注前不会进入能力敏感的模型选择器。
            </div>
          )}
        </div>
        <button
          type="button"
          aria-label={`移除 ${model}`}
          className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          onClick={onRemove}
        >
          <X className="size-3.5" />
        </button>
      </div>

      <div className="mt-3">
        <div className="mb-2 flex items-center gap-1.5">
          <span className="text-xs text-muted-foreground">模型类型</span>
          {metadata.capability_source === "unclassified" && (
            <AlertTriangle
              className="size-3.5 shrink-0 text-warning"
              aria-label="未识别模型类型"
            />
          )}
          {/* 重置 = 清除人工标注回到自动识别；没有人工标注时无从可重置 */}
          <button
            type="button"
            aria-label={`重置 ${model} 为自动识别`}
            title="清除人工标注，回到自动识别"
            className={cn(
              "ml-auto rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground",
              metadata.capability_source !== "manual" && "invisible",
            )}
            tabIndex={metadata.capability_source === "manual" ? undefined : -1}
            disabled={saving}
            onClick={onReset}
          >
            <RotateCcw className="size-3.5" />
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {CAPABILITY_ITEMS.map((item) => {
            const selected = capabilities.includes(item.value);
            const color = selected ? item.color : CAPABILITY_INACTIVE_COLOR;
            const Icon = item.icon;
            return (
              <button
                key={item.value}
                type="button"
                role="switch"
                aria-checked={selected}
                aria-label={`${model} ${item.label}能力`}
                disabled={saving}
                className="inline-flex cursor-pointer items-center gap-1 rounded-full px-2 py-1 text-xs leading-none whitespace-nowrap transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-50"
                style={{ color, backgroundColor: `${color}20` }}
                onClick={() => toggle(item.value)}
              >
                <Icon className="size-3" />
                {item.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="ml-auto h-7"
          disabled={testing || metadata.capability_source === "unclassified"}
          title={
            metadata.capability_source === "unclassified"
              ? "未分类模型无法确定调用形态，先标注能力"
              : "按已标注能力发一次最小真实请求"
          }
          onClick={onTest}
        >
          <Plug className={cn("size-3.5", testing && "animate-pulse")} />
          {testing ? "测试中" : "测试"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7"
          disabled={!dirty || saving}
          onClick={() => onSave(capabilities)}
        >
          <Save className="size-3.5" />
          保存能力
        </Button>
      </div>
      {testResult && (
        <div className="mt-2">
          <ConnectivityBadge result={testResult} />
        </div>
      )}
    </div>
  );
}

function ModelImportDialog({
  provider,
  models,
  importing,
  onClose,
  onImport,
}: {
  provider: LlmProviderDto;
  models: ProviderModelCatalogItem[];
  importing: boolean;
  onClose: () => void;
  onImport: (models: string[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [capability, setCapability] = useState<ModelCapability | "all">("all");
  const [selected, setSelected] = useState<string[]>([]);
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const existing = new Set(provider.models);
  const filtered = models.filter((item) => {
    const matchesSearch = item.model
      .toLowerCase()
      .includes(search.trim().toLowerCase());
    const matchesCapability =
      capability === "all" || item.capabilities.includes(capability);
    return matchesSearch && matchesCapability;
  });
  const selectable = filtered.filter((item) => !existing.has(item.model));
  const allVisibleSelected =
    selectable.length > 0 &&
    selectable.every((item) => selected.includes(item.model));

  // 未识别厂商永远排在最后，其余按显示名排序，避免网关顺序抖动导致分组跳位
  const groups = [
    ...filtered
      .reduce((acc, item) => {
        const key = item.vendor ?? UNKNOWN_VENDOR;
        (acc.get(key) ?? acc.set(key, []).get(key)!).push(item);
        return acc;
      }, new Map<string, ProviderModelCatalogItem[]>())
      .entries(),
  ].sort(([left], [right]) => {
    if (left === UNKNOWN_VENDOR) return 1;
    if (right === UNKNOWN_VENDOR) return -1;
    return vendorLabel(left).localeCompare(vendorLabel(right), "zh-Hans-CN");
  });

  function toggle(model: string, checked: boolean) {
    setSelected((current) =>
      checked
        ? [...new Set([...current, model])]
        : current.filter((item) => item !== model),
    );
  }

  function toggleGroup(items: ProviderModelCatalogItem[], checked: boolean) {
    const affected = new Set(
      items.filter((item) => !existing.has(item.model)).map((item) => item.model),
    );
    setSelected((current) =>
      checked
        ? [...new Set([...current, ...affected])]
        : current.filter((item) => !affected.has(item)),
    );
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[82vh] max-w-2xl flex-col">
        <DialogHeader>
          <DialogTitle>
            从 {provider.name} 选择模型
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              发现 {models.length} 个
            </span>
          </DialogTitle>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-56 flex-1">
            <Search className="absolute left-2.5 top-2.5 size-3.5 text-muted-foreground" />
            <Input
              className="pl-8"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索模型 ID"
            />
          </div>
          <Select
            value={capability}
            onValueChange={(value) =>
              setCapability((value ?? "all") as ModelCapability | "all")
            }
          >
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部类型</SelectItem>
              {CAPABILITY_ITEMS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            type="button"
            variant="outline"
            disabled={!selectable.length}
            onClick={() => {
              const visible = new Set(selectable.map((item) => item.model));
              setSelected((current) =>
                allVisibleSelected
                  ? current.filter((item) => !visible.has(item))
                  : [...new Set([...current, ...visible])],
              );
            }}
          >
            {allVisibleSelected ? "取消全选" : "全选当前"}
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border">
          {groups.length ? (
            <div className="divide-y divide-border">
              {groups.map(([vendor, items]) => {
                const isCollapsed = collapsed.includes(vendor);
                const groupSelectable = items.filter(
                  (item) => !existing.has(item.model),
                );
                const groupSelected = groupSelectable.filter((item) =>
                  selected.includes(item.model),
                ).length;
                const groupAllSelected =
                  groupSelectable.length > 0 &&
                  groupSelected === groupSelectable.length;
                return (
                  <div key={vendor}>
                    <div className="flex items-center gap-2 bg-muted/40 px-3 py-2">
                      <button
                        type="button"
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                        onClick={() =>
                          setCollapsed((current) =>
                            isCollapsed
                              ? current.filter((item) => item !== vendor)
                              : [...current, vendor],
                          )
                        }
                        aria-expanded={!isCollapsed}
                      >
                        <ChevronDown
                          className={cn(
                            "size-3.5 shrink-0 text-muted-foreground transition-transform",
                            isCollapsed && "-rotate-90",
                          )}
                        />
                        <span className="truncate text-xs font-medium">
                          {vendorLabel(vendor)}
                        </span>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {groupSelected
                            ? `已选 ${groupSelected} / ${items.length}`
                            : `${items.length} 个`}
                        </span>
                      </button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-6 shrink-0 px-2 text-xs"
                        disabled={!groupSelectable.length || importing}
                        onClick={() => toggleGroup(items, !groupAllSelected)}
                      >
                        {groupAllSelected ? "取消整组" : "选中整组"}
                      </Button>
                    </div>
                    {!isCollapsed && (
                      <div className="divide-y divide-border">
                        {items.map((item) => {
                          const isExisting = existing.has(item.model);
                          const ownedBy = item.provider_metadata.owned_by;
                          return (
                            <div
                              key={item.model}
                              className={cn(
                                "flex items-start gap-3 px-3 py-2.5",
                                isExisting
                                  ? "bg-muted/30 opacity-60"
                                  : "hover:bg-muted/30",
                              )}
                            >
                              <Checkbox
                                className="mt-0.5"
                                checked={isExisting || selected.includes(item.model)}
                                disabled={isExisting || importing}
                                onCheckedChange={(checked) =>
                                  toggle(item.model, checked)
                                }
                                aria-label={`选择 ${item.model}`}
                              />
                              <span className="min-w-0 flex-1">
                                <span className="block truncate font-mono text-xs font-medium">
                                  {item.model}
                                </span>
                                <span className="mt-1 flex flex-wrap items-center gap-1.5">
                                  {item.capabilities.length ? (
                                    item.capabilities.map((value) => (
                                      <ToneBadge key={value} tone="muted">
                                        {CAPABILITY_ITEMS.find(
                                          (entry) => entry.value === value,
                                        )?.label ?? value}
                                      </ToneBadge>
                                    ))
                                  ) : (
                                    <ToneBadge tone="warning">未分类</ToneBadge>
                                  )}
                                  <span className="text-xs text-muted-foreground">
                                    {CAPABILITY_SOURCE_LABEL[item.capability_source]}
                                  </span>
                                  {ownedBy ? (
                                    <span className="text-xs text-muted-foreground">
                                      · 网关归属 {ownedBy}
                                    </span>
                                  ) : null}
                                </span>
                              </span>
                              {isExisting && (
                                <span className="shrink-0 text-xs text-muted-foreground">
                                  已导入
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-sm text-muted-foreground">
              没有符合筛选条件的模型
            </div>
          )}
        </div>
        <DialogFooter>
          <span className="mr-auto self-center text-xs text-muted-foreground">
            已选择 {selected.length} 个；只有确认后才会写入模型列表
          </span>
          <Button type="button" variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            type="button"
            disabled={!selected.length || importing}
            onClick={() => onImport(selected)}
          >
            导入所选模型
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface Draft {
  protocol: "openai" | "anthropic";
  api_key: string;
  base_url: string;
}

function ProviderDetail({
  provider,
  saving,
  importing,
  onSave,
  onToggle,
  onDelete,
  onImport,
  onModels,
  onCapabilities,
  onResetCapabilities,
  capabilitySavingModel,
  instanceTesting,
  instanceTestResult,
  onTestInstance,
  modelTestingModel,
  modelTestResults,
  onTestModel,
}: {
  provider: LlmProviderDto;
  saving: boolean;
  importing: boolean;
  onSave: (draft: Draft) => void;
  onToggle: (enabled: boolean) => void;
  onDelete: () => Promise<unknown>;
  onImport: () => void;
  onModels: (models: string[]) => void;
  onCapabilities: (model: string, capabilities: ModelCapability[]) => void;
  onResetCapabilities: (model: string) => void;
  capabilitySavingModel: string | null;
  instanceTesting: boolean;
  instanceTestResult?: ProviderConnectivityDto;
  onTestInstance: () => void;
  modelTestingModel: string | null;
  modelTestResults: Record<string, ProviderConnectivityDto>;
  onTestModel: (model: string) => void;
}) {
  const [newModel, setNewModel] = useState("");
  const [draft, setDraft] = useState<Draft>({
    protocol: provider.protocol,
    api_key: provider.api_key,
    base_url: provider.base_url,
  });
  // 切换选中实例时重置草稿(render 期调整)
  const [seen, setSeen] = useState(provider);
  if (seen !== provider) {
    setSeen(provider);
    setDraft({
      protocol: provider.protocol,
      api_key: provider.api_key,
      base_url: provider.base_url,
    });
  }
  const dirty =
    draft.protocol !== provider.protocol ||
    draft.api_key !== provider.api_key ||
    draft.base_url !== provider.base_url;
  const unclassifiedCount = provider.models.filter(
    (model) => metadataFor(provider, model).capability_source === "unclassified",
  ).length;

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-mono text-sm font-medium">
              {provider.name}
            </span>
            <ToneBadge tone={provider.enabled ? "success" : "muted"}>
              {provider.enabled ? "启用" : "停用"}
            </ToneBadge>
            {provider.is_builtin && <ToneBadge tone="muted">内置</ToneBadge>}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {PROTOCOL_LABEL[provider.protocol]}
            {provider.is_builtin && "，凭证留空时回落后端 .env"}
          </div>
        </div>
        {!provider.is_builtin && (
          <ConfirmDialog
            trigger={
              <Button size="icon" variant="ghost" aria-label="删除实例">
                <Trash2 className="size-4 text-destructive" />
              </Button>
            }
            title={`删除提供商「${provider.name}」？`}
            description="被模型路由引用时无法删除；删除后不可恢复。"
            confirmLabel="删除"
            destructive
            onConfirm={onDelete}
          />
        )}
      </div>

      <div className="flex-1 space-y-0 overflow-y-auto">
        <div className="divide-y divide-border">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3.5">
            <div className="min-w-0 flex-1 text-sm">接入协议</div>
            <div className="w-72 shrink-0">
              <Select
                value={draft.protocol}
                onValueChange={(value) =>
                  setDraft((prev) => ({
                    ...prev,
                    protocol: (value ?? prev.protocol) as Draft["protocol"],
                  }))
                }
              >
                <SelectTrigger
                  className="w-full"
                  disabled={provider.is_builtin}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="openai">OpenAI 协议</SelectItem>
                  <SelectItem value="anthropic">Anthropic 协议</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-sm">
                API 密钥
                <ToneBadge tone={provider.has_api_key ? "success" : "muted"}>
                  {provider.has_api_key ? "已配置" : "未配置"}
                </ToneBadge>
              </div>
              {/* 读出面永远是掩码,把它原样保存回去 = 不修改密钥(见 lib/admin-types.ts) */}
              <div className="mt-0.5 text-xs text-muted-foreground">
                {provider.is_builtin
                  ? "留空使用 .env 配置；已配置时显示为掩码,保持原样即不修改"
                  : "已配置时显示为掩码,保持原样即不修改；清空则删除密钥"}
              </div>
            </div>
            <Input
              className="w-72 shrink-0"
              type="password"
              placeholder={provider.is_builtin ? "留空使用 .env 配置" : "sk-…"}
              value={draft.api_key}
              onChange={(event) =>
                setDraft((prev) => ({ ...prev, api_key: event.target.value }))
              }
            />
          </div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="text-sm">Base URL</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                {draft.protocol === "openai"
                  ? "OpenAI 兼容网关形如 http://host:port/v1；留空用官方端点"
                  : "兼容网关形如 http://host:port（不带 /v1）；留空用官方端点"}
              </div>
            </div>
            <Input
              className="w-72 shrink-0"
              placeholder={
                draft.protocol === "openai" ? "http://host:port/v1" : "http://host:port"
              }
              value={draft.base_url}
              onChange={(event) =>
                setDraft((prev) => ({ ...prev, base_url: event.target.value }))
              }
            />
          </div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="text-sm">连通性</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                拉取 /v1/models 验证凭证与网络；单个模型是否可用需在下方逐项测试
              </div>
              {instanceTestResult && (
                <div className="mt-1.5">
                  <ConnectivityBadge result={instanceTestResult} />
                </div>
              )}
            </div>
            <Button
              variant="outline"
              size="sm"
              className="w-72 shrink-0"
              disabled={instanceTesting}
              onClick={onTestInstance}
            >
              <Plug className={cn("size-3.5", instanceTesting && "animate-pulse")} />
              {instanceTesting ? "测试中…" : "测试连通性"}
            </Button>
          </div>
          <div className="flex items-center justify-between px-4 py-3.5">
            <div>
              <div className="text-sm">启用此提供商</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                停用后路由解析与模型候选都会跳过它
              </div>
            </div>
            <Switch
              checked={provider.enabled}
              disabled={saving}
              onCheckedChange={(checked) => onToggle(checked)}
            />
          </div>
        </div>

        <div className="border-t border-border px-4 py-3.5">
          <div className="mb-2 flex items-center gap-2">
            <div className="text-sm">
              模型列表
              <span className="ml-2 text-xs text-muted-foreground">
                路由表单候选，共 {provider.models.length} 个
              </span>
            </div>
            {unclassifiedCount > 0 && (
              <ToneBadge tone="warning">{unclassifiedCount} 个未分类</ToneBadge>
            )}
            <Button
              variant="outline"
              size="sm"
              className="ml-auto h-7"
              disabled={importing}
              onClick={onImport}
            >
              <RefreshCw className={cn("size-3.5", importing && "animate-spin")} />
              从网关导入
            </Button>
          </div>
          {unclassifiedCount > 0 && (
            <div className="mb-3 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-2.5 text-xs text-warning">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>
                网关导入后有 {unclassifiedCount} 个模型无法可信识别能力。请逐项标注；
                未分类模型不会出现在图片描述等能力敏感的业务选择器中。
              </span>
            </div>
          )}
          <div className="mb-3 flex items-center gap-2">
            <Input
              className="h-8 max-w-72 font-mono text-xs"
              placeholder="手动添加模型名，回车确认"
              value={newModel}
              onChange={(event) => setNewModel(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                event.preventDefault();
                const model = newModel.trim();
                if (!model || provider.models.includes(model)) return;
                onModels([...provider.models, model].sort());
                setNewModel("");
              }}
            />
            <Button
              variant="outline"
              size="sm"
              className="h-8"
              disabled={!newModel.trim() || provider.models.includes(newModel.trim())}
              onClick={() => {
                onModels([...provider.models, newModel.trim()].sort());
                setNewModel("");
              }}
            >
              <Plus className="size-3.5" />
              添加
            </Button>
          </div>
          {provider.models.length ? (
            <div className="space-y-2">
              {provider.models.map((model) => {
                const metadata = metadataFor(provider, model);
                return (
                  <ModelCapabilityEditor
                    key={`${model}:${metadata.capability_source}:${metadata.capabilities.join(",")}`}
                    model={model}
                    metadata={metadata}
                    saving={capabilitySavingModel === model}
                    testing={modelTestingModel === model}
                    testResult={modelTestResults[model]}
                    onSave={(capabilities) =>
                      onCapabilities(model, capabilities)
                    }
                    onRemove={() =>
                      onModels(provider.models.filter((item) => item !== model))
                    }
                    onTest={() => onTestModel(model)}
                    onReset={() => onResetCapabilities(model)}
                  />
                );
              })}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">
              还没有模型：点「从网关导入」拉取 /v1/models，网关不支持时手动添加
            </div>
          )}
        </div>
      </div>

      {dirty && (
        <div className="flex justify-end border-t border-border px-4 py-3">
          <Button size="sm" disabled={saving} onClick={() => onSave(draft)}>
            <Save className="size-3.5" />
            保存变更
          </Button>
        </div>
      )}
    </div>
  );
}

export default function AdminProvidersPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string>();
  const [adding, setAdding] = useState(false);
  const [importPreview, setImportPreview] = useState<{
    provider: LlmProviderDto;
    models: ProviderModelCatalogItem[];
  } | null>(null);
  const [addForm, setAddForm] = useState({
    name: "",
    protocol: "openai" as Draft["protocol"],
    api_key: "",
    base_url: "",
  });

  const providers = useQuery({
    queryKey: ["admin-providers"],
    queryFn: () => api.get<LlmProviderDto[]>("/admin/providers"),
  });

  const selected =
    providers.data?.find((row) => row.id === selectedId) ?? providers.data?.[0];

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["admin-providers"] });
  }

  const discoverModels = useMutation({
    mutationFn: (provider: LlmProviderDto) =>
      api
        .get<ProviderModelCatalogItem[]>(
          `/admin/providers/${provider.id}/discover-models`,
        )
        .then((models) => ({ provider, models })),
    onSuccess: setImportPreview,
    onError: (error) =>
      toast.error(errMsg(error, "读取模型目录失败，可改为手动添加模型")),
  });

  const importModels = useMutation({
    mutationFn: ({ id, models }: { id: string; models: string[] }) =>
      api.post<LlmProviderDto>(`/admin/providers/${id}/import-models`, {
        models,
      }),
    onSuccess: (row, variables) => {
      const imported = variables.models.length;
      toast.success(`已导入 ${imported} 个模型，当前共 ${row.models.length} 个`);
      const selectedModels = new Set(variables.models);
      const unclassifiedCount = row.models.filter(
        (model) =>
          selectedModels.has(model) &&
          metadataFor(row, model).capability_source === "unclassified",
      ).length;
      if (unclassifiedCount > 0) {
        toast.warning(
          `${unclassifiedCount} 个模型未能识别能力，请完成人工标注后再用于业务`,
        );
      }
      setImportPreview(null);
      invalidate();
    },
    onError: (error) =>
      toast.error(errMsg(error, "导入失败，可改为手动添加模型")),
  });

  // 连通性结果按实例/模型分别留存,切实例后仍能看到上次结论;
  // 后端只回脱敏错误码,因此这里直接存整份响应。
  const [instanceTests, setInstanceTests] = useState<
    Record<string, ProviderConnectivityDto>
  >({});
  const [modelTests, setModelTests] = useState<
    Record<string, Record<string, ProviderConnectivityDto>>
  >({});

  const testInstance = useMutation({
    mutationFn: (id: string) =>
      api.post<ProviderConnectivityDto>(
        `/admin/providers/${id}/test-connection`,
        {},
      ),
    onSuccess: (result, id) => {
      setInstanceTests((current) => ({ ...current, [id]: result }));
      if (result.ok) {
        toast.success(`连通，目录 ${result.model_count ?? 0} 个模型`);
      } else {
        toast.error(
          result.error_code
            ? connectivityErrorLabel(result.error_code)
            : "连通性检测失败",
        );
      }
    },
    onError: (error) => toast.error(errMsg(error, "连通性检测失败")),
  });

  const testModel = useMutation({
    mutationFn: ({ id, model }: { id: string; model: string }) =>
      api.post<ProviderConnectivityDto>(`/admin/providers/${id}/test-model`, {
        model,
      }),
    onSuccess: (result, variables) => {
      setModelTests((current) => ({
        ...current,
        [variables.id]: {
          ...(current[variables.id] ?? {}),
          [variables.model]: result,
        },
      }));
      if (!result.ok) {
        toast.error(
          result.error_code
            ? `${variables.model}：${connectivityErrorLabel(result.error_code)}`
            : `${variables.model} 检测失败`,
        );
      }
    },
    onError: (error) => toast.error(errMsg(error, "模型检测失败")),
  });

  const create = useMutation({
    mutationFn: () => api.post<LlmProviderDto>("/admin/providers", addForm),
    onSuccess: (row) => {
      toast.success("提供商已添加");
      setAdding(false);
      setAddForm({ name: "", protocol: "openai", api_key: "", base_url: "" });
      setSelectedId(row.id);
      invalidate();
    },
    onError: (error) => toast.error(errMsg(error, "添加失败")),
  });
  const patch = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Partial<Draft> & { enabled?: boolean; models?: string[] };
    }) => api.patch<LlmProviderDto>(`/admin/providers/${id}`, body),
    onSuccess: () => {
      toast.success("已保存，立即生效");
      invalidate();
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });
  // mode=auto 清除人工标注(后端回到 registry/未分类),manual 写入本次勾选
  const updateCapabilities = useMutation({
    mutationFn: ({
      id,
      model,
      capabilities,
      mode = "manual",
    }: {
      id: string;
      model: string;
      capabilities?: ModelCapability[];
      mode?: "manual" | "auto";
    }) =>
      api.put<ProviderModelCatalogItem>(
        `/admin/providers/${id}/model-capabilities`,
        mode === "auto" ? { model, mode } : { model, capabilities },
      ),
    onSuccess: (row, variables) => {
      toast.success(
        variables.mode === "auto"
          ? `${row.model} 已清除人工标注，回到自动识别`
          : `${row.model} 能力已人工确认；能力来源已更新为「人工标注」`,
      );
      invalidate();
    },
    onError: (error) => toast.error(errMsg(error, "模型能力保存失败")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/providers/${id}`),
    onSuccess: () => {
      toast.success("提供商已删除");
      invalidate();
    },
    onError: (error) => toast.error(errMsg(error, "删除失败")),
  });

  const canCreate = /^[a-z0-9][a-z0-9_-]*$/.test(addForm.name);

  return (
    <div className="flex h-[calc(100vh-6.5rem)] flex-col gap-4">
      <PageHeader
        title="模型提供商"
        description="任意添加多个提供商实例，模型路由的降级链可跨提供商混用；内置实例凭证留空回落 .env"
        actions={
          <Button onClick={() => setAdding(true)}>
            <Plus className="size-4" />
            添加提供商
          </Button>
        }
      />

      {providers.error ? (
        <ErrorState error={providers.error} onRetry={() => providers.refetch()} />
      ) : providers.isPending ? (
        <div className="flex flex-1 gap-4">
          <Skeleton className="w-64 rounded-lg" />
          <Skeleton className="flex-1 rounded-lg" />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
          <aside className="flex w-64 shrink-0 flex-col border-r border-border">
            <div className="flex-1 space-y-1 overflow-y-auto p-2">
              {providers.data?.map((row) => (
                <div
                  key={row.id}
                  className={cn(
                    "group flex items-center gap-2.5 rounded-md border border-transparent px-3 py-2.5",
                    row.id === selected?.id
                      ? "border-border bg-accent"
                      : "hover:bg-accent/50",
                  )}
                >
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
                    onClick={() => setSelectedId(row.id)}
                  >
                    <span
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        row.enabled
                          ? (PROTOCOL_DOT[row.protocol] ?? "bg-muted-foreground")
                          : "bg-muted-foreground/40",
                      )}
                    />
                    <span className="min-w-0">
                      <span className="block truncate font-mono text-sm">
                        {row.name}
                      </span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {row.protocol}
                        {row.is_builtin ? " · 内置" : ""}
                      </span>
                    </span>
                  </button>
                  <Switch
                    checked={row.enabled}
                    disabled={patch.isPending}
                    onCheckedChange={(checked) =>
                      patch.mutate({ id: row.id, body: { enabled: checked } })
                    }
                    aria-label={`启用 ${row.name}`}
                  />
                </div>
              ))}
            </div>
          </aside>
          {selected ? (
            <ProviderDetail
              key={selected.id}
              provider={selected}
              saving={patch.isPending}
              importing={discoverModels.isPending || importModels.isPending}
              onSave={(draft) => patch.mutate({ id: selected.id, body: draft })}
              onToggle={(enabled) =>
                patch.mutate({ id: selected.id, body: { enabled } })
              }
              onDelete={() => remove.mutateAsync(selected.id)}
              onImport={() => discoverModels.mutate(selected)}
              onModels={(models) =>
                patch.mutate({ id: selected.id, body: { models } })
              }
              onResetCapabilities={(model) =>
                updateCapabilities.mutate({
                  id: selected.id,
                  model,
                  mode: "auto",
                })
              }
              onCapabilities={(model, capabilities) =>
                updateCapabilities.mutate({
                  id: selected.id,
                  model,
                  capabilities,
                })
              }
              capabilitySavingModel={
                updateCapabilities.isPending
                  ? (updateCapabilities.variables?.model ?? null)
                  : null
              }
              instanceTesting={
                testInstance.isPending && testInstance.variables === selected.id
              }
              instanceTestResult={instanceTests[selected.id]}
              onTestInstance={() => testInstance.mutate(selected.id)}
              modelTestingModel={
                testModel.isPending && testModel.variables?.id === selected.id
                  ? (testModel.variables?.model ?? null)
                  : null
              }
              modelTestResults={modelTests[selected.id] ?? {}}
              onTestModel={(model) =>
                testModel.mutate({ id: selected.id, model })
              }
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-center">
              <Boxes className="size-6 text-muted-foreground" />
              <div className="mt-2 text-sm text-muted-foreground">
                还没有提供商实例
              </div>
            </div>
          )}
        </div>
      )}

      {importPreview && (
        <ModelImportDialog
          key={importPreview.provider.id}
          provider={importPreview.provider}
          models={importPreview.models}
          importing={importModels.isPending}
          onClose={() => setImportPreview(null)}
          onImport={(models) =>
            importModels.mutate({ id: importPreview.provider.id, models })
          }
        />
      )}

      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>添加提供商</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <label className="block text-sm">
              实例名（小写标识，路由链按此引用）
              <Input
                className="mt-1 font-mono"
                placeholder="如：openrouter / siliconflow"
                value={addForm.name}
                onChange={(event) =>
                  setAddForm((prev) => ({ ...prev, name: event.target.value }))
                }
              />
            </label>
            <label className="block text-sm">
              接入协议
              <Select
                value={addForm.protocol}
                onValueChange={(value) =>
                  setAddForm((prev) => ({
                    ...prev,
                    protocol: (value ?? prev.protocol) as Draft["protocol"],
                  }))
                }
              >
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="openai">OpenAI 协议（Responses）</SelectItem>
                  <SelectItem value="anthropic">Anthropic 协议（Messages）</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label className="block text-sm">
              API 密钥
              <Input
                className="mt-1"
                type="password"
                value={addForm.api_key}
                onChange={(event) =>
                  setAddForm((prev) => ({ ...prev, api_key: event.target.value }))
                }
              />
            </label>
            <label className="block text-sm">
              Base URL
              <Input
                className="mt-1"
                placeholder={
                  addForm.protocol === "openai"
                    ? "http://host:port/v1"
                    : "http://host:port"
                }
                value={addForm.base_url}
                onChange={(event) =>
                  setAddForm((prev) => ({ ...prev, base_url: event.target.value }))
                }
              />
            </label>
          </div>
          <DialogFooter>
            <Button
              disabled={!canCreate || create.isPending}
              onClick={() => create.mutate()}
            >
              添加
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
