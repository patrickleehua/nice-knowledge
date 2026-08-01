import assert from "node:assert/strict";
import test from "node:test";
import { elapsedSecondsSince, formatDuration } from "./duration.ts";

test("formats each duration bucket at its boundary", () => {
  assert.equal(formatDuration(0), "0 秒");
  assert.equal(formatDuration(45), "45 秒");
  assert.equal(formatDuration(59), "59 秒");
  assert.equal(formatDuration(59.9), "59 秒");
  assert.equal(formatDuration(60), "1 分 0 秒");
  assert.equal(formatDuration(754), "12 分 34 秒");
  assert.equal(formatDuration(3599), "59 分 59 秒");
  assert.equal(formatDuration(3600), "1 小时 0 分");
  assert.equal(formatDuration(4680), "1 小时 18 分");
});

test("never renders NaN, Infinity or negative durations", () => {
  assert.equal(formatDuration(null), null);
  assert.equal(formatDuration(undefined), null);
  assert.equal(formatDuration(Number.NaN), null);
  assert.equal(formatDuration(Number.POSITIVE_INFINITY), null);
  assert.equal(formatDuration(Number.NEGATIVE_INFINITY), null);
  // 时钟偏移导致的负值钳到 0,而不是显示 "-3 秒"
  assert.equal(formatDuration(-3), "0 秒");
  assert.equal(formatDuration(-3600), "0 秒");
});

test("measures elapsed seconds from an ISO start, clamping clock skew", () => {
  const started = "2026-07-25T10:00:00Z";
  assert.equal(elapsedSecondsSince(started, Date.parse(started)), 0);
  assert.equal(elapsedSecondsSince(started, Date.parse(started) + 90_000), 90);
  // 浏览器时钟快于服务端:钳到 0,秒表不倒走
  assert.equal(elapsedSecondsSince(started, Date.parse(started) - 5_000), 0);
});

test("returns null when the start timestamp is missing or unusable", () => {
  assert.equal(elapsedSecondsSince(null), null);
  assert.equal(elapsedSecondsSince(undefined), null);
  assert.equal(elapsedSecondsSince(""), null);
  assert.equal(elapsedSecondsSince("not-a-timestamp", Date.now()), null);
});
