/**
 * 破坏性/关键操作统一确认弹窗(alert-dialog 封装)。
 * onConfirm 支持返回 Promise:等待期间确认按钮 loading 且双按钮禁用,
 * 成功后自动关闭,抛错则保持打开(错误提示由调用方 toast)。
 *
 * @example
 * ```tsx
 * // 非受控:传 trigger
 * <ConfirmDialog
 *   trigger={<Button variant="destructive">删除</Button>}
 *   title="删除该知识库?"
 *   description="删除后不可恢复。"
 *   destructive
 *   onConfirm={() => deleteMutation.mutateAsync()}
 * />
 *
 * // 受控:open/onOpenChange
 * <ConfirmDialog
 *   open={open}
 *   onOpenChange={setOpen}
 *   title="重新解析需求?"
 *   description="将覆盖已有的人工修正。"
 *   onConfirm={handleReparse}
 * />
 * ```
 */
"use client";

import * as React from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Spinner } from "@/components/shared/spinner";

export function ConfirmDialog({
  trigger,
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  destructive,
  onConfirm,
}: {
  trigger?: React.ReactElement;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  onConfirm: () => void | Promise<unknown>;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(false);
  const [pending, setPending] = React.useState(false);

  const isControlled = open !== undefined;
  const actualOpen = isControlled ? open : uncontrolledOpen;

  const setOpen = (next: boolean) => {
    if (pending) return;
    if (!isControlled) setUncontrolledOpen(next);
    onOpenChange?.(next);
  };

  const handleConfirm = async () => {
    setPending(true);
    try {
      await onConfirm();
      if (!isControlled) setUncontrolledOpen(false);
      onOpenChange?.(false);
    } finally {
      setPending(false);
    }
  };

  return (
    <AlertDialog open={actualOpen} onOpenChange={setOpen}>
      {trigger && <AlertDialogTrigger render={trigger} />}
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && (
            <AlertDialogDescription>{description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{cancelLabel}</AlertDialogCancel>
          <AlertDialogAction
            variant={destructive ? "destructive" : "default"}
            disabled={pending}
            onClick={handleConfirm}
          >
            {pending && <Spinner size={3.5} />}
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
