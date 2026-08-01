"use client";

/**
 * 工具结果渲染器注册表 —— SDK 前端的核心扩展点。
 *
 * TF 里这一层是写死的 `<BusinessResult>`:一个按工具名 switch 到旅游业务卡片
 * 的巨型组件。SDK 不认识宿主的工具,更不该认识宿主的业务卡片,因此换成
 * `Record<toolName, Renderer>` 的可注册表:
 *
 * ```tsx
 * // 宿主在 app 根部一次性注入(推荐:随 providers.tsx 一起挂)
 * import { ToolResultRendererProvider } from "@/components/agent/result-renderers";
 *
 * const renderers = {
 *   ticket_create: ({ output }) => <TicketCard ticket={output} />,
 * };
 *
 * <ToolResultRendererProvider value={renderers}>{children}</ToolResultRendererProvider>
 * ```
 *
 * 或者在模块加载期用全局注册表(不需要 React 树上下文时更省事):
 *
 * ```ts
 * import { registerToolResultRenderers } from "@/components/agent/result-renderers";
 * registerToolResultRenderers({ ticket_create: TicketCard });
 * ```
 *
 * 解析顺序:Provider(就近) → 全局注册表 → 无。**无注册器的工具不会被当成
 * "重点结果"单独抽出来渲染**(见 hasToolResultRenderer),只在工具卡展开区里
 * 走通用 JSON 渲染 —— 这样宿主不注册任何东西也能看到全部输出。
 */

import { createContext, createElement, useContext } from "react";

import { isRecord, type UnknownRecord } from "./tool-presentation";

export interface ToolResultRendererProps {
  /** 工具名(同一个渲染器可复用于多个工具时用得上) */
  name: string;
  /** 工具输出;仅当输出是 JSON 对象时才会走渲染器 */
  output: UnknownRecord;
  /** 会话绑定的宿主作用域(chat_sessions.scope_type/scope_id),可能为空 */
  scopeType?: string | null;
  scopeId?: string | null;
}

export type ToolResultRenderer = React.ComponentType<ToolResultRendererProps>;

export type ToolResultRendererRegistry = Readonly<
  Record<string, ToolResultRenderer>
>;

// ---- 全局注册表(模块加载期注入) ------------------------------------------

const globalRenderers = new Map<string, ToolResultRenderer>();

export function registerToolResultRenderer(
  name: string,
  renderer: ToolResultRenderer,
): void {
  globalRenderers.set(name, renderer);
}

export function registerToolResultRenderers(
  entries: ToolResultRendererRegistry,
): void {
  for (const [name, renderer] of Object.entries(entries)) {
    globalRenderers.set(name, renderer);
  }
}

/** 测试与热重载用。 */
export function resetToolResultRenderers(): void {
  globalRenderers.clear();
}

// ---- React 上下文(就近注入,优先于全局) ----------------------------------

const RendererContext = createContext<ToolResultRendererRegistry | null>(null);

export function ToolResultRendererProvider({
  value,
  children,
}: {
  value: ToolResultRendererRegistry;
  children: React.ReactNode;
}) {
  return (
    <RendererContext.Provider value={value}>
      {children}
    </RendererContext.Provider>
  );
}

export function useToolResultRenderer(
  name: string,
): ToolResultRenderer | undefined {
  const scoped = useContext(RendererContext);
  return scoped?.[name] ?? globalRenderers.get(name);
}

/**
 * 该工具是否有专属渲染器 —— 决定它的结果要不要从工具卡里抽出来、当作对话正文
 * 旁的"重点结果"独立展示。注意这是**非响应式**读取(供 useMemo / 排序等场景),
 * 只覆盖全局注册表;Provider 注入的用 useToolResultRenderer。
 */
export function hasToolResultRenderer(
  name: string,
  output: unknown,
): boolean {
  return isRecord(output) && globalRenderers.has(name);
}

// ---- demo 默认渲染器:通用 JSON --------------------------------------------

/**
 * demo 提供的默认渲染器:折叠式 JSON。宿主没写卡片时也不至于什么都看不到。
 * 注册方式与业务渲染器完全一致,可当作最小实现范本。
 */
export function JsonResultRenderer({ name, output }: ToolResultRendererProps) {
  return (
    <section className="rounded-xl bg-muted/45 p-3">
      <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">
        {name} 结果
      </p>
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5">
        {JSON.stringify(output, null, 2)}
      </pre>
    </section>
  );
}

/**
 * 渲染一个工具结果:命中注册器就用它,否则回落 demo 的通用 JSON 渲染。
 * 调用方(run-sections / tool-run-card)不需要知道注册表长什么样。
 */
export function ToolResult(props: ToolResultRendererProps) {
  // 用 createElement 而不是 `<Renderer />`:渲染器来自注册表(运行时才知道是
  // 哪个组件),JSX 写法会被 React Compiler 判成"渲染期创建组件"。
  const renderer = useToolResultRenderer(props.name) ?? JsonResultRenderer;
  return createElement(renderer, props);
}
