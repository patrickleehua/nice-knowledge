"use client";

import type { Components } from "react-markdown";
import {
  citationRefFromHref,
  isGeneratedMediaReference,
} from "./agent-markdown-content";

export {
  citationRefFromHref,
  isGeneratedMediaReference,
  linkifyCitations,
  sanitizeAgentMarkdown,
} from "./agent-markdown-content";

const CITATION_FLASH_MS = 1_400;

/**
 * Jumps to the source card rendered by the web_search / web_fetch tool card.
 * A collapsed (or replayed) tool card has no anchor — the marker stays inert
 * instead of throwing.
 *
 * 编号只在一次 agent run 内唯一,同一会话的后续轮次会再次从 1 开始,所以按 id
 * 全局取第一个会跳到上一轮的来源。工具卡片总是渲染在它所支撑的正文之前,因此
 * 这里取「位于该角标之前、文档序最靠后」的那张卡片——即本轮的来源。
 */
function findCitationTarget(ref: number, origin: Element): HTMLElement | null {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>(`[data-ref="${ref}"]`),
  );
  if (nodes.length <= 1) return nodes[0] ?? null;
  let previous: HTMLElement | null = null;
  for (const node of nodes) {
    // origin 在 node 之后 = node 在角标前面;querySelectorAll 按文档序返回,
    // 所以最后一个满足条件的就是离角标最近的那张卡片
    const relation = node.compareDocumentPosition(origin);
    if (relation & Node.DOCUMENT_POSITION_FOLLOWING) previous = node;
  }
  return previous ?? nodes[0];
}

function focusCitation(ref: number, origin: Element): void {
  if (typeof document === "undefined") return;
  const target = findCitationTarget(ref, origin);
  if (!target) return;
  const reduced =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  target.scrollIntoView({
    behavior: reduced ? "auto" : "smooth",
    block: "center",
  });
  target.classList.add("cite-flash");
  window.setTimeout(
    () => target.classList.remove("cite-flash"),
    CITATION_FLASH_MS,
  );
}

function CitationMarker({ value }: { value: number }) {
  return (
    <sup className="mx-px leading-none">
      <button
        type="button"
        data-citation-ref={value}
        aria-label={`跳转到来源 ${value}`}
        onClick={(event) => focusCitation(value, event.currentTarget)}
        className="inline-flex min-w-[1.25em] cursor-pointer items-center justify-center rounded bg-primary/10 px-1 py-px text-[0.7em] font-medium text-primary tabular-nums no-underline transition-colors hover:bg-primary/20 focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:outline-none"
      >
        {value}
      </button>
    </sup>
  );
}

/**
 * Generated media needs a Bearer-authenticated fetch and is rendered only by
 * the contextual gallery. Internal model-authored links/images are suppressed.
 */
export const agentMarkdownComponents: Components = {
  a({ href, children, title }) {
    if (isGeneratedMediaReference(href)) return null;
    const citation = citationRefFromHref(href);
    if (citation !== null) return <CitationMarker value={citation} />;
    return (
      <a
        href={href}
        title={title}
      >
        {children}
      </a>
    );
  },
  img({ src, alt, title }) {
    if (isGeneratedMediaReference(src)) return null;
    if (typeof src !== "string") return null;
    // Non-generated Markdown images keep react-markdown's native behavior.
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={src} alt={alt ?? ""} title={title} />;
  },
};
