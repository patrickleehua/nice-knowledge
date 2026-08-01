import assert from "node:assert/strict";
import test from "node:test";
import {
  reconcileToolResults,
  toolResultKeysForItems,
} from "./tool-result-reconciliation.ts";

function toolResultItem({
  key,
  runId,
  persistedRecordId,
  source,
  toolCallId = "call-1",
  title = "季度总结",
}) {
  return {
    key,
    role: "run",
    runId,
    persistedRecordId,
    source,
    events: [
      {
        type: "tool.result",
        seq: 1,
        tool_call_id: toolCallId,
        name: "report_build",
        ok: true,
        output: { report_id: "report-1", title, status: "created" },
      },
    ],
  };
}

const hasRenderer = (name) => name === "report_build";

test("persisted result replaces its live projection by run and tool call", () => {
  const live = toolResultItem({
    key: "live-run",
    runId: "run-1",
    source: "live",
  });
  const persisted = toolResultItem({
    key: "message-1",
    runId: "run-1",
    persistedRecordId: "message-1",
    source: "persisted",
  });
  const reconciled = reconcileToolResults([live, persisted], hasRenderer);

  assert.deepEqual([...toolResultKeysForItems([live], reconciled)], []);
  assert.deepEqual(
    [...toolResultKeysForItems([persisted], reconciled)],
    [["call-1", "run:run-1:tool:call-1"]],
  );
});

test("equivalent operations with different execution identities remain distinct", () => {
  const first = toolResultItem({ key: "run-1", runId: "run-1", source: "live" });
  const second = toolResultItem({
    key: "run-2",
    runId: "run-2",
    source: "live",
  });
  const reconciled = reconcileToolResults([first, second], hasRenderer);

  assert.deepEqual(
    [
      ...toolResultKeysForItems([first], reconciled).values(),
      ...toolResultKeysForItems([second], reconciled).values(),
    ],
    ["run:run-1:tool:call-1", "run:run-2:tool:call-1"],
  );
});

test("legacy persisted results fall back to record identity without content dedupe", () => {
  const first = toolResultItem({
    key: "message-1",
    persistedRecordId: "message-1",
    source: "persisted",
  });
  const second = toolResultItem({
    key: "message-2",
    persistedRecordId: "message-2",
    source: "persisted",
  });
  const reconciled = reconcileToolResults([first, second], hasRenderer);

  assert.deepEqual(
    [
      ...toolResultKeysForItems([first], reconciled).values(),
      ...toolResultKeysForItems([second], reconciled).values(),
    ],
    ["message:message-1", "message:message-2"],
  );
});
