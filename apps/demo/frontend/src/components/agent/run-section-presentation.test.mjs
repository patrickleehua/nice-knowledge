import assert from "node:assert/strict";
import test from "node:test";
import { defaultActivityDisclosureOpen } from "./run-section-presentation.ts";

test("keeps an approval pause collapsed after the user submits a decision", () => {
  assert.equal(
    defaultActivityDisclosureOpen({
      streaming: true,
      waitingForApproval: false,
      pausedForApproval: true,
    }),
    false,
  );
});

test("opens an ordinary active run until it reaches an approval boundary", () => {
  assert.equal(
    defaultActivityDisclosureOpen({
      streaming: true,
      waitingForApproval: false,
      pausedForApproval: false,
    }),
    true,
  );
  assert.equal(
    defaultActivityDisclosureOpen({
      streaming: true,
      waitingForApproval: true,
      pausedForApproval: true,
    }),
    false,
  );
});
