"use client";

// 路由级错误边界(Next 16 error.tsx 约定,须为 Client Component):
// 捕获段内渲染/数据错误,ErrorState 统一呈现,reset 重试重渲染,另给返回首页出口。

import Link from "next/link";
import { useEffect } from "react";
import { ErrorState } from "@/components/shared";
import { buttonVariants } from "@/components/ui/button";

export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 生产环境 Server Component 错误信息会被脱敏,digest 用于对齐服务端日志
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-1 p-6 text-center">
      <h2 className="text-base font-semibold text-foreground">页面出错了</h2>
      <ErrorState error={error} onRetry={reset} className="py-4" />
      {error.digest && (
        <p className="text-xs text-muted-foreground">错误编号:{error.digest}</p>
      )}
      <Link
        href="/"
        className={buttonVariants({ variant: "ghost", size: "sm" })}
      >
        返回首页
      </Link>
    </div>
  );
}
