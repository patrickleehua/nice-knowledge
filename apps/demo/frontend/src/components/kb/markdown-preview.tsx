"use client";

// 可复用 Markdown 预览:统一中间表示(KB-1)渲染 + 锚点定位。
// react-markdown v10 已移除 sourcePos 选项,这里用自写 rehype 插件把 mdast/hast
// 的 position 写入 data-sourcepos="startLine:col-endLine:col",
// anchor.startLine 变化时找覆盖该行的最外层元素,scrollIntoView + ring 高亮 1.8s;
// anchor.headingPath 则取路径最后一段做 h1-h6 文本匹配定位。

import { useQuery } from "@tanstack/react-query";
import { Copy, FileX2, ImageOff, Loader2 } from "lucide-react";
import { useEffect, useRef } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { KnowledgeMedia } from "@/components/kb/knowledge-media";
import { api, ApiError } from "@/lib/api";
import { kbImages, isKnowledgeAssetId } from "@/lib/kb-images";
import {
  replaceMarkdownImages,
  safeDecodeURIComponent,
} from "@/lib/kb-images-utils.mjs";
import { cn, errMsg } from "@/lib/utils";
import type {
  DocumentMarkdown,
  KbImageAsset,
  SourceDocument,
} from "@/lib/types";
export interface PreviewAnchor {
  /** markdown 全文 1-based 行号区间(chunk 溯源锚点) */
  startLine?: number;
  endLine?: number;
  /** 标题面包屑(如"制度汇编 > 报销"),取最后一段匹配 h1-h6 */
  headingPath?: string;
}

// ---- rehype 插件:把 hast position 写入 data-sourcepos ----------------------

interface HastNode {
  type: string;
  properties?: Record<string, unknown>;
  position?: {
    start: { line: number; column: number };
    end: { line: number; column: number };
  };
  children?: HastNode[];
}

function stampSourcePos(node: HastNode) {
  if (node.type === "element" && node.position) {
    node.properties = {
      ...node.properties,
      "data-sourcepos": `${node.position.start.line}:${node.position.start.column}-${node.position.end.line}:${node.position.end.column}`,
    };
  }
  for (const child of node.children ?? []) stampSourcePos(child);
}

const rehypeSourcePos = () => (tree: unknown) => {
  stampSourcePos(tree as HastNode);
};

function markdownAsset(
  src: string | undefined,
  assets: KbImageAsset[],
  sourceFilename?: string,
): KbImageAsset | null {
  if (!src) return null;
  const direct =
    /^\/api\/v1\/kb\/image-assets\/([0-9a-f-]+)\/(?:content|thumbnail)(?:\?.*)?$/i.exec(
      src,
    ) ??
    /^kb-image:(?:\/\/)?([0-9a-f-]+)$/i.exec(src) ??
    /^kb-image\/([0-9a-f-]+)$/i.exec(src);
  if (direct && isKnowledgeAssetId(direct[1])) {
    return assets.find((asset) => asset.asset_id === direct[1]) ?? null;
  }
  const normalized = src.split(/[?#]/, 1)[0]?.split(/[\\/]/).at(-1);
  const standalone = assets.filter(
    (asset) => asset.source_occurrence === "source:0",
  );
  if (
    standalone.length === 1 &&
    normalized &&
    sourceFilename &&
    safeDecodeURIComponent(normalized) ===
      sourceFilename.split(/[\\/]/).at(-1)
  ) {
    return standalone[0];
  }
  return null;
}

function sanitizedMarkdown(
  markdown: string,
  assets: KbImageAsset[],
  sourceFilename?: string,
): string {
  return replaceMarkdownImages(
    markdown,
    ({ alt, src }: { alt: string; src: string }) => {
      const asset = markdownAsset(src, assets, sourceFilename);
      return asset
        ? `![${alt || "知识来源图片"}](/api/v1/kb/image-assets/${asset.asset_id}/content)`
        : `> [图片证据当前不可用]`;
    },
  );
}

// ---- 定位与高亮 -------------------------------------------------------------

const HIGHLIGHT_CLASSES = ["ring-2", "ring-primary", "rounded-sm"];
const HIGHLIGHT_MS = 1800;

function parseSourcePos(el: HTMLElement): [number, number] | null {
  const raw = el.dataset.sourcepos;
  if (!raw) return null;
  const m = /^(\d+):\d+-(\d+):\d+$/.exec(raw);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

/** [start, end] 行区间的目标元素集:优先取完整落在区间内的最外层元素
 * (如大表格中该 chunk 对应的那几行 tr),没有再退回相交的最外层元素
 * (querySelectorAll 文档序,父先于子,故 contains 去重即得最外层)。 */
function findByLines(
  container: HTMLElement,
  start: number,
  end: number,
): HTMLElement[] {
  const contained: HTMLElement[] = [];
  const intersecting: HTMLElement[] = [];
  for (const el of container.querySelectorAll<HTMLElement>("[data-sourcepos]")) {
    const pos = parseSourcePos(el);
    if (!pos || pos[0] > end || pos[1] < start) continue;
    if (pos[0] >= start && pos[1] <= end) {
      if (!contained.some((kept) => kept.contains(el))) contained.push(el);
    }
    if (!intersecting.some((kept) => kept.contains(el))) intersecting.push(el);
  }
  return contained.length ? contained : intersecting;
}

function findByHeading(container: HTMLElement, headingPath: string): HTMLElement[] {
  const last = headingPath.split(">").at(-1)?.trim();
  if (!last) return [];
  for (const h of container.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6")) {
    const text = h.textContent?.trim() ?? "";
    if (text === last || text.includes(last)) return [h];
  }
  return [];
}

// ---- 组件 -------------------------------------------------------------------

export function MarkdownPreview({
  docId,
  anchor,
  className,
}: {
  docId: string;
  anchor?: PreviewAnchor | null;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

  const { data, error, isPending } = useQuery({
    queryKey: ["kb-doc-markdown", docId],
    queryFn: () => api.get<DocumentMarkdown>(`/kb/documents/${docId}/markdown`),
    staleTime: 30_000,
    // 404 = 无产物,是明确业务态,不重试
    retry: (count, err) =>
      !(err instanceof ApiError && err.status === 404) && count < 2,
  });
  const document = useQuery({
    queryKey: ["kb-doc-markdown-source", docId],
    queryFn: () => api.get<SourceDocument>(`/kb/documents/${docId}`),
    enabled: Boolean(data?.markdown.includes("![")),
  });
  const imageAssets = useQuery({
    queryKey: ["kb-doc-image-assets", docId, document.data?.kb_id],
    queryFn: () =>
      kbImages.list(document.data!.kb_id, {
        documentId: docId,
        pageSize: 100,
      }),
    enabled: Boolean(document.data?.kb_id),
  });
  const documentAssets = imageAssets.data?.items ?? [];
  const safeMarkdown = data
    ? sanitizedMarkdown(data.markdown, documentAssets, document.data?.filename)
    : "";

  // anchor 或数据变化后定位:等一帧确保 markdown 已渲染进 DOM
  useEffect(() => {
    if (!anchor || !data) return;
    const container = containerRef.current;
    if (!container) return;
    const raf = requestAnimationFrame(() => {
      const targets =
        anchor.startLine != null
          ? findByLines(container, anchor.startLine, anchor.endLine ?? anchor.startLine)
          : anchor.headingPath
            ? findByHeading(container, anchor.headingPath)
            : [];
      if (!targets.length) return;
      cleanupRef.current?.();
      targets[0].scrollIntoView({ block: "center", behavior: "smooth" });
      for (const el of targets) el.classList.add(...HIGHLIGHT_CLASSES);
      const timer = setTimeout(() => {
        for (const el of targets) el.classList.remove(...HIGHLIGHT_CLASSES);
        cleanupRef.current = null;
      }, HIGHLIGHT_MS);
      cleanupRef.current = () => {
        clearTimeout(timer);
        for (const el of targets) el.classList.remove(...HIGHLIGHT_CLASSES);
      };
    });
    return () => cancelAnimationFrame(raf);
  }, [anchor, data]);

  if (isPending) {
    return (
      <div className={cn("flex items-center justify-center py-16", className)}>
        <Loader2 className="size-5 animate-spin text-primary" />
      </div>
    );
  }

  if (error || !data) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center gap-2 py-16 text-sm text-muted-foreground",
          className,
        )}
      >
        <FileX2 className="size-6" />
        {notFound ? "该文档尚未生成 Markdown(重新摄入可生成)" : errMsg(error)}
      </div>
    );
  }

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-1.5 text-xs text-muted-foreground">
        <span className="truncate">
          解析器 <span className="font-mono">{data.parser ?? "unknown"}</span>
          <span className="mx-1.5">·</span>
          {data.markdown.split("\n").length} 行
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 shrink-0 px-2 text-xs"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(safeMarkdown);
              toast.success("已复制 Markdown 原文");
            } catch {
              toast.error("复制失败,浏览器未授权剪贴板");
            }
          }}
        >
          <Copy className="size-3" />
          复制原文
        </Button>
      </div>
      <div
        ref={containerRef}
        className="prose prose-sm dark:prose-invert min-h-0 max-w-none flex-1 overflow-auto px-4 py-3 prose-table:text-xs"
      >
        <Markdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeSourcePos]}
          components={{
            img: ({ src, alt }) => {
              const asset = markdownAsset(
                typeof src === "string" ? src : undefined,
                documentAssets,
                document.data?.filename,
              );
              if (!asset) {
                return (
                  <span className="not-prose my-3 flex min-h-24 items-center justify-center gap-2 rounded-lg border border-dashed border-border text-xs text-muted-foreground">
                    <ImageOff className="size-4" />
                    图片证据当前不可用
                  </span>
                );
              }
              return (
                <span className="not-prose my-3 block max-w-2xl">
                  <KnowledgeMedia
                    assetId={asset.asset_id}
                    alt={
                      alt ||
                      asset.caption ||
                      `来自 ${asset.source_filename} 的图片`
                    }
                    width={asset.width}
                    height={asset.height}
                    thumbnailUrl={asset.thumbnail_url}
                    contentUrl={asset.content_url}
                    sourceLabel={asset.source_filename}
                  />
                </span>
              );
            },
          }}
        >
          {safeMarkdown}
        </Markdown>
      </div>
    </div>
  );
}
