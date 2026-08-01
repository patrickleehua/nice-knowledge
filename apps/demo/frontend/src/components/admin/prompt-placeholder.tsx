"use client";

// DB Prompt 的 {{占位符}} 仅作展示:任务 prompt 运行时零变量替换
// (system = content 原样发给模型),徽章+提示条是为了防止运营者误以为
// 会像"系统 Prompt 资源"那样做变量渲染——两套机制长得像但行为完全不同。

import { cn } from "@/lib/utils";

// 与后端 admin.py 的 _PROMPT_VAR_RE(loader.py _VAR_RE 同款)保持一致:{{var}},var 限标识符
const PROMPT_VAR_RE = /\{\{(\w+)\}\}/g;

/** 从 prompt 正文实时扫描 {{占位符}}:去重 + 排序,与后端 variables 字段同口径 */
export function extractPromptVariables(content: string): string[] {
  const found = new Set<string>();
  for (const match of content.matchAll(PROMPT_VAR_RE)) found.add(match[1]);
  return [...found].sort();
}

/** 占位符徽章行:variables 为空时整行不渲染 */
export function PlaceholderBadges({
  variables,
  className,
}: {
  variables: string[];
  className?: string;
}) {
  if (variables.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      <span className="text-xs text-muted-foreground">占位符</span>
      {variables.map((name) => (
        <span
          key={name}
          className="rounded border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-xs"
        >
          {`{{${name}}}`}
        </span>
      ))}
    </div>
  );
}

/** 占位符行为提示条:凡是展示占位符徽章的地方都应配这条说明 */
export function PlaceholderHint({ className }: { className?: string }) {
  return (
    <p
      className={cn(
        "rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs leading-5 text-warning",
        className,
      )}
    >
      注意:任务 prompt 运行时不做变量替换,占位符会原样发给模型;变量拼接由调用方代码完成
    </p>
  );
}
