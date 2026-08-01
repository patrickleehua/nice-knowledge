export function isGeneratedMediaReference(
  value: string | Blob | undefined,
): boolean {
  return (
    typeof value === "string" &&
    (value.startsWith("/genimg/") ||
      value.startsWith("/api/v1/genimg/"))
  );
}

const GENERATED_MEDIA_MARKDOWN =
  /!?\[[^\]]*\]\(\s*<?\/(?:api\/v1\/)?genimg\/[^)\s>]+>?(?:\s+["'][^"']*["'])?\s*\)/giu;
const GENERATED_MEDIA_LINE =
  /^[ \t]*\/(?:api\/v1\/)?genimg\/\S+[ \t]*$/gimu;
const REDUNDANT_IMAGE_COMPLETION =
  /^(?:图片|图像)(?:已经|已)?生成(?:完成|成功)?\s*[：:。.!！]*$/u;

const CITATION_HREF_PREFIX = "#cite-";
const CITATION_NUMBER = /^\d{1,3}$/u;
const BACKTICK_RUN = /^`+/u;
const FENCE_LINE = /^ {0,3}(`{3,}|~{3,})/u;

/**
 * `#cite-3` -> 3. Any other href (including real anchors) returns null, so the
 * renderer only turns synthesized citation links into superscript markers.
 */
export function citationRefFromHref(
  href: string | null | undefined,
): number | null {
  if (typeof href !== "string" || !href.startsWith(CITATION_HREF_PREFIX))
    return null;
  const raw = href.slice(CITATION_HREF_PREFIX.length);
  if (!CITATION_NUMBER.test(raw)) return null;
  const ref = Number(raw);
  return ref > 0 ? ref : null;
}

interface BracketGroup {
  inner: string;
  end: number;
}

/** Reads `[...]` starting at `start`, tolerating nesting and escapes. */
function readBracketGroup(line: string, start: number): BracketGroup | null {
  let depth = 0;
  for (let index = start; index < line.length; index += 1) {
    const char = line[index];
    if (char === "\\") {
      index += 1;
      continue;
    }
    if (char === "[") {
      depth += 1;
      continue;
    }
    if (char === "]") {
      depth -= 1;
      if (depth === 0)
        return { inner: line.slice(start + 1, index), end: index + 1 };
    }
  }
  return null;
}

/** Returns the index just past the `(...)` link destination at `start`. */
function skipParenSpan(line: string, start: number): number {
  let depth = 0;
  for (let index = start; index < line.length; index += 1) {
    const char = line[index];
    if (char === "\\") {
      index += 1;
      continue;
    }
    if (char === "(") {
      depth += 1;
      continue;
    }
    if (char === ")") {
      depth -= 1;
      if (depth === 0) return index + 1;
    }
  }
  return line.length;
}

/**
 * Rewrites standalone `[1]` markers inside one line. Inline code, links,
 * images, footnotes and link reference definitions are copied verbatim so a
 * bracket that already means something in Markdown is never hijacked.
 */
function linkifyInlineCitations(line: string): string {
  let result = "";
  let index = 0;
  while (index < line.length) {
    const char = line[index];
    if (char === "\\") {
      result += line.slice(index, index + 2);
      index += 2;
      continue;
    }
    if (char === "`") {
      const run = BACKTICK_RUN.exec(line.slice(index))?.[0] ?? "`";
      const closer = line.indexOf(run, index + run.length);
      if (closer === -1) {
        result += run;
        index += run.length;
        continue;
      }
      result += line.slice(index, closer + run.length);
      index = closer + run.length;
      continue;
    }
    if (char !== "[") {
      result += char;
      index += 1;
      continue;
    }
    const group = readBracketGroup(line, index);
    if (!group) {
      result += char;
      index += 1;
      continue;
    }
    const next = line[group.end];
    const numeric =
      CITATION_NUMBER.test(group.inner) && Number(group.inner) > 0;
    const image = index > 0 && line[index - 1] === "!";
    // `[1](url)` is an inline link and `[1]: url` a reference definition.
    if (numeric && !image && next !== "(" && next !== ":") {
      result += `[${group.inner}](${CITATION_HREF_PREFIX}${Number(group.inner)})`;
      index = group.end;
      continue;
    }
    let end = group.end;
    if (next === "(") end = skipParenSpan(line, group.end);
    else if (next === "[" && !numeric)
      end = readBracketGroup(line, group.end)?.end ?? group.end;
    result += line.slice(index, end);
    index = end;
  }
  return result;
}

/**
 * Turns `[1]` / `[1][2]` citation markers emitted next to web-search answers
 * into links to the matching source card (`#cite-1`). Fenced code blocks are
 * skipped wholesale; everything else is decided per bracket group. The
 * transform is idempotent — a second pass sees `[1](#cite-1)` and leaves it be.
 */
export function linkifyCitations(value: string): string {
  if (!value.includes("[")) return value;
  const lines = value.split("\n");
  let fence: string | null = null;
  for (let index = 0; index < lines.length; index += 1) {
    const marker = FENCE_LINE.exec(lines[index])?.[1];
    if (fence !== null) {
      if (marker && marker[0] === fence[0] && marker.length >= fence.length)
        fence = null;
      continue;
    }
    if (marker) {
      fence = marker;
      continue;
    }
    lines[index] = linkifyInlineCitations(lines[index]);
  }
  return lines.join("\n");
}

/**
 * The contextual artifact projector owns generated media. Remove internal
 * media references from model-authored Markdown so replay never shows a 404 or
 * a second download prompt beside the real gallery, then wire up citations.
 */
export function sanitizeAgentMarkdown(value: string): string {
  const sanitized = value
    .replace(GENERATED_MEDIA_MARKDOWN, "")
    .replace(GENERATED_MEDIA_LINE, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (REDUNDANT_IMAGE_COMPLETION.test(sanitized)) return "";
  return linkifyCitations(sanitized);
}
