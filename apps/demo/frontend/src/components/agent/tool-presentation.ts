// 工具卡片的**纯数据**展示层:摘要文案与展开策略。
//
// 这里刻意不 import React —— 本文件被 node --test 原生加载(解析不了 "@/" 别名,
// 也没有 JSX 运行时)。结果的**渲染**扩展点在 result-renderers.tsx。
//
// SDK 化改造(MIGRATION-PLAN §5.8):TF 版本在这里硬编码了 BUSINESS_KINDS 与
// 逐个旅游工具的文案。SDK 不认识宿主的业务工具,因此改为:
//   1. 内置 17 个通用工具各自的摘要器(它们的输出结构由 SDK 定义,可以写死);
//   2. `registerToolSummarizer()` 让宿主为自己的工具追加摘要器;
//   3. 兜底走通用 JSON 摘要(items/total/status/键数),永不返回空白。

export type ToolStatus = "running" | "waiting" | "ok" | "failed";

export type UnknownRecord = Record<string, unknown>;

export function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function readRecord(
  value: unknown,
  key?: string,
): UnknownRecord | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return isRecord(candidate) ? candidate : undefined;
}

export function readString(value: unknown, key?: string): string | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return typeof candidate === "string" && candidate.trim()
    ? candidate
    : undefined;
}

export function readNumber(value: unknown, key?: string): number | undefined {
  const candidate = key && isRecord(value) ? value[key] : value;
  return typeof candidate === "number" && Number.isFinite(candidate)
    ? candidate
    : undefined;
}

export function readStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function readRecordArray(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function countRecordValues(value: unknown): number {
  if (!isRecord(value)) return 0;
  return Object.values(value).reduce(
    (total: number, item) =>
      total + (typeof item === "number" && item > 0 ? item : 0),
    0,
  );
}

// ---------------------------------------------------------------------------
// 扩展点 1:工具结果摘要器注册表
// ---------------------------------------------------------------------------

/** 返回一行摘要;返回 null / undefined = 交回通用兜底。 */
export type ToolSummarizer = (
  output: UnknownRecord,
) => string | null | undefined;

const summarizers = new Map<string, ToolSummarizer>();

/** 宿主为自己的工具注册摘要器(同名覆盖,便于宿主改写内置文案)。 */
export function registerToolSummarizer(
  name: string,
  summarizer: ToolSummarizer,
): void {
  summarizers.set(name, summarizer);
}

export function registerToolSummarizers(
  entries: Readonly<Record<string, ToolSummarizer>>,
): void {
  for (const [name, summarizer] of Object.entries(entries)) {
    summarizers.set(name, summarizer);
  }
}

/** 测试与热重载用:清空宿主注册项,内置摘要器不受影响。 */
export function resetToolSummarizers(): void {
  summarizers.clear();
}

// ---------------------------------------------------------------------------
// 扩展点 2:默认展开的工具名单
// ---------------------------------------------------------------------------

// 失败一律展开(见 shouldExpandTool);成功时默认折叠,除非宿主显式声明
// "这个工具的结果本身就是答案",比如一个把长文档拉平的自定义工具。
const autoExpand = new Set<string>();

export function registerAutoExpandTools(...names: string[]): void {
  for (const name of names) autoExpand.add(name);
}

export function resetAutoExpandTools(): void {
  autoExpand.clear();
}

export function shouldExpandTool(
  name: string,
  status: ToolStatus,
  output: unknown,
): boolean {
  if (status === "failed") return true;
  return status === "ok" && isRecord(output) && autoExpand.has(name);
}

// ---------------------------------------------------------------------------
// 内置通用工具的摘要(nicekit/agent/builtin_tools.py 的输出结构)
// ---------------------------------------------------------------------------

function builtinSummary(name: string, output: UnknownRecord): string | null {
  // 子 agent 委派:摘要给"派了谁 + 跑了几轮",结论正文在展开区里
  if (name === "Agent") {
    const agent = readString(output, "agent");
    const who = agent ? `${agent} · ` : "";
    if (readString(output, "error")) return `${who}未完成`;
    const turns = readNumber(output, "turns");
    return turns === undefined ? `${who}已完成` : `${who}${turns} 轮完成`;
  }

  if (name === "kb_search") {
    const hits = readRecordArray(output.hits).length;
    return hits ? `${hits} 条命中` : "未命中知识";
  }

  if (name === "kb_image_inspect") {
    return readString(readRecord(output, "inspection"), "description")
      ? "已核验来源原图"
      : "图片核验完成";
  }

  if (name === "web_search") {
    const results = readRecordArray(output.results);
    if (!results.length) return "未找到相关网页";
    const official = results.filter(
      (item) => readString(item, "source_tier") === "official",
    ).length;
    const summary = official
      ? `${results.length} 条来源 · ${official} 官方`
      : `${results.length} 条来源`;
    return output.cached === true ? `缓存 · ${summary}` : summary;
  }

  if (name === "web_fetch") {
    const pages = readRecordArray(output.pages);
    if (!pages.length) return "未读取到网页";
    const succeeded = pages.filter(
      (page) => readString(page, "status") === "ok",
    ).length;
    if (!succeeded) return "网页读取失败";
    const failed = pages.length - succeeded;
    return failed
      ? `已读取 ${succeeded} 个网页 · ${failed} 个失败`
      : `已读取 ${succeeded} 个网页`;
  }

  if (name === "image_generate") {
    const count = Array.isArray(output.images) ? output.images.length : 0;
    if (count) return `${count} 张图片`;
    return output.status === "unavailable" || readString(output, "error")
      ? "图片生成失败"
      : "未生成图片";
  }

  return null;
}

/** 兜底:任何 JSON 输出都能给出一行可读摘要,绝不返回空白。 */
export function genericJsonSummary(output: UnknownRecord): string {
  const items = Array.isArray(output.items) ? output.items.length : undefined;
  if (items !== undefined) return `${items} 项结果`;
  const total = readNumber(output, "total");
  if (total !== undefined) return `${total} 项结果`;
  const count = readNumber(output, "count");
  if (count !== undefined) return `${count} 项结果`;
  const status = readString(output, "status");
  if (status) return status;
  const keys = Object.keys(output);
  if (!keys.length) return "已完成";
  return `${keys.length} 个字段`;
}

export function toolResultSummary(name: string, output: unknown): string {
  if (!isRecord(output)) {
    if (typeof output === "string" && output.trim()) return output.trim();
    return "已完成";
  }
  // 宿主注册优先:宿主既能给自己的工具写摘要,也能改写内置文案
  const custom = summarizers.get(name)?.(output);
  if (custom) return custom;
  return builtinSummary(name, output) ?? genericJsonSummary(output);
}
