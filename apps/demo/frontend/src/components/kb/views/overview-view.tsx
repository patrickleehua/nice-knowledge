"use client";

// overview 视图:库信息卡 + 知识流水线总览图(KbOverviewMap)。
// 原「三统计卡 + 三快捷入口卡」已合并进流水线节点徽章,数据源不变,
// 统计与跳转都由总览图承担,不再重复平铺。

import { FolderOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { KnowledgeBase } from "@/lib/types";
import type { KbViewKey } from "@/components/kb/workbench/views";
import { useKbDocuments } from "@/components/kb/workbench/kb-data";
import { KbOverviewMap } from "@/components/kb/overview-map";

export function OverviewView({
  kbId,
  kb,
  onNavigate,
}: {
  kbId: string;
  kb?: KnowledgeBase;
  onNavigate: (view: KbViewKey) => void;
}) {
  // 与 KbOverviewMap 同 queryKey,react-query 去重,这里只为空库判定
  const { data: docs } = useKbDocuments(kbId);

  // 新建的空库直接给下一步指引;总览图同步置灰(仅「资料」可点)
  const isEmpty = docs !== undefined && docs.length === 0;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{kb?.name ?? "知识库"}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm">
          <div className="text-muted-foreground">
            类型 {kb?.kb_type ?? "…"} · {kb?.description || "暂无描述"}
          </div>
        </CardContent>
      </Card>

      {isEmpty && (
        <Card className="border-primary/40 bg-primary/5">
          <CardContent className="flex flex-wrap items-center gap-3 pt-4">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">这个知识库还是空的</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                上传资料 → 审核抽取出的事实 → 构建并激活快照，知识才会被检索与下游消费使用。
              </p>
            </div>
            <Button size="sm" onClick={() => onNavigate("sources")}>
              <FolderOpen className="size-4" />
              去上传资料
            </Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">知识流水线</CardTitle>
        </CardHeader>
        <CardContent>
          <KbOverviewMap kbId={kbId} kb={kb} onNavigate={onNavigate} />
        </CardContent>
      </Card>
    </div>
  );
}
