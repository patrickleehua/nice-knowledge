import assert from "node:assert/strict";
import test from "node:test";
import {
  PROMOTION_MIN_CONFIDENCE,
  PROMOTION_MIN_SIGHTINGS,
  memoryScopeLabel,
  memoryScopeTarget,
  memorySourceLabel,
  memorySourceSessionId,
  memoryStatusMeta,
  memoryTypeMeta,
  promotionHint,
} from "./memory.ts";

function item(overrides = {}) {
  return {
    id: "m-1",
    // scope 是自由字符串:这里用一个"宿主注册的"范围名做样例
    scope: "workspace",
    scope_ref_id: "ws-42",
    scope_ref_label: null,
    memory_type: "preference_candidate",
    title: "报告一律输出 Markdown",
    content: "对外交付物默认用 Markdown,不要生成 PDF。",
    source: "memory_extraction:5a2c9f00-0000-4000-8000-000000000001",
    source_message_id: null,
    confidence: 0.6,
    status: "active",
    hit_count: 0,
    sightings: 1,
    last_hit_at: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

test("来源前缀翻成人话,并能取回会话 id", () => {
  assert.equal(memorySourceLabel("memory_extraction:abc"), "对话自动沉淀");
  assert.equal(memorySourceLabel("memory_write:abc"), "助手主动记录");
  // 未知来源原样展示,不猜
  assert.equal(memorySourceLabel("legacy_import"), "legacy_import");
  assert.equal(memorySourceSessionId("memory_write:abc"), "abc");
  assert.equal(memorySourceSessionId("legacy_import"), null);
  assert.equal(memorySourceSessionId("memory_write:"), null);
});

test("scope 词表是开放的:内置 org 有文案,宿主自定义范围原样回显", () => {
  assert.equal(memoryScopeLabel("org"), "全组织通用");
  assert.equal(memoryScopeLabel("workspace"), "workspace");
});

test("范围归属优先取宿主解析出的展示名,取不到回落 ref,org 无归属", () => {
  assert.equal(
    memoryScopeTarget(item({ scope_ref_label: "运营工作区" })),
    "运营工作区",
  );
  assert.equal(memoryScopeTarget(item({ scope_ref_id: "ws-42" })), "ws-42");
  assert.equal(
    memoryScopeTarget(item({ scope: "org", scope_ref_id: null })),
    "—",
  );
});

test("未知类型/状态兜底成中性徽章而不是空白", () => {
  assert.equal(memoryTypeMeta("constraint").tone, "destructive");
  assert.deepEqual(memoryTypeMeta("brand_new"), {
    label: "brand_new",
    tone: "muted",
  });
  assert.equal(memoryStatusMeta("active").tone, "success");
  assert.deepEqual(memoryStatusMeta("weird"), { label: "weird", tone: "muted" });
});

test("提升提示只对候选给出,且区分缺次数与缺置信", () => {
  // 正式类型没有候选期,不该出现提升文案
  assert.equal(promotionHint(item({ memory_type: "preference" })), null);
  assert.equal(promotionHint(item({ memory_type: "constraint" })), null);

  const needMore = promotionHint(item({ sightings: 1, confidence: 0.9 }));
  assert.match(
    needMore,
    new RegExp(`再被印证 ${PROMOTION_MIN_SIGHTINGS - 1} 次`),
  );

  const needConfidence = promotionHint(
    item({ sightings: PROMOTION_MIN_SIGHTINGS, confidence: 0.6 }),
  );
  assert.match(needConfidence, new RegExp(`${PROMOTION_MIN_CONFIDENCE}`));
  assert.match(needConfidence, /置信需达/);

  const ready = promotionHint(
    item({
      sightings: PROMOTION_MIN_SIGHTINGS,
      confidence: PROMOTION_MIN_CONFIDENCE,
    }),
  );
  assert.match(ready, /已满足提升条件/);
});
