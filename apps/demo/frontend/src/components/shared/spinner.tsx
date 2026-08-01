/**
 * 全站唯一转圈实现(LoaderCircle + animate-spin)。
 *
 * @example
 * ```tsx
 * <Spinner />                    // size-4(默认)
 * <Spinner size={3.5} />         // size-3.5(按钮内)
 * <Spinner size={5} className="text-primary" />
 * ```
 */

import { LoaderCircle } from "lucide-react";

import { cn } from "@/lib/utils";

export type SpinnerSize = 3.5 | 4 | 5;

const SIZE_CLASSES: Record<SpinnerSize, string> = {
  3.5: "size-3.5",
  4: "size-4",
  5: "size-5",
};

export function Spinner({
  size = 4,
  className,
}: {
  size?: SpinnerSize;
  className?: string;
}) {
  return (
    <LoaderCircle
      aria-hidden="true"
      className={cn("animate-spin", SIZE_CLASSES[size], className)}
    />
  );
}
