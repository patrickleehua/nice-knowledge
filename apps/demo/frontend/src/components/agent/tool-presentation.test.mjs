import assert from "node:assert/strict";
import test from "node:test";
import {
  genericJsonSummary,
  registerAutoExpandTools,
  registerToolSummarizer,
  resetAutoExpandTools,
  resetToolSummarizers,
  shouldExpandTool,
  toolResultSummary,
} from "./tool-presentation.ts";

test("summarizes the SDK builtin tools", () => {
  assert.equal(
    toolResultSummary("kb_search", { hits: [{}, {}, {}] }),
    "3 条命中",
  );
  assert.equal(toolResultSummary("kb_search", { hits: [] }), "未命中知识");
  assert.equal(
    toolResultSummary("web_search", {
      results: [{ source_tier: "official" }, { source_tier: "ugc" }],
    }),
    "2 条来源 · 1 官方",
  );
  assert.equal(
    toolResultSummary("web_fetch", {
      pages: [{ status: "ok" }, { status: "blocked" }],
    }),
    "已读取 1 个网页 · 1 个失败",
  );
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
      output: "结论正文",
      turns: 3,
      stop: "end_turn",
    }),
    "检索研究员 · 3 轮完成",
  );
  assert.equal(
    toolResultSummary("Agent", { agent: "复核员", error: "执行超时" }),
    "复核员 · 未完成",
  );
});

test("falls back to a generic JSON summary for unknown tools", () => {
  // SDK 不认识宿主的工具,兜底摘要必须永远给得出一行可读文案
  assert.equal(genericJsonSummary({ items: [{}, {}] }), "2 项结果");
  assert.equal(genericJsonSummary({ total: 7 }), "7 项结果");
  assert.equal(genericJsonSummary({ status: "queued" }), "queued");
  assert.equal(genericJsonSummary({ a: 1, b: 2 }), "2 个字段");
  assert.equal(genericJsonSummary({}), "已完成");
  assert.equal(toolResultSummary("host_tool", { items: [{}] }), "1 项结果");
  assert.equal(toolResultSummary("custom_tool", null), "已完成");
});

test("hosts can register summarizers, and they win over builtins", (t) => {
  t.after(resetToolSummarizers);
  registerToolSummarizer("ticket_create", (output) =>
    output.id ? `工单 ${output.id} 已建` : null,
  );
  assert.equal(
    toolResultSummary("ticket_create", { id: "T-1" }),
    "工单 T-1 已建",
  );
  // 摘要器返回 null 时交回兜底,而不是渲染空白
  assert.equal(toolResultSummary("ticket_create", { total: 2 }), "2 项结果");

  registerToolSummarizer("kb_search", () => "自定义命中文案");
  assert.equal(toolResultSummary("kb_search", { hits: [{}] }), "自定义命中文案");
});

test("expansion policy: failures always open, success only when registered", (t) => {
  t.after(resetAutoExpandTools);
  assert.equal(shouldExpandTool("anything", "failed", { error: "boom" }), true);
  assert.equal(shouldExpandTool("kb_search", "ok", { hits: [] }), false);
  registerAutoExpandTools("report_build");
  assert.equal(shouldExpandTool("report_build", "ok", { ok: true }), true);
  // 非 record 输出不展开(没有结构可摊)
  assert.equal(shouldExpandTool("report_build", "ok", "text"), false);
});
