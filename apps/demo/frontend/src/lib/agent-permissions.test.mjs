import assert from "node:assert/strict";
import test from "node:test";
import {
  buildFullAccessPermissionUpdate,
  canRevokePermissionGrant,
  completeCustomPermissionRules,
  deferPermissionUpdate,
  permissionControlUnavailable,
  rebasePermissionUpdate,
  resolvedPermissionProfileOptions,
} from "./agent-permissions.ts";

function permissionState(overrides = {}) {
  return {
    session_id: "session-1",
    revision: 3,
    profile: "request_approval",
    scope: "session",
    expires_at: null,
    active_run: false,
    project_id: "project-1",
    custom_rules: {},
    policy_snapshot: {},
    profile_options: [
      {
        id: "request_approval",
        label: "请求审批",
        description: "严格",
        allowed: true,
        restriction: null,
      },
      {
        id: "auto_review",
        label: "智能审批",
        description: "自动审",
        allowed: false,
        restriction: "Reviewer 未就绪",
      },
      {
        id: "full_access",
        label: "完全访问（业务范围内）",
        description: "业务范围内",
        allowed: true,
        restriction: null,
      },
    ],
    organization: {
      policy_id: "policy-1",
      policy_version: 7,
      is_enabled: true,
      shadow_evaluation: false,
      max_scope: "project",
      max_grant_ttl_seconds: 3600,
      max_full_access_ttl_seconds: 3600,
      reviewer_enabled: false,
      reviewer_eligible_categories: [],
      reviewer_eligible_tools: [],
      denied_categories: [],
      denied_tools: [],
      user_required_categories: [],
      user_required_tools: [],
    },
    grants: [
      {
        id: "grant-1",
        session_id: "session-1",
        project_id: "project-1",
        tool_name: "image_generate",
        category: null,
        scope: "project",
        resource_type: null,
        resource_id: null,
        policy_version: 7,
        created_at: null,
        expires_at: "2026-07-23T12:00:00Z",
        revoked_at: null,
      },
    ],
    pending_decision: null,
    reviewer_overrides: [],
    ...overrides,
  };
}

test("resolves exactly four profiles and preserves organization restrictions", () => {
  const options = resolvedPermissionProfileOptions(permissionState());
  assert.deepEqual(
    options.map((option) => option.id),
    ["request_approval", "auto_review", "full_access", "custom"],
  );
  assert.equal(options[1].allowed, false);
  assert.equal(options[1].restriction, "Reviewer 未就绪");
  assert.equal(options[3].allowed, false);
  assert.equal(options[3].restriction, "组织未开放该模式");
});

test("builds explicit bounded full-access activation only for valid scope and expiry", () => {
  const state = permissionState();
  assert.deepEqual(buildFullAccessPermissionUpdate(state, "project", 1800), {
    expected_revision: 3,
    expected_policy_version: 7,
    profile: "full_access",
    scope: "project",
    expires_in_seconds: 1800,
    acknowledge_full_access: true,
  });
  assert.equal(buildFullAccessPermissionUpdate(state, "project", 7200), null);
  assert.equal(
    buildFullAccessPermissionUpdate(
      permissionState({
        organization: { ...state.organization, max_scope: "session" },
      }),
      "project",
      1800,
    ),
    null,
  );
});

test("completes custom category rules and fails mixed or missing groups to ask-user", () => {
  const rules = completeCustomPermissionRules({
    network: "auto_review",
    external_cost: "auto_review",
    financial: "deny",
  });
  assert.equal(rules.network, "auto_review");
  assert.equal(rules.external_cost, "auto_review");
  assert.equal(rules.financial, "deny");
  assert.equal(rules.local_data, "ask_user");
  assert.equal(rules.destructive, "ask_user");
  assert.equal(rules.workflow, "ask_user");
  assert.equal(rules.export, "ask_user");

  const mixed = completeCustomPermissionRules({
    network: "allow",
    external_cost: "deny",
  });
  assert.equal(mixed.network, "ask_user");
  assert.equal(mixed.external_cost, "ask_user");
});

test("allows next-turn selection during runs while gating grant revocation", () => {
  const state = permissionState();
  assert.equal(permissionControlUnavailable(state, {}), false);
  assert.equal(
    permissionControlUnavailable(permissionState({ active_run: true }), {}),
    false,
  );
  assert.equal(permissionControlUnavailable(state, { pending: true }), true);
  assert.equal(canRevokePermissionGrant(state, "grant-1"), true);
  assert.equal(canRevokePermissionGrant(state, "grant-1", true), false);
  assert.equal(canRevokePermissionGrant(state, "missing"), false);
  assert.equal(
    canRevokePermissionGrant(permissionState({ active_run: true }), "grant-1"),
    false,
  );
});

test("stores a permission intent and rebases it on the latest revision", () => {
  const intent = deferPermissionUpdate({
    expected_revision: 3,
    expected_policy_version: 7,
    profile: "auto_review",
    scope: "session",
  });
  assert.deepEqual(intent, {
    profile: "auto_review",
    scope: "session",
  });
  assert.deepEqual(
    rebasePermissionUpdate(
      permissionState({
        revision: 9,
        organization: {
          ...permissionState().organization,
          policy_version: 11,
        },
      }),
      intent,
    ),
    {
      expected_revision: 9,
      expected_policy_version: 11,
      profile: "auto_review",
      scope: "session",
    },
  );
});
