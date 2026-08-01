import assert from "node:assert/strict";
import test from "node:test";
import {
  documentUploadPreflight,
  SUPPORTED_DOCUMENT_SUFFIXES,
} from "./upload-preflight.mjs";

test("supported document suffixes include parser contract formats", () => {
  assert.deepEqual(SUPPORTED_DOCUMENT_SUFFIXES, [
    ".docx",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".pptx",
    ".pdf",
    ".csv",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
  ]);
});

test("preflight accepts exact size and handles uppercase suffixes", () => {
  const file = { name: "GUIDE.PDF", size: 20 };
  const result = documentUploadPreflight([file], 20, 1);

  assert.deepEqual(result.accepted, [file]);
  assert.equal(result.batchExceeded, false);
});

test("preflight reports unsupported empty and oversized files", () => {
  const result = documentUploadPreflight(
    [
      { name: "slides.key", size: 10 },
      { name: "empty.txt", size: 0 },
      { name: "large.docx", size: 21 },
      { name: "ok.md", size: 20 },
    ],
    20,
    5,
  );

  assert.deepEqual(result.accepted, [{ name: "ok.md", size: 20 }]);
  assert.deepEqual(
    {
      unsupported: result.unsupported,
      empty: result.empty,
      oversized: result.oversized,
    },
    { unsupported: 1, empty: 1, oversized: 1 },
  );
});

test("preflight rejects an entire eligible batch above the limit", () => {
  const result = documentUploadPreflight(
    [
      { name: "one.txt", size: 1 },
      { name: "two.txt", size: 1 },
      { name: "ignored.key", size: 1 },
    ],
    20,
    1,
  );

  assert.equal(result.eligible, 2);
  assert.equal(result.batchExceeded, true);
  assert.deepEqual(result.accepted, []);
});
