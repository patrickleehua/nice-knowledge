import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPageOrder,
  sortByNavigationOrder,
  wikiPageNavigationKey,
} from "./wiki-navigation.ts";

test("saved Wiki order wins and newly added pages remain stable at the end", () => {
  const pages = [
    { id: "new-a" },
    { id: "saved-b" },
    { id: "saved-a" },
    { id: "new-b" },
  ];

  assert.deepEqual(
    sortByNavigationOrder(pages, ["saved-a", "saved-b"]).map((page) => page.id),
    ["saved-a", "saved-b", "new-a", "new-b"],
  );
});

test("snapshot page keys survive projection ID changes", () => {
  const beforeRelease = {
    id: "old-projection-id",
    page_type: "destination",
    title: "伦敦",
    snapshot_id: "old-snapshot",
  };
  const afterRelease = { ...beforeRelease, id: "new-projection-id" };

  assert.equal(
    wikiPageNavigationKey(beforeRelease),
    wikiPageNavigationKey(afterRelease),
  );
});

test("reordering one Wiki group preserves every other group", () => {
  const groups = new Map([
    ["overview", [{ id: "overview" }]],
    ["destination", [{ id: "london" }, { id: "leeds" }]],
    ["source_summary", [{ id: "source" }]],
  ]);

  assert.deepEqual(
    buildPageOrder(
      ["overview", "destination", "source_summary"],
      groups,
      "destination",
      [{ id: "leeds" }, { id: "london" }],
    ),
    ["overview", "leeds", "london", "source"],
  );
});
