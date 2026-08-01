"use client";

// 未保存改动拦截。
//
// 覆盖两条离开路径:
// 1. 关闭/刷新标签页 → beforeunload(浏览器原生确认框,文案由浏览器决定);
// 2. 应用内跳转(切知识库视图等)→ 由调用方在跳转前 check(),弹自己的确认框。
//
// 注册表放在 ref 里,注册/注销不触发重渲染;check() 在事件处理器里同步读取。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";

interface GuardRegistry {
  register: (id: symbol, label: string) => void;
  unregister: (id: symbol) => void;
  /** 有未保存改动时返回第一个来源的标签,否则返回 null */
  check: () => string | null;
}

const noop: GuardRegistry = {
  register: () => {},
  unregister: () => {},
  check: () => null,
};

const UnsavedGuardContext = createContext<GuardRegistry>(noop);

export function UnsavedGuardProvider({ children }: { children: ReactNode }) {
  const blockers = useRef(new Map<symbol, string>());

  const value = useMemo<GuardRegistry>(
    () => ({
      register: (id, label) => blockers.current.set(id, label),
      unregister: (id) => blockers.current.delete(id),
      check: () => blockers.current.values().next().value ?? null,
    }),
    [],
  );

  return (
    <UnsavedGuardContext.Provider value={value}>
      {children}
    </UnsavedGuardContext.Provider>
  );
}

/**
 * 声明「本组件当前有未保存改动」。active 为 true 期间:
 * 关标签页会触发浏览器确认,应用内跳转会被 useUnsavedGuardCheck() 拦下。
 */
export function useUnsavedGuard(active: boolean, label: string) {
  const registry = useContext(UnsavedGuardContext);

  useEffect(() => {
    if (!active) return;
    const id = Symbol(label);
    registry.register(id, label);

    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // 现代浏览器忽略自定义文案,但仍需要设置 returnValue 才会弹确认框
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);

    return () => {
      registry.unregister(id);
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [active, label, registry]);
}

/** 跳转前调用:有未保存改动则返回来源标签,由调用方决定怎么提示。 */
export function useUnsavedGuardCheck() {
  const registry = useContext(UnsavedGuardContext);
  return useCallback(() => registry.check(), [registry]);
}
