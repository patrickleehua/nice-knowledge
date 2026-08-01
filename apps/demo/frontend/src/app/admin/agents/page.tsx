"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Check, Plus, Power, Rocket, Trash2 } from "lucide-react";
import { useForm } from "react-hook-form";
import { useState } from "react";
import { toast } from "sonner";
import { z } from "zod";
import { ConfirmDialog, ErrorState, PageHeader, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { api, ApiError } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type {
  AgentCardDto,
  AgentToolDto,
  AgentVersionDto,
  McpServerDto,
  SkillDto,
} from "@/lib/types";

const versionSchema = z.object({
  system_prompt: z.string().min(1),
  model_task: z.string().min(1),
  max_turns: z.coerce.number().int().min(1).max(50),
  timeout_seconds: z.coerce.number().int().min(1).max(3600),
});
type VersionForm = z.infer<typeof versionSchema>;

export default function AdminAgentsPage() {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const cards = useQuery({
    queryKey: ["admin-agent-cards"],
    queryFn: () => api.get<AgentCardDto[]>("/admin/agent-cards"),
  });
  const editing = cards.data?.find((card) => card.id === editingId) ?? null;

  const toggleActive = useMutation({
    mutationFn: (card: AgentCardDto) =>
      api.patch(`/admin/agent-cards/${card.id}`, { is_active: !card.is_active }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["admin-agent-cards"] }),
    onError: (error) => toast.error(errMsg(error)),
  });
  const deleteCard = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/agent-cards/${id}`),
    onSuccess: () => {
      toast.success("Agent 已删除");
      queryClient.invalidateQueries({ queryKey: ["admin-agent-cards"] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409)
        toast.error("已有会话使用此 Agent，请改为停用");
      else toast.error(errMsg(error, "删除失败"));
    },
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Agent 管理"
        description="每个 Agent 一张卡片，点击卡片查看与编辑能力配置"
        actions={
          <Button onClick={() => setCreating(true)}>
            <Plus className="size-4" />
            新建 Agent
          </Button>
        }
      />
      {cards.error ? (
        <ErrorState error={cards.error} onRetry={() => cards.refetch()} />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {cards.data?.map((card) => (
            <div
              key={card.id}
              role="button"
              tabIndex={0}
              onClick={() => setEditingId(card.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ")
                  setEditingId(card.id);
              }}
              className={cn(
                "group flex cursor-pointer flex-col gap-3 rounded-lg border border-border bg-card p-4 text-left transition-colors hover:border-primary/50",
                !card.is_active && "opacity-60",
              )}
            >
              <div className="flex items-start gap-3">
                <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-primary/10 text-lg">
                  {card.icon || <Bot className="size-5 text-primary" />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold">
                    {card.name}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                    {card.description || "暂无描述"}
                  </p>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <ToneBadge tone={card.active_version ? "success" : "muted"}>
                  {card.active_version
                    ? `v${card.active_version.version}`
                    : "无激活版本"}
                </ToneBadge>
                <ToneBadge tone="muted">
                  {card.is_platform ? "平台卡" : "租户卡"}
                </ToneBadge>
                {card.active_version && (
                  <span className="text-xs text-muted-foreground">
                    工具 {card.active_version.tools.length} · 技能{" "}
                    {card.active_version.skills.length}
                  </span>
                )}
                <span
                  className="ml-auto flex items-center gap-1"
                  onClick={(event) => event.stopPropagation()}
                >
                  <Tooltip>
                    <TooltipTrigger
                      render={
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          onClick={() => toggleActive.mutate(card)}
                        >
                          <Power
                            className={cn(
                              "size-3.5",
                              card.is_active
                                ? "text-emerald-600"
                                : "text-muted-foreground",
                            )}
                          />
                        </Button>
                      }
                    />
                    <TooltipContent>
                      {card.is_active ? "停用" : "启用"}
                    </TooltipContent>
                  </Tooltip>
                  <ConfirmDialog
                    trigger={
                      <Button type="button" size="icon" variant="ghost">
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    }
                    title={`删除 Agent「${card.name}」?`}
                    description="所有版本将一并删除且不可恢复；已有会话使用时无法删除。"
                    confirmLabel="删除"
                    destructive
                    onConfirm={() => deleteCard.mutateAsync(card.id)}
                  />
                </span>
              </div>
            </div>
          ))}
          {cards.data?.length === 0 && (
            <div className="col-span-full py-16 text-center text-sm text-muted-foreground">
              还没有 Agent，点右上角新建
            </div>
          )}
        </div>
      )}
      {editing && (
        <AgentEditorDialog
          card={editing}
          onClose={() => setEditingId(null)}
        />
      )}
      <CreateAgentDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(id) => setEditingId(id)}
      />
    </div>
  );
}

function CreateAgentDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const createCard = useMutation({
    mutationFn: () =>
      api.post<AgentCardDto>("/admin/agent-cards", {
        name,
        description: description || null,
      }),
    onSuccess: (card) => {
      setName("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["admin-agent-cards"] });
      onClose();
      onCreated(card.id);
    },
    onError: (error) => toast.error(errMsg(error)),
  });
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建 Agent</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <label className="block text-sm">
            名称
            <Input
              className="mt-1"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="如 workbench"
            />
          </label>
          <label className="block text-sm">
            描述
            <Textarea
              className="mt-1"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
        </div>
        <DialogFooter>
          <Button onClick={() => createCard.mutate()} disabled={!name.trim()}>
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AgentEditorDialog({
  card,
  onClose,
}: {
  card: AgentCardDto;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const active = card.active_version;
  const [tools, setTools] = useState<string[]>(active?.tools ?? []);
  const [skills, setSkills] = useState<string[]>(active?.skills ?? []);
  const [servers, setServers] = useState<string[]>(
    active?.mcp_bindings.map((item) => item.server_id) ?? [],
  );
  const [info, setInfo] = useState({
    name: card.name,
    description: card.description ?? "",
    icon: card.icon ?? "",
    sort_order: card.sort_order,
  });
  const { register, handleSubmit, reset } = useForm<VersionForm>({
    defaultValues: {
      system_prompt: active?.system_prompt ?? "",
      model_task: active?.model_task ?? "agent.workbench",
      max_turns: active?.max_turns ?? 8,
      timeout_seconds: active?.timeout_seconds ?? 300,
    },
  });
  const toolOptions = useQuery({
    queryKey: ["admin-agent-tools"],
    queryFn: () => api.get<AgentToolDto[]>("/admin/agent-tools"),
  });
  const skillOptions = useQuery({
    queryKey: ["admin-skills"],
    queryFn: () => api.get<SkillDto[]>("/admin/skills"),
  });
  const mcpOptions = useQuery({
    queryKey: ["admin-mcp"],
    queryFn: () => api.get<McpServerDto[]>("/admin/mcp-servers"),
  });
  const versions = useQuery({
    queryKey: ["admin-agent-versions", card.id],
    queryFn: () =>
      api.get<AgentVersionDto[]>(`/admin/agent-cards/${card.id}/versions`),
  });

  const saveInfo = useMutation({
    mutationFn: () =>
      api.patch(`/admin/agent-cards/${card.id}`, {
        name: info.name,
        description: info.description || null,
        icon: info.icon || null,
        sort_order: info.sort_order,
      }),
    onSuccess: () => {
      toast.success("基本信息已保存");
      queryClient.invalidateQueries({ queryKey: ["admin-agent-cards"] });
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });
  const createVersion = useMutation({
    mutationFn: (form: VersionForm) => {
      const parsed = versionSchema.parse(form);
      return api.post<AgentVersionDto>(
        `/admin/agent-cards/${card.id}/versions`,
        {
          ...parsed,
          tools,
          skills,
          mcp_bindings: servers.map((server_id) => ({
            server_id,
            enabled_tools: null,
          })),
        },
      );
    },
    onSuccess: () => {
      toast.success("草稿版本已创建，在版本历史中激活后生效");
      queryClient.invalidateQueries({
        queryKey: ["admin-agent-versions", card.id],
      });
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });
  const activate = useMutation({
    mutationFn: (versionId: string) =>
      api.post(`/admin/agent-cards/${card.id}/versions/${versionId}/activate`),
    onSuccess: () => {
      toast.success("版本已激活");
      queryClient.invalidateQueries({ queryKey: ["admin-agent-cards"] });
      queryClient.invalidateQueries({
        queryKey: ["admin-agent-versions", card.id],
      });
    },
    onError: (error) => toast.error(errMsg(error)),
  });

  function toggle(
    value: string,
    values: string[],
    setter: (values: string[]) => void,
  ) {
    setter(
      values.includes(value)
        ? values.filter((item) => item !== value)
        : [...values, value],
    );
  }
  function loadDraft(version: AgentVersionDto) {
    reset(version);
    setTools(version.tools);
    setSkills(version.skills);
    setServers(version.mcp_bindings.map((item) => item.server_id));
  }

  const toolGroups = new Map<string, AgentToolDto[]>();
  for (const tool of toolOptions.data ?? []) {
    const group = toolGroups.get(tool.category) ?? [];
    group.push(tool);
    toolGroups.set(tool.category, group);
  }

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bot className="size-4 text-primary" />
            {card.name}
            <ToneBadge tone={active ? "success" : "muted"}>
              当前 {active ? `v${active.version}` : "无激活版本"}
            </ToneBadge>
          </DialogTitle>
        </DialogHeader>
        <Tabs defaultValue="capability" className="min-h-0 flex-1">
          <TabsList>
            <TabsTrigger value="capability">能力配置</TabsTrigger>
            <TabsTrigger value="history">版本历史</TabsTrigger>
            <TabsTrigger value="info">基本信息</TabsTrigger>
          </TabsList>
          <TabsContent
            value="capability"
            className="max-h-[60vh] overflow-y-auto pr-1"
          >
            <form
              className="space-y-4"
              onSubmit={handleSubmit((form) => createVersion.mutate(form))}
            >
              <label className="block text-sm">
                System prompt
                <Textarea
                  rows={8}
                  className="mt-1 font-mono text-xs"
                  {...register("system_prompt")}
                />
              </label>
              <div className="grid gap-3 sm:grid-cols-3">
                <label className="text-sm">
                  模型任务
                  <Input className="mt-1" {...register("model_task")} />
                </label>
                <label className="text-sm">
                  最大轮数
                  <Input
                    type="number"
                    className="mt-1"
                    {...register("max_turns")}
                  />
                </label>
                <label className="text-sm">
                  超时（秒）
                  <Input
                    type="number"
                    className="mt-1"
                    {...register("timeout_seconds")}
                  />
                </label>
              </div>
              <fieldset>
                <legend className="mb-2 text-sm font-medium">原生工具</legend>
                <div className="space-y-3">
                  {[...toolGroups.entries()].map(([category, group]) => (
                    <div key={category}>
                      <div className="mb-1.5 flex items-center gap-2">
                        <span className="text-xs font-medium text-muted-foreground">
                          {category}
                        </span>
                        <button
                          type="button"
                          className="text-xs text-primary hover:underline"
                          onClick={() =>
                            setTools([
                              ...new Set([
                                ...tools,
                                ...group.map((tool) => tool.name),
                              ]),
                            ])
                          }
                        >
                          全选
                        </button>
                        <button
                          type="button"
                          className="text-xs text-muted-foreground hover:underline"
                          onClick={() =>
                            setTools(
                              tools.filter(
                                (name) =>
                                  !group.some((tool) => tool.name === name),
                              ),
                            )
                          }
                        >
                          清空
                        </button>
                      </div>
                      <div className="grid gap-2 sm:grid-cols-2">
                        {group.map((tool) => (
                          <Tooltip key={tool.name}>
                            <TooltipTrigger
                              render={
                                <label className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                                  <Checkbox
                                    checked={tools.includes(tool.name)}
                                    onCheckedChange={() =>
                                      toggle(tool.name, tools, setTools)
                                    }
                                  />
                                  <span className="truncate">{tool.label}</span>
                                  <span className="truncate font-mono text-[10px] text-muted-foreground">
                                    {tool.name}
                                  </span>
                                  <ToneBadge
                                    tone={
                                      tool.side_effect === "read"
                                        ? "muted"
                                        : "warning"
                                    }
                                    className="ml-auto"
                                  >
                                    {tool.side_effect}
                                  </ToneBadge>
                                </label>
                              }
                            />
                            <TooltipContent className="max-w-xs">
                              {tool.description}
                            </TooltipContent>
                          </Tooltip>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </fieldset>
              <ChoiceGroup
                title="MCP 服务器"
                empty="还没有可用的 MCP 服务器"
                options={
                  mcpOptions.data?.map((item) => ({
                    value: item.id,
                    label: item.name,
                    detail: item.transport,
                  })) ?? []
                }
                values={servers}
                onToggle={(value) => toggle(value, servers, setServers)}
              />
              <ChoiceGroup
                title="Skills"
                empty="还没有技能包，可在「技能管理」新建"
                options={
                  skillOptions.data?.map((item) => ({
                    value: item.slug,
                    label: item.name,
                    detail: item.version,
                  })) ?? []
                }
                values={skills}
                onToggle={(value) => toggle(value, skills, setSkills)}
              />
              <Button type="submit">
                <Check className="size-4" />
                保存草稿版本
              </Button>
            </form>
          </TabsContent>
          <TabsContent
            value="history"
            className="max-h-[60vh] space-y-2 overflow-y-auto pr-1"
          >
            {versions.data?.map((version) => (
              <div
                key={version.id}
                className="flex items-center gap-3 rounded border border-border px-3 py-2 text-sm"
              >
                <span className="font-mono">v{version.version}</span>
                <ToneBadge tone={version.is_active ? "success" : "muted"}>
                  {version.status}
                </ToneBadge>
                <span className="text-xs text-muted-foreground">
                  工具 {version.tools.length}
                </span>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  onClick={() => loadDraft(version)}
                >
                  以此为底稿
                </Button>
                {!version.is_active && (
                  <ConfirmDialog
                    trigger={
                      <Button type="button" size="sm" variant="outline">
                        <Rocket className="size-3.5" />
                        激活
                      </Button>
                    }
                    title={`激活 v${version.version}?`}
                    description="将替换当前激活版本，新会话立即使用新配置。"
                    onConfirm={() => activate.mutateAsync(version.id)}
                  />
                )}
              </div>
            ))}
            {versions.data?.length === 0 && (
              <p className="py-8 text-center text-xs text-muted-foreground">
                还没有版本，先在「能力配置」保存草稿
              </p>
            )}
          </TabsContent>
          <TabsContent value="info" className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-sm">
                名称
                <Input
                  className="mt-1"
                  value={info.name}
                  onChange={(event) =>
                    setInfo({ ...info, name: event.target.value })
                  }
                />
              </label>
              <label className="text-sm">
                图标（emoji）
                <Input
                  className="mt-1"
                  value={info.icon}
                  onChange={(event) =>
                    setInfo({ ...info, icon: event.target.value })
                  }
                  placeholder="如 🧭"
                />
              </label>
            </div>
            <label className="block text-sm">
              描述
              <Textarea
                rows={3}
                className="mt-1"
                value={info.description}
                onChange={(event) =>
                  setInfo({ ...info, description: event.target.value })
                }
              />
            </label>
            <label className="block w-40 text-sm">
              排序权重
              <Input
                type="number"
                className="mt-1"
                value={info.sort_order}
                onChange={(event) =>
                  setInfo({
                    ...info,
                    sort_order: Number(event.target.value) || 0,
                  })
                }
              />
            </label>
            <Button
              onClick={() => saveInfo.mutate()}
              disabled={!info.name.trim()}
            >
              保存基本信息
            </Button>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

function ChoiceGroup({
  title,
  empty,
  options,
  values,
  onToggle,
}: {
  title: string;
  empty: string;
  options: { value: string; label: string; detail: string }[];
  values: string[];
  onToggle: (value: string) => void;
}) {
  return (
    <fieldset>
      <legend className="mb-2 text-sm font-medium">{title}</legend>
      {options.length === 0 ? (
        <p className="text-xs text-muted-foreground">{empty}</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {options.map((option) => (
            <label
              key={option.value}
              className="flex items-center gap-2 rounded border border-border p-2 text-sm"
            >
              <Checkbox
                checked={values.includes(option.value)}
                onCheckedChange={() => onToggle(option.value)}
              />
              <span className="truncate">{option.label}</span>
              <span className="ml-auto text-xs text-muted-foreground">
                {option.detail}
              </span>
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}
