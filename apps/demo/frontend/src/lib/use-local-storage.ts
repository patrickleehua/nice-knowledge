"use client";

// localStorage 偏好读取:走 useSyncExternalStore,服务端快照返回 fallback,
// 客户端挂载后自动切到已存值。相比"effect 里 setState"少一次级联渲染,
// 也不会因首屏 style 差异触发 hydration 抖动。
// getSnapshot 只返回原始值(number/boolean),引用恒等天然成立,不会死循环。

import { useCallback, useSyncExternalStore } from "react";

const CHANGE_EVENT = "tf-local-storage-change";

function subscribe(onChange: () => void) {
  // storage 事件只在其他标签页触发,同页写入靠自定义事件补齐
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

function writeKey(key: string, value: string) {
  window.localStorage.setItem(key, value);
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** 数值偏好,读到的值会被 clamp 进 [min, max];非法值回落 fallback。 */
export function useStoredNumber(
  key: string,
  fallback: number,
  min = Number.NEGATIVE_INFINITY,
  max = Number.POSITIVE_INFINITY,
): [number, (next: number) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return fallback;
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) return fallback;
      return Math.max(min, Math.min(max, parsed));
    },
    () => fallback,
  );
  const setValue = useCallback(
    (next: number) => writeKey(key, String(next)),
    [key],
  );
  return [value, setValue];
}

// 字符串列表的解析结果要缓存:useSyncExternalStore 的 getSnapshot 每次返回新数组
// 会被判定为「变了」而无限重渲染,所以按 raw 字符串比对复用同一个引用。
const EMPTY_LIST: readonly string[] = [];
const listCache = new Map<string, { raw: string | null; value: string[] }>();

function readList(key: string): readonly string[] {
  const raw = window.localStorage.getItem(key);
  const cached = listCache.get(key);
  if (cached && cached.raw === raw) return cached.value;
  let value: string[] = [];
  if (raw) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        value = parsed.filter(
          (item): item is string => typeof item === "string",
        );
      }
    } catch {
      // 存储被外部写坏时按空列表处理,不影响功能
    }
  }
  listCache.set(key, { raw, value });
  return value;
}

/**
 * 去重的字符串列表偏好(如检索历史):push 会把已存在项提到最前,并裁到 max 条。
 */
export function useStoredList(
  key: string,
  max = 10,
): {
  items: readonly string[];
  push: (item: string) => void;
  remove: (item: string) => void;
  clear: () => void;
} {
  const items = useSyncExternalStore(
    subscribe,
    () => readList(key),
    () => EMPTY_LIST,
  );

  const write = useCallback(
    (next: readonly string[]) => writeKey(key, JSON.stringify(next)),
    [key],
  );

  const push = useCallback(
    (item: string) => {
      const trimmed = item.trim();
      if (!trimmed) return;
      const current = readList(key);
      write([trimmed, ...current.filter((x) => x !== trimmed)].slice(0, max));
    },
    [key, max, write],
  );

  const remove = useCallback(
    (item: string) => write(readList(key).filter((x) => x !== item)),
    [key, write],
  );

  const clear = useCallback(() => write([]), [write]);

  return { items, push, remove, clear };
}

/** 布尔偏好,存储形态为 "1" / "0"。 */
export function useStoredFlag(
  key: string,
  fallback = false,
): [boolean, (next: boolean) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => {
      const raw = window.localStorage.getItem(key);
      return raw === null ? fallback : raw === "1";
    },
    () => fallback,
  );
  const setValue = useCallback(
    (next: boolean) => writeKey(key, next ? "1" : "0"),
    [key],
  );
  return [value, setValue];
}
