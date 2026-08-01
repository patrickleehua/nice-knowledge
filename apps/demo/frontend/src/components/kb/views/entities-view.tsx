"use client";

// entities 视图两 Tab:业务实体 / 实体归一。
// 「实体类型配置」是组织级配置(不带 kb_id),本体在设置页的「实体类型」Tab,
// 不再占本视图一个 Tab——原先「图标栏 → Tabs → 页内多张卡片表格 → 每表各自分页」
// 是四层嵌套;这里只保留一个跳转入口,从建模现场直达 schema 管理。
// Tab 进 URL(?tab=),刷新与分享不丢。

import { Settings2 } from "lucide-react";
import { CanonicalEntitiesPanel } from "@/components/kb/canonical-entities-panel";
import { EntitiesTab } from "@/components/kb/entities-tab";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUrlState } from "@/lib/use-url-state";

export function EntitiesView({ kbId }: { kbId: string }) {
  const { get, set } = useUrlState();
  const tab = get("tab") === "canonical" ? "canonical" : "business";

  return (
    <Tabs
      value={tab}
      onValueChange={(next) =>
        set({ tab: next === "business" ? null : String(next) })
      }
      className="gap-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <TabsList className="w-full sm:w-fit">
          <TabsTrigger value="business">业务实体</TabsTrigger>
          <TabsTrigger value="canonical">实体归一</TabsTrigger>
        </TabsList>
        {/* 直接改 ?view=&tab= 跳设置页:本视图不注册 unsaved-guard,无绕过风险 */}
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground"
          onClick={() => set({ view: "settings", tab: "types" })}
        >
          <Settings2 className="size-3.5" />
          实体类型配置
        </Button>
      </div>
      <TabsContent value="business">
        <EntitiesTab kbId={kbId} />
      </TabsContent>
      <TabsContent value="canonical">
        <CanonicalEntitiesPanel kbId={kbId} />
      </TabsContent>
    </Tabs>
  );
}
