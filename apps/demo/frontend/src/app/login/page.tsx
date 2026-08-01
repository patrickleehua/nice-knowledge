"use client";

import { Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { homeForRole, saveSession, type Role } from "@/lib/auth";
import type { LoginResponse } from "@/lib/types";

interface FormValues {
  email: string;
  password: string;
  org_slug?: string;
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [loading, setLoading] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>();

  async function onSubmit(values: FormValues) {
    setLoading(true);
    try {
      const resp = await api.publicPost<LoginResponse>("/auth/login", {
        email: values.email,
        password: values.password,
        org_slug: values.org_slug || null,
      });
      saveSession({
        accessToken: resp.access_token,
        refreshToken: resp.refresh_token,
        org: { ...resp.org, role: resp.org.role as Role },
      });
      toast.success(`欢迎回来,${resp.org.name}`);
      router.push(params.get("next") ?? homeForRole(resp.org.role as Role));
    } catch (err) {
      toast.error(
        err instanceof ApiError && err.status === 401
          ? "邮箱或密码错误"
          : err instanceof ApiError
            ? err.message
            : "登录失败,请重试",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <div className="mb-2 flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-md bg-primary text-base font-bold text-primary-foreground">
            N
          </span>
          <span className="text-lg font-semibold">nicekit demo</span>
        </div>
        <CardTitle className="text-base font-normal text-muted-foreground">
          登录多租户 Agent + 知识库工作台
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form className="space-y-4" onSubmit={handleSubmit(onSubmit)}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="email">
              邮箱
            </label>
            <Input
              id="email"
              type="email"
              placeholder="you@company.com"
              {...register("email", {
                required: "请输入邮箱",
                pattern: { value: /^[^@\s]+@[^@\s]+\.[^@\s]+$/, message: "请输入合法邮箱" },
              })}
            />
            {errors.email && (
              <p className="text-xs text-destructive">{errors.email.message}</p>
            )}
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="password">
              密码
            </label>
            <Input
              id="password"
              type="password"
              {...register("password", {
                required: "请输入密码",
                minLength: { value: 6, message: "密码至少 6 位" },
              })}
            />
            {errors.password && (
              <p className="text-xs text-destructive">{errors.password.message}</p>
            )}
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="org_slug">
              组织标识(可选,多组织成员使用)
            </label>
            <Input
              id="org_slug"
              placeholder="如 platform / demo"
              {...register("org_slug")}
            />
          </div>
          <Button type="submit" className="w-full" disabled={loading}>
            {loading && <Loader2 className="size-4 animate-spin" />}
            登录
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Suspense>
        <LoginForm />
      </Suspense>
    </div>
  );
}
