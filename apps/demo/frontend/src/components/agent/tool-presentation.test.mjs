import assert from "node:assert/strict";
import test from "node:test";
import {
  businessResultKind,
  shouldExpandTool,
  toolResultSummary,
} from "./tool-presentation.ts";

test("summarizes high-value business tool outputs", () => {
  assert.equal(
    toolResultSummary("project_create", {
      project_id: "project-1",
      title: "王先生一家 · 墨尔本5日",
      status: "created",
    }),
    "已创建「王先生一家 · 墨尔本5日」",
  );
  assert.equal(
    toolResultSummary("demand_parse", {
      parsed: { destinations: ["墨尔本"] },
      missing_fields: ["departure_dates", "hotel_level"],
    }),
    "墨尔本 · 2 项待补充",
  );
  assert.equal(
    toolResultSummary("kb_retrieve", {
      empty: false,
      counts: { destinations: 3, hotels: 4, routes: 2 },
    }),
    "9 条可用资料",
  );
  assert.equal(
    toolResultSummary("itinerary_generate", {
      option_count: 3,
      options: { options: [] },
    }),
    "已生成 3 套方案",
  );
  assert.equal(
    toolResultSummary("quote_generate", {
      item_count: 8,
      totals: { per_person: 13800 },
    }),
    "¥13,800/人 · 8 项成本",
  );
});

test("identifies business renderers and expansion policy", () => {
  assert.equal(
    businessResultKind("itinerary_generate", { option_count: 3 }),
    "itinerary",
  );
  assert.equal(businessResultKind("unknown", { ok: true }), null);
  assert.equal(shouldExpandTool("project_create", "ok", { title: "A" }), true);
  assert.equal(shouldExpandTool("kb_retrieve", "ok", { counts: {} }), false);
  assert.equal(shouldExpandTool("anything", "failed", { error: "boom" }), true);
});

test("keeps unsupported outputs concise", () => {
  assert.equal(
    toolResultSummary("project_list", { items: [{}, {}] }),
    "2 项结果",
  );
  assert.equal(toolResultSummary("custom_tool", null), "已完成");
  assert.equal(
    toolResultSummary("image_generate", {
      status: "unavailable",
      images: [],
      error: "暂不可用",
    }),
    "图片生成失败",
  );
});

test("summarizes sub agent delegation by specialist and turn count", () => {
  assert.equal(
    toolResultSummary("Agent", {
      agent: "检索研究员",
      output: "巴黎 6 月适合出行",
      turns: 3,
      stop: "end_turn",
    }),
    "检索研究员 · 3 轮完成",
  );
  assert.equal(
    toolResultSummary("Agent", { agent: "报价核算员", error: "执行超时" }),
    "报价核算员 · 未完成",
  );
});
