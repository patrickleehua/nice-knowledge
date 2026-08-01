/**
 * 表单字段统一布局:Label + 控件 slot + 描述 + 错误红字(react-hook-form 兼容)。
 *
 * @example
 * ```tsx
 * <FormField label="知识库名称" htmlFor="name" required error={errors.name?.message}>
 *   <Input id="name" {...register("name")} aria-invalid={!!errors.name} />
 * </FormField>
 * ```
 */

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

export function FormField({
  label,
  htmlFor,
  required,
  error,
  description,
  children,
  className,
}: {
  label: React.ReactNode;
  htmlFor?: string;
  required?: boolean;
  error?: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label htmlFor={htmlFor}>
        {label}
        {required && (
          <span aria-hidden="true" className="text-destructive">
            *
          </span>
        )}
      </Label>
      {children}
      {description && !error && (
        <p className="text-xs text-muted-foreground">{description}</p>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
