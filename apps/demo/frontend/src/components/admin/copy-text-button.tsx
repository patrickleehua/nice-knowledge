"use client";

// 复制大段文本到剪贴板的小按钮:系统资源正文与组装预览全文都需要,抽出避免两处重复。

import { Copy } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";

export function CopyTextButton({
  text,
  disabled,
  label = "复制",
}: {
  text: string;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={disabled || !text}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast.success("已复制到剪贴板");
        } catch {
          // http 环境或权限被拒时 clipboard API 不可用,给出可操作的提示而非静默失败
          toast.error("复制失败,请手动选择文本复制");
        }
      }}
    >
      <Copy className="size-3.5" />
      {label}
    </Button>
  );
}
