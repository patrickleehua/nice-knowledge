"use client";

// 列表页状态统一入 query string:筛选 / 分页 / Tab / 排序刷新不丢、可分享。
// 全部走 router.replace(scroll:false),不污染浏览器历史栈(前进后退按"页面"粒度,
// 而不是按"改了一个筛选"粒度);需要进历史栈的场景(如打开详情)显式传 push:true。

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

export type UrlPatch = Record<string, string | number | null | undefined>;

interface SetOptions {
  /** 一并清空的参数(如换筛选条件时重置 page) */
  reset?: string[];
  /** 进浏览器历史栈,默认 replace */
  push?: boolean;
}

/**
 * query string 读写。写入时 null/undefined/"" 表示删除该参数。
 *
 * @example
 * ```tsx
 * const { get, set } = useUrlState();
 * const status = get("status");
 * set({ status: "failed" }, { reset: ["page"] });
 * ```
 */
export function useUrlState() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const get = useCallback(
    (key: string) => searchParams.get(key),
    [searchParams],
  );

  const getAll = useCallback(
    (key: string) => searchParams.getAll(key),
    [searchParams],
  );

  const set = useCallback(
    (patch: UrlPatch, options: SetOptions = {}) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const key of options.reset ?? []) params.delete(key);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "") {
          params.delete(key);
        } else {
          params.set(key, String(value));
        }
      }
      const qs = params.toString();
      const href = qs ? `${pathname}?${qs}` : pathname;
      if (options.push) router.push(href, { scroll: false });
      else router.replace(href, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  /** 同名多值参数(如多选知识库范围 ?kb=a&kb=b) */
  const setAll = useCallback(
    (key: string, values: string[], options: SetOptions = {}) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const resetKey of options.reset ?? []) params.delete(resetKey);
      params.delete(key);
      for (const value of values) params.append(key, value);
      const qs = params.toString();
      const href = qs ? `${pathname}?${qs}` : pathname;
      if (options.push) router.push(href, { scroll: false });
      else router.replace(href, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  return useMemo(
    () => ({ get, getAll, set, setAll }),
    [get, getAll, set, setAll],
  );
}

/**
 * 单个参数的 useState 式封装。fallback 为该参数缺省时的值,写入 fallback 会清掉参数,
 * 保证"默认态"的 URL 是干净的。
 */
export function useUrlParam(
  key: string,
  fallback = "",
): [string, (next: string, options?: SetOptions) => void] {
  const { get, set } = useUrlState();
  const value = get(key) ?? fallback;
  const setValue = useCallback(
    (next: string, options?: SetOptions) => {
      set({ [key]: next === fallback ? null : next }, options);
    },
    [fallback, key, set],
  );
  return [value, setValue];
}

/** 数字参数(分页等),非法值回落到 fallback。 */
export function useUrlNumber(
  key: string,
  fallback: number,
): [number, (next: number, options?: SetOptions) => void] {
  const { get, set } = useUrlState();
  const raw = get(key);
  const parsed = raw === null ? Number.NaN : Number(raw);
  const value = Number.isFinite(parsed) ? parsed : fallback;
  const setValue = useCallback(
    (next: number, options?: SetOptions) => {
      set({ [key]: next === fallback ? null : next }, options);
    },
    [fallback, key, set],
  );
  return [value, setValue];
}
