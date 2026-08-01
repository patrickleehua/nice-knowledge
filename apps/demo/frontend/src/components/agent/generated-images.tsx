"use client";

import {
  ChevronLeft,
  ChevronRight,
  Download,
  ImageOff,
  RefreshCw,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { downloadFile, fetchObjectUrl } from "@/lib/api";
import type { GeneratedImageArtifactItem } from "@/lib/agent-events";
import { cn } from "@/lib/utils";
import {
  generatedImageGridClass,
  generatedImageMaxWidth,
  imageFollowUpPrompt,
} from "./generated-image-presentation";

export type GeneratedImage = GeneratedImageArtifactItem;

function useImageSources(images: GeneratedImage[]) {
  const [sources, setSources] = useState<Record<string, string | null>>({});
  const [failed, setFailed] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let active = true;
    const objectUrls = new Set<string>();
    for (const image of images) {
      void fetchObjectUrl(image.url)
        .then((url) => {
          if (!active) {
            URL.revokeObjectURL(url);
            return;
          }
          objectUrls.add(url);
          setSources((current) => ({ ...current, [image.filename]: url }));
        })
        .catch(() => {
          if (active)
            setFailed((current) => new Set(current).add(image.filename));
        });
    }
    return () => {
      active = false;
      for (const url of objectUrls) URL.revokeObjectURL(url);
    };
  }, [images]);

  return { sources, failed };
}

function ImageViewer({
  images,
  sources,
  initialIndex,
  onClose,
}: {
  images: GeneratedImage[];
  sources: Record<string, string | null>;
  initialIndex: number;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(initialIndex);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const image = images[index];
  const src = sources[image.filename];

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft" && images.length > 1)
        setIndex((current) => (current - 1 + images.length) % images.length);
      if (event.key === "ArrowRight" && images.length > 1)
        setIndex((current) => (current + 1) % images.length);
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [
        ...dialogRef.current.querySelectorAll<HTMLElement>(
          "button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
        ),
      ];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1) ?? first;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      previousFocus?.focus();
    };
  }, [images.length, onClose]);

  async function download() {
    try {
      await downloadFile(image.url, image.filename);
    } catch {
      toast.error("图片下载失败");
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-3 backdrop-blur-sm sm:p-8"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`查看 AI 生成图片 ${index + 1}/${images.length}`}
        className="relative flex h-full max-h-[92dvh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl bg-[#151514] text-white shadow-2xl ring-1 ring-white/15"
      >
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-white/10 px-3 sm:px-4">
          <Sparkles className="size-3.5 text-white/65" />
          <span className="text-xs font-medium">AI 生成</span>
          {images.length > 1 && (
            <span className="text-xs tabular-nums text-white/55">
              {index + 1} / {images.length}
            </span>
          )}
          <button
            type="button"
            onClick={() => void download()}
            className="ml-auto rounded-lg p-2 text-white/75 hover:bg-white/10 hover:text-white"
            aria-label="下载当前图片"
          >
            <Download className="size-4" />
          </button>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded-lg p-2 text-white/75 hover:bg-white/10 hover:text-white"
            aria-label="关闭图片查看器"
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="relative min-h-0 flex-1 p-3 sm:p-5">
          {src ? (
            // Authenticated media is represented by a client-owned object URL.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={src}
              width={image.width}
              height={image.height}
              alt={`AI 生成图片 ${index + 1}`}
              className="h-full w-full object-contain"
            />
          ) : (
            <div className="h-full w-full animate-pulse rounded-xl bg-white/5 motion-reduce:animate-none" />
          )}
          {images.length > 1 && (
            <>
              <button
                type="button"
                onClick={() =>
                  setIndex(
                    (current) => (current - 1 + images.length) % images.length,
                  )
                }
                className="absolute top-1/2 left-4 -translate-y-1/2 rounded-full bg-black/55 p-2.5 text-white/80 ring-1 ring-white/10 hover:bg-black/75 hover:text-white"
                aria-label="上一张图片"
              >
                <ChevronLeft className="size-5" />
              </button>
              <button
                type="button"
                onClick={() =>
                  setIndex((current) => (current + 1) % images.length)
                }
                className="absolute top-1/2 right-4 -translate-y-1/2 rounded-full bg-black/55 p-2.5 text-white/80 ring-1 ring-white/10 hover:bg-black/75 hover:text-white"
                aria-label="下一张图片"
              >
                <ChevronRight className="size-5" />
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ImageTile({
  image,
  index,
  src,
  failed,
  onOpen,
}: {
  image: GeneratedImage;
  index: number;
  src: string | null | undefined;
  failed: boolean;
  onOpen: () => void;
}) {
  async function download(event: React.MouseEvent) {
    event.stopPropagation();
    try {
      await downloadFile(image.url, image.filename);
    } catch {
      toast.error("图片下载失败");
    }
  }

  return (
    <div
      className="group relative min-w-0 overflow-hidden rounded-2xl bg-black/[0.035] ring-1 ring-inset ring-black/[0.075] dark:bg-white/[0.045] dark:ring-white/[0.08]"
      style={{
        aspectRatio: `${image.width} / ${image.height}`,
        maxWidth: generatedImageMaxWidth(image.width, image.height),
        width: "100%",
      }}
    >
      {failed ? (
        <div className="flex h-full items-center justify-center text-muted-foreground">
          <ImageOff className="size-5" />
        </div>
      ) : src ? (
        <button
          type="button"
          onClick={onOpen}
          className="h-full w-full cursor-zoom-in focus-visible:outline-2 focus-visible:outline-offset-[-3px] focus-visible:outline-primary"
          aria-label={`查看 AI 生成图片 ${index + 1}`}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={src}
            width={image.width}
            height={image.height}
            alt={`AI 生成图片 ${index + 1}`}
            className="h-full w-full object-contain"
          />
        </button>
      ) : (
        <div className="h-full w-full animate-pulse bg-gradient-to-br from-foreground/[0.025] to-foreground/[0.075] motion-reduce:animate-none" />
      )}
      <div className="pointer-events-none absolute inset-x-0 top-0 flex items-center justify-between bg-gradient-to-b from-black/45 to-transparent p-2.5 text-white opacity-100 sm:opacity-0 sm:transition-opacity sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
        <span className="rounded-full bg-black/35 px-2 py-1 text-[10px] font-medium backdrop-blur-sm">
          AI 生成
        </span>
        <button
          type="button"
          onClick={(event) => void download(event)}
          className="pointer-events-auto rounded-full bg-black/40 p-2 backdrop-blur-sm hover:bg-black/65"
          aria-label={`下载图片 ${index + 1}`}
        >
          <Download className="size-3.5" />
        </button>
      </div>
    </div>
  );
}

export function GeneratedImagePlaceholder({
  count,
  width,
  height,
}: {
  count: number;
  width: number;
  height: number;
}) {
  const safeCount = Math.min(4, Math.max(1, count));
  return (
    <section aria-label="正在生成图片" className="space-y-2.5">
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Sparkles className="size-3.5" />
        <span>AI 图片生成中</span>
      </div>
      <div
        className={cn(
          "grid justify-items-start gap-2.5",
          generatedImageGridClass(safeCount),
        )}
      >
        {Array.from({ length: safeCount }, (_, index) => (
          <div
            key={index}
            className="animate-pulse rounded-2xl bg-gradient-to-br from-foreground/[0.025] to-foreground/[0.075] ring-1 ring-inset ring-black/[0.055] motion-reduce:animate-none dark:ring-white/[0.065]"
            style={{
              aspectRatio: `${width} / ${height}`,
              maxWidth: generatedImageMaxWidth(width, height),
              width: "100%",
            }}
          />
        ))}
      </div>
    </section>
  );
}

export function GeneratedImages({
  images,
  prompt,
  onAdjust,
  onGenerateAgain,
}: {
  images: GeneratedImage[];
  prompt: string;
  onAdjust?: (draft: string) => void;
  onGenerateAgain?: (message: string) => void;
}) {
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  const { sources, failed } = useImageSources(images);
  if (!images.length) return null;
  return (
    <section aria-label="AI 生成图片" className="space-y-2.5">
      <div
        className={cn(
          "grid justify-items-start gap-2.5",
          generatedImageGridClass(images.length),
        )}
      >
        {images.map((image, index) => (
          <ImageTile
            key={image.filename}
            image={image}
            index={index}
            src={sources[image.filename]}
            failed={failed.has(image.filename)}
            onOpen={() => setViewerIndex(index)}
          />
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
        <span className="mr-1 inline-flex items-center gap-1.5 px-1">
          <Sparkles className="size-3.5" />
          AI 生成
        </span>
        {onAdjust && (
          <button
            type="button"
            onClick={() => onAdjust(imageFollowUpPrompt("adjust", prompt))}
            className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 hover:bg-foreground/[0.045] hover:text-foreground"
          >
            <SlidersHorizontal className="size-3.5" />
            调整方向
          </button>
        )}
        {onGenerateAgain && (
          <button
            type="button"
            onClick={() =>
              onGenerateAgain(imageFollowUpPrompt("again", prompt))
            }
            className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 hover:bg-foreground/[0.045] hover:text-foreground"
          >
            <RefreshCw className="size-3.5" />
            再生成一组
          </button>
        )}
      </div>
      {viewerIndex !== null && (
        <ImageViewer
          images={images}
          sources={sources}
          initialIndex={viewerIndex}
          onClose={() => setViewerIndex(null)}
        />
      )}
    </section>
  );
}
