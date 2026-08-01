"use client";

// 虚拟列表滚到接近底部时预取下一页。
//
// 取代原来的 useLazyList:那套是「后端 100/页 useInfiniteQuery」之上再叠一层
// 「前端 160/批渲染」,同一个「加载更多」在两种状态下含义不同(有时是渲染更多
// 已拿到的数据,有时是去后端要下一页)。现在只剩一套语义——
// 滚到接近底部就自动去后端要下一页,渲染量由虚拟化控制,不再分批。
//
// useVirtualizer 本身刻意不在这里包一层:React Compiler 把它列为
// incompatible library(返回的方法不能被安全 memo),放在自定义 hook 里会直接报错;
// 放在组件里则只是跳过该组件的编译。所以库调用留在各组件内,这里只留纯逻辑。

import { useEffect } from "react";

export function useEndReached({
  lastIndex,
  count,
  hasMore,
  loading,
  onEndReached,
  threshold = 6,
}: {
  /** 当前渲染窗口里最后一行的索引 */
  lastIndex: number;
  count: number;
  hasMore: boolean;
  loading: boolean;
  onEndReached: () => void;
  /** 距末尾多少行时预取 */
  threshold?: number;
}) {
  useEffect(() => {
    if (!hasMore || loading) return;
    if (count > 0 && lastIndex >= count - threshold) onEndReached();
  }, [count, hasMore, lastIndex, loading, onEndReached, threshold]);
}
