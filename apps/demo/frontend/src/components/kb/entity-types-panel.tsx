"use client";

// M3a 类型注册表:实体类型列表 + 自建类型的新建/编辑/删除。
// 内置 5 类只读可查看详情;is_own=true 才可改删(权限由后端裁决,403/409 消息如实展示)。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Eye, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog, DataTable, FormField, ToneBadge } from "@/components/shared";
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
import type { EntityType, FilterableField } from "@/lib/types";

export const REVIEW_POLICY_LABELS: Record<EntityType["review_policy"], string> = {
  auto: "自动确认",
  ai: "AI 审核",
  human: "人工必审",
};

const FILTER_TYPE_LABELS: Record<FilterableField["type"], string> = {
  text: "文本",
  number: "数字",
  date: "日期",
};

const TYPE_KEY_RE = /^[a-z][a-z0-9_]*$/;

/** 新建类型时的 field_schema 起始模板(与后端约束一致:必含必填字符串 name) */
const DEFAULT_SCHEMA_TEXT = JSON.stringify(
  {
    type: "object",
    properties: {
      name: { type: "string", title: "名称" },
    },
    required: ["name"],
    additionalProperties: false,
  },
  null,
  2,
);

interface TypeForm {
  type_key: string;
  display_name: string;
  description: string;
  review_policy: EntityType["review_policy"];
  schemaText: string;
  filterable: { field: string; type: FilterableField["type"]; label: string }[];
  card_template: string;
}

type EditorState =
  | { mode: "create" }
  | { mode: "edit"; type: EntityType }
  | { mode: "view"; type: EntityType };

function formFromType(t: EntityType): TypeForm {
  return {
    type_key: t.type_key,
    display_name: t.display_name,
    description: t.description ?? "",
    review_policy: t.review_policy,
    schemaText: JSON.stringify(t.field_schema, null, 2),
    filterable: t.filterable_fields.map((f) => ({
      field: f.field,
      type: f.type,
      label: f.label ?? "",
    })),
    card_template: t.card_template ?? "",
  };
}

const EMPTY_FORM: TypeForm = {
  type_key: "",
  display_name: "",
  description: "",
  review_policy: "auto",
  schemaText: DEFAULT_SCHEMA_TEXT,
  filterable: [],
  card_template: "",
};

/** 内置/只读类型的详情视图 */
function TypeDetail({ type }: { type: EntityType }) {
  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-[6rem_1fr] gap-y-1.5">
        <span className="text-muted-foreground">类型标识</span>
        <span className="font-mono text-xs">{type.type_key}</span>
        <span className="text-muted-foreground">显示名</span>
        <span>{type.display_name}</span>
        <span className="text-muted-foreground">审核档位</span>
        <span>{REVIEW_POLICY_LABELS[type.review_policy]}</span>
        {type.description && (
          <>
            <span className="text-muted-foreground">描述</span>
            <span>{type.description}</span>
          </>
        )}
      </div>
      {type.filterable_fields.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">可过滤字段</p>
          <div className="flex flex-wrap gap-1.5">
            {type.filterable_fields.map((f) => (
              <span
                key={f.field}
                className="rounded border border-border px-1.5 py-0.5 font-mono text-xs"
              >
                {f.field}
                <span className="ml-1 text-muted-foreground">
                  {FILTER_TYPE_LABELS[f.type]}
                  {f.label ? ` · ${f.label}` : ""}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
      {type.card_template && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">卡片模板</p>
          <pre className="rounded bg-muted p-2 text-xs whitespace-pre-wrap">
            {type.card_template}
          </pre>
        </div>
      )}
      <div className="space-y-1">
        <p className="text-xs font-medium text-muted-foreground">字段 Schema</p>
        <pre className="max-h-64 overflow-auto rounded bg-muted p-2 text-xs">
          {JSON.stringify(type.field_schema, null, 2)}
        </pre>
      </div>
    </div>
  );
}

export function EntityTypesPanel() {
  const queryClient = useQueryClient();
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [form, setForm] = useState<TypeForm>(EMPTY_FORM);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  const { data: types, isLoading, error, refetch } = useQuery({
    queryKey: ["kb-entity-types"],
    queryFn: () => api.get<EntityType[]>("/kb/entity-types"),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["kb-entity-types"] });

  const save = useMutation({
    mutationFn: () => {
      if (!editor || editor.mode === "view") throw new Error("no editing state");
      let schema: unknown;
      try {
        schema = JSON.parse(form.schemaText);
      } catch (e) {
        setSchemaError(`JSON 解析失败:${e instanceof Error ? e.message : String(e)}`);
        throw new Error("schema 不是合法 JSON");
      }
      setSchemaError(null);
      const body = {
        display_name: form.display_name.trim(),
        description: form.description.trim() || null,
        field_schema: schema,
        filterable_fields: form.filterable
          .filter((f) => f.field.trim())
          .map((f) => ({
            field: f.field.trim(),
            type: f.type,
            label: f.label.trim() || null,
          })),
        card_template: form.card_template.trim() || null,
        review_policy: form.review_policy,
      };
      if (editor.mode === "edit") {
        return api.patch<EntityType>(`/kb/entity-types/${editor.type.id}`, body);
      }
      return api.post<EntityType>("/kb/entity-types", {
        type_key: form.type_key.trim(),
        ...body,
      });
    },
    onSuccess: () => {
      toast.success("类型已保存");
      setEditor(null);
      invalidate();
    },
    // 后端 422 返回中文校验消息(如 schema 不合规/type_key 重复 409),如实展示
    onError: (err) => {
      if (err instanceof Error && err.message === "schema 不是合法 JSON") return;
      toast.error(errMsg(err));
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/kb/entity-types/${id}`),
    onSuccess: () => {
      toast.success("类型已删除");
      invalidate();
    },
    // 类型下已有实体时后端 409 拒绝
    onError: (err) => toast.error(errMsg(err)),
  });

  function openCreate() {
    setForm(EMPTY_FORM);
    setSchemaError(null);
    setEditor({ mode: "create" });
  }

  function openType(t: EntityType) {
    if (t.is_own && !t.is_builtin) {
      setForm(formFromType(t));
      setSchemaError(null);
      setEditor({ mode: "edit", type: t });
    } else {
      setEditor({ mode: "view", type: t });
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns = useMemo<ColumnDef<EntityType, any>[]>(
    () => [
      {
        id: "type_key",
        header: "类型标识",
        accessorFn: (t) => t.type_key,
        cell: ({ row }) => (
          <span className="font-mono text-xs">{row.original.type_key}</span>
        ),
      },
      {
        id: "display_name",
        header: "显示名",
        accessorFn: (t) => t.display_name,
      },
      {
        id: "origin",
        header: "来源",
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_builtin ? (
            <ToneBadge tone="muted">内置</ToneBadge>
          ) : row.original.is_own ? (
            <ToneBadge tone="primary">自建</ToneBadge>
          ) : (
            <ToneBadge tone="muted">只读</ToneBadge>
          ),
      },
      {
        id: "review_policy",
        header: "审核档位",
        accessorFn: (t) => REVIEW_POLICY_LABELS[t.review_policy],
      },
      {
        id: "filterable",
        header: "可过滤字段",
        enableSorting: false,
        accessorFn: (t) => t.filterable_fields.length,
        cell: ({ row }) => `${row.original.filterable_fields.length} 个`,
      },
      {
        id: "__actions__",
        enableSorting: false,
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => {
          const t = row.original;
          const editable = t.is_own && !t.is_builtin;
          return (
            <div className="flex justify-end gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                aria-label={editable ? "编辑" : "查看"}
                onClick={() => openType(t)}
              >
                {editable ? (
                  <Pencil className="size-3.5" />
                ) : (
                  <Eye className="size-3.5" />
                )}
              </Button>
              {editable && (
                <ConfirmDialog
                  trigger={
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label="删除"
                    >
                      <Trash2 className="size-3.5 text-destructive" />
                    </Button>
                  }
                  title={`删除类型「${t.display_name}」?`}
                  description="删除后不可恢复;该类型下已有实体数据时删除会被拒绝(需先清空实体)。"
                  destructive
                  confirmLabel="删除"
                  onConfirm={() => remove.mutateAsync(t.id)}
                />
              )}
            </div>
          );
        },
      },
    ],
    [remove],
  );

  const isView = editor?.mode === "view";
  const canSubmit =
    form.display_name.trim() &&
    (editor?.mode !== "create" || TYPE_KEY_RE.test(form.type_key.trim()));

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-sm">
          实体类型配置({types?.length ?? 0})
        </CardTitle>
        <Button size="sm" variant="outline" onClick={openCreate}>
          <Plus className="size-3.5" />
          新建类型
        </Button>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={types}
          isLoading={isLoading}
          error={error}
          onRetry={() => refetch()}
          getRowId={(t) => t.id}
          empty={{ title: "暂无实体类型" }}
        />
      </CardContent>

      <Dialog
        open={editor !== null}
        onOpenChange={(open) => !open && setEditor(null)}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {editor?.mode === "create"
                ? "新建实体类型"
                : editor?.mode === "edit"
                  ? `编辑类型「${editor.type.display_name}」`
                  : editor
                    ? `类型详情「${editor.type.display_name}」`
                    : ""}
            </DialogTitle>
          </DialogHeader>

          {isView && editor && <TypeDetail type={editor.type} />}

          {editor && !isView && (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField
                  label="类型标识(type_key)"
                  htmlFor="et-type-key"
                  required
                  description="小写字母开头,仅含 a-z 0-9 _;创建后不可改"
                  error={
                    editor.mode === "create" &&
                    form.type_key.trim() &&
                    !TYPE_KEY_RE.test(form.type_key.trim())
                      ? "需匹配 ^[a-z][a-z0-9_]*$"
                      : undefined
                  }
                >
                  <Input
                    id="et-type-key"
                    value={form.type_key}
                    disabled={editor.mode === "edit"}
                    placeholder="如 visa_policy"
                    onChange={(e) => setForm({ ...form, type_key: e.target.value })}
                  />
                </FormField>
                <FormField label="显示名" htmlFor="et-display-name" required>
                  <Input
                    id="et-display-name"
                    value={form.display_name}
                    onChange={(e) =>
                      setForm({ ...form, display_name: e.target.value })
                    }
                  />
                </FormField>
              </div>

              <FormField label="描述" htmlFor="et-description">
                <Textarea
                  id="et-description"
                  rows={2}
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </FormField>

              <FormField
                label="审核档位"
                description="抽取事实进入该类型时的审核策略"
              >
                <Select
                  items={REVIEW_POLICY_LABELS}
                  value={form.review_policy}
                  onValueChange={(v) =>
                    setForm({
                      ...form,
                      review_policy: v as EntityType["review_policy"],
                    })
                  }
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(REVIEW_POLICY_LABELS).map(([k, label]) => (
                      <SelectItem key={k} value={k}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>

              <FormField
                label="字段 Schema(JSON Schema)"
                htmlFor="et-schema"
                required
                description="type=object;properties 必含必填字符串属性 name;保存时后端做完整校验"
                error={schemaError ?? undefined}
              >
                <Textarea
                  id="et-schema"
                  rows={10}
                  className="font-mono text-xs"
                  value={form.schemaText}
                  onChange={(e) => {
                    setForm({ ...form, schemaText: e.target.value });
                    setSchemaError(null);
                  }}
                />
              </FormField>

              <FormField
                label="可过滤字段"
                description="声明后检索可按这些字段过滤;标签留空则显示字段名"
              >
                <div className="space-y-2">
                  {form.filterable.map((f, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <Input
                        className="flex-1 font-mono text-xs"
                        placeholder="字段名"
                        value={f.field}
                        onChange={(e) => {
                          const next = [...form.filterable];
                          next[i] = { ...f, field: e.target.value };
                          setForm({ ...form, filterable: next });
                        }}
                      />
                      <Select
                        items={FILTER_TYPE_LABELS}
                        value={f.type}
                        onValueChange={(v) => {
                          const next = [...form.filterable];
                          next[i] = { ...f, type: v as FilterableField["type"] };
                          setForm({ ...form, filterable: next });
                        }}
                      >
                        <SelectTrigger className="h-9 w-24 shrink-0">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {Object.entries(FILTER_TYPE_LABELS).map(([k, label]) => (
                            <SelectItem key={k} value={k}>
                              {label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <Input
                        className="flex-1"
                        placeholder="标签(可选)"
                        value={f.label}
                        onChange={(e) => {
                          const next = [...form.filterable];
                          next[i] = { ...f, label: e.target.value };
                          setForm({ ...form, filterable: next });
                        }}
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 shrink-0"
                        aria-label="移除该字段"
                        onClick={() =>
                          setForm({
                            ...form,
                            filterable: form.filterable.filter((_, j) => j !== i),
                          })
                        }
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  ))}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setForm({
                        ...form,
                        filterable: [
                          ...form.filterable,
                          { field: "", type: "text", label: "" },
                        ],
                      })
                    }
                  >
                    <Plus className="size-3.5" />
                    添加字段
                  </Button>
                </div>
              </FormField>

              <FormField
                label="卡片模板"
                htmlFor="et-card-template"
                description="检索命中卡片的正文模板,支持 {字段名} 占位,如:{city} · {price} 元;留空用默认键值对展示"
              >
                <Textarea
                  id="et-card-template"
                  rows={2}
                  value={form.card_template}
                  onChange={(e) =>
                    setForm({ ...form, card_template: e.target.value })
                  }
                />
              </FormField>

              <Button
                className="w-full"
                disabled={save.isPending || !canSubmit}
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
