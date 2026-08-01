import assert from "node:assert/strict";
import test from "node:test";
import {
  generatedImageGridClass,
  generatedImageMaxWidth,
  imageFollowUpPrompt,
} from "./generated-image-presentation.ts";

test("uses one full-width column and responsive grids for two to four images", () => {
  assert.equal(generatedImageGridClass(1), "grid-cols-1");
  for (const count of [2, 3, 4]) {
    assert.equal(generatedImageGridClass(count), "grid-cols-1 sm:grid-cols-2");
  }
});

test("caps contextual image height while preserving common aspect ratios", () => {
  assert.equal(generatedImageMaxWidth(1024, 1024), "26rem");
  assert.equal(generatedImageMaxWidth(1024, 1536), `${52 / 3}rem`);
  assert.equal(generatedImageMaxWidth(1536, 1024), "39rem");
  assert.equal(generatedImageMaxWidth(16, 9), "44rem");
});

test("follow-up actions stay explicit and return through the Agent prompt", () => {
  const adjust = imageFollowUpPrompt("adjust", "滨海湾夜景");
  const again = imageFollowUpPrompt("again", "滨海湾夜景");
  assert.match(adjust, /不是原图编辑/);
  assert.match(adjust, /滨海湾夜景/);
  assert.match(again, /image_generate/);
  assert.match(again, /重新生成/);
});
