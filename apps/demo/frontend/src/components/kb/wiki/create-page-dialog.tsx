"use client";

// 新建 wiki 页 Dialog(KB-5C):title + page_type 下拉(预设 7 类 + 自定义输入)。
// 支持 prefillTitle(点击不存在的 wikilink 时预填);创建成功回调 onCreated,
// 由 wiki-view 切到该页并直接进入 edit 模式。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { FormField } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
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
import type { KbPage } from "@/lib/types";
import { kbPagesQueryKey } from "./data";
import { PAGE_TYPE_META, PAGE_TYPE_ORDER } from "./page-types";

const CUSTOM_TYPE = "__custom__";

const TYPE_ITEMS: Record<string, string> = {
  ...Object.fromEntries(
    PAGE_TYPE_ORDER.map((t) => [t, PAGE_TYPE_META[t].label]),
  ),
  [CUSTOM_TYPE]: "自定义…",
};

export function CreatePageDialog({
  kbId,
  open,
  prefillTitle,
  onOpenChange,
  onCreated,
}: {
  kbId: string;
  open: boolean;
  /** wikilink「暂无此页」入口预填的标题 */
  prefillTitle?: string;
  onOpenChange: (open: boolean) => void;
  onCreated: (page: KbPage) => void;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [typeChoice, setTypeChoice] = useState("topic");
  const [customType, setCustomType] = useState("");

  // 每次打开(false→true)重置表单并应用预填标题。用「render 期对比上次值」
  // 而非 effect setState(后者触发级联渲染告警);React 支持 render 期条件 setState。
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setTitle(prefillTitle ?? "");
      setTypeChoice("topic");
      setCustomType("");
    }
  }

  const pageType =
    typeChoice === CUSTOM_TYPE
      ? customType.trim().toLowerCase()
      : typeChoice;

  const create = useMutation({
    mutationFn: () =>
      api.post<KbPage>("/kb/pages", {
        kb_id: kbId,
        title: title.trim(),
        page_type: pageType || "topic",
        content: null,
      }),
    onSuccess: (page) => {
      toast.success("已创建,开始撰写正文");
      queryClient.invalidateQueries({ queryKey: kbPagesQueryKey(kbId) });
      onOpenChange(false);
      onCreated(page);
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>新建 wiki 页</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <FormField label="标题" htmlFor="wiki-page-title" required>
            <Input
              id="wiki-page-title"
              value={title}
              autoFocus
              placeholder="如:英国签证材料清单"
              onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && title.trim() && !create.isPending) {
                  create.mutate();
                }
              }}
            />
          </FormField>
          <FormField label="类型" htmlFor="wiki-page-type">
            <Select
              items={TYPE_ITEMS}
              value={typeChoice}
              onValueChange={(v) => setTypeChoice(v as string)}
            >
              <SelectTrigger id="wiki-page-type" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_TYPE_ORDER.map((t) => {
                  const Icon = PAGE_TYPE_META[t].icon;
                  return (
                    <SelectItem key={t} value={t}>
                      <Icon className="size-3.5 text-muted-foreground" />
                      {PAGE_TYPE_META[t].label}
                    </SelectItem>
                  );
                })}
                <SelectItem value={CUSTOM_TYPE}>自定义…</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {typeChoice === CUSTOM_TYPE && (
            <FormField
              label="自定义类型"
              htmlFor="wiki-page-custom-type"
              description="开放字符串,小写标识,如 visa / faq / policy"
            >
              <Input
                id="wiki-page-custom-type"
                value={customType}
                placeholder="visa"
                onChange={(e) => setCustomType(e.target.value)}
              />
            </FormField>
          )}
          <Button
            className="w-full"
            disabled={!title.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            创建并编辑
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
