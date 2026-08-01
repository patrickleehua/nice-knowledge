"use client";

import { LocateFixed } from "lucide-react";
import { useEffect, useState } from "react";
import type { ConversationAnchor } from "@/lib/agent-events";
import { cn } from "@/lib/utils";

export function ConversationLocator({
  anchors,
}: {
  anchors: ConversationAnchor[];
}) {
  const [activeId, setActiveId] = useState(anchors.at(-1)?.id ?? "");
  const anchorSignature = anchors
    .map((anchor) => `${anchor.id}:${anchor.label}`)
    .join("\u0000");

  useEffect(() => {
    const root = document.querySelector<HTMLElement>(
      "[data-conversation-scroll]",
    );
    const elements = Array.from(
      (root ?? document).querySelectorAll<HTMLElement>(
        '[id^="conversation-turn-"]',
      ),
    );
    if (!elements.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries)
          if (entry.isIntersecting) setActiveId(entry.target.id);
      },
      {
        root,
        rootMargin: "-10% 0px -78% 0px",
        threshold: 0,
      },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [anchorSignature]);

  if (!anchors.length) return null;

  return (
    <nav
      aria-label="对话快速定位"
      className="sticky top-4 hidden max-h-[calc(100dvh-13rem)] self-start overflow-y-auto py-1 xl:block"
    >
      <p className="mb-2 flex items-center gap-1.5 px-2 text-[11px] font-medium text-muted-foreground/75">
        <LocateFixed className="size-3.5" />
        快速定位
      </p>
      <div className="relative space-y-0.5 before:absolute before:top-3 before:bottom-3 before:left-[0.72rem] before:w-px before:bg-black/10 dark:before:bg-white/10">
        {anchors.map((anchor, index) => {
          const active = anchor.id === activeId;
          return (
            <button
              key={anchor.id}
              type="button"
              onClick={() =>
                document.getElementById(anchor.id)?.scrollIntoView({
                  behavior: "smooth",
                  block: "start",
                })
              }
              aria-current={active ? "location" : undefined}
              title={anchor.label}
              className={cn(
                "group relative flex min-h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-[11px] transition-colors",
                active
                  ? "bg-white/80 text-foreground shadow-xs ring-1 ring-inset ring-black/[0.055] dark:bg-white/[0.07] dark:ring-white/[0.06]"
                  : "text-muted-foreground hover:bg-white/55 hover:text-foreground dark:hover:bg-white/[0.045]",
              )}
            >
              <span
                className={cn(
                  "relative z-10 flex size-3.5 shrink-0 items-center justify-center rounded-full bg-[#f7f7f5] ring-2 ring-[#f7f7f5] dark:bg-[#191918] dark:ring-[#191918]",
                )}
              >
                <span
                  className={cn(
                    "size-1.5 rounded-full transition-colors",
                    active
                      ? "bg-foreground"
                      : "bg-muted-foreground/35 group-hover:bg-muted-foreground/65",
                  )}
                />
              </span>
              <span className="min-w-0 truncate">
                <span className="mr-1 tabular-nums opacity-55">
                  {index + 1}
                </span>
                {anchor.label}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
