"use client";

import { Expand, ImageOff, Loader2, Minus, Plus } from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { fetchBlob } from "@/lib/api";
import {
  authenticatedMediaPath,
  isKnowledgeAssetId,
} from "@/lib/kb-images";
import { cn } from "@/lib/utils";

type MediaState =
  | { status: "loading"; url: null }
  | { status: "ready"; url: string }
  | { status: "unavailable"; url: null };

type ViewerTransform = {
  scale: number;
  x: number;
  y: number;
};

type DragState = {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  startX: number;
  startY: number;
};

const MIN_ZOOM = 1;
const MAX_ZOOM = 5;
const ZOOM_STEP = 0.25;
const WHEEL_ZOOM_SENSITIVITY = 0.002;
const KEYBOARD_PAN_STEP = 48;
const INITIAL_TRANSFORM: ViewerTransform = { scale: MIN_ZOOM, x: 0, y: 0 };

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function normalizeScale(value: number) {
  return Math.round(clamp(value, MIN_ZOOM, MAX_ZOOM) * 1000) / 1000;
}

function useAuthenticatedImage(path: string | null): MediaState {
  const [result, setResult] = useState<{
    path: string | null;
    state: MediaState;
  }>({
    path: null,
    state: { status: "unavailable", url: null },
  });

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void fetchBlob(path, true, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setResult({
          path,
          state: { status: "ready", url: objectUrl },
        });
      })
      .catch((error: unknown) => {
        if (
          error instanceof DOMException &&
          error.name === "AbortError"
        ) {
          return;
        }
        if (!controller.signal.aborted) {
          setResult({
            path,
            state: { status: "unavailable", url: null },
          });
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (!path) return { status: "unavailable", url: null };
  return result.path === path
    ? result.state
    : { status: "loading", url: null };
}

export interface KnowledgeMediaProps {
  assetId: string;
  alt: string;
  width?: number | null;
  height?: number | null;
  thumbnailUrl?: string | null;
  contentUrl?: string | null;
  sourceLabel?: string;
  className?: string;
  viewerClassName?: string;
  openLabel?: string;
  showViewer?: boolean;
}

export function KnowledgeMedia({
  assetId,
  alt,
  width,
  height,
  thumbnailUrl,
  contentUrl,
  sourceLabel,
  className,
  viewerClassName,
  openLabel = "查看完整图片",
  showViewer = true,
}: KnowledgeMediaProps) {
  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerTransform, setViewerTransform] =
    useState<ViewerTransform>(INITIAL_TRANSFORM);
  const [dragging, setDragging] = useState(false);
  const viewerFrameRef = useRef<HTMLDivElement>(null);
  const fullImageRef = useRef<HTMLImageElement>(null);
  const transformRef = useRef<ViewerTransform>(INITIAL_TRANSFORM);
  const dragRef = useRef<DragState | null>(null);
  const validAsset = isKnowledgeAssetId(assetId);
  const thumbnailPath = useMemo(
    () =>
      validAsset
        ? authenticatedMediaPath(assetId, "thumbnail", thumbnailUrl)
        : null,
    [assetId, thumbnailUrl, validAsset],
  );
  const contentPath = useMemo(
    () =>
      validAsset
        ? authenticatedMediaPath(assetId, "content", contentUrl)
        : null,
    [assetId, contentUrl, validAsset],
  );
  const thumbnail = useAuthenticatedImage(thumbnailPath);
  const fullImage = useAuthenticatedImage(viewerOpen ? contentPath : null);
  const aspectRatio =
    width && height && width > 0 && height > 0 ? `${width} / ${height}` : "4 / 3";
  const meaningfulAlt = alt.trim() || "知识来源图片";
  const interactive = showViewer && thumbnail.status === "ready";

  const clampTransform = useCallback(
    (next: ViewerTransform): ViewerTransform => {
      const scale = normalizeScale(next.scale);
      const frame = viewerFrameRef.current;
      const image = fullImageRef.current;
      if (scale === MIN_ZOOM || !frame || !image) {
        return { scale, x: 0, y: 0 };
      }

      const frameWidth = frame.clientWidth;
      const frameHeight = frame.clientHeight;
      const intrinsicWidth = image.naturalWidth || width || frameWidth;
      const intrinsicHeight = image.naturalHeight || height || frameHeight;
      if (
        frameWidth <= 0 ||
        frameHeight <= 0 ||
        intrinsicWidth <= 0 ||
        intrinsicHeight <= 0
      ) {
        return { scale, x: next.x, y: next.y };
      }

      const imageRatio = intrinsicWidth / intrinsicHeight;
      const frameRatio = frameWidth / frameHeight;
      const fittedWidth =
        imageRatio > frameRatio ? frameWidth : frameHeight * imageRatio;
      const fittedHeight =
        imageRatio > frameRatio ? frameWidth / imageRatio : frameHeight;
      const maxX = Math.max(0, (fittedWidth * scale - frameWidth) / 2);
      const maxY = Math.max(0, (fittedHeight * scale - frameHeight) / 2);
      return {
        scale,
        x: clamp(next.x, -maxX, maxX),
        y: clamp(next.y, -maxY, maxY),
      };
    },
    [height, width],
  );

  const commitTransform = useCallback(
    (next: ViewerTransform) => {
      const clamped = clampTransform(next);
      transformRef.current = clamped;
      setViewerTransform(clamped);
    },
    [clampTransform],
  );

  const resetTransform = useCallback(() => {
    dragRef.current = null;
    setDragging(false);
    transformRef.current = INITIAL_TRANSFORM;
    setViewerTransform(INITIAL_TRANSFORM);
  }, []);

  const zoomTo = useCallback(
    (requestedScale: number, origin?: { clientX: number; clientY: number }) => {
      const current = transformRef.current;
      const scale = normalizeScale(requestedScale);
      if (scale === current.scale) return;
      if (scale === MIN_ZOOM) {
        resetTransform();
        return;
      }

      let x = current.x;
      let y = current.y;
      const frame = viewerFrameRef.current;
      if (origin && frame) {
        const rect = frame.getBoundingClientRect();
        const pointerX = origin.clientX - (rect.left + rect.width / 2);
        const pointerY = origin.clientY - (rect.top + rect.height / 2);
        const scaleRatio = scale / current.scale;
        x = pointerX - (pointerX - current.x) * scaleRatio;
        y = pointerY - (pointerY - current.y) * scaleRatio;
      }
      commitTransform({ scale, x, y });
    },
    [commitTransform, resetTransform],
  );

  const handleViewerOpenChange = useCallback(
    (open: boolean) => {
      if (!open) resetTransform();
      setViewerOpen(open);
    },
    [resetTransform],
  );

  useEffect(() => {
    const frame = viewerFrameRef.current;
    if (!viewerOpen || !frame || fullImage.status !== "ready") return;

    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      if (event.deltaY === 0) return;
      const scale =
        transformRef.current.scale *
        Math.exp(-event.deltaY * WHEEL_ZOOM_SENSITIVITY);
      zoomTo(scale, { clientX: event.clientX, clientY: event.clientY });
    };

    frame.addEventListener("wheel", handleWheel, { passive: false });
    return () => frame.removeEventListener("wheel", handleWheel);
  }, [fullImage.status, viewerOpen, zoomTo]);

  useEffect(() => {
    if (!viewerOpen) return;
    const handleResize = () => commitTransform(transformRef.current);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [commitTransform, viewerOpen]);

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (
      fullImage.status !== "ready" ||
      transformRef.current.scale === MIN_ZOOM ||
      (event.pointerType === "mouse" && event.button !== 0)
    ) {
      return;
    }
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    const current = transformRef.current;
    dragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startX: current.x,
      startY: current.y,
    };
    setDragging(true);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    commitTransform({
      scale: transformRef.current.scale,
      x: drag.startX + event.clientX - drag.startClientX,
      y: drag.startY + event.clientY - drag.startClientY,
    });
  };

  const finishDragging = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragRef.current = null;
    setDragging(false);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (fullImage.status !== "ready") return;
    const current = transformRef.current;
    switch (event.key) {
      case "+":
      case "=":
        event.preventDefault();
        zoomTo(current.scale + ZOOM_STEP);
        break;
      case "-":
        event.preventDefault();
        zoomTo(current.scale - ZOOM_STEP);
        break;
      case "0":
        event.preventDefault();
        resetTransform();
        break;
      case "ArrowLeft":
        event.preventDefault();
        commitTransform({ ...current, x: current.x - KEYBOARD_PAN_STEP });
        break;
      case "ArrowRight":
        event.preventDefault();
        commitTransform({ ...current, x: current.x + KEYBOARD_PAN_STEP });
        break;
      case "ArrowUp":
        event.preventDefault();
        commitTransform({ ...current, y: current.y - KEYBOARD_PAN_STEP });
        break;
      case "ArrowDown":
        event.preventDefault();
        commitTransform({ ...current, y: current.y + KEYBOARD_PAN_STEP });
        break;
    }
  };

  return (
    <>
      <button
        type="button"
        disabled={!interactive}
        aria-label={`${openLabel}：${meaningfulAlt}`}
        onClick={() => setViewerOpen(true)}
        className={cn(
          "group relative block w-full overflow-hidden rounded-lg border border-border bg-muted/35 text-left outline-none",
          interactive &&
            "cursor-zoom-in focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          !interactive && "cursor-default",
          className,
        )}
        style={{ aspectRatio }}
      >
        {thumbnail.status === "loading" && (
          <span
            className="absolute inset-0 flex items-center justify-center gap-2 text-xs text-muted-foreground"
            role="status"
          >
            <Loader2 className="size-4 animate-spin" />
            正在加载图片
          </span>
        )}
        {thumbnail.status === "unavailable" && (
          <span
            className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 px-3 text-center text-xs text-muted-foreground"
            role="status"
          >
            <ImageOff className="size-5" />
            证据图片当前不可用
          </span>
        )}
        {thumbnail.status === "ready" && (
          <>
            {/* Blob URLs are already authenticated application responses. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={thumbnail.url}
              alt={meaningfulAlt}
              className="absolute inset-0 size-full object-contain"
            />
            {interactive && (
              <span className="absolute right-2 bottom-2 flex items-center gap-1 rounded-md bg-background/90 px-2 py-1 text-[11px] font-medium opacity-0 shadow-sm transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                <Expand className="size-3" />
                {openLabel}
              </span>
            )}
          </>
        )}
      </button>

      <Dialog open={viewerOpen} onOpenChange={handleViewerOpenChange}>
        <DialogContent
          className={cn(
            "h-[calc(100dvh-2rem)] max-h-[64rem] w-[calc(100vw-2rem)] grid-rows-[auto_minmax(0,1fr)] overflow-hidden sm:max-w-[min(92vw,1280px)]",
            viewerClassName,
          )}
        >
          <DialogHeader>
            <DialogTitle>{meaningfulAlt}</DialogTitle>
            <DialogDescription>
              {sourceLabel
                ? `知识来源：${sourceLabel}`
                : "来自当前授权范围内的知识文档"}
              。Ctrl + 滚轮缩放，放大后拖拽移动，双击快速缩放；按 Escape
              关闭并返回打开位置。
            </DialogDescription>
          </DialogHeader>
          <div
            ref={viewerFrameRef}
            role="group"
            aria-label="图片查看区域"
            tabIndex={fullImage.status === "ready" ? 0 : -1}
            data-zoom={viewerTransform.scale}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={finishDragging}
            onPointerCancel={finishDragging}
            onDoubleClick={(event) =>
              viewerTransform.scale > MIN_ZOOM
                ? resetTransform()
                : zoomTo(2, {
                    clientX: event.clientX,
                    clientY: event.clientY,
                  })
            }
            onKeyDown={handleKeyDown}
            className={cn(
              "relative flex min-h-0 touch-none items-center justify-center overflow-hidden rounded-lg bg-muted/40 outline-none select-none focus-visible:ring-2 focus-visible:ring-ring",
              fullImage.status === "ready" &&
                (viewerTransform.scale > MIN_ZOOM
                  ? dragging
                    ? "cursor-grabbing"
                    : "cursor-grab"
                  : "cursor-zoom-in"),
            )}
          >
            {fullImage.status === "ready" && (
              <div
                role="toolbar"
                aria-label="图片缩放控制"
                className="absolute top-3 right-3 z-10 flex items-center gap-1 rounded-lg border border-border/70 bg-background/90 p-1 shadow-sm backdrop-blur-sm"
                onPointerDown={(event) => event.stopPropagation()}
                onDoubleClick={(event) => event.stopPropagation()}
              >
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="缩小图片"
                  title="缩小（−）"
                  disabled={viewerTransform.scale <= MIN_ZOOM}
                  onClick={() =>
                    zoomTo(transformRef.current.scale - ZOOM_STEP)
                  }
                >
                  <Minus className="size-4" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="min-w-14 px-2 font-mono text-xs tabular-nums"
                  aria-label="重置图片缩放"
                  title="恢复 100%（0）"
                  disabled={viewerTransform.scale === MIN_ZOOM}
                  onClick={resetTransform}
                >
                  {Math.round(viewerTransform.scale * 100)}%
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="放大图片"
                  title="放大（+）"
                  disabled={viewerTransform.scale >= MAX_ZOOM}
                  onClick={() =>
                    zoomTo(transformRef.current.scale + ZOOM_STEP)
                  }
                >
                  <Plus className="size-4" />
                </Button>
              </div>
            )}
            {fullImage.status === "loading" && (
              <span className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在加载完整图片
              </span>
            )}
            {fullImage.status === "unavailable" && (
              <span className="flex flex-col items-center gap-2 text-sm text-muted-foreground">
                <ImageOff className="size-6" />
                证据图片当前不可用或已无权访问
              </span>
            )}
            {fullImage.status === "ready" && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                ref={fullImageRef}
                src={fullImage.url}
                width={width ?? undefined}
                height={height ?? undefined}
                alt={`${meaningfulAlt}（完整图片）`}
                draggable={false}
                onLoad={() => commitTransform(transformRef.current)}
                className="pointer-events-none size-full object-contain will-change-transform"
                style={{
                  transform: `translate3d(${viewerTransform.x}px, ${viewerTransform.y}px, 0) scale(${viewerTransform.scale})`,
                  transformOrigin: "center",
                }}
              />
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
