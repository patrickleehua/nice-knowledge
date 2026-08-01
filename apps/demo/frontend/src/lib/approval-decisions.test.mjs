import assert from "node:assert/strict";
import test from "node:test";
import {
  buildBundleConfirmationAction,
  confirmationKey,
  legacyConfirmationAction,
} from "./approval-decisions.ts";

function bundle() {
  return {
    kind: "approval_bundle",
    bundle_id: "bundle-1",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: "call-image",
        status: "pending",
        eligible_scopes: ["once", "session_tool"],
      },
      {
        tool_call_id: "call-delete",
        status: "pending",
        eligible_scopes: ["once"],
      },
      {
        tool_call_id: "call-read",
        status: "allowed",
        eligible_scopes: [],
      },
    ],
  };
}

test("builds independent bundle decisions with a session grant and trimmed note", () => {
  const action = buildBundleConfirmationAction(bundle(), {
    "call-image": { decision: "approve", scope: "session_tool" },
    "call-delete": {
      decision: "deny",
      scope: "once",
      note: "  保留成本版本  ",
    },
  });
  assert.deepEqual(action, {
    bundle_id: "bundle-1",
    decisions: [
      {
        tool_call_id: "call-image",
        decision: "approve",
        scope: "session_tool",
        note: undefined,
      },
      {
        tool_call_id: "call-delete",
        decision: "deny",
        scope: "once",
        note: "保留成本版本",
      },
    ],
  });
});

test("rejects incomplete or ineligible client-side bundle decisions", () => {
  assert.equal(
    buildBundleConfirmationAction(bundle(), {
      "call-image": { decision: "approve", scope: "once" },
    }),
    null,
  );
  assert.equal(
    buildBundleConfirmationAction(bundle(), {
      "call-image": { decision: "approve", scope: "once" },
      "call-delete": { decision: "approve", scope: "session_tool" },
    }),
    null,
  );
});

test("keeps legacy approve and reject as exact one-time decisions", () => {
  const pending = {
    tool_call_id: "legacy-call",
    name: "image_generate",
    input: {},
    summary: "生成图片",
  };
  assert.deepEqual(legacyConfirmationAction(pending, true), {
    tool_call_id: "legacy-call",
    approved: true,
  });
  assert.deepEqual(legacyConfirmationAction(pending, false), {
    tool_call_id: "legacy-call",
    approved: false,
  });
});

test("uses stable keys for optimistic approval dismissal", () => {
  assert.equal(
    confirmationKey({
      kind: "approval_bundle",
      bundle_id: "bundle-1",
      items: [],
    }),
    "bundle:bundle-1",
  );
  assert.equal(
    confirmationKey({
      tool_call_id: "call-1",
      name: "tool",
      input: {},
      summary: "",
    }),
    "tool:call-1",
  );
  assert.equal(confirmationKey(null), null);
});
