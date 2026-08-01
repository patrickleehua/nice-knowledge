"use client";

// 通用实体区块:类型选择 → 实体列表 + field_schema 动态渲染的编辑表单。
//
// SDK 化改造(MIGRATION-PLAN B29/§5.8):TF 有五张旅游专表各带一套 CRUD 端点与
// 一张写死列名的表格。SDK 不带任何行业表——**所有**实体统一走
// `/kb/entity-types`(类型注册表)+ `/kb/bases/{kb_id}/entities`(attributes
// JSONB),表单由 field_schema 动态生成,因此这里不区分内置/自定义类型。
//
// 快照管理库(active_snapshot_id 非空)一律只读:入口禁用 + 顶部提示条;写 409 消息由后端兜底。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Info, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, FormField } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { EntityType, GenericEntity, KnowledgeBase } from "@/lib/types";

const SNAPSHOT_READONLY_HINT =
  "该库处于快照管理,实体只读——数据须经摄入→审核→投影链路进入。";

// ---- field_schema → 表单控件描述 --------------------------------------------

type ControlKind = "text" | "number" | "select" | "lines" | "json";

interface FieldSpec {
  key: string;
  label: string;
  required: boolean;
  control: ControlKind;
  enumValues?: string[];
  description?: string;
}

interface JsonSchemaProp {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  items?: { type?: string };
}

/** 解析 JSON Schema 为有序字段描述(name 恒排首位;未知形态兜底 JSON 文本域) */
function parseFieldSpecs(schema: Record<string, unknown>): FieldSpec[] {
  const properties = (schema.properties ?? {}) as Record<string, JsonSchemaProp>;
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as string[]) : [],
  );
  const specs = Object.entries(properties).map(([key, prop]): FieldSpec => {
    const base = {
      key,
      label: prop.title || key,
      required: required.has(key),
      description: prop.description,
    };
    if (Array.isArray(prop.enum) && prop.enum.every((v) => typeof v === "string")) {
      return { ...base, control: "select", enumValues: prop.enum as string[] };
    }
    if (prop.type === "string") return { ...base, control: "text" };
    if (prop.type === "number" || prop.type === "integer") {
      return { ...base, control: "number" };
    }
    if (prop.type === "array" && prop.items?.type === "string") {
      return { ...base, control: "lines" };
    }
    return { ...base, control: "json" };
  });
  // name 是所有类型的必填主字段,固定排在最前
  return specs.sort((a, b) => (a.key === "name" ? -1 : b.key === "name" ? 1 : 0));
}

/** 实体属性值 → 表单文本值 */
function toFormValue(control: ControlKind, v: unknown): string {
  if (v === undefined || v === null) return "";
  switch (control) {
    case "lines":
      return Array.isArray(v) ? v.map(String).join("\n") : String(v);
    case "json":
      return JSON.stringify(v, null, 2);
    default:
      return String(v);
  }
}

/**
 * 表单文本值 → attributes 值;空值返回 undefined(整字段省略)。
 * 非法数字/JSON 抛带字段名的中文错误,由调用方 toast。
 */
function toAttributeValue(spec: FieldSpec, raw: string): unknown {
  const text = raw.trim();
  if (!text) return undefined;
  switch (spec.control) {
    case "number": {
      const n = Number(text);
      if (Number.isNaN(n)) throw new Error(`「${spec.label}」需为数字`);
      return n;
    }
    case "lines":
      return raw
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
    case "json":
      try {
        return JSON.parse(text);
      } catch {
        throw new Error(`「${spec.label}」不是合法 JSON`);
      }
    default:
      return text;
  }
}

/** 实体列表行的关键属性摘要(跳过 name,取前 3 个标量属性) */
function attributeSummary(attributes: Record<string, unknown>): string {
  return Object.entries(attributes)
    .filter(([k, v]) => k !== "name" && v !== null && v !== undefined && v !== "")
    .slice(0, 3)
    .map(([k, v]) => {
      const text =
        typeof v === "object" ? JSON.stringify(v) : String(v);
      return `${k}: ${text.length > 24 ? `${text.slice(0, 24)}…` : text}`;
    })
    .join(" · ");
}

// ---- 组件 -------------------------------------------------------------------

export function CustomEntitiesSection({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ row: GenericEntity | null } | null>(
    null,
  );
  const [form, setForm] = useState<Record<string, string>>({});

  const { data: types } = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
  });
  // 内置类型与宿主注册类型同等对待:两者都只是注册表里的一条 schema
  const availableTypes = useMemo(() => types ?? [], [types]);
  const activeKey = selectedKey ?? availableTypes[0]?.type_key ?? null;
  const activeType =
    availableTypes.find((t) => t.type_key === activeKey) ?? null;

  // 快照管理判定:active_snapshot_id 非空 → 只读(旧后端无此字段时视为可写,写入由后端 409 兜底)
  const { data: bases } = useQuery({
    queryKey: ["kb-bases"],
    queryFn: () => api.get<KnowledgeBase[]>("/kb/bases"),
  });
  const snapshotManaged = Boolean(
    bases?.find((b) => b.id === kbId)?.active_snapshot_id,
  );

  const { data: entities } = useQuery({
    queryKey: ["kb-generic-entities", kbId, activeKey],
    queryFn: () =>
      api.get<GenericEntity[]>(
        `/kb/bases/${kbId}/entities?entity_type_key=${encodeURIComponent(activeKey ?? "")}`,
      ),
    enabled: activeKey !== null,
  });

  const specs = useMemo(
    () => (activeType ? parseFieldSpecs(activeType.field_schema) : []),
    [activeType],
  );

  const save = useMutation({
    mutationFn: () => {
      if (!editing || !activeType) throw new Error("no editing state");
      const attributes: Record<string, unknown> = {};
      for (const spec of specs) {
        const v = toAttributeValue(spec, form[spec.key] ?? "");
        if (v !== undefined) attributes[spec.key] = v;
      }
      if (editing.row) {
        return api.patch<GenericEntity>(
          `/kb/bases/${kbId}/entities/${editing.row.id}`,
          { attributes },
        );
      }
      return api.post<GenericEntity>(`/kb/bases/${kbId}/entities`, {
        entity_type_key: activeType.type_key,
        attributes,
      });
    },
    onSuccess: () => {
      toast.success("已保存");
      setEditing(null);
      queryClient.invalidateQueries({
        queryKey: ["kb-generic-entities", kbId, activeKey],
      });
    },
    // 后端 422/409 中文消息如实展示;本地转换错误(数字/JSON)以自身消息兜底
    onError: (err) =>
      toast.error(errMsg(err, err instanceof Error ? err.message : undefined)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/kb/bases/${kbId}/entities/${id}`),
    onSuccess: () => {
      toast.success("已删除");
      queryClient.invalidateQueries({
        queryKey: ["kb-generic-entities", kbId, activeKey],
      });
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  function openEditor(row: GenericEntity | null) {
    const init: Record<string, string> = {};
    for (const spec of specs) {
      init[spec.key] = toFormValue(spec.control, row?.attributes[spec.key]);
    }
    setForm(init);
    setEditing({ row });
  }

  // 没有任何自定义类型时不渲染(类型在「类型注册表」中创建后自动出现)
  if (!availableTypes.length) return null;

  const typeItems: Record<string, string> = Object.fromEntries(
    availableTypes.map((t) => [t.type_key, t.display_name]),
  );
  const requiredMissing = specs.some(
    (s) => s.required && !(form[s.key] ?? "").trim(),
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2 text-sm">
          实体({entities?.length ?? 0})
        </CardTitle>
        <div className="flex items-center gap-2">
          <Select
            items={typeItems}
            value={activeKey}
            onValueChange={(v) => setSelectedKey(v as string)}
          >
            <SelectTrigger className="h-8">
              <SelectValue placeholder="选择类型…" />
            </SelectTrigger>
            <SelectContent>
              {availableTypes.map((t) => (
                <SelectItem key={t.type_key} value={t.type_key}>
                  {t.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            variant="outline"
            disabled={snapshotManaged || !activeType}
            onClick={() => openEditor(null)}
          >
            <Plus className="size-3.5" />
            新增
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {snapshotManaged && (
          <div className="flex items-start gap-2 rounded-md border border-border bg-muted/50 p-2 text-xs text-muted-foreground">
            <Info className="mt-0.5 size-3.5 shrink-0" />
            {SNAPSHOT_READONLY_HINT}
          </div>
        )}
        {!entities?.length && (
          <p className="text-xs text-muted-foreground">该类型下暂无实体</p>
        )}
        {entities?.map((e) => (
          <div
            key={e.id}
            className="flex items-center gap-2 rounded border border-border p-2 text-sm"
          >
            <div className="min-w-0 flex-1">
              <span className="block truncate font-medium">{e.name}</span>
              {attributeSummary(e.attributes) && (
                <span className="block truncate text-xs text-muted-foreground">
                  {attributeSummary(e.attributes)}
                </span>
              )}
            </div>
            {!snapshotManaged && (
              <>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7 shrink-0"
                  aria-label="编辑"
                  onClick={() => openEditor(e)}
                >
                  <Pencil className="size-3.5" />
                </Button>
                <ConfirmDialog
                  trigger={
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-7 shrink-0"
                      aria-label="删除"
                    >
                      <Trash2 className="size-3.5 text-destructive" />
                    </Button>
                  }
                  title={`删除「${e.name}」?`}
                  description="删除后不可恢复。"
                  destructive
                  confirmLabel="删除"
                  onConfirm={() => remove.mutateAsync(e.id)}
                />
              </>
            )}
          </div>
        ))}
      </CardContent>

      {/* schema 驱动的编辑弹窗(新建/修改共用) */}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {editing?.row ? "编辑" : "新增"}
              {activeType?.display_name}
            </DialogTitle>
          </DialogHeader>
          {editing && activeType && (
            <div className="space-y-3">
              {specs.map((spec) => (
                <FormField
                  key={spec.key}
                  label={spec.label}
                  htmlFor={`ce-${spec.key}`}
                  required={spec.required}
                  description={
                    spec.description ??
                    (spec.control === "lines"
                      ? "每行一项"
                      : spec.control === "json"
                        ? "JSON 格式"
                        : undefined)
                  }
                >
                  {spec.control === "select" ? (
                    <Select
                      items={Object.fromEntries(
                        (spec.enumValues ?? []).map((v) => [v, v]),
                      )}
                      value={form[spec.key] || null}
                      onValueChange={(v) =>
                        setForm({ ...form, [spec.key]: v as string })
                      }
                    >
                      <SelectTrigger className="h-9 w-full">
                        <SelectValue placeholder="选择…" />
                      </SelectTrigger>
                      <SelectContent>
                        {(spec.enumValues ?? []).map((v) => (
                          <SelectItem key={v} value={v}>
                            {v}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : spec.control === "lines" || spec.control === "json" ? (
                    <Textarea
                      id={`ce-${spec.key}`}
                      rows={spec.control === "json" ? 5 : 3}
                      className={
                        spec.control === "json" ? "font-mono text-xs" : undefined
                      }
                      value={form[spec.key] ?? ""}
                      onChange={(e) =>
                        setForm({ ...form, [spec.key]: e.target.value })
                      }
                    />
                  ) : (
                    <Input
                      id={`ce-${spec.key}`}
                      type={spec.control === "number" ? "number" : "text"}
                      value={form[spec.key] ?? ""}
                      onChange={(e) =>
                        setForm({ ...form, [spec.key]: e.target.value })
                      }
                    />
                  )}
                </FormField>
              ))}
              <Button
                className="w-full"
                disabled={save.isPending || requiredMissing}
                onClick={() => save.mutate()}
              >
                保存
              </Button>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
