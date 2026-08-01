"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Building2, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  FormField,
  PageHeader,
  Spinner,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { AdminOrg } from "@/lib/types";

export default function AdminOrgsPage() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminName, setAdminName] = useState("");

  const orgsQuery = useQuery({
    queryKey: ["admin-orgs"],
    queryFn: () => api.get<AdminOrg[]>("/admin/orgs"),
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api.post<AdminOrg>("/admin/orgs", {
        name,
        slug,
        admin_email: adminEmail || null,
        admin_password: adminEmail ? adminPassword || null : null,
        admin_full_name: adminEmail ? adminName || null : null,
      }),
    onSuccess: () => {
      toast.success("租户已创建");
      setOpen(false);
      setName("");
      setSlug("");
      setAdminEmail("");
      setAdminPassword("");
      setAdminName("");
      queryClient.invalidateQueries({ queryKey: ["admin-orgs"] });
    },
    onError: (err) => toast.error(errMsg(err, "创建失败")),
  });

  const toggleMutation = useMutation({
    mutationFn: (org: AdminOrg) =>
      api.patch<AdminOrg>(`/admin/orgs/${org.id}`, { is_active: !org.is_active }),
    onSuccess: (org) => {
      toast.success(org.is_active ? "租户已启用" : "租户已停用");
      queryClient.invalidateQueries({ queryKey: ["admin-orgs"] });
    },
    onError: (err) => toast.error(errMsg(err, "操作失败")),
  });

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns = useMemo<ColumnDef<AdminOrg, any>[]>(
    () => [
      {
        accessorKey: "name",
        header: "名称",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        accessorKey: "slug",
        header: "slug",
        cell: ({ row }) => (
          <span className="font-mono text-xs text-muted-foreground">
            {row.original.slug}
          </span>
        ),
      },
      { accessorKey: "member_count", header: "成员数" },
      {
        accessorKey: "is_active",
        header: "状态",
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_active ? (
            <ToneBadge tone="success" className="rounded-full">
              启用
            </ToneBadge>
          ) : (
            <ToneBadge tone="muted" className="rounded-full">
              已停用
            </ToneBadge>
          ),
      },
      {
        accessorKey: "created_at",
        header: "创建时间",
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {row.original.created_at
              ? new Date(row.original.created_at).toLocaleDateString()
              : "—"}
          </span>
        ),
      },
      {
        id: "__actions__",
        enableSorting: false,
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => {
          const o = row.original;
          return (
            <div className="text-right">
              {o.is_active ? (
                <ConfirmDialog
                  trigger={
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={toggleMutation.isPending}
                    >
                      停用
                    </Button>
                  }
                  title={`停用「${o.name}」?`}
                  description="停用后该租户全员将无法登录。"
                  destructive
                  confirmLabel="停用"
                  onConfirm={() => toggleMutation.mutateAsync(o)}
                />
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={toggleMutation.isPending}
                  onClick={() => toggleMutation.mutate(o)}
                >
                  启用
                </Button>
              )}
            </div>
          );
        },
      },
    ],
    [toggleMutation],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="租户管理"
        description="创建租户时可同时开通首个组织管理员;停用后该租户全员无法登录"
        actions={
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger render={<Button />}>
              <Plus className="size-4" />
              新建租户
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>新建租户</DialogTitle>
              </DialogHeader>
              <div className="space-y-4">
                <FormField label="名称" htmlFor="org-name" required>
                  <Input
                    id="org-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="如:示例科技有限公司"
                  />
                </FormField>
                <FormField
                  label="slug"
                  htmlFor="org-slug"
                  required
                  description="小写字母/数字/连字符,登录与分享用"
                >
                  <Input
                    id="org-slug"
                    value={slug}
                    onChange={(e) => setSlug(e.target.value)}
                    placeholder="如:tule-travel"
                    className="font-mono"
                  />
                </FormField>
                <div className="space-y-1.5 rounded-md border border-border p-3">
                  <p className="text-xs font-medium text-muted-foreground">
                    首个组织管理员(可选;已注册邮箱只加成员关系)
                  </p>
                  <Input
                    type="email"
                    value={adminEmail}
                    onChange={(e) => setAdminEmail(e.target.value)}
                    placeholder="管理员邮箱"
                  />
                  {adminEmail && (
                    <>
                      <Input
                        value={adminName}
                        onChange={(e) => setAdminName(e.target.value)}
                        placeholder="姓名"
                      />
                      <Input
                        type="password"
                        value={adminPassword}
                        onChange={(e) => setAdminPassword(e.target.value)}
                        placeholder="初始密码(新用户必填,至少 8 位)"
                      />
                    </>
                  )}
                </div>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setOpen(false)}>
                  取消
                </Button>
                <Button
                  disabled={!name.trim() || !slug.trim() || createMutation.isPending}
                  onClick={() => createMutation.mutate()}
                >
                  {createMutation.isPending && <Spinner size={3.5} />}
                  创建
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        }
      />

      <Card>
        <CardContent className="pt-4">
          <DataTable
            columns={columns}
            data={orgsQuery.data}
            isLoading={orgsQuery.isLoading}
            error={orgsQuery.error}
            onRetry={() => orgsQuery.refetch()}
            getRowId={(o) => o.id}
            empty={{ icon: Building2, title: "还没有租户" }}
          />
        </CardContent>
      </Card>
    </div>
  );
}
