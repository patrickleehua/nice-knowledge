import assert from "node:assert/strict";
import test from "node:test";
import {
  authenticatedMediaPath,
  isKnowledgeAssetId,
  replaceMarkdownImages,
  safeDecodeURIComponent,
} from "./kb-images-utils.mjs";

const assetId = "123e4567-e89b-42d3-a456-426614174000";
const snapshotId = "223e4567-e89b-42d3-a456-426614174001";

test("URL-free media references resolve to authenticated application routes", () => {
  assert.equal(
    authenticatedMediaPath(assetId, "thumbnail"),
    `/kb/image-assets/${assetId}/thumbnail`,
  );
  assert.equal(
    authenticatedMediaPath(
      assetId,
      "content",
      `/api/v1/kb/image-assets/${assetId}/content?snapshot_id=${snapshotId}`,
    ),
    `/kb/image-assets/${assetId}/content?snapshot_id=${snapshotId}`,
  );
});

test("raw keys, foreign asset routes, and arbitrary query strings are ignored", () => {
  for (const projected of [
    "org/kb/raw/object.png",
    "/api/v1/kb/image-assets/323e4567-e89b-42d3-a456-426614174002/content",
    `/api/v1/kb/image-assets/${assetId}/content?token=secret`,
  ]) {
    assert.equal(
      authenticatedMediaPath(assetId, "content", projected),
      `/kb/image-assets/${assetId}/content`,
    );
  }
  assert.equal(isKnowledgeAssetId("raw/object-key"), false);
});

test("malformed legacy image destinations do not throw while decoding", () => {
  assert.equal(safeDecodeURIComponent("photo%20one.png"), "photo one.png");
  assert.equal(safeDecodeURIComponent("photo%ZZone.png"), "photo%ZZone.png");
});

test("Markdown image destinations with spaces are rewritten before rendering or copying", () => {
  const markdown = [
    "![map](<legacy folder/trip map.png> \"source\")",
    "![photo](legacy-photo.png)",
  ].join("\n");

  assert.equal(
    replaceMarkdownImages(
      markdown,
      ({ alt, src }) => `![${alt}](safe:${src.replaceAll(" ", "_")})`,
    ),
    [
      "![map](safe:legacy_folder/trip_map.png)",
      "![photo](safe:legacy-photo.png)",
    ].join("\n"),
  );
});
