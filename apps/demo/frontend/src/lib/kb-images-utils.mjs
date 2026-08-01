const ASSET_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** @param {string} value */
export function isKnowledgeAssetId(value) {
  return ASSET_ID_PATTERN.test(value);
}

/** @param {string} value */
export function safeDecodeURIComponent(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

const MARKDOWN_IMAGE_PATTERN =
  /!\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^)\n]*\)))?\s*\)/g;

/**
 * Rewrites valid Markdown image destinations, including angle-bracket
 * destinations that contain spaces.
 *
 * @param {string} markdown
 * @param {(image: { alt: string; src: string }) => string} replaceImage
 */
export function replaceMarkdownImages(markdown, replaceImage) {
  return markdown.replace(
    MARKDOWN_IMAGE_PATTERN,
    (_match, alt, angleSrc, bareSrc) =>
      replaceImage({ alt, src: angleSrc ?? bareSrc }),
  );
}

/**
 * Only accept the exact authenticated application route projected for this
 * asset. Persisted URL-free media references fall back to the canonical path.
 *
 * @param {string} assetId
 * @param {"thumbnail" | "content"} variant
 * @param {string | null | undefined} projectedUrl
 */
export function authenticatedMediaPath(assetId, variant, projectedUrl) {
  const canonical = `/kb/image-assets/${assetId}/${variant}`;
  if (!isKnowledgeAssetId(assetId) || !projectedUrl) return canonical;
  const expected = `/api/v1${canonical}`;
  if (projectedUrl === expected) return canonical;
  if (!projectedUrl.startsWith(`${expected}?`)) return canonical;
  const query = projectedUrl.slice(expected.length + 1);
  const params = new URLSearchParams(query);
  const snapshotId = params.get("snapshot_id");
  return params.size === 1 && snapshotId && isKnowledgeAssetId(snapshotId)
    ? `${canonical}?snapshot_id=${snapshotId}`
    : canonical;
}
