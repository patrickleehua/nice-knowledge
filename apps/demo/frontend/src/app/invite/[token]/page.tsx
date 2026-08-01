"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleAlert } from "lucide-react";
import { useRouter, useParams } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { ROLE_LABELS, type InviteInfo } from "@/lib/types";

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [accepted, setAccepted] = useState(false);

  const { data: info, error, isLoading } = useQuery({
    queryKey: ["invite", token],
    queryFn: () => api.get<InviteInfo>(`/auth/invite/${token}`),
    retry: false,
  });

  const acceptMutation = useMutation({
    mutationFn: () =>
      api.post<InviteInfo>("/auth/invite/accept", {
        token,
        full_name: info?.user_exists ? null : fullName || null,
        password: info?.user_exists ? null : password,
      }),
    onSuccess: () => setAccepted(true),
  });

  const newUser = info && !info.user_exists;
  const acceptError =
    acceptMutation.error instanceof ApiError ? acceptMutation.error.message : null;

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <span className="mx-auto flex size-10 items-center justify-center rounded-md bg-primary text-lg font-bold text-primary-foreground">
            T
          </span>
          <CardTitle>加入组织</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoading ? (
            <div className="h-24 animate-pulse rounded-md bg-muted" />
          ) : error || !info ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <CircleAlert className="size-8 text-destructive" />
              <p className="text-sm text-muted-foreground">
                邀请无效或已过期,请联系组织管理员重新发起邀请。
              </p>
            </div>
          ) : accepted ? (
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <CircleCheck className="size-8 text-success" />
              <p className="text-sm">
                已加入 <span className="font-medium">{info.org_name}</span>
                ,角色:{ROLE_LABELS[info.role] ?? info.role}
              </p>
              <Button className="w-full" onClick={() => router.push("/login")}>
                去登录
              </Button>
            </div>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                <span className="font-medium text-foreground">{info.org_name}</span> 邀请{" "}
                <span className="font-medium text-foreground">{info.email}</span> 以
                「{ROLE_LABELS[info.role] ?? info.role}」角色加入。
              </p>
              {newUser && (
                <>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">姓名</label>
                    <Input
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      placeholder="你的姓名"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">设置密码(至少 8 位)</label>
                    <Input
                      type="password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  </div>
                </>
              )}
              {acceptError && (
                <p className="text-sm text-destructive">{acceptError}</p>
              )}
              <Button
                className="w-full"
                disabled={
                  acceptMutation.isPending || (Boolean(newUser) && password.length < 8)
                }
                onClick={() => acceptMutation.mutate()}
              >
                接受邀请
              </Button>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
