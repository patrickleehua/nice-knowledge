"use client";

// 自定义 Prompt 任务的目录信息编辑(详情弹窗内使用):
// PUT /admin/prompt-catalog/{task} upsert 中文名/作用描述,DELETE 清除登记。
// 内置任务的目录随源码治理(后端 403),调用方只对 builtin=false 的任务渲染本组件。
// 使用处用 key={task} 挂载:切换任务时草稿随组件重建,不需要手写重置逻辑。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Spinner, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";

export function PromptCatalogEntryEditor({
  task,
  registered,
  initialName,
  initialDescription,
}: {
  task: string;
  /** true=已有 DB 目录登记(此时 initialName/Description 才是登记值而非回落值) */
  registered: boolean;
  initialName: string;
  initialDescription: string;
}) {
  const queryClient = useQueryClient();
  // 未登记时 name_zh 回落为机器名,不能把机器名当草稿预填——那会诱导原样保存
  const [name, setName] = useState(registered ? initialName : "");
  const [description, setDescription] = useState(registered ? initialDescription : "");

  const saveMutation = useMutation({
    mutationFn: () =>
      api.put(`/admin/prompt-catalog/${encodeURIComponent(task)}`, {
        name_zh: name.trim(),
        description: description.trim(),
      }),
    onSuccess: () => {
      toast.success("目录信息已保存");
      queryClient.invalidateQueries({ queryKey: ["admin-prompts"] });
    },
    onError: (err) => toast.error(errMsg(err, "目录信息保存失败")),
  });

  const removeMutation = useMutation({
    mutationFn: () => api.delete(`/admin/prompt-catalog/${encodeURIComponent(task)}`),
    onSuccess: () => {
      toast.success("已清除目录登记,列表将回落为机器名展示");
      setName("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["admin-prompts"] });
    },
    onError: (err) => toast.error(errMsg(err, "清除登记失败")),
  });

  const pending = saveMutation.isPending || removeMutation.isPending;

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium">目录信息(中文名 / 作用描述,可随时改)</span>
        {registered ? (
          <ToneBadge tone="success">已登记</ToneBadge>
        ) : (
          <ToneBadge tone="warning">未登记说明</ToneBadge>
        )}
      </div>
      <Input
        value={name}
        maxLength={100}
        onChange={(e) => setName(e.target.value)}
        placeholder="中文名称(必填,列表以此展示)"
        aria-label="目录中文名称"
      />
      <Textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={2}
        placeholder="作用描述:这条 prompt 在哪个链路里干什么"
        aria-label="目录作用描述"
      />
      <div className="flex justify-end gap-2">
        {registered && (
          <Button
            variant="ghost"
            size="sm"
            disabled={pending}
            onClick={() => removeMutation.mutate()}
          >
            {removeMutation.isPending && <Spinner size={3.5} />}
            清除登记
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={!name.trim() || pending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending && <Spinner size={3.5} />}
          保存目录信息
        </Button>
      </div>
    </div>
  );
}
