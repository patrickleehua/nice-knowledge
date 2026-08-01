"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Copy, MailPlus, Trash2, Users, UserX } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  FormField,
  PageHeader,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import {
  ASSIGNABLE_ROLES,
  ROLE_LABELS,
  type InviteCreated,
  type InviteOut,
  type Member,
} from "@/lib/types";

const ROLE_ITEMS: Record<string, string> = Object.fromEntries(
  ASSIGNABLE_ROLES.map((r) => [r, ROLE_LABELS[r]]),
);

export default function MembersPage() {
  const queryClient = useQueryClient();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("sales");
  const [createdInvite, setCreatedInvite] = useState<InviteCreated | null>(null);

  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => api.get<Member[]>("/org/members"),
  });
  const invitesQuery = useQuery({
    queryKey: ["org-invites"],
    queryFn: () => api.get<InviteOut[]>("/org/members/invites"),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["org-members"] });
    queryClient.invalidateQueries({ queryKey: ["org-invites"] });
  };

  const inviteMutation = useMutation({
    mutationFn: () =>
      api.post<InviteCreated>("/org/members/invites", { email, role }),
    onSuccess: (data) => {
      setCreatedInvite(data);
      setEmail("");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "邀请失败")),
  });

  const roleMutation = useMutation({
    mutationFn: ({ id, newRole }: { id: string; newRole: string }) =>
      api.patch<Member>(`/org/members/${id}`, { role: newRole }),
    onSuccess: () => {
      toast.success("角色已更新");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "更新失败")),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/org/members/${id}`),
    onSuccess: () => {
      toast.success("成员已移除");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "移除失败")),
  });

  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/org/members/invites/${id}`),
    onSuccess: () => {
      toast.success("邀请已撤销");
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "撤销失败")),
  });

  const inviteLink = createdInvite
    ? `${typeof window !== "undefined" ? location.origin : ""}/invite/${createdInvite.token}`
    : "";

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const memberColumns = useMemo<ColumnDef<Member, any>[]>(
    () => [
      {
        accessorKey: "full_name",
        header: "姓名",
        cell: ({ row }) => (
          <span className="font-medium">{row.original.full_name}</span>
        ),
      },
      {
        accessorKey: "email",
        header: "邮箱",
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.email}</span>
        ),
      },
      {
        accessorKey: "role",
        header: "角色",
        enableSorting: false,
        cell: ({ row }) => {
          const m = row.original;
          if (m.role === "platform_admin") {
            return <span>{ROLE_LABELS[m.role]}</span>;
          }
          return (
            <Select
              items={ROLE_ITEMS}
              value={m.role}
              onValueChange={(v) =>
                roleMutation.mutate({
                  id: m.membership_id,
                  newRole: v as string,
                })
              }
            >
              <SelectTrigger size="sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ASSIGNABLE_ROLES.map((r) => (
                  <SelectItem key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          );
        },
      },
      {
        id: "__actions__",
        enableSorting: false,
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => (
          <div className="text-right">
            <ConfirmDialog
              trigger={
                <Button variant="ghost" size="icon" aria-label="移除成员">
                  <UserX className="size-4 text-destructive" />
                </Button>
              }
              title={`将 ${row.original.full_name} 移出本组织?`}
              description="移除后该成员将立即失去本组织的访问权限。"
              destructive
              confirmLabel="移除"
              onConfirm={() => removeMutation.mutateAsync(row.original.membership_id)}
            />
          </div>
        ),
      },
    ],
    [roleMutation, removeMutation],
  );

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const inviteColumns = useMemo<ColumnDef<InviteOut, any>[]>(
    () => [
      { accessorKey: "email", header: "邮箱" },
      {
        accessorKey: "role",
        header: "角色",
        cell: ({ row }) => ROLE_LABELS[row.original.role] ?? row.original.role,
      },
      {
        accessorKey: "expires_at",
        header: "过期时间",
        cell: ({ row }) => (
          <span className="text-muted-foreground">
            {new Date(row.original.expires_at).toLocaleString()}
          </span>
        ),
      },
      {
        id: "__actions__",
        enableSorting: false,
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => (
          <div className="text-right">
            <Button
              variant="ghost"
              size="icon"
              aria-label="撤销邀请"
              onClick={() => revokeMutation.mutate(row.original.id)}
            >
              <Trash2 className="size-4 text-destructive" />
            </Button>
          </div>
        ),
      },
    ],
    [revokeMutation],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="成员与角色"
        description="管理本组织成员;邀请链接 7 天有效,仅生成时可见"
        actions={
          <Dialog
            open={inviteOpen}
            onOpenChange={(open) => {
              setInviteOpen(open);
              if (!open) setCreatedInvite(null);
            }}
          >
            <DialogTrigger render={<Button />}>
              <MailPlus className="size-4" />
              邀请成员
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>邀请成员</DialogTitle>
              </DialogHeader>
              {createdInvite ? (
                <div className="space-y-4">
                  <p className="text-sm">
                    已为 <span className="font-medium">{createdInvite.email}</span>{" "}
                    生成邀请(角色:{ROLE_LABELS[createdInvite.role] ?? createdInvite.role})。
                    把下面的链接发给对方,链接只显示这一次:
                  </p>
                  <div className="flex items-center gap-2">
                    <Input readOnly value={inviteLink} className="font-mono text-xs" />
                    <Button
                      variant="outline"
                      size="icon"
                      aria-label="复制链接"
                      onClick={() => {
                        navigator.clipboard.writeText(inviteLink);
                        toast.success("已复制");
                      }}
                    >
                      <Copy className="size-4" />
                    </Button>
                  </div>
                  <Button className="w-full" onClick={() => setCreatedInvite(null)}>
                    继续邀请其他人
                  </Button>
                </div>
              ) : (
                <div className="space-y-4">
                  <FormField label="邮箱" htmlFor="invite-email" required>
                    <Input
                      id="invite-email"
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="colleague@example.com"
                    />
                  </FormField>
                  <FormField label="角色" htmlFor="invite-role">
                    <Select
                      items={ROLE_ITEMS}
                      value={role}
                      onValueChange={(v) => setRole(v as string)}
                    >
                      <SelectTrigger id="invite-role" className="h-9 w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {ASSIGNABLE_ROLES.map((r) => (
                          <SelectItem key={r} value={r}>
                            {ROLE_LABELS[r]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                  <Button
                    className="w-full"
                    disabled={!email.trim() || inviteMutation.isPending}
                    onClick={() => inviteMutation.mutate()}
                  >
                    生成邀请链接
                  </Button>
                </div>
              )}
            </DialogContent>
          </Dialog>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            成员({membersQuery.data?.length ?? 0})
          </CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={memberColumns}
            data={membersQuery.data}
            isLoading={membersQuery.isLoading}
            error={membersQuery.error}
            onRetry={() => membersQuery.refetch()}
            getRowId={(m) => m.membership_id}
            empty={{ icon: Users, title: "暂无成员" }}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            待接受邀请({invitesQuery.data?.length ?? 0})
          </CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={inviteColumns}
            data={invitesQuery.data}
            isLoading={invitesQuery.isLoading}
            error={invitesQuery.error}
            onRetry={() => invitesQuery.refetch()}
            getRowId={(i) => i.id}
            empty={{ icon: MailPlus, title: "当前没有待接受的邀请" }}
          />
        </CardContent>
      </Card>
    </div>
  );
}
