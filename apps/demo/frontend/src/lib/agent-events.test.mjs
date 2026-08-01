import assert from "node:assert/strict";
import test from "node:test";
import {
  buildActivityTimeline,
  buildConversationAnchors,
  buildImageArtifacts,
  buildImageArtifactsByRun,
  buildPermissionReplay,
  buildPermissionTimelineProjection,
  flattenAgentEventGroups,
  groupAssistantSections,
  groupConversationItems,
  isSemanticAgentEvent,
  historyToItems,
  toolResultSucceeded,
} from "./agent-events.ts";

test("coalesces real Reviewer events into tool execution details", () => {
  const events = [
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "image-1",
      name: "image_generate",
      input: { prompt: "海报" },
    },
    {
      type: "reviewer.requested",
      seq: 2,
      tool_call_id: "image-1",
      name: "image_generate",
      reason_code: "profile_auto_review",
      matched_rule: null,
      action_hash: "a".repeat(64),
      risk: "sensitive",
      categories: ["external_cost"],
    },
    {
      type: "reviewer.decision",
      seq: 3,
      tool_call_id: "image-1",
      name: "image_generate",
      decision: "approve",
      reason_code: "reviewer_approved",
      rationale: "用户明确要求生成这张配图。",
      risk_flags: ["external_cost"],
      override_eligible: false,
      circuit_breaker: false,
    },
    {
      type: "tool.result",
      seq: 4,
      tool_call_id: "image-1",
      name: "image_generate",
      ok: true,
      output: { status: "ok" },
    },
  ];
  const timeline = buildActivityTimeline(events);
  assert.deepEqual(
    timeline.items.map((item) => item.type),
    ["tool"],
  );
  assert.equal(timeline.items[0].run.permission.reviewer.decision, "approve");
  assert.equal(
    timeline.items[0].run.permission.reviewer.rationale,
    "用户明确要求生成这张配图。",
  );
});

test("projects one approval bundle at its request position and suppresses legacy duplication", () => {
  const pending = {
    kind: "approval_bundle",
    bundle_id: "bundle-1",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: "call-1",
        name: "image_generate",
        status: "pending",
      },
    ],
  };
  const completed = {
    ...pending,
    status: "completed",
    items: [{ ...pending.items[0], status: "approved", decision: "approve" }],
  };
  const firstRun = [
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "call-1",
      name: "image_generate",
      input: {},
    },
    { type: "approval.bundle.requested", seq: 2, bundle: pending },
    {
      type: "tool.confirm",
      seq: 3,
      tool_call_id: "call-1",
      name: "image_generate",
      input: {},
      summary: "生成图片",
    },
  ];
  const projection = buildPermissionTimelineProjection([
    firstRun,
    [{ type: "approval.bundle.completed", seq: 1, bundle: completed }],
  ]);
  const timeline = buildActivityTimeline(firstRun, {
    permissionProjection: projection,
  });
  assert.deepEqual(
    timeline.items.map((item) => item.type),
    ["tool", "approval"],
  );
  assert.equal(timeline.items[0].run.status, "running");
  assert.equal(timeline.items[1].pending.status, "completed");
});

test("projects approval decisions before the completed bundle arrives", () => {
  const pending = {
    kind: "approval_bundle",
    bundle_id: "bundle-decision",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: "call-1",
        name: "image_generate",
        status: "pending",
        decision: null,
        decision_scope: null,
      },
    ],
  };
  const projection = buildPermissionTimelineProjection([
    [{ type: "approval.bundle.requested", seq: 1, bundle: pending }],
    [
      {
        type: "approval.bundle.decision",
        seq: 1,
        bundle_id: pending.bundle_id,
        tool_call_id: "call-1",
        decision: "approve",
        scope: "once",
        note: null,
      },
    ],
  ]);
  const bundle = projection.bundles.get(pending.bundle_id);
  assert.equal(bundle.status, "ready");
  assert.equal(bundle.items[0].status, "approved");
  assert.equal(bundle.items[0].decision, "approve");
});

test("coalesces denial, circuit and exact override lifecycle by original tool call", () => {
  const override = {
    candidate_id: "candidate-1",
    tool_call_id: "call-denied",
    name: "external_write",
    action_hash: "b".repeat(64),
  };
  const firstRun = [
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "call-denied",
      name: "external_write",
      input: {},
    },
    {
      type: "reviewer.circuit_breaker",
      seq: 2,
      tool_call_id: "call-denied",
      name: "external_write",
      denial_count: 3,
      action_hash: "b".repeat(64),
    },
    {
      type: "reviewer.decision",
      seq: 3,
      tool_call_id: "call-denied",
      name: "external_write",
      decision: "deny",
      reason_code: "reviewer_circuit_breaker",
      rationale: "本轮已连续三次未通过独立审批。",
      risk_flags: ["financial"],
      override_eligible: true,
      circuit_breaker: true,
    },
    { type: "reviewer.override.available", seq: 4, override },
  ];
  const secondRun = [
    { type: "reviewer.override.consumed", seq: 1, override },
    {
      type: "reviewer.override.completed",
      seq: 2,
      candidate_id: "candidate-1",
      tool_call_id: "override-call",
      override_of: "call-denied",
      name: "external_write",
      ok: false,
      reason_code: "reviewer_override_not_allowed",
      action_hash: "b".repeat(64),
    },
  ];
  const projection = buildPermissionTimelineProjection([firstRun, secondRun]);
  const reviewer = projection.reviewerByToolCall.get("call-denied");
  assert.equal(reviewer.decision, "deny");
  assert.equal(reviewer.circuitBreaker, true);
  assert.equal(reviewer.denialCount, 3);
  assert.equal(reviewer.override.status, "completed");
  assert.equal(reviewer.override.ok, false);
  assert.equal(reviewer.override.reasonCode, "reviewer_override_not_allowed");
});

test("replays permission events in order without reopening completed work", () => {
  const bundle = {
    kind: "approval_bundle",
    bundle_id: "11111111-1111-1111-1111-111111111111",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: "call-1",
        status: "pending",
      },
    ],
  };
  const replay = buildPermissionReplay([
    {
      type: "reviewer.override.completed",
      seq: 8,
      candidate_id: "22222222-2222-2222-2222-222222222222",
    },
    {
      type: "approval.bundle.completed",
      seq: 6,
      bundle: {
        ...bundle,
        status: "completed",
        items: [{ ...bundle.items[0], status: "approved" }],
      },
    },
    { type: "approval.bundle.requested", seq: 3, bundle },
    {
      type: "approval.bundle.decision",
      seq: 4,
      bundle_id: bundle.bundle_id,
      tool_call_id: "call-1",
      decision: "approve",
    },
    {
      type: "reviewer.override.available",
      seq: 2,
      override: {
        candidate_id: "22222222-2222-2222-2222-222222222222",
      },
    },
    {
      type: "reviewer.override.consumed",
      seq: 7,
      override: {
        candidate_id: "22222222-2222-2222-2222-222222222222",
      },
    },
    { type: "policy.snapshot", seq: 1, profile: "request_approval" },
  ]);

  assert.deepEqual(
    replay.events.map((event) => event.seq),
    [1, 2, 3, 4, 6, 7, 8],
  );
  assert.deepEqual(replay.pendingBundles, []);
  assert.deepEqual(replay.completedBundleIds, [bundle.bundle_id]);
  assert.deepEqual(replay.availableOverrides, []);
  assert.deepEqual(replay.completedOverrideIds, [
    "22222222-2222-2222-2222-222222222222",
  ]);
});

test("builds concise anchors for conversation quick navigation", () => {
  const groups = groupConversationItems([
    { key: "u1", role: "user", content: "请帮我创建一个新加坡五日项目" },
    { key: "a1", role: "assistant", content: "好的" },
    { key: "u2", role: "user", content: "继续汇总检索结果" },
    { key: "a2", role: "assistant", content: "完成" },
  ]);

  assert.deepEqual(buildConversationAnchors(groups), [
    {
      id: "conversation-turn-0",
      groupIndex: 0,
      label: "请帮我创建一个新加坡五日项目",
    },
    {
      id: "conversation-turn-2",
      groupIndex: 2,
      label: "继续汇总检索结果",
    },
  ]);
});

test("builds semantic waterfall blocks without rendering token deltas", () => {
  const timeline = buildActivityTimeline([
    { type: "thought.delta", seq: 1, text: "逐字推理增量" },
    { type: "thought", seq: 2, text: "先分析需求" },
    {
      type: "assistant.message",
      seq: 3,
      text: "我先读取项目资料。",
    },
    {
      type: "tool.call",
      seq: 4,
      tool_call_id: "tool-1",
      name: "kb_search",
      input: {},
    },
    {
      type: "tool.progress",
      seq: 5,
      parent_tool_call_id: "tool-1",
      text: "检索背景资料",
    },
    {
      type: "tool.result",
      seq: 6,
      tool_call_id: "tool-1",
      name: "kb_search",
      ok: true,
      output: { counts: { chunks: 3 } },
    },
    { type: "text.delta", seq: 7, text: "逐字回答增量" },
    { type: "text", seq: 8, text: "资料读取完成。" },
  ]);

  assert.deepEqual(
    timeline.items.map((item) => item.type),
    ["thought", "message", "tool"],
  );
  assert.equal(timeline.items[0].text, "先分析需求");
  assert.equal(timeline.items[1].text, "我先读取项目资料。");
  assert.equal(timeline.items[2].run.status, "ok");
  assert.deepEqual(timeline.items[2].run.progress, ["检索背景资料"]);
  assert.equal(timeline.finalText, "资料读取完成。");
  assert.equal(JSON.stringify(timeline).includes("逐字"), false);
});

test("classifies transport deltas as non-semantic events", () => {
  assert.equal(
    isSemanticAgentEvent({ type: "text.delta", seq: 1, text: "片段" }),
    false,
  );
  assert.equal(
    isSemanticAgentEvent({
      type: "thought.delta",
      seq: 2,
      text: "片段",
    }),
    false,
  );
  assert.equal(
    isSemanticAgentEvent({
      type: "assistant.message",
      seq: 3,
      text: "公开过程说明",
    }),
    true,
  );
});

test("keeps error records in the execution waterfall", () => {
  const timeline = buildActivityTimeline([
    { type: "error", seq: 2, message: "生成失败" },
    { type: "text", seq: 3, text: "请补充资料" },
  ]);

  assert.deepEqual(
    timeline.items.map((item) => item.type),
    ["error"],
  );
  assert.equal(timeline.finalText, "请补充资料");
});

// stage.update 事件已从后端删除(MIGRATION-PLAN §5.4:SDK 不按工具名前缀推导
// 业务阶段)。未知事件类型必须被静默丢弃而不是崩掉整条时间线。
test("drops unknown event types instead of throwing", () => {
  const timeline = buildActivityTimeline([
    { type: "stage.update", seq: 1, stage: "whatever" },
    { type: "text", seq: 2, text: "正文" },
  ]);

  assert.deepEqual(timeline.items, []);
  assert.equal(timeline.finalText, "正文");
});

test("orders replayed execution records by SSE sequence", () => {
  const timeline = buildActivityTimeline([
    { type: "error", seq: 8, message: "稍后重试" },
    {
      type: "tool.result",
      seq: 6,
      tool_call_id: "tool-1",
      name: "kb_search",
      ok: true,
      output: { hits: 3 },
    },
    { type: "thought", seq: 2, text: "先梳理已知信息" },
    {
      type: "tool.call",
      seq: 4,
      tool_call_id: "tool-1",
      name: "kb_search",
      input: {},
    },
  ]);

  assert.deepEqual(
    timeline.items.map((item) => `${item.type}:${item.seq}`),
    ["thought:2", "tool:4", "error:8"],
  );
  assert.equal(timeline.items[1].run.status, "ok");
});

test("groups adjacent assistant and tool records into one Agent turn", () => {
  const items = [
    { key: "u1", role: "user", content: "创建项目" },
    { key: "a1", role: "assistant", content: "我来处理" },
    { key: "t1", role: "run", events: [] },
    { key: "a2", role: "assistant", content: "已经完成" },
    { key: "u2", role: "user", content: "继续" },
  ];

  const groups = groupConversationItems(items);

  assert.equal(groups.length, 3);
  assert.equal(groups[0].role, "user");
  assert.equal(groups[1].role, "assistant");
  assert.deepEqual(
    groups[1].items.map((item) => item.key),
    ["a1", "t1", "a2"],
  );
  assert.equal(groups[2].role, "user");
});

test("starts a single Agent group when history begins with tools", () => {
  const groups = groupConversationItems([
    { key: "t1", role: "run", events: [] },
    { key: "a1", role: "assistant", content: "完成" },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].role, "assistant");
  assert.deepEqual(
    groups[0].items.map((item) => item.key),
    ["t1", "a1"],
  );
});

test("merges adjacent run records without reordering assistant narration", () => {
  const sections = groupAssistantSections([
    { key: "a1", role: "assistant", content: "先说明" },
    { key: "t1", role: "run", events: [] },
    { key: "t2", role: "run", events: [] },
    { key: "a2", role: "assistant", content: "中间说明" },
    { key: "t3", role: "run", events: [] },
  ]);

  assert.deepEqual(
    sections.map((section) => section.type),
    ["text", "run", "text", "run"],
  );
  assert.deepEqual(
    sections[1].items.map((item) => item.key),
    ["t1", "t2"],
  );
  assert.equal(sections[2].item.key, "a2");
});

test("keeps completed runs as separate execution disclosures", () => {
  const sections = groupAssistantSections([
    {
      key: "run-1",
      role: "run",
      events: [
        { type: "thought", seq: 1, text: "第一轮" },
        {
          type: "turn.done",
          seq: 2,
          reason: "confirm",
          usage: { input_tokens: 1, output_tokens: 1 },
        },
      ],
    },
    {
      key: "run-2",
      role: "run",
      events: [
        { type: "thought", seq: 1, text: "确认后继续" },
        {
          type: "turn.done",
          seq: 2,
          reason: "end_turn",
          usage: { input_tokens: 1, output_tokens: 1 },
        },
      ],
    },
  ]);

  assert.equal(sections.length, 2);
  assert.deepEqual(
    sections.map((section) => section.items.map((item) => item.key)),
    [["run-1"], ["run-2"]],
  );
});

test("merges an approval pause and its matching resume into one logical run", () => {
  const bundle = {
    kind: "approval_bundle",
    bundle_id: "bundle-continued",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [{ tool_call_id: "image-continued", status: "pending" }],
  };
  const sections = groupAssistantSections([
    {
      key: "pause",
      role: "run",
      events: [
        {
          type: "tool.call",
          seq: 1,
          tool_call_id: "image-continued",
          name: "image_generate",
          input: {},
        },
        { type: "approval.bundle.requested", seq: 2, bundle },
        {
          type: "turn.done",
          seq: 3,
          reason: "confirm",
          usage: { input_tokens: 1, output_tokens: 1 },
        },
      ],
    },
    {
      key: "resume",
      role: "run",
      events: [
        {
          type: "approval.bundle.decision",
          seq: 1,
          bundle_id: bundle.bundle_id,
          tool_call_id: "image-continued",
          decision: "approve",
          scope: "once",
        },
        {
          type: "tool.call",
          seq: 2,
          tool_call_id: "image-continued",
          name: "image_generate",
          input: {},
        },
      ],
    },
  ]);

  assert.equal(sections.length, 1);
  assert.deepEqual(
    sections[0].items.map((item) => item.key),
    ["pause", "resume"],
  );
  assert.deepEqual(
    flattenAgentEventGroups(
      sections[0].items.map((item) => item.events),
    ).map((event) => event.seq),
    [1, 2, 3, 4, 5],
  );
});

test("projects a running image placeholder into the successful artifact in place", () => {
  const call = {
    type: "tool.call",
    seq: 4,
    tool_call_id: "img-1",
    name: "image_generate",
    input: { prompt: "新加坡夜景海报", n: 2, size: "1536x1024" },
  };
  const running = buildImageArtifacts([call]);
  assert.equal(running.length, 1);
  assert.equal(running[0].status, "running");
  assert.equal(running[0].requestedCount, 2);
  assert.deepEqual(
    [running[0].requestedWidth, running[0].requestedHeight],
    [1536, 1024],
  );

  const completed = buildImageArtifacts([
    call,
    {
      type: "tool.result",
      seq: 7,
      tool_call_id: "img-1",
      name: "image_generate",
      ok: true,
      output: {
        status: "ok",
        images: [
          {
            filename: "one.png",
            url: "/genimg/one.png",
            content_type: "image/png",
            size_bytes: 100,
            width: 1200,
            height: 800,
          },
        ],
      },
    },
  ]);
  assert.equal(completed.length, 1);
  assert.equal(completed[0].id, "img-1");
  assert.equal(completed[0].status, "success");
  assert.deepEqual(
    [completed[0].images[0].width, completed[0].images[0].height],
    [1200, 800],
  );
});

test("does not project an image placeholder while approval is pending", () => {
  const call = {
    type: "tool.call",
    seq: 1,
    tool_call_id: "pending-image",
    name: "image_generate",
    input: { prompt: "新加坡海报", n: 1, size: "1024x1536" },
  };
  const bundle = {
    kind: "approval_bundle",
    bundle_id: "pending-image-bundle",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: call.tool_call_id,
        name: call.name,
        status: "pending",
        decision: null,
        decision_scope: null,
      },
    ],
  };

  assert.deepEqual(
    buildImageArtifacts([
      call,
      { type: "approval.bundle.requested", seq: 2, bundle },
    ]),
    [],
  );
});

test("projects a placeholder only when an approved image call resumes", () => {
  const call = {
    type: "tool.call",
    seq: 1,
    tool_call_id: "resumed-image",
    name: "image_generate",
    input: { prompt: "新加坡海报", n: 2, size: "1536x1024" },
  };
  const bundle = {
    kind: "approval_bundle",
    bundle_id: "resumed-image-bundle",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: call.tool_call_id,
        name: call.name,
        status: "pending",
        decision: null,
        decision_scope: null,
      },
    ],
  };
  const approvedButNotResumed = [
    call,
    { type: "approval.bundle.requested", seq: 2, bundle },
    {
      type: "approval.bundle.decision",
      seq: 3,
      bundle_id: bundle.bundle_id,
      tool_call_id: call.tool_call_id,
      decision: "approve",
      scope: "once",
      note: null,
    },
  ];

  assert.deepEqual(buildImageArtifacts(approvedButNotResumed), []);

  const artifacts = buildImageArtifacts([
    ...approvedButNotResumed,
    { ...call, seq: 4 },
  ]);
  assert.equal(artifacts.length, 1);
  assert.equal(artifacts[0].status, "running");
  assert.equal(artifacts[0].requestedCount, 2);
  assert.equal(artifacts[0].seq, 1);
});

test("keeps a resumed image placeholder at its original run position", () => {
  const call = {
    type: "tool.call",
    seq: 1,
    tool_call_id: "positioned-image",
    name: "image_generate",
    input: { prompt: "新加坡海报", n: 1, size: "1024x1536" },
  };
  const bundle = {
    kind: "approval_bundle",
    bundle_id: "positioned-image-bundle",
    model_hop: 1,
    status: "pending",
    requested_at: "2026-07-23T00:00:00Z",
    policy_snapshot: {},
    assistant_text: null,
    items: [
      {
        tool_call_id: call.tool_call_id,
        name: call.name,
        status: "pending",
        decision: null,
        decision_scope: null,
      },
    ],
  };
  const runs = buildImageArtifactsByRun([
    [call, { type: "approval.bundle.requested", seq: 2, bundle }],
    [
      {
        type: "approval.bundle.decision",
        seq: 1,
        bundle_id: bundle.bundle_id,
        tool_call_id: call.tool_call_id,
        decision: "approve",
        scope: "once",
        note: null,
      },
      { ...call, seq: 2 },
    ],
  ]);

  assert.equal(runs[0].length, 1);
  assert.equal(runs[0][0].status, "running");
  assert.deepEqual(runs[1], []);
});

test("does not project a legacy image placeholder before its decision", () => {
  const call = {
    type: "tool.call",
    seq: 1,
    tool_call_id: "legacy-pending-image",
    name: "image_generate",
    input: { prompt: "新加坡海报", n: 1, size: "1024x1024" },
  };
  const confirm = {
    type: "tool.confirm",
    seq: 2,
    tool_call_id: call.tool_call_id,
    name: call.name,
    input: call.input,
    message: "是否允许生成图片？",
  };

  assert.deepEqual(buildImageArtifacts([call, confirm]), []);
  const decision = {
    type: "approval.legacy.decision",
    seq: 3,
    tool_call_id: call.tool_call_id,
    name: call.name,
    decision: "approve",
    decision_source: "user",
    scope: "once",
    reason_code: "user_approved",
    categories: [],
    policy_id: null,
    policy_version: 1,
    profile: "request_approval",
    permission_scope: "session",
  };
  assert.deepEqual(buildImageArtifacts([call, confirm, decision]), []);
  assert.equal(
    buildImageArtifacts([call, confirm, decision, { ...call, seq: 4 }])[0]
      .status,
    "running",
  );
});

test("coalesces an approved image across pause and resume runs into one position", () => {
  const call = {
    type: "tool.call",
    seq: 1,
    tool_call_id: "approved-image",
    name: "image_generate",
    input: { prompt: "新加坡海报", n: 1, size: "1024x1536" },
  };
  const runs = buildImageArtifactsByRun([
    [
      call,
      {
        type: "turn.done",
        seq: 2,
        reason: "confirm",
        usage: { input_tokens: 1, output_tokens: 1 },
      },
    ],
    [
      call,
      {
        type: "tool.result",
        seq: 2,
        tool_call_id: "approved-image",
        name: "image_generate",
        ok: true,
        output: {
          status: "ok",
          error: null,
          images: [
            {
              filename: "approved.png",
              url: "/genimg/approved.png",
              content_type: "image/png",
              size_bytes: 100,
              width: 1024,
              height: 1536,
            },
          ],
        },
      },
    ],
  ]);

  assert.equal(runs.length, 2);
  assert.equal(runs[0].length, 1);
  assert.equal(runs[0][0].status, "success");
  assert.equal(runs[0][0].prompt, "新加坡海报");
  assert.deepEqual(runs[1], []);
});

test("reconciles a legacy false transport flag with a successful image result", () => {
  const output = {
    status: "ok",
    error: null,
    images: [
      {
        filename: "legacy-runtime.png",
        url: "/genimg/legacy-runtime.png",
        content_type: "image/png",
        size_bytes: 10,
        width: 10,
        height: 8,
      },
    ],
  };
  assert.equal(toolResultSucceeded(false, output), true);
  const timeline = buildActivityTimeline([
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "legacy-runtime",
      name: "image_generate",
      input: {},
    },
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "legacy-runtime",
      name: "image_generate",
      ok: false,
      output,
    },
  ]);
  assert.equal(timeline.items[0].run.status, "ok");
  assert.equal(
    buildImageArtifacts([
      {
        type: "tool.result",
        seq: 1,
        tool_call_id: "legacy-runtime",
        name: "image_generate",
        ok: false,
        output,
      },
    ])[0].status,
    "success",
  );
});

test("suppresses success artifacts for failures and rejections", () => {
  const base = {
    type: "tool.call",
    seq: 1,
    name: "image_generate",
    input: { prompt: "海报", n: 1, size: null },
  };
  const failed = buildImageArtifacts([
    { ...base, tool_call_id: "failed" },
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "failed",
      name: "image_generate",
      ok: false,
      output: { error: "图片服务暂时不可用" },
    },
  ]);
  const rejected = buildImageArtifacts([
    { ...base, tool_call_id: "rejected" },
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "rejected",
      name: "image_generate",
      ok: false,
      output: { error: "用户拒绝执行该操作" },
    },
  ]);
  assert.equal(failed[0].status, "failed");
  assert.equal(rejected[0].status, "rejected");
  assert.deepEqual(failed[0].images, []);
  assert.deepEqual(rejected[0].images, []);
});

test("uses requested dimensions for one legacy result and square as final fallback", () => {
  const legacyImage = {
    filename: "legacy.webp",
    url: "/genimg/legacy.webp",
    content_type: "image/webp",
    size_bytes: 10,
  };
  const withInput = buildImageArtifacts([
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "legacy-1",
      name: "image_generate",
      input: { prompt: "p", n: 1, size: "1024x1536" },
    },
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "legacy-1",
      name: "image_generate",
      ok: true,
      output: { status: "ok", images: [legacyImage] },
    },
  ]);
  const withoutInput = buildImageArtifacts([
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "legacy-2",
      name: "image_generate",
      ok: true,
      output: { status: "ok", images: [legacyImage] },
    },
  ]);
  assert.deepEqual(
    [withInput[0].images[0].width, withInput[0].images[0].height],
    [1024, 1536],
  );
  assert.deepEqual(
    [withoutInput[0].images[0].width, withoutInput[0].images[0].height],
    [1, 1],
  );
});

test("live and persisted image tool records project equivalently", () => {
  const output = {
    status: "ok",
    error: null,
    images: [
      {
        filename: "same.png",
        url: "/genimg/same.png",
        content_type: "image/png",
        size_bytes: 12,
        width: 16,
        height: 9,
      },
    ],
  };
  const live = buildImageArtifacts([
    {
      type: "tool.call",
      seq: 1,
      tool_call_id: "call-1",
      name: "image_generate",
      input: { prompt: "p", n: 1, size: "1536x1024" },
    },
    {
      type: "tool.result",
      seq: 2,
      tool_call_id: "call-1",
      name: "image_generate",
      ok: true,
      output,
    },
  ]);
  const historyEvents = historyToItems([
    {
      id: "message-1",
      sequence: 3,
      role: "tool",
      content: "{}",
      tool_name: "image_generate",
      tool_call_id: "call-1",
      tool_input: { prompt: "p", n: 1, size: "1536x1024" },
      tool_output: output,
      trace_id: null,
      created_at: null,
    },
  ]).flatMap((item) => item.events ?? []);
  const history = buildImageArtifacts(historyEvents);
  assert.deepEqual(history[0].images, live[0].images);
  assert.equal(history[0].prompt, live[0].prompt);
  assert.equal(history[0].status, "success");
  assert.equal(history.length, 1);
});

test("hides legacy synthetic approval continuation messages from history", () => {
  const items = historyToItems([
    {
      id: "hidden",
      sequence: 1,
      role: "user",
      content: "(已处理审批决定)",
      tool_name: null,
      tool_call_id: null,
      tool_input: null,
      tool_output: null,
      trace_id: null,
      created_at: null,
    },
    {
      id: "visible",
      sequence: 2,
      role: "assistant",
      content: "已完成。",
      tool_name: null,
      tool_call_id: null,
      tool_input: null,
      tool_output: null,
      trace_id: null,
      created_at: null,
    },
  ]);
  assert.deepEqual(
    items.map((item) => item.key),
    ["visible"],
  );
});

test("renders goal continuation input as a system notice, not a user bubble", () => {
  const items = historyToItems([
    {
      id: "continuation",
      sequence: 1,
      role: "user",
      content:
        "[目标续跑] 继续推进当前会话目标。使用注入的 <goal_context> 作为目标。",
      tool_name: null,
      tool_call_id: null,
      tool_input: null,
      tool_output: null,
      trace_id: null,
      created_at: null,
    },
    {
      id: "reply",
      sequence: 2,
      role: "assistant",
      content: "已核对结论。",
      tool_name: null,
      tool_call_id: null,
      tool_input: null,
      tool_output: null,
      trace_id: null,
      created_at: null,
    },
  ]);

  // 合成输入不是用户写的:渲染成系统小条,且不带上原始 prompt 全文
  assert.equal(items[0].role, "notice");
  assert.ok(!items[0].content.includes("goal_context"));
  assert.equal(items[1].role, "assistant");

  const groups = groupConversationItems(items);
  assert.deepEqual(
    groups.map((group) => group.role),
    ["notice", "assistant"],
  );
  // 系统小条不是对话轮次,不进定位器
  assert.deepEqual(buildConversationAnchors(groups), []);
});

test("renders the icron synthetic input as a system notice", () => {
  const items = historyToItems([
    {
      id: "icron",
      sequence: 1,
      role: "user",
      content:
        "[定时任务] 「每日出团提醒」到点自动执行。当前是无人值守运行:" +
        "<icron_instruction>汇总明天出发的团</icron_instruction>",
      tool_name: null,
      tool_call_id: null,
      tool_input: null,
      tool_output: null,
      trace_id: null,
      created_at: null,
    },
  ]);

  // 到点触发的合成输入不是用户写的,也不该把整段 prompt 摊在对话里
  assert.equal(items[0].role, "notice");
  assert.ok(!items[0].content.includes("icron_instruction"));
  assert.ok(items[0].content.includes("无人值守"));
});
