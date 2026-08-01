"use client";

// Prompt 注册表三视角(TripBoss AutoPrompt 式重构):
// 1) 系统资源:随源码发布的只读 prompt 段,分类浏览 + 详情
// 2) 组装预览:按 agent 卡把静态段组装成最终 system prompt
// 3) 可编辑 Prompt:DB 登记的任务级 prompt(发版/启停),原功能原样保留

import { PageHeader } from "@/components/shared";
import { PromptAssemblyTab } from "@/components/admin/prompt-assembly-tab";
import { PromptRegistryTab } from "@/components/admin/prompt-registry-tab";
import { PromptResourcesTab } from "@/components/admin/prompt-resources-tab";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function AdminPromptsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Prompt 注册表"
        description="系统 prompt 资源浏览、按 agent 卡的组装预览,与 DB 可编辑 prompt 的版本管理"
      />

      <Tabs defaultValue="resources">
        <TabsList>
          <TabsTrigger value="resources">系统资源</TabsTrigger>
          <TabsTrigger value="assembly">组装预览</TabsTrigger>
          <TabsTrigger value="registry">可编辑 Prompt</TabsTrigger>
        </TabsList>

        <TabsContent value="resources">
          <PromptResourcesTab />
        </TabsContent>
        <TabsContent value="assembly">
          <PromptAssemblyTab />
        </TabsContent>
        <TabsContent value="registry">
          <PromptRegistryTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
