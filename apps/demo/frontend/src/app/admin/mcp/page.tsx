"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  CircleAlert,
  Edit3,
  Network,
  Plus,
  RefreshCw,
  Terminal,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import {
  ConfirmDialog,
  ErrorState,
  PageHeader,
  ToneBadge,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
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
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { cn, errMsg } from "@/lib/utils";
import type {
  McpServerDto,
  McpServerStatusDto,
  McpToolDto,
} from "@/lib/types";

const schema = z
  .object({
    name: z
      .string()
      .regex(/^[a-z0-9][a-z0-9_-]*$/, "名称只能是小写字母数字与 - _"),
    transport: z.enum(["streamable_http", "sse", "stdio"]),
    endpoint_url: z.string(),
    headers: z.string(),
    command: z.string(),
    args: z.string(), // 每行一个参数
    env: z.string(), // JSON
    connect_timeout_seconds: z.string(), // 空 = 默认
    call_timeout_seconds: z.string(),
  })
  .superRefine((data, ctx) => {
    if (data.transport === "stdio") {
      if (!data.command.trim())
        ctx.addIssue({
          code: "custom",
          path: ["command"],
          message: "stdio 传输必须填写启动命令",
        });
    } else if (!z.url().safeParse(data.endpoint_url).success) {
      ctx.addIssue({
        code: "custom",
        path: ["endpoint_url"],
        message: "请填写合法的 Endpoint URL",
      });
    }
    for (const key of ["headers", "env"] as const) {
      try {
        JSON.parse(data[key] || "{}");
      } catch {
        ctx.addIssue({ code: "custom", path: [key], message: "不是合法 JSON" });
      }
    }
    for (const key of [
      "connect_timeout_seconds",
      "call_timeout_seconds",
    ] as const) {
      const raw = data[key].trim();
      if (!raw) continue;
      const value = Number(raw);
      if (!Number.isInteger(value) || value < 1 || value > 600)
        ctx.addIssue({
          code: "custom",
          path: [key],
          message: "1~600 的整数,留空用默认",
        });
    }
  });
type FormData = z.infer<typeof schema>;

const EMPTY_FORM: FormData = {
  name: "",
  transport: "streamable_http",
  endpoint_url: "",
  headers: "{}",
  command: "",
  args: "",
  env: "{}",
  connect_timeout_seconds: "",
  call_timeout_seconds: "",
};

/** 表单 → API payload:args 按行拆、JSON 解析、空超时转 null。 */
function toPayload(form: FormData) {
  return {
    name: form.name,
    transport: form.transport,
    endpoint_url: form.transport === "stdio" ? "" : form.endpoint_url.trim(),
    headers: JSON.parse(form.headers || "{}") as Record<string, string>,
    command: form.transport === "stdio" ? form.command.trim() : null,
    args:
      form.transport === "stdio"
        ? form.args
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean)
        : [],
    env:
      form.transport === "stdio"
        ? (JSON.parse(form.env || "{}") as Record<string, string>)
        : {},
    connect_timeout_seconds: form.connect_timeout_seconds.trim()
      ? Number(form.connect_timeout_seconds)
      : null,
    call_timeout_seconds: form.call_timeout_seconds.trim()
      ? Number(form.call_timeout_seconds)
      : null,
  };
}

type ServerPatch = Partial<Pick<McpServerDto, "enabled" | "enabled_tools">>;

const STATUS_META = {
  online: { label: "在线", tone: "success" as const, dot: "bg-emerald-500" },
  offline: { label: "离线", tone: "destructive" as const, dot: "bg-destructive" },
  disabled: { label: "已停用", tone: "muted" as const, dot: "bg-muted-foreground/40" },
};

function statusOf(server: McpServerDto, status?: McpServerStatusDto) {
  return STATUS_META[status?.status ?? (server.enabled ? "offline" : "disabled")];
}

function endpointLabel(server: McpServerDto) {
  return server.transport === "stdio"
    ? [server.command, ...(server.args ?? [])].join(" ")
    : server.endpoint_url;
}

function formatCheckedAt(value: string | null) {
  if (!value) return "尚未检测";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return <span className="mt-1 block text-xs text-destructive">{message}</span>;
}

function IconButton({
  label,
  children,
  ...props
}: React.ComponentProps<typeof Button> & { label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button size="icon" variant="ghost" aria-label={label} {...props} />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/** 左栏:服务器列表项(状态点 + 名称 + 启用开关)。 */
function ServerListItem({
  server,
  status,
  active,
  saving,
  onSelect,
  onToggle,
}: {
  server: McpServerDto;
  status?: McpServerStatusDto;
  active: boolean;
  saving: boolean;
  onSelect: () => void;
  onToggle: (enabled: boolean) => void;
}) {
  const meta = statusOf(server, status);
  return (
    <div
      className={cn(
        "group flex items-center gap-2.5 rounded-md border border-transparent px-3 py-2.5",
        active ? "border-border bg-accent" : "hover:bg-accent/50",
      )}
    >
      <button
        type="button"
        className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
        onClick={onSelect}
      >
        <span className={cn("size-2 shrink-0 rounded-full", meta.dot)} />
        <span className="min-w-0">
          <span className="block truncate font-mono text-sm">
            {server.name}
          </span>
          <span className="block truncate text-xs text-muted-foreground">
            {server.transport}
          </span>
        </span>
      </button>
      <Switch
        checked={server.enabled}
        disabled={saving}
        onCheckedChange={(checked) => onToggle(checked)}
        aria-label={`启用 ${server.name}`}
      />
    </div>
  );
}

/** 右栏:工具 tab 的单行(开关 + 名称 + 单行描述,点击展开全文)。 */
function ToolRow({
  tool,
  enabled,
  onToggle,
}: {
  tool: McpToolDto;
  enabled: boolean;
  onToggle: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="flex items-start gap-3 px-3 py-2.5 hover:bg-muted/40">
      <button
        type="button"
        className="min-w-0 flex-1 text-left"
        onClick={() => setExpanded((value) => !value)}
        title={expanded ? undefined : "点击展开完整描述"}
      >
        <span className="block truncate font-mono text-xs font-medium">
          {tool.name}
        </span>
        {tool.description && (
          <span
            className={cn(
              "mt-0.5 block text-xs text-muted-foreground",
              !expanded && "line-clamp-1",
            )}
          >
            {tool.description}
          </span>
        )}
      </button>
      <Switch
        className="mt-0.5"
        checked={enabled}
        onCheckedChange={onToggle}
        aria-label={`启用 ${tool.name}`}
      />
    </div>
  );
}

/** 右栏:选中服务器详情(通用 / 工具 两个 tab)。 */
function ServerDetail({
  server,
  status,
  statusPending,
  refreshing,
  saving,
  onEdit,
  onRefresh,
  onPatch,
  onDelete,
}: {
  server: McpServerDto;
  status?: McpServerStatusDto;
  statusPending: boolean;
  refreshing: boolean;
  saving: boolean;
  onEdit: () => void;
  onRefresh: () => void;
  onPatch: (patch: ServerPatch) => void;
  onDelete: () => Promise<unknown>;
}) {
  const [selection, setSelection] = useState<string[] | null>(
    server.enabled_tools,
  );
  // 切换服务器 / 服务端 enabled_tools 变化时同步本地勾选(render 期调整)
  const [seen, setSeen] = useState(server);
  if (seen !== server) {
    setSeen(server);
    setSelection(server.enabled_tools);
  }

  const tools: McpToolDto[] =
    status?.tools ??
    (server.enabled_tools ?? []).map((name) => ({
      name,
      description: "",
      schema: {},
    }));
  const enabledOf = (name: string) =>
    selection === null || selection.includes(name);
  const selectedCount = tools.filter((tool) => enabledOf(tool.name)).length;
  const dirty =
    JSON.stringify(selection) !== JSON.stringify(server.enabled_tools);
  const meta = statusOf(server, status);

  function toggleTool(name: string) {
    if (selection === null) {
      setSelection(tools.map((tool) => tool.name).filter((item) => item !== name));
      return;
    }
    setSelection(
      selection.includes(name)
        ? selection.filter((item) => item !== name)
        : [...selection, name],
    );
  }

  const TransportIcon = server.transport === "stdio" ? Terminal : Network;

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <TransportIcon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-mono text-sm font-medium">
              {server.name}
            </span>
            <ToneBadge tone={meta.tone}>
              {statusPending && server.enabled ? "检测中" : meta.label}
            </ToneBadge>
          </div>
          <div className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
            {endpointLabel(server)}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <IconButton
            label="重新检测"
            onClick={onRefresh}
            disabled={refreshing || !server.enabled}
          >
            <RefreshCw className={cn("size-4", refreshing && "animate-spin")} />
          </IconButton>
          <IconButton label="编辑" onClick={onEdit}>
            <Edit3 className="size-4" />
          </IconButton>
          <ConfirmDialog
            trigger={
              <Button size="icon" variant="ghost" aria-label="删除">
                <Trash2 className="size-4" />
              </Button>
            }
            title={`删除 ${server.name}？`}
            description="服务器配置及其 Agent 绑定将不可再使用。"
            confirmLabel="删除"
            destructive
            onConfirm={onDelete}
          />
        </div>
      </div>

      {status?.error && (
        <div className="mx-4 mt-3 flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
          {status.error}
        </div>
      )}

      <Tabs defaultValue="general" className="min-h-0 flex-1 gap-0">
        <div className="border-b border-border px-4 py-2">
          <TabsList>
            <TabsTrigger value="general">通用</TabsTrigger>
            <TabsTrigger value="tools">
              工具
              <span className="ml-1 text-xs text-muted-foreground">
                {selectedCount}/{tools.length}
              </span>
            </TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="general" className="overflow-y-auto p-4">
          <dl className="grid max-w-xl grid-cols-2 gap-x-6 gap-y-4 text-sm">
            <div>
              <dt className="text-xs text-muted-foreground">传输协议</dt>
              <dd className="mt-1 font-mono">{server.transport}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">超时（连接 / 调用）</dt>
              <dd className="mt-1">
                {server.connect_timeout_seconds ?? 10}s /{" "}
                {server.call_timeout_seconds ?? 60}s
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">响应延迟</dt>
              <dd className="mt-1">{status?.latency_ms ?? "--"} ms</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">最近检测</dt>
              <dd className="mt-1">{formatCheckedAt(status?.checked_at ?? null)}</dd>
            </div>
            <div className="col-span-2">
              <dt className="text-xs text-muted-foreground">
                {server.transport === "stdio" ? "启动命令" : "Endpoint"}
              </dt>
              <dd className="mt-1 font-mono text-xs break-all">
                {endpointLabel(server)}
              </dd>
            </div>
          </dl>
          <div className="mt-6 flex items-center justify-between rounded-md border border-border px-4 py-3 text-sm max-w-xl">
            <div>
              <div className="font-medium">启用此服务器</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                启用后程序启动时自动加载连接
              </div>
            </div>
            <Switch
              checked={server.enabled}
              disabled={saving}
              onCheckedChange={(checked) => onPatch({ enabled: checked })}
            />
          </div>
        </TabsContent>

        <TabsContent value="tools" className="flex min-h-0 flex-col">
          <div className="flex items-center gap-3 border-b border-border px-4 py-2 text-xs">
            <span className="text-muted-foreground">
              {selectedCount}/{tools.length} 已启用
            </span>
            <button
              type="button"
              className="text-primary hover:underline"
              onClick={() => setSelection(null)}
            >
              全部启用
            </button>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground"
              onClick={() => setSelection([])}
            >
              全部停用
            </button>
            {dirty && (
              <Button
                size="sm"
                className="ml-auto h-7"
                disabled={saving}
                onClick={() => onPatch({ enabled_tools: selection })}
              >
                <Check className="size-3.5" />
                应用变更
              </Button>
            )}
          </div>
          <div className="min-h-0 flex-1 divide-y divide-border overflow-y-auto">
            {statusPending && !status ? (
              <div className="space-y-2 p-4">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            ) : tools.length ? (
              tools.map((tool) => (
                <ToolRow
                  key={tool.name}
                  tool={tool}
                  enabled={enabledOf(tool.name)}
                  onToggle={() => toggleTool(tool.name)}
                />
              ))
            ) : (
              <div className="flex h-full items-center justify-center py-10 text-xs text-muted-foreground">
                {server.enabled ? "服务暂未返回可用工具" : "启用服务后检测可用工具"}
              </div>
            )}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default function AdminMcpPage() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<McpServerDto | null | undefined>();
  const [selectedId, setSelectedId] = useState<string>();
  const [refreshingId, setRefreshingId] = useState<string>();
  const {
    register,
    handleSubmit,
    setValue,
    reset,
    control,
    setError,
    clearErrors,
    formState: { errors },
  } = useForm<FormData>({ defaultValues: EMPTY_FORM });
  const transport = useWatch({ control, name: "transport" });

  const servers = useQuery({
    queryKey: ["admin-mcp"],
    queryFn: () => api.get<McpServerDto[]>("/admin/mcp-servers"),
  });
  const statuses = useQuery({
    queryKey: ["admin-mcp-status"],
    queryFn: () =>
      api.get<McpServerStatusDto[]>("/admin/mcp-servers/status?refresh=true"),
    enabled: Boolean(servers.data?.length),
    retry: false,
  });
  const statusById = new Map(
    statuses.data?.map((status) => [status.server_id, status]) ?? [],
  );
  const selected =
    servers.data?.find((server) => server.id === selectedId) ??
    servers.data?.[0];

  const save = useMutation({
    mutationFn: (form: FormData) => {
      const payload = toPayload(form);
      return editing
        ? api.patch<McpServerDto>(`/admin/mcp-servers/${editing.id}`, payload)
        : api.post<McpServerDto>("/admin/mcp-servers", payload);
    },
    onSuccess: (saved) => {
      toast.success("MCP 配置已保存");
      setEditing(undefined);
      setSelectedId(saved.id);
      queryClient.invalidateQueries({ queryKey: ["admin-mcp"] });
      queryClient.invalidateQueries({ queryKey: ["admin-mcp-status"] });
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });
  const patchServer = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: ServerPatch }) =>
      api.patch<McpServerDto>(`/admin/mcp-servers/${id}`, patch),
    onSuccess: (_, variables) => {
      toast.success(
        "enabled_tools" in variables.patch ? "工具配置已应用" : "服务器状态已更新",
      );
      queryClient.invalidateQueries({ queryKey: ["admin-mcp"] });
      queryClient.invalidateQueries({ queryKey: ["admin-mcp-status"] });
    },
    onError: (error) => toast.error(errMsg(error, "更新失败")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/mcp-servers/${id}`),
    onSuccess: () => {
      toast.success("MCP 服务器已删除");
      queryClient.invalidateQueries({ queryKey: ["admin-mcp"] });
      queryClient.invalidateQueries({ queryKey: ["admin-mcp-status"] });
    },
    onError: (error) => toast.error(errMsg(error, "删除失败")),
  });
  const refresh = useMutation({
    mutationFn: (id: string) => {
      setRefreshingId(id);
      return api.get<McpServerStatusDto>(`/admin/mcp-servers/${id}/status`);
    },
    onSuccess: (result) => {
      queryClient.setQueryData<McpServerStatusDto[]>(
        ["admin-mcp-status"],
        (current = []) => [
          ...current.filter((item) => item.server_id !== result.server_id),
          result,
        ],
      );
      toast.success(result.status === "online" ? "连接正常" : "服务当前不可用");
    },
    onError: (error) => toast.error(errMsg(error, "检测失败")),
    onSettled: () => setRefreshingId(undefined),
  });

  function open(server?: McpServerDto) {
    setEditing(server ?? null);
    reset(
      server
        ? {
            name: server.name,
            transport: server.transport,
            endpoint_url: server.endpoint_url,
            headers: JSON.stringify(server.headers, null, 2),
            command: server.command ?? "",
            args: (server.args ?? []).join("\n"),
            env: JSON.stringify(server.env ?? {}, null, 2),
            connect_timeout_seconds: server.connect_timeout_seconds
              ? String(server.connect_timeout_seconds)
              : "",
            call_timeout_seconds: server.call_timeout_seconds
              ? String(server.call_timeout_seconds)
              : "",
          }
        : EMPTY_FORM,
    );
  }

  function submit(form: FormData) {
    clearErrors();
    const parsed = schema.safeParse(form);
    if (!parsed.success) {
      for (const issue of parsed.error.issues) {
        const field = issue.path[0] as keyof FormData;
        setError(field, { message: issue.message });
      }
      return;
    }
    save.mutate(parsed.data);
  }

  const onlineCount = statuses.data?.filter(
    (item) => item.status === "online",
  ).length;

  return (
    <div className="flex h-[calc(100vh-6.5rem)] flex-col gap-4">
      <PageHeader
        title="MCP 服务器"
        description={
          servers.data?.length
            ? `${servers.data.length} 个服务器${onlineCount === undefined ? "，正在检测连接" : `，${onlineCount} 个在线`}`
            : "管理外部工具服务、连接状态和可用工具"
        }
        actions={
          <Button onClick={() => open()}>
            <Plus className="size-4" />
            添加服务器
          </Button>
        }
      />

      {servers.error ? (
        <ErrorState error={servers.error} onRetry={() => servers.refetch()} />
      ) : servers.isPending ? (
        <div className="flex flex-1 gap-4">
          <Skeleton className="w-64 rounded-lg" />
          <Skeleton className="flex-1 rounded-lg" />
        </div>
      ) : servers.data?.length ? (
        <div className="flex min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-card">
          <aside className="flex w-64 shrink-0 flex-col border-r border-border">
            <div className="flex-1 space-y-1 overflow-y-auto p-2">
              {servers.data.map((server) => (
                <ServerListItem
                  key={server.id}
                  server={server}
                  status={statusById.get(server.id)}
                  active={server.id === selected?.id}
                  saving={patchServer.isPending}
                  onSelect={() => setSelectedId(server.id)}
                  onToggle={(enabled) =>
                    patchServer.mutate({ id: server.id, patch: { enabled } })
                  }
                />
              ))}
            </div>
          </aside>
          {selected ? (
            <ServerDetail
              key={selected.id}
              server={selected}
              status={statusById.get(selected.id)}
              statusPending={statuses.isPending || statuses.isFetching}
              refreshing={refreshingId === selected.id}
              saving={patchServer.isPending}
              onEdit={() => open(selected)}
              onRefresh={() => refresh.mutate(selected.id)}
              onPatch={(patch) =>
                patchServer.mutate({ id: selected.id, patch })
              }
              onDelete={() => remove.mutateAsync(selected.id)}
            />
          ) : null}
        </div>
      ) : (
        <div className="flex min-h-64 flex-1 flex-col items-center justify-center rounded-lg border border-dashed text-center">
          <div className="flex size-10 items-center justify-center rounded-md bg-muted text-muted-foreground">
            <Network className="size-5" />
          </div>
          <div className="mt-3 text-sm font-medium">还没有 MCP 服务器</div>
          <div className="mt-1 text-xs text-muted-foreground">
            添加后会在程序启动时自动恢复已启用的连接
          </div>
          <Button className="mt-4" size="sm" onClick={() => open()}>
            <Plus className="size-4" />
            添加服务器
          </Button>
        </div>
      )}

      <Dialog
        open={editing !== undefined}
        onOpenChange={(next) => !next && setEditing(undefined)}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {editing ? "编辑 MCP 服务器" : "添加 MCP 服务器"}
            </DialogTitle>
          </DialogHeader>
          <form className="space-y-4" onSubmit={handleSubmit(submit)}>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-sm">
                名称
                <Input className="mt-1 font-mono" {...register("name")} />
                <FieldError message={errors.name?.message} />
              </label>
              <label className="block text-sm">
                传输协议
                <Select
                  value={transport}
                  onValueChange={(value) =>
                    setValue("transport", value as FormData["transport"])
                  }
                >
                  <SelectTrigger className="mt-1 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="streamable_http">
                      Streamable HTTP
                    </SelectItem>
                    <SelectItem value="sse">SSE</SelectItem>
                    <SelectItem value="stdio">
                      stdio（本机子进程，仅平台管理员）
                    </SelectItem>
                  </SelectContent>
                </Select>
              </label>
            </div>
            {transport === "stdio" ? (
              <>
                <label className="block text-sm">
                  启动命令
                  <Input
                    className="mt-1 font-mono"
                    placeholder="npx / uvx / python …"
                    {...register("command")}
                  />
                  <FieldError message={errors.command?.message} />
                </label>
                <label className="block text-sm">
                  参数（每行一个）
                  <Textarea
                    rows={4}
                    className="mt-1 font-mono text-xs"
                    placeholder={"-y\n@modelcontextprotocol/server-filesystem\nC:\\data"}
                    {...register("args")}
                  />
                </label>
                <label className="block text-sm">
                  环境变量（JSON）
                  <Textarea
                    rows={4}
                    className="mt-1 font-mono text-xs"
                    {...register("env")}
                  />
                  <FieldError message={errors.env?.message} />
                </label>
              </>
            ) : (
              <>
                <label className="block text-sm">
                  Endpoint URL
                  <Input className="mt-1" {...register("endpoint_url")} />
                  <FieldError message={errors.endpoint_url?.message} />
                </label>
                <label className="block text-sm">
                  请求头（JSON）
                  <Textarea
                    rows={5}
                    className="mt-1 font-mono text-xs"
                    {...register("headers")}
                  />
                  <FieldError message={errors.headers?.message} />
                </label>
              </>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-sm">
                连接超时（秒）
                <Input
                  className="mt-1"
                  placeholder="默认 10"
                  inputMode="numeric"
                  {...register("connect_timeout_seconds")}
                />
                <FieldError message={errors.connect_timeout_seconds?.message} />
              </label>
              <label className="block text-sm">
                工具调用超时（秒）
                <Input
                  className="mt-1"
                  placeholder="默认 60"
                  inputMode="numeric"
                  {...register("call_timeout_seconds")}
                />
                <FieldError message={errors.call_timeout_seconds?.message} />
              </label>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={save.isPending}>
                保存
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
