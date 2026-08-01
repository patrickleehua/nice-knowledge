import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_FULL_ACCESS_TTL_SECONDS,
  MAX_GRANT_TTL_SECONDS,
  draftToPayload,
  formatTtlLabel,
  groupToolsByGroup,
  hasPolicyChanges,
  policyRelevantTools,
  policyToDraft,
  toggleValue,
  validatePolicyDraft,
} from "./org-agent-permissions.ts";

function policy(overrides = {}) {
  return {
    id: "policy-1",
    version: 3,
    default_profile: "request_approval",
    allowed_profiles: ["request_approval"],
    hard_rules: {
      compatibility_mode: true,
      tool_decisions: { image_generate: "ask_user" },
      category_decisions: { financial: "ask_user" },
      denied_tools: ["quote_cost_item_delete"],
      denied_categories: ["destructive"],
      user_required_categories: ["financial"],
    },
    reviewer_enabled: false,
    reviewer_eligible_categories: [],
    reviewer_eligible_tools: [],
    max_scope: "resource",
    max_grant_ttl_seconds: 28800,
    max_full_access_ttl_seconds: 3600,
    is_enabled: true,
    shadow_evaluation: false,
    is_active: true,
    created_at: "2026-07-25T00:00:00Z",
    reviewer_route_healthy: true,
    ...overrides,
  };
}

const KNOWN_TOOLS = [
  "image_generate",
  "ota_apply",
  "quote_cost_item_delete",
  "kb_list",
];

function options(overrides = {}) {
  return { reviewerRouteHealthy: true, knownTools: KNOWN_TOOLS, ...overrides };
}

test("draft lifts hard rules into editable fields", () => {
  const draft = policyToDraft(policy());
  assert.deepEqual(draft.denied_tools, ["quote_cost_item_delete"]);
  assert.deepEqual(draft.denied_categories, ["destructive"]);
  assert.deepEqual(draft.user_required_categories, ["financial"]);
  assert.deepEqual(draft.user_required_tools, []);
  assert.deepEqual(draft.reviewer_overridable_categories, []);
});

test("payload preserves hard rules the page does not edit", () => {
  const current = policy();
  const draft = policyToDraft(current);
  draft.denied_tools = ["ota_apply"];
  const payload = draftToPayload(draft, current);

  assert.equal(payload.expected_version, 3);
  // 页面不编辑这三项,保存时必须原样带回,否则一次保存就静默清空。
  assert.equal(payload.hard_rules.compatibility_mode, true);
  assert.deepEqual(payload.hard_rules.tool_decisions, {
    image_generate: "ask_user",
  });
  assert.deepEqual(payload.hard_rules.category_decisions, {
    financial: "ask_user",
  });
  assert.deepEqual(payload.hard_rules.denied_tools, ["ota_apply"]);
});

test("payload omits absent optional rules instead of sending undefined", () => {
  const current = policy({ hard_rules: {} });
  const payload = draftToPayload(policyToDraft(current), current);
  assert.equal("compatibility_mode" in payload.hard_rules, false);
  assert.equal("tool_decisions" in payload.hard_rules, false);
  assert.equal("category_decisions" in payload.hard_rules, false);
  assert.deepEqual(payload.hard_rules.denied_tools, []);
});

test("request approval cannot be disabled", () => {
  const draft = policyToDraft(policy());
  draft.allowed_profiles = ["custom"];
  draft.default_profile = "custom";
  const issues = validatePolicyDraft(draft, options());
  assert.equal(
    issues.some((issue) => issue.field === "allowed_profiles"),
    true,
  );
});

test("default profile must be allowed and never full access", () => {
  const draft = policyToDraft(policy());
  draft.allowed_profiles = ["request_approval", "custom"];
  draft.default_profile = "auto_review";
  assert.equal(
    validatePolicyDraft(draft, options()).some(
      (issue) => issue.field === "default_profile",
    ),
    true,
  );

  draft.allowed_profiles = ["request_approval", "full_access"];
  draft.default_profile = "full_access";
  const messages = validatePolicyDraft(draft, options())
    .filter((issue) => issue.field === "default_profile")
    .map((issue) => issue.message);
  assert.equal(messages.length, 1);
  assert.match(messages[0], /不能设为组织默认/);
});

test("auto review requires reviewer enabled, eligibility, and a healthy route", () => {
  const draft = policyToDraft(policy());
  draft.allowed_profiles = ["request_approval", "auto_review"];

  const blocked = validatePolicyDraft(draft, options({ reviewerRouteHealthy: false }))
    .filter((issue) => issue.field === "reviewer")
    .map((issue) => issue.message);
  assert.equal(blocked.length, 3);

  draft.reviewer_enabled = true;
  draft.reviewer_eligible_categories = ["external_cost"];
  assert.deepEqual(
    validatePolicyDraft(draft, options()).filter(
      (issue) => issue.field === "reviewer",
    ),
    [],
  );
});

test("auto review accepts tool-only eligibility", () => {
  const draft = policyToDraft(policy());
  draft.allowed_profiles = ["request_approval", "auto_review"];
  draft.reviewer_enabled = true;
  draft.reviewer_eligible_tools = ["image_generate"];
  assert.deepEqual(
    validatePolicyDraft(draft, options()).filter(
      (issue) => issue.field === "reviewer",
    ),
    [],
  );
});

test("overridable categories must be a subset of reviewer eligible ones", () => {
  const draft = policyToDraft(policy());
  draft.reviewer_eligible_categories = ["external_cost"];
  draft.reviewer_overridable_categories = ["external_cost", "financial"];
  const issue = validatePolicyDraft(draft, options()).find(
    (item) => item.field === "hard_rules",
  );
  assert.match(issue.message, /财务变更/);

  draft.reviewer_overridable_categories = ["external_cost"];
  assert.deepEqual(
    validatePolicyDraft(draft, options()).filter(
      (item) => item.field === "hard_rules",
    ),
    [],
  );
});

test("tool names outside the catalog are rejected before submitting", () => {
  const draft = policyToDraft(policy());
  draft.denied_tools = ["forged_super_tool"];
  draft.user_required_tools = ["kb_list"];
  const issue = validatePolicyDraft(draft, options()).find(
    (item) => item.field === "hard_rules",
  );
  assert.match(issue.message, /forged_super_tool/);
  assert.equal(issue.message.includes("kb_list"), false);
});

test("ttl bounds match the backend field constraints", () => {
  const draft = policyToDraft(policy());
  draft.max_grant_ttl_seconds = 30;
  draft.max_full_access_ttl_seconds = MAX_FULL_ACCESS_TTL_SECONDS + 1;
  assert.equal(
    validatePolicyDraft(draft, options()).filter(
      (issue) => issue.field === "limits",
    ).length,
    2,
  );

  draft.max_grant_ttl_seconds = MAX_GRANT_TTL_SECONDS;
  draft.max_full_access_ttl_seconds = MAX_FULL_ACCESS_TTL_SECONDS;
  assert.deepEqual(
    validatePolicyDraft(draft, options()).filter(
      (issue) => issue.field === "limits",
    ),
    [],
  );

  draft.max_grant_ttl_seconds = 90.5;
  assert.equal(
    validatePolicyDraft(draft, options()).some(
      (issue) => issue.field === "limits",
    ),
    true,
  );
});

test("a valid strict policy produces no issues", () => {
  assert.deepEqual(validatePolicyDraft(policyToDraft(policy()), options()), []);
});

test("change detection ignores selection order", () => {
  const current = policy({
    allowed_profiles: ["request_approval", "custom"],
    hard_rules: { denied_categories: ["destructive", "financial"] },
  });
  const draft = policyToDraft(current);
  draft.allowed_profiles = ["custom", "request_approval"];
  draft.denied_categories = ["financial", "destructive"];
  assert.equal(hasPolicyChanges(draft, current), false);

  draft.denied_categories = ["financial"];
  assert.equal(hasPolicyChanges(draft, current), true);
});

test("catalog defaults to tools that cannot run unattended", () => {
  const catalog = [
    {
      name: "kb_list",
      label: "项目列表",
      group: "项目",
      description: "",
      effect: "read",
      risk: "routine",
      categories: ["local_data"],
      delegation: "automatic",
    },
    {
      name: "ota_apply",
      label: "写入外部结果",
      group: "比价",
      description: "",
      effect: "write",
      risk: "sensitive",
      categories: ["financial", "local_data"],
      delegation: "reviewable",
    },
  ];
  assert.deepEqual(
    policyRelevantTools(catalog).map((tool) => tool.name),
    ["ota_apply"],
  );
  assert.equal(policyRelevantTools(catalog, true).length, 2);
  assert.deepEqual(
    groupToolsByGroup(catalog).map((entry) => entry.group),
    ["项目", "比价"],
  );
});

test("ttl labels use the coarsest exact unit", () => {
  assert.equal(formatTtlLabel(86400), "1 天");
  assert.equal(formatTtlLabel(28800), "8 小时");
  assert.equal(formatTtlLabel(1800), "30 分钟");
  assert.equal(formatTtlLabel(90), "90 秒");
  assert.equal(formatTtlLabel(0), "—");
});

test("toggle keeps values unique", () => {
  assert.deepEqual(toggleValue(["a"], "b", true), ["a", "b"]);
  assert.deepEqual(toggleValue(["a", "b"], "b", true), ["a", "b"]);
  assert.deepEqual(toggleValue(["a", "b"], "b", false), ["a"]);
});
