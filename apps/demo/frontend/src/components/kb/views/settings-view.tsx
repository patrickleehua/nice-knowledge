"use client";

// settings 视图:三块内容职责完全不同——摄入切片配置是「写路径参数」,库级分享是
// 「协作授权」,实体类型注册表是「组织级知识建模 schema」(严格说都不算库设置,只是
// 借设置页安家)。原先单列纵向堆叠,注册表这张 9+ 行的数据表被挤在页面最底部的窄列,
// 横向空间也全部浪费,所以按职责拆成页内三 Tab:
// - 摄入配置:表单按 解析 / 切片 / 表格 / 增强项 四组分区,xl 起双列利用宽屏;
// - 分享协作:独立协作面,与摄入参数互不相干,混排只会互相稀释;
// - 实体类型:数据表需要整行宽度,独占一个 Tab 而不是挤在卡片列尾。
// Tab 进 URL(?tab=),与 entities 视图同一套约定,刷新/分享不丢;切视图时由
// 工作台的 VIEW_SCOPED_PARAMS 统一清理。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Boxes,
  RotateCcw,
  Save,
  Share2,
  SlidersHorizontal,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Controller,
  useForm,
  useWatch,
  type FieldErrors,
  type Resolver,
} from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { EntityTypesPanel } from "@/components/kb/entity-types-panel";
import {
  CaptionModelDialog,
  readinessLabel,
} from "@/components/kb/settings/caption-model-dialog";
import { KbDangerZone } from "@/components/kb/settings/kb-danger-zone";
import { ConfirmDialog, FormField, ToneBadge } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { useCurrentOrg } from "@/lib/auth";
import { errMsg } from "@/lib/utils";
import { useUnsavedGuard } from "@/lib/unsaved-guard";
import { useUrlState } from "@/lib/use-url-state";
import type {
  IngestProfilePresets,
  KbImageEnrichmentReadiness,
  KbShare,
  KnowledgeBase,
  ProviderModelCatalogItem,
} from "@/lib/types";

// ---- IngestProfile 表单契约(与后端 domain.kb.IngestProfile 校验一致) ----------

const profileSchema = z
  .object({
    parser: z.enum(["fast", "docling"]),
    chunk_strategy: z.enum(["structure", "fixed"]),
    chunk_max_chars: z
      .number({ message: "请输入 200-8000 的整数" })
      .int("需为整数")
      .min(200, "最小 200")
      .max(8000, "最大 8000"),
    chunk_overlap_chars: z
      .number({ message: "请输入 0-500 的整数" })
      .int("需为整数")
      .min(0, "最小 0")
      .max(500, "最大 500"),
    table_mode: z.enum(["row", "whole"]),
    parent_child: z.boolean(),
    caption_images: z.boolean(),
    caption_provider: z.string().trim().min(1).nullable().optional(),
    caption_model: z.string().trim().min(1).nullable().optional(),
    auto_wiki: z.boolean(),
  })
  .refine((v) => v.chunk_overlap_chars < v.chunk_max_chars, {
    message: "重叠字符数必须小于切片最大字符数",
    path: ["chunk_overlap_chars"],
  })
  .refine((v) => Boolean(v.caption_provider) === Boolean(v.caption_model), {
    message: "图片描述 Provider 与模型必须同时选择或同时留空",
    path: ["caption_model"],
  });

type ProfileForm = z.infer<typeof profileSchema>;

// 手写 resolver 桥接 zod:@hookform/resolvers 的类型桥钉在 zod 4.0,
// 与项目 zod 4.4 不兼容;校验语义与 zodResolver 一致
const profileResolver: Resolver<ProfileForm> = async (values) => {
  const result = profileSchema.safeParse(values);
  if (result.success) return { values: result.data, errors: {} };
  const errors: FieldErrors<ProfileForm> = {};
  for (const issue of result.error.issues) {
    const key = (issue.path[0] ?? "root") as keyof ProfileForm;
    if (!errors[key]) {
      errors[key] = { type: String(issue.code), message: issue.message };
    }
  }
  return { values: {}, errors };
};

const DEFAULT_PROFILE: ProfileForm = {
  parser: "docling",
  chunk_strategy: "structure",
  chunk_max_chars: 1200,
  chunk_overlap_chars: 150,
  table_mode: "row",
  parent_child: false,
  caption_images: true,
  caption_provider: null,
  caption_model: null,
  auto_wiki: true,
};

const PARSER_ITEMS: Record<string, string> = {
  fast: "fast(内置轻量解析)",
  docling: "docling(高质量结构解析)",
};
const STRATEGY_ITEMS: Record<string, string> = {
  structure: "结构感知(按标题/段落边界)",
  fixed: "固定长度",
};
const TABLE_MODE_ITEMS: Record<string, string> = {
  row: "行组切片(每片携带表头)",
  whole: "整表不切",
};

/**
 * 表单分组:小标题 + 说明 + 分组内容。
 * 摄入配置有十来个字段,平铺时「解析怎么选」和「要不要生成 Wiki」混在一列没有层次,
 * 按职责切成 解析/切片/表格/增强项 四块后每块可独立扫读。
 */
function FieldGroup({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <div className="space-y-0.5 border-b border-border pb-2">
        <h3 className="text-sm font-medium">{title}</h3>
        {description && (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </div>
      {children}
    </section>
  );
}

/** 当前生效配置的结构化摘要,替代原来四段用「；」拼接的长 description */
function CaptionSummary({
  readiness,
  isError,
}: {
  readiness?: KbImageEnrichmentReadiness;
  isError: boolean;
}) {
  if (isError) {
    return (
      <p className="text-xs text-warning">
        无法读取服务配置状态；为避免创建不可执行配置，暂不允许开启。
      </p>
    );
  }
  if (!readiness) {
    return (
      <p className="text-xs text-muted-foreground">
        正在检查 OCR 与图片描述服务配置…
      </p>
    );
  }
  return (
    <dl className="grid gap-1 text-xs sm:grid-cols-[4.5rem_minmax(0,1fr)]">
      <dt className="text-muted-foreground">当前生效</dt>
      <dd className="flex flex-wrap items-center gap-1.5">
        <ToneBadge
          tone={
            readiness.selection_source === "kb_override" ? "primary" : "muted"
          }
        >
          {readiness.selection_source === "kb_override"
            ? "本库覆盖"
            : "平台默认"}
        </ToneBadge>
        <span className="font-mono">
          {readiness.caption_provider ?? "未配置"} /{" "}
          {readiness.caption_model ?? "未配置"}
        </span>
        <ToneBadge tone={readiness.ready ? "success" : "warning"}>
          {readiness.ready ? "可用" : "不可用"}
        </ToneBadge>
      </dd>
      <dt className="text-muted-foreground">OCR</dt>
      <dd className="font-mono">
        {readiness.ocr_provider} / {readiness.ocr_model}
      </dd>
      {!readiness.ready && (
        <>
          <dt className="text-muted-foreground">原因</dt>
          <dd className="text-warning">{readinessLabel(readiness.code)}</dd>
        </>
      )}
      <dt className="text-muted-foreground">接受策略</dt>
      <dd className="text-muted-foreground">人工审核后发布</dd>
    </dl>
  );
}

// ---- Tab 1:摄入切片配置 ------------------------------------------------------
// react-hook-form + zod,预设填充可再改,保存 PATCH bases/{id},恢复默认发送
// ingest_profile: null。图片视觉描述由后端同一执行配置的 readiness 控制,
// 不在浏览器推断凭证状态。
// 保存/恢复默认收在 sticky 底部操作条而不是每组独立保存:profile 是一个整体 PATCH
// 负载,且存在跨组校验(重叠<最大字符、provider/model 成对),拆开保存会破坏契约。

function IngestProfileTab({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const [presetKey, setPresetKey] = useState<string | null>(null);
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const [chosenCatalogModel, setChosenCatalogModel] =
    useState<ProviderModelCatalogItem | null>(null);

  const { data: bases } = useQuery({
    queryKey: ["kb-bases"],
    queryFn: () => api.get<KnowledgeBase[]>("/kb/bases"),
  });
  const kb = bases?.find((b) => b.id === kbId);

  const { data: presetsResp } = useQuery({
    queryKey: ["kb-ingest-presets"],
    queryFn: () => api.get<IngestProfilePresets>("/kb/ingest/profiles/presets"),
    staleTime: 5 * 60_000,
  });
  const presets = presetsResp?.presets ?? {};
  const enrichmentReadiness = useQuery({
    queryKey: ["kb-image-enrichment-readiness", kbId],
    queryFn: () =>
      api.get<KbImageEnrichmentReadiness>(
        `/kb/image-enrichment/readiness?kb_id=${encodeURIComponent(kbId)}`,
      ),
    staleTime: 30_000,
  });
  const platformReadiness = useQuery({
    queryKey: ["kb-image-enrichment-readiness", "platform-default"],
    queryFn: () =>
      api.get<KbImageEnrichmentReadiness>("/kb/image-enrichment/readiness"),
    staleTime: 30_000,
  });
  const presetItems: Record<string, string> = Object.fromEntries(
    Object.entries(presets).map(([k, v]) => [k, v.label]),
  );

  const {
    register,
    handleSubmit,
    control,
    reset,
    setValue,
    formState: { errors, isDirty },
  } = useForm<ProfileForm>({
    resolver: profileResolver,
    defaultValues: DEFAULT_PROFILE,
  });
  const selectedCaptionProvider =
    useWatch({ control, name: "caption_provider" }) ?? null;
  const selectedCaptionModel =
    useWatch({ control, name: "caption_model" }) ?? null;

  // kb 首次到位时回填当前配置;之后 refetch 不覆盖用户编辑
  const initializedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!kb || initializedFor.current === kb.id) return;
    initializedFor.current = kb.id;
    reset({
      ...(presetsResp?.default ?? DEFAULT_PROFILE),
      ...kb.ingest_profile,
    });
  }, [kb, presetsResp?.default, reset]);

  const save = useMutation({
    mutationFn: (profile: ProfileForm | null) =>
      api.patch<KnowledgeBase>(`/kb/bases/${kbId}`, {
        ingest_profile: profile,
      }),
    onSuccess: (updated, sent) => {
      toast.success(
        sent === null ? "已恢复默认配置" : "摄入配置已保存,下次摄入生效",
      );
      reset({ ...DEFAULT_PROFILE, ...updated.ingest_profile });
      setPresetKey(null);
      setChosenCatalogModel(null);
      queryClient.invalidateQueries({ queryKey: ["kb-bases"] });
      queryClient.invalidateQueries({
        queryKey: ["kb-image-enrichment-readiness", kbId],
      });
    },
    // 400 时后端返回逐字段校验信息,如实展示
    onError: (err) => toast.error(errMsg(err)),
  });

  // 有未保存改动时,切视图与关标签页都会被拦下
  useUnsavedGuard(isDirty && !save.isPending, "摄入切片配置");

  const applyPreset = (key: string) => {
    setPresetKey(key);
    const preset = presets[key];
    if (preset) {
      reset({ ...DEFAULT_PROFILE, ...preset.profile });
      setChosenCatalogModel(null);
    }
  };

  return (
    // 宽度收在 max-w-6xl:双列表单每列约 34rem,再宽单个控件会被拉成一整行长条
    <form
      className="max-w-6xl space-y-4"
      onSubmit={handleSubmit((values) => save.mutate(values))}
    >
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2 text-sm">
            <SlidersHorizontal className="size-4" />
            摄入切片配置
            {kb &&
              (kb.ingest_profile ? (
                <ToneBadge tone="primary">已自定义</ToneBadge>
              ) : (
                <ToneBadge tone="muted">默认配置</ToneBadge>
              ))}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* 预设是「整表快速填充」,作用于全部分组,所以放在分组网格之上,不属于任何一组 */}
          <FormField
            label="预设模板"
            description="选择预设快速填充表单,填充后仍可逐项调整"
            className="max-w-sm"
          >
            <Select
              items={presetItems}
              value={presetKey}
              onValueChange={(v) => applyPreset(v as string)}
            >
              <SelectTrigger className="h-9 w-full">
                <SelectValue placeholder="选择预设…" />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(presetItems).map(([k, label]) => (
                  <SelectItem key={k} value={k}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>

          {/* xl 起双列:左列是「怎么切」的三小组(解析/切片/表格,字段短平),
              右列是「切完之后做什么」(增强项,含图片描述的高块摘要),两列高度天然接近 */}
          <div className="grid gap-6 xl:grid-cols-2 xl:gap-x-10">
            <div className="space-y-6">
              <FieldGroup title="解析" description="文档转结构化文本的方式">
                <FormField
                  label="解析后端"
                  error={errors.parser?.message}
                  description="Docling 异常时后端会记录降级结果"
                >
                  <Controller
                    control={control}
                    name="parser"
                    render={({ field }) => (
                      <Select
                        items={PARSER_ITEMS}
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="h-9 w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {Object.entries(PARSER_ITEMS).map(([k, label]) => (
                            <SelectItem key={k} value={k}>
                              {label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                </FormField>
              </FieldGroup>

              <FieldGroup
                title="切片"
                description="决定检索片段的粒度与上下文重叠"
              >
                <div className="grid gap-4 sm:grid-cols-2">
                  <FormField
                    label="切片策略"
                    error={errors.chunk_strategy?.message}
                    className="sm:col-span-2"
                  >
                    <Controller
                      control={control}
                      name="chunk_strategy"
                      render={({ field }) => (
                        <Select
                          items={STRATEGY_ITEMS}
                          value={field.value}
                          onValueChange={field.onChange}
                        >
                          <SelectTrigger className="h-9 w-full">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {Object.entries(STRATEGY_ITEMS).map(
                              ([k, label]) => (
                                <SelectItem key={k} value={k}>
                                  {label}
                                </SelectItem>
                              ),
                            )}
                          </SelectContent>
                        </Select>
                      )}
                    />
                  </FormField>

                  <FormField
                    label="切片最大字符数"
                    error={errors.chunk_max_chars?.message}
                    description="200-8000"
                  >
                    <Input
                      type="number"
                      aria-invalid={!!errors.chunk_max_chars}
                      {...register("chunk_max_chars", { valueAsNumber: true })}
                    />
                  </FormField>

                  <FormField
                    label="切片重叠字符数"
                    error={errors.chunk_overlap_chars?.message}
                    description="0-500,须小于最大字符数"
                  >
                    <Input
                      type="number"
                      aria-invalid={!!errors.chunk_overlap_chars}
                      {...register("chunk_overlap_chars", {
                        valueAsNumber: true,
                      })}
                    />
                  </FormField>
                </div>
              </FieldGroup>

              <FieldGroup title="表格" description="表格内容进入片段的方式">
                <FormField
                  label="表格切片模式"
                  error={errors.table_mode?.message}
                >
                  <Controller
                    control={control}
                    name="table_mode"
                    render={({ field }) => (
                      <Select
                        items={TABLE_MODE_ITEMS}
                        value={field.value}
                        onValueChange={field.onChange}
                      >
                        <SelectTrigger className="h-9 w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {Object.entries(TABLE_MODE_ITEMS).map(
                            ([k, label]) => (
                              <SelectItem key={k} value={k}>
                                {label}
                              </SelectItem>
                            ),
                          )}
                        </SelectContent>
                      </Select>
                    )}
                  />
                </FormField>
              </FieldGroup>
            </div>

            <FieldGroup
              title="增强项"
              description="摄入完成后的自动加工,可按需关闭"
            >
              <div className="space-y-4">
                <FormField
                  label="自动生成 Wiki 草稿"
                  description={
                    kb?.active_snapshot_id
                      ? "活动快照模式暂不支持自动 Wiki 生成"
                      : "文档摄入完成后生成或更新待发布草稿"
                  }
                >
                  <Controller
                    control={control}
                    name="auto_wiki"
                    render={({ field }) => (
                      <div className="flex h-9 items-center gap-2">
                        <Switch
                          checked={field.value}
                          onCheckedChange={field.onChange}
                          disabled={Boolean(kb?.active_snapshot_id)}
                          aria-label="自动生成 Wiki 草稿"
                        />
                        <span className="text-sm text-muted-foreground">
                          {kb?.active_snapshot_id
                            ? "快照模式不可用"
                            : field.value
                              ? "已开启"
                              : "已关闭"}
                        </span>
                      </div>
                    )}
                  />
                </FormField>

                <FormField
                  label="图片视觉描述"
                  error={errors.caption_model?.message}
                  description="关闭时仍会提取图片与 OCR，只停止调用视觉模型生成描述"
                >
                  <Controller
                    control={control}
                    name="caption_images"
                    render={({ field }) => {
                      const ready = enrichmentReadiness.data?.ready === true;
                      // 已在本次会话里选中一个合格模型时,即便服务端 readiness 还没刷新也放行开关
                      const pickedEligibleModel =
                        chosenCatalogModel !== null &&
                        chosenCatalogModel.provider ===
                          selectedCaptionProvider &&
                        chosenCatalogModel.model === selectedCaptionModel;
                      const usable = ready || pickedEligibleModel;
                      return (
                        <div className="space-y-3">
                          <div className="flex min-h-9 items-center gap-2">
                            <Switch
                              checked={field.value}
                              onCheckedChange={field.onChange}
                              disabled={!usable && !field.value}
                              aria-label="图片视觉描述"
                              aria-describedby="caption-readiness-status"
                            />
                            <span
                              id="caption-readiness-status"
                              className="text-sm text-muted-foreground"
                            >
                              {field.value
                                ? usable
                                  ? "已开启"
                                  : "已配置开启，但当前模型不可用；可关闭或改选模型"
                                : usable
                                  ? "已关闭（仍保留图片与 OCR）"
                                  : "当前生效模型未就绪，选择可用覆盖模型后可开启"}
                            </span>
                          </div>

                          <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                            <CaptionSummary
                              readiness={enrichmentReadiness.data}
                              isError={enrichmentReadiness.isError}
                            />
                            {selectedCaptionProvider &&
                              selectedCaptionModel && (
                                <p className="flex flex-wrap items-center gap-1.5 text-xs">
                                  <ToneBadge tone="primary">
                                    保存后生效
                                  </ToneBadge>
                                  <span className="font-mono">
                                    {selectedCaptionProvider} /{" "}
                                    {selectedCaptionModel}
                                  </span>
                                </p>
                              )}
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              onClick={() => setModelDialogOpen(true)}
                            >
                              <Boxes className="size-4" />
                              选择执行模型…
                            </Button>
                          </div>

                          <CaptionModelDialog
                            open={modelDialogOpen}
                            onOpenChange={setModelDialogOpen}
                            selectedProvider={selectedCaptionProvider}
                            selectedModel={selectedCaptionModel}
                            platformReadiness={platformReadiness.data}
                            onChoose={(choice) => {
                              setValue("caption_provider", choice.provider, {
                                shouldDirty: true,
                                shouldValidate: true,
                              });
                              setValue("caption_model", choice.model, {
                                shouldDirty: true,
                                shouldValidate: true,
                              });
                              setChosenCatalogModel(choice.catalogItem);
                            }}
                          />
                        </div>
                      );
                    }}
                  />
                </FormField>
              </div>
            </FieldGroup>
          </div>

          {/* parent_child 保留在契约中但尚未提供可用实现。 */}
        </CardContent>
      </Card>

      {/* 操作条放在 Card 外做 sticky:Card 自带 overflow-hidden,卡内 sticky 不会生效;
          贴工作台滚动容器底部,表单滚到哪里都能保存 */}
      <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-3 shadow-sm">
        <Button type="submit" disabled={save.isPending || !isDirty}>
          <Save className="size-4" />
          保存配置
        </Button>
        {/* 恢复默认会清掉本库全部自定义配置,给二次确认 */}
        <ConfirmDialog
          trigger={
            <Button type="button" variant="outline" disabled={save.isPending}>
              <RotateCcw className="size-4" />
              恢复默认
            </Button>
          }
          title="恢复为默认摄入配置?"
          description="本知识库的自定义解析、切片与图片描述配置都会被清除，改用平台默认值。已完成的摄入不受影响。"
          confirmLabel="恢复默认"
          onConfirm={() => save.mutateAsync(null)}
        />
        {isDirty && (
          <span className="text-xs text-warning">有未保存的修改</span>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          仅对之后的摄入生效
        </span>
      </div>
    </form>
  );
}

// ---- Tab 2:库级分享 ----------------------------------------------------------

function SharesTab({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const [slug, setSlug] = useState("");

  const {
    data: shares,
    isPending: sharesPending,
    isError: sharesError,
  } = useQuery({
    queryKey: ["kb-shares", kbId],
    queryFn: () => api.get<KbShare[]>(`/kb/bases/${kbId}/shares`),
  });

  const share = useMutation({
    mutationFn: () =>
      api.post<KbShare>(`/kb/bases/${kbId}/shares`, { grantee_org_slug: slug }),
    onSuccess: () => {
      toast.success("已分享(对方只读)");
      setSlug("");
      queryClient.invalidateQueries({ queryKey: ["kb-shares", kbId] });
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  const revoke = useMutation({
    mutationFn: (granteeOrgId: string) =>
      api.delete(`/kb/bases/${kbId}/shares/${granteeOrgId}`),
    onSuccess: () => {
      toast.success("已取消分享");
      queryClient.invalidateQueries({ queryKey: ["kb-shares", kbId] });
    },
    onError: (err) => toast.error(errMsg(err)),
  });

  return (
    <Card className="max-w-5xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Share2 className="size-4" />
          库级分享(对方组织只读)
        </CardTitle>
      </CardHeader>
      {/* lg 起左右分栏:左边「发起分享」是动作,右边「已分享列表」是状态,
          并排后新增与撤销互不遮挡,也不再是一条窄列长滚动 */}
      <CardContent className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div className="space-y-2">
          <div className="flex gap-2">
            <Input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="输入目标组织标识(slug)"
            />
            <Button
              disabled={!slug.trim() || share.isPending}
              onClick={() => share.mutate()}
            >
              分享
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            对方组织获得本库的只读检索权限,可随时取消。
          </p>
        </div>
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground">
            已分享组织
          </p>
          {/* 加载中不能显示「尚未分享」:那是在还不知道的时候谎称没有数据 */}
          {sharesPending ? (
            <p className="text-sm text-muted-foreground">正在加载分享列表…</p>
          ) : sharesError ? (
            <p className="text-sm text-destructive">
              分享列表加载失败,请稍后重试
            </p>
          ) : (
            !shares?.length && (
              <p className="text-sm text-muted-foreground">
                尚未分享给任何组织
              </p>
            )
          )}
          {shares?.map((s) => (
            <div
              key={s.id}
              className="flex items-center justify-between gap-2 rounded border border-border p-2 text-sm"
            >
              {/* 显示组织名 + slug;组织已删除时后端返回 null,如实标注而不是回落成 UUID 冒充名字 */}
              <span className="min-w-0">
                <span className="block truncate font-medium">
                  {s.grantee_org_name ?? "组织已不存在"}
                </span>
                <span className="block truncate font-mono text-xs text-muted-foreground">
                  {s.grantee_org_slug ?? s.grantee_org_id}
                </span>
              </span>
              <ConfirmDialog
                trigger={
                  <Button size="sm" variant="outline" className="shrink-0">
                    取消分享
                  </Button>
                }
                title={`取消对「${s.grantee_org_name ?? s.grantee_org_slug ?? "该组织"}」的分享?`}
                description="对方将立即失去本库的只读检索权限。"
                destructive
                confirmLabel="取消分享"
                onConfirm={() => revoke.mutateAsync(s.grantee_org_id)}
              />
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// ---- 视图入口:三 Tab 分区 ----------------------------------------------------

export function SettingsView({ kbId }: { kbId: string }) {
  // 知识快照发布已独立为「发布」视图(release-view.tsx),不再混在设置页里;
  // 实体类型注册表是组织级配置(不带 kb_id),本体留在这里,实体视图只放跳转入口。
  const { get, set } = useUrlState();
  const currentOrg = useCurrentOrg();
  const canManageLifecycle =
    currentOrg?.role === "org_admin" || currentOrg?.role === "platform_admin";
  const raw = get("tab");
  const tab =
    raw === "share" ||
    raw === "types" ||
    (raw === "danger" && canManageLifecycle)
      ? raw
      : "ingest";

  return (
    <Tabs
      value={tab}
      onValueChange={(next) =>
        set({ tab: next === "ingest" ? null : String(next) })
      }
      className="gap-5"
    >
      <TabsList className="w-full sm:w-fit">
        <TabsTrigger value="ingest">摄入配置</TabsTrigger>
        <TabsTrigger value="share">分享协作</TabsTrigger>
        <TabsTrigger value="types">实体类型</TabsTrigger>
        {canManageLifecycle && (
          <TabsTrigger value="danger">危险操作</TabsTrigger>
        )}
      </TabsList>
      {/* keepMounted:切去别的 Tab 时不卸载摄入表单,未保存的编辑与 unsaved-guard
          注册都还在;分享/注册表没有需要留存的本地编辑态,按需挂载即可 */}
      <TabsContent value="ingest" keepMounted>
        <IngestProfileTab kbId={kbId} />
      </TabsContent>
      <TabsContent value="share">
        <SharesTab kbId={kbId} />
      </TabsContent>
      <TabsContent value="types">
        <EntityTypesPanel />
      </TabsContent>
      {canManageLifecycle && (
        <TabsContent value="danger">
          <KbDangerZone kbId={kbId} />
        </TabsContent>
      )}
    </Tabs>
  );
}
