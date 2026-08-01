"use client";

// 模型价格管理(stage-d 09):provider+model 唯一,四价(输入/输出/缓存命中/缓存创建),
// 单位 USD / 1M tokens;计费看板与请求日志的成本都按本表现算。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CircleDollarSign, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorState,
  PageHeader,
  Spinner,
} from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { useModelPrices, type ModelPriceOut } from "@/lib/billing";
import { errMsg } from "@/lib/utils";

interface PriceForm {
  provider: string;
  model: string;
  display_name: string;
  input_price: string;
  output_price: string;
  cache_read_price: string;
  cache_write_price: string;
}

const EMPTY_FORM: PriceForm = {
  provider: "",
  model: "",
  display_name: "",
  input_price: "",
  output_price: "",
  cache_read_price: "0",
  cache_write_price: "0",
};

function fmtPrice(v: string): string {
  const n = Number(v);
  return Number.isNaN(n) ? v : `$${n.toLocaleString("en-US", { maximumFractionDigits: 4 })}`;
}

export default function AdminPricesPage() {
  const queryClient = useQueryClient();
  const pricesQuery = useModelPrices();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ModelPriceOut | null>(null);
  const [form, setForm] = useState<PriceForm>(EMPTY_FORM);
  const [deleting, setDeleting] = useState<ModelPriceOut | null>(null);

  function openCreate() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setDialogOpen(true);
  }

  function openEdit(price: ModelPriceOut) {
    setEditing(price);
    setForm({
      provider: price.provider,
      model: price.model,
      display_name: price.display_name,
      input_price: price.input_price,
      output_price: price.output_price,
      cache_read_price: price.cache_read_price,
      cache_write_price: price.cache_write_price,
    });
    setDialogOpen(true);
  }

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["model-prices"] });
    queryClient.invalidateQueries({ queryKey: ["billing"] });
    queryClient.invalidateQueries({ queryKey: ["llm-traces"] });
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      const prices = {
        input_price: form.input_price.trim(),
        output_price: form.output_price.trim(),
        cache_read_price: form.cache_read_price.trim() || "0",
        cache_write_price: form.cache_write_price.trim() || "0",
      };
      if (editing) {
        return api.patch<ModelPriceOut>(`/admin/model-prices/${editing.id}`, {
          display_name: form.display_name.trim(),
          ...prices,
        });
      }
      return api.post<ModelPriceOut>("/admin/model-prices", {
        provider: form.provider.trim(),
        model: form.model.trim(),
        display_name: form.display_name.trim(),
        ...prices,
      });
    },
    onSuccess: () => {
      toast.success(editing ? "价格已更新,看板成本即时生效" : "价格已登记");
      setDialogOpen(false);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "保存失败")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/model-prices/${id}`),
    onSuccess: () => {
      toast.success("价格条目已删除,该模型用量转为未定价");
      setDeleting(null);
      invalidate();
    },
    onError: (err) => toast.error(errMsg(err, "删除失败")),
  });

  const rightAligned = (label: string) =>
    function RightAlignedHeader() {
      return <span className="block w-full text-right">{label}</span>;
    };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const columns: ColumnDef<ModelPriceOut, any>[] = [
    {
      accessorKey: "model",
      header: "模型",
      cell: ({ row }) => (
        <div>
          <div className="font-mono text-xs">{row.original.model}</div>
          <div className="text-xs text-muted-foreground">{row.original.provider}</div>
        </div>
      ),
    },
    { accessorKey: "display_name", header: "显示名称" },
    {
      accessorKey: "input_price",
      header: rightAligned("输入成本"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">{fmtPrice(row.original.input_price)}</span>
      ),
    },
    {
      accessorKey: "output_price",
      header: rightAligned("输出成本"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">{fmtPrice(row.original.output_price)}</span>
      ),
    },
    {
      accessorKey: "cache_read_price",
      header: rightAligned("缓存命中"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">
          {fmtPrice(row.original.cache_read_price)}
        </span>
      ),
    },
    {
      accessorKey: "cache_write_price",
      header: rightAligned("缓存创建"),
      cell: ({ row }) => (
        <span className="block text-right tabular-nums">
          {fmtPrice(row.original.cache_write_price)}
        </span>
      ),
    },
    {
      id: "actions",
      header: rightAligned("操作"),
      cell: ({ row }) => (
        <span className="flex items-center justify-end gap-0.5">
          <Button
            size="icon-xs"
            variant="ghost"
            aria-label="编辑价格"
            onClick={() => openEdit(row.original)}
          >
            <Pencil />
          </Button>
          <Button
            size="icon-xs"
            variant="ghost"
            aria-label="删除价格"
            onClick={() => setDeleting(row.original)}
          >
            <Trash2 className="text-destructive" />
          </Button>
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        title="模型价格"
        description="按 provider + model 设置单价(USD / 1M tokens);计费看板与请求日志成本按本表现算,改价即时生效(不回溯历史账单口径)"
        actions={
          <Button size="sm" onClick={openCreate}>
            <Plus />
            新增价格
          </Button>
        }
      />

      {pricesQuery.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : pricesQuery.error ? (
        <ErrorState error={pricesQuery.error} onRetry={() => pricesQuery.refetch()} />
      ) : !pricesQuery.data?.length ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={CircleDollarSign}
              title="还没有登记任何模型价格"
              description="登记价格后,用量看板与请求日志会按 tokens 用量自动计算成本;未定价模型会如实标注。"
            />
          </CardContent>
        </Card>
      ) : (
        <DataTable
          columns={columns}
          data={pricesQuery.data}
          getRowId={(r) => r.id}
          empty={{ title: "暂无价格" }}
        />
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? `编辑价格:${editing.model}` : "新增模型价格"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              {!editing && (
                <>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">Provider</label>
                    <Input
                      value={form.provider}
                      placeholder="如:anthropic / openai"
                      onChange={(e) => setForm((f) => ({ ...f, provider: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">模型 ID</label>
                    <Input
                      value={form.model}
                      placeholder="如:claude-fable-5"
                      onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
                    />
                  </div>
                </>
              )}
              <div className="col-span-2 space-y-1.5">
                <label className="text-sm font-medium">显示名称</label>
                <Input
                  value={form.display_name}
                  placeholder="如:Claude Fable 5"
                  onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
                />
              </div>
              {(
                [
                  ["input_price", "输入成本($/1M)"],
                  ["output_price", "输出成本($/1M)"],
                  ["cache_read_price", "缓存命中($/1M)"],
                  ["cache_write_price", "缓存创建($/1M)"],
                ] as const
              ).map(([key, label]) => (
                <div key={key} className="space-y-1.5">
                  <label className="text-sm font-medium">{label}</label>
                  <Input
                    type="number"
                    min="0"
                    step="0.0001"
                    value={form[key]}
                    onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
                  />
                </div>
              ))}
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setDialogOpen(false)}>
                取消
              </Button>
              <Button
                disabled={
                  saveMutation.isPending ||
                  !form.display_name.trim() ||
                  form.input_price.trim() === "" ||
                  form.output_price.trim() === "" ||
                  (!editing && (!form.provider.trim() || !form.model.trim()))
                }
                onClick={() => saveMutation.mutate()}
              >
                {saveMutation.isPending && <Spinner size={3.5} />}
                {editing ? "保存" : "登记价格"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`删除价格:${deleting?.display_name ?? ""}`}
        description="删除后该模型的用量将显示为未定价(不影响已记录的 tokens 用量)。"
        confirmLabel="删除"
        destructive
        onConfirm={() => (deleting ? deleteMutation.mutateAsync(deleting.id) : undefined)}
      />
    </div>
  );
}
