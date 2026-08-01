// 全局 404(Next 16 not-found.tsx 约定):notFound() 抛出与全站未匹配 URL 都落到这里。
// "/" 会按角色重定向到各自首页(app/page.tsx),故返回首页对全角色安全。

import { FileQuestion } from "lucide-react";
import Link from "next/link";
import { EmptyState } from "@/components/shared";
import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <EmptyState
        icon={FileQuestion}
        title="页面不存在"
        description="地址可能已失效或输入有误。"
        action={
          <Link
            href="/"
            className={buttonVariants({ variant: "outline", size: "sm" })}
          >
            返回首页
          </Link>
        }
      />
    </div>
  );
}
