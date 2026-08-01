import assert from "node:assert/strict";
import test from "node:test";
import { preserveSelectedSession } from "./chat-session-state.ts";

const selected = {
  id: "session-general",
  agent_card_id: "agent-1",
  project_id: null,
  customer_id: null,
  title: "墨尔本询价",
  status: "active",
  pending_confirmation: null,
  plan: null,
  created_at: null,
  updated_at: null,
};

test("preserves the selected general session when a binding refresh omits it", () => {
  const previous = {
    items: [selected, { ...selected, id: "session-other" }],
    total: 2,
    page: 1,
    page_size: 50,
  };
  const refreshed = {
    items: [{ ...selected, id: "session-other" }],
    total: 1,
    page: 1,
    page_size: 50,
  };

  const reconciled = preserveSelectedSession(previous, refreshed, selected.id);

  assert.deepEqual(
    reconciled.items.map((item) => item.id),
    ["session-general", "session-other"],
  );
  assert.equal(reconciled.items[0], selected);
});

test("uses the refreshed selected session row when the server returns it", () => {
  const bound = { ...selected, project_id: "project-1" };
  const refreshed = {
    items: [bound],
    total: 1,
    page: 1,
    page_size: 50,
  };

  const reconciled = preserveSelectedSession(
    { ...refreshed, items: [selected] },
    refreshed,
    selected.id,
  );

  assert.equal(reconciled, refreshed);
  assert.equal(reconciled.items[0].project_id, "project-1");
});

test("does not resurrect an unselected session", () => {
  const refreshed = {
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
  };

  assert.equal(
    preserveSelectedSession(
      { ...refreshed, items: [selected] },
      refreshed,
      null,
    ),
    refreshed,
  );
});
