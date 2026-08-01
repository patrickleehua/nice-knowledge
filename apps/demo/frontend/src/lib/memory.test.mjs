import assert from "node:assert/strict";
import test from "node:test";
import {
  PROMOTION_MIN_CONFIDENCE,
  PROMOTION_MIN_SIGHTINGS,
  memoryScopeTarget,
  memorySourceLabel,
  memorySourceSessionId,
  promotionHint,
} from "./memory.ts";

function item(overrides = {}) {
  return {
    id: "m-1",
    scope: "customer",
    scope_ref_id: "王总",
    scope_ref_label: null,
    memory_type: "preference_candidate",
    title: "不接受夜间大巴",
    content: "老人同行,夜间大巴一律不接受。",
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

test("范围归属优先取项目标题,取不到回落 id,全社无归属", () => {
  assert.equal(
    memoryScopeTarget(item({ scope: "project", scope_ref_label: "王总墨尔本8日" })),
    "王总墨尔本8日",
  );
  assert.equal(
    memoryScopeTarget(item({ scope: "project", scope_ref_id: "p-1" })),
    "p-1",
  );
  assert.equal(memoryScopeTarget(item({ scope: "org", scope_ref_id: null })), "—");
});

test("提升提示只对候选给出,且区分缺次数与缺置信", () => {
  // 正式类型没有候选期,不该出现提升文案
  assert.equal(promotionHint(item({ memory_type: "preference" })), null);
  assert.equal(promotionHint(item({ memory_type: "constraint" })), null);

  const needMore = promotionHint(item({ sightings: 1, confidence: 0.9 }));
  assert.match(needMore, new RegExp(`再被印证 ${PROMOTION_MIN_SIGHTINGS - 1} 次`));

  const needConfidence = promotionHint(
    item({ sightings: PROMOTION_MIN_SIGHTINGS, confidence: 0.6 }),
  );
  assert.match(needConfidence, new RegExp(`${PROMOTION_MIN_CONFIDENCE}`));
  assert.match(needConfidence, /置信需达/);

  const ready = promotionHint(
    item({ sightings: PROMOTION_MIN_SIGHTINGS, confidence: PROMOTION_MIN_CONFIDENCE }),
  );
  assert.match(ready, /已满足提升条件/);
});
