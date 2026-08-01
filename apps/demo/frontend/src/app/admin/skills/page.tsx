"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Plus, Sparkles, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, ErrorState, PageHeader, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { SkillDetailDto, SkillDto } from "@/lib/types";

const TEMPLATE = `---
name: 技能名称
description: 一句话说明用途
version: 1.0.0
category: general
---

# 使用说明
`;

const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;

export default function AdminSkillsPage() {
  const queryClient = useQueryClient();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const skills = useQuery({
    queryKey: ["admin-skills"],
    queryFn: () => api.get<SkillDto[]>("/admin/skills"),
  });
  const deleteSkill = useMutation({
    mutationFn: (slug: string) => api.delete(`/admin/skills/${slug}`),
    onSuccess: () => {
      toast.success("技能已删除");
      queryClient.invalidateQueries({ queryKey: ["admin-skills"] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409)
        toast.error("有 Agent 的激活版本正在使用此技能，请先解绑");
      else toast.error(errMsg(error, "删除失败"));
    },
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="技能管理"
        description="维护 SKILL.md 技能包；Agent 绑定后按需读取（只读，不执行脚本）"
        actions={
          <Button onClick={() => setCreating(true)}>
            <Plus className="size-4" />
            新建技能
          </Button>
        }
      />
      {skills.error ? (
        <ErrorState error={skills.error} onRetry={() => skills.refetch()} />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {skills.data?.map((skill) => (
            <div
              key={skill.slug}
              role="button"
              tabIndex={0}
              onClick={() => setEditingSlug(skill.slug)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ")
                  setEditingSlug(skill.slug);
              }}
              className="flex cursor-pointer flex-col gap-3 rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/50"
            >
              <div className="flex items-start gap-3">
                <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-primary/10">
                  <Sparkles className="size-5 text-primary" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">
                    {skill.name}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                    {skill.description || "暂无描述"}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <ToneBadge tone="muted">v{skill.version}</ToneBadge>
                <ToneBadge tone="info">{skill.category}</ToneBadge>
                <span className="flex items-center gap-1 text-xs text-muted-foreground">
                  <FileText className="size-3" />
                  {skill.files.length} 个文件
                </span>
                <span
                  className="ml-auto"
                  onClick={(event) => event.stopPropagation()}
                >
                  <ConfirmDialog
                    trigger={
                      <Button type="button" size="icon" variant="ghost">
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    }
                    title={`删除技能「${skill.name}」?`}
                    description="整个技能包目录将被删除且不可恢复。"
                    confirmLabel="删除"
                    destructive
                    onConfirm={() => deleteSkill.mutateAsync(skill.slug)}
                  />
                </span>
              </div>
            </div>
          ))}
          {skills.data?.length === 0 && (
            <div className="col-span-full py-16 text-center text-sm text-muted-foreground">
              还没有技能包，点右上角新建
            </div>
          )}
        </div>
      )}
      {(editingSlug !== null || creating) && (
        <SkillEditorDialog
          slug={editingSlug}
          onClose={() => {
            setEditingSlug(null);
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

function SkillEditorDialog({
  slug,
  onClose,
}: {
  slug: string | null; // null = 新建
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [newSlug, setNewSlug] = useState("");
  // null = 用户尚未编辑,展示服务端内容(编辑态)或模板(新建态)
  const [draft, setDraft] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["admin-skill", slug],
    queryFn: () => api.get<SkillDetailDto>(`/admin/skills/${slug}`),
    enabled: !!slug,
  });
  const content = draft ?? (slug ? (detail.data?.content ?? "") : TEMPLATE);

  const targetSlug = slug ?? newSlug.trim();
  const slugValid = SLUG_RE.test(targetSlug);
  const save = useMutation({
    mutationFn: () => api.put(`/admin/skills/${targetSlug}`, { content }),
    onSuccess: () => {
      toast.success(slug ? "技能已更新" : "技能已创建");
      queryClient.invalidateQueries({ queryKey: ["admin-skills"] });
      queryClient.invalidateQueries({ queryKey: ["admin-skill", targetSlug] });
      onClose();
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{slug ? `编辑技能 ${slug}` : "新建技能"}</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
          {!slug && (
            <label className="block text-sm">
              slug（目录名，小写字母/数字/中划线）
              <Input
                className="mt-1 font-mono"
                value={newSlug}
                onChange={(event) => setNewSlug(event.target.value)}
                placeholder="如 code-review-guide"
              />
              {newSlug.trim() && !slugValid && (
                <p className="mt-1 text-xs text-destructive">
                  slug 格式不合法
                </p>
              )}
            </label>
          )}
          <label className="block text-sm">
            SKILL.md 内容（frontmatter 必须包含 name 与 description）
            <Textarea
              rows={16}
              className="mt-1 font-mono text-xs"
              value={content}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          {slug && detail.data && detail.data.files.length > 1 && (
            <div className="text-xs text-muted-foreground">
              包内其它文件（只读）：
              {detail.data.files
                .filter((file) => file !== "SKILL.md")
                .join("、")}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button
            onClick={() => save.mutate()}
            disabled={!slugValid || !content.trim() || save.isPending}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
