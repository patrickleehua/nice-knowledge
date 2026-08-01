"use client";

// wiki 正文 Markdown 渲染(KB-5C):渲染管线与 kb/markdown-preview.tsx 保持一致
// (react-markdown v10 + remark-gfm + prose 样式),但 props 直接传 content 字符串。
// 额外支持 [[wikilink]]:渲染前做字符串 transform(见 wikilink.ts),
// a 组件拦截 #wikilink: 前缀——存在的页蓝色下划线可点跳转;
// 不存在的页虚线下划线 + Tooltip「暂无此页,点击创建」→ 预填标题的新建入口。

import { useMemo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { KbPage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { findPageByTitle } from "./data";
import { transformWikilinks, WIKILINK_HREF_PREFIX } from "./wikilink";

export function WikiMarkdownView({
  content,
  pages,
  onOpenPage,
  onCreatePage,
  className,
}: {
  content: string;
  /** 当前库页列表(react-query 缓存),用于 wikilink 解析 */
  pages: KbPage[] | undefined;
  onOpenPage: (pageId: string) => void;
  /** 点击不存在的 wikilink → 打开预填 title 的新建 Dialog */
  onCreatePage: (title: string) => void;
  className?: string;
}) {
  const transformed = useMemo(() => transformWikilinks(content), [content]);

  return (
    <TooltipProvider delay={150}>
      <div
        className={cn(
          "prose prose-sm dark:prose-invert max-w-none prose-table:text-xs",
          className,
        )}
      >
        <Markdown
          remarkPlugins={[remarkGfm]}
          components={{
            a: ({ href, children }) => {
              if (!href?.startsWith(WIKILINK_HREF_PREFIX)) {
                // 普通链接:新开标签页
                return (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                );
              }
              const title = decodeURIComponent(
                href.slice(WIKILINK_HREF_PREFIX.length),
              );
              const page = findPageByTitle(pages, title);
              if (page) {
                return (
                  <button
                    type="button"
                    onClick={() => onOpenPage(page.id)}
                    className="cursor-pointer font-normal text-primary underline decoration-primary/50 underline-offset-2 transition-colors hover:decoration-primary"
                  >
                    {children}
                  </button>
                );
              }
              return (
                <Tooltip>
                  <TooltipTrigger
                    onClick={() => onCreatePage(title)}
                    className="cursor-pointer font-normal text-muted-foreground underline decoration-dashed decoration-muted-foreground/60 underline-offset-2 transition-colors hover:text-foreground"
                  >
                    {children}
                  </TooltipTrigger>
                  <TooltipContent>暂无此页,点击创建</TooltipContent>
                </Tooltip>
              );
            },
          }}
        >
          {transformed}
        </Markdown>
      </div>
    </TooltipProvider>
  );
}
