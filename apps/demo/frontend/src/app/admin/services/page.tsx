"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CloudSun, Globe, ImageIcon, Save } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ErrorState, PageHeader } from "@/components/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/utils";
import type { ServiceConfigsDto } from "@/lib/types";

// 字段定义与后端 service_configs payload 键对应;留空 = 回落 .env(迁移期兜底)。
//
// 密钥读出面契约(A12):后端一律回 `********` 掩码,绝不回明文/密文。
// **"不修改密钥"的表达方式就是把掩码原样回传** —— 草稿初值即掩码,用户不动
// 它,PATCH 带回去后端识别为"保持不变"。因此这里不做"空串=清空"的特殊处理,
// 也不要在提交前把掩码替换成空串。
// showWhen:按其它字段的当前值联动显隐(如仅在选中对应服务商时显示其密钥行)
interface FieldDef {
  key: string;
  label: string;
  hint?: string;
  placeholder?: string;
  secret?: boolean;
  suffix?: string;
  options?: { value: string; label: string }[];
  showWhen?: (values: Record<string, string>) => boolean;
  // 多行输入(如域名黑名单):payload 仍存字符串,后端按换行/逗号切分成列表
  multiline?: boolean;
}

interface ServiceDef {
  name: string;
  title: string;
  description: string;
  icon: typeof Globe;
  fields: FieldDef[];
}

const SERVICES: ServiceDef[] = [
  {
    name: "websearch",
    title: "联网搜索",
    description: "agent 的 web_search 搜索与 web_fetch 正文读取",
    icon: Globe,
    fields: [
      {
        key: "provider",
        label: "搜索服务商",
        hint: "自动模式按 Tavily → 博查顺序故障转移",
        options: [
          { value: "auto", label: "自动（故障转移）" },
          { value: "tavily", label: "Tavily" },
          { value: "bocha", label: "博查" },
        ],
      },
      {
        key: "tavily_api_key",
        label: "Tavily API 密钥",
        secret: true,
        showWhen: (values) =>
          !values.provider ||
          values.provider === "auto" ||
          values.provider === "tavily",
      },
      {
        key: "bocha_api_key",
        label: "博查 API 密钥",
        secret: true,
        showWhen: (values) =>
          !values.provider ||
          values.provider === "auto" ||
          values.provider === "bocha",
      },
      {
        key: "timeout_seconds",
        label: "请求超时",
        placeholder: "15",
        suffix: "秒",
      },
      {
        key: "deny_domains",
        label: "来源黑名单",
        hint: "每行一条，支持 *://*.example.com/* 匹配式或 /正则/；命中的结果不入库也不抓取",
        placeholder: "留空表示不屏蔽任何来源",
        multiline: true,
      },
      {
        key: "compression_method",
        label: "正文压缩方式",
        hint: "web_fetch 读取的长正文如何压缩后再交给模型",
        options: [
          { value: "cutoff", label: "定长截断（默认）" },
          { value: "rerank", label: "重排取相关段落" },
          { value: "none", label: "不压缩" },
        ],
      },
      {
        key: "compression_total_chars",
        label: "正文字符预算",
        hint: "所有网页正文合计上限，超出按上面的方式压缩",
        placeholder: "12000",
        suffix: "字",
      },
    ],
  },
  {
    name: "imagegen",
    title: "图片生成",
    description: "Agent 的 GPT-Image-2 兼容生图服务",
    icon: ImageIcon,
    fields: [
      { key: "api_key", label: "API 密钥", secret: true },
      {
        key: "base_url",
        label: "Base URL",
        hint: "必须包含 https:// 或 http://，保存时会校验",
        placeholder: "https://api.v36.cm",
      },
      { key: "model", label: "模型", placeholder: "gpt-image-2-c" },
      {
        key: "api_mode",
        label: "接口模式",
        hint: "GPT-Image-2 使用 chat（SSE）；标准图片接口使用 images",
        options: [
          { value: "images", label: "images" },
          { value: "chat", label: "chat" },
        ],
      },
      {
        key: "timeout_seconds",
        label: "请求超时",
        placeholder: "300",
        suffix: "秒",
      },
    ],
  },
  {
    name: "weather",
    title: "天气数据",
    description: "weather_get 工具的数据源；open-meteo 免密钥恒作兜底",
    icon: CloudSun,
    fields: [
      {
        key: "weatherapi_api_key",
        label: "WeatherAPI 密钥",
        hint: "全球主源；留空则该源不启用",
        secret: true,
      },
      {
        key: "qweather_api_key",
        label: "和风天气密钥",
        hint: "中国及亚洲数据增强；需与专属 API Host 同时配置才生效",
        secret: true,
      },
      {
        key: "qweather_api_host",
        label: "和风专属 API Host",
        hint: "每账号独立，形如 xxx.qweatherapi.com",
        placeholder: "xxx.qweatherapi.com",
      },
    ],
  },
];

function fromPayload(
  fields: FieldDef[],
  payload: Record<string, unknown>,
): Record<string, string> {
  return Object.fromEntries(
    fields.map((field) => [field.key, String(payload[field.key] ?? "")]),
  );
}

function ServiceSection({
  service,
  payload,
  onSave,
  saving,
}: {
  service: ServiceDef;
  payload: Record<string, unknown>;
  onSave: (payload: Record<string, string>) => void;
  saving: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    fromPayload(service.fields, payload),
  );
  // 服务端 payload 变化(首载/保存成功)时同步本地表单(render 期调整,不走 effect)
  const [seenPayload, setSeenPayload] = useState(payload);
  if (seenPayload !== payload) {
    setSeenPayload(payload);
    setValues(fromPayload(service.fields, payload));
  }

  const dirty = service.fields.some(
    (field) => (values[field.key] ?? "") !== String(payload[field.key] ?? ""),
  );
  const Icon = service.icon;

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center gap-3 border-b border-border px-5 py-3.5">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <Icon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-medium">{service.title}</h2>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {service.description}
          </p>
        </div>
        <Button
          size="sm"
          disabled={!dirty || saving}
          onClick={() => onSave(values)}
        >
          <Save className="size-3.5" />
          保存
        </Button>
      </div>
      <div className="divide-y divide-border">
        {service.fields
          .filter((field) => field.showWhen?.(values) ?? true)
          .map((field) => (
            <div
              key={field.key}
              className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3.5"
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm">{field.label}</div>
                {field.hint && (
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {field.hint}
                  </div>
                )}
              </div>
              <div className="flex w-72 shrink-0 items-center gap-2">
                {field.multiline ? (
                  <Textarea
                    rows={4}
                    className="font-mono text-xs"
                    placeholder={field.placeholder ?? "留空使用 .env 配置"}
                    value={values[field.key] ?? ""}
                    onChange={(event) =>
                      setValues((prev) => ({
                        ...prev,
                        [field.key]: event.target.value,
                      }))
                    }
                  />
                ) : field.options ? (
                  <Select
                    value={values[field.key] || undefined}
                    onValueChange={(value) =>
                      setValues((prev) => ({
                        ...prev,
                        [field.key]: value ?? "",
                      }))
                    }
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="使用 .env 默认" />
                    </SelectTrigger>
                    <SelectContent>
                      {field.options.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    type={
                      field.secret
                        ? "password"
                        : field.key === "dim"
                          ? "number"
                          : "text"
                    }
                    placeholder={field.placeholder ?? "留空使用 .env 配置"}
                    value={values[field.key] ?? ""}
                    onChange={(event) =>
                      setValues((prev) => ({
                        ...prev,
                        [field.key]: event.target.value,
                      }))
                    }
                  />
                )}
                {field.suffix && (
                  <span className="text-xs text-muted-foreground">
                    {field.suffix}
                  </span>
                )}
              </div>
            </div>
          ))}
      </div>
    </section>
  );
}

export default function AdminServicesPage() {
  const queryClient = useQueryClient();
  const configs = useQuery({
    queryKey: ["admin-service-configs"],
    queryFn: () => api.get<ServiceConfigsDto>("/admin/service-configs"),
  });
  const save = useMutation({
    mutationFn: ({
      name,
      payload,
    }: {
      name: string;
      payload: Record<string, string>;
    }) =>
      api.put(`/admin/service-configs/${name}`, {
        payload,
      }),
    onSuccess: () => {
      toast.success("配置已保存，立即生效");
      queryClient.invalidateQueries({ queryKey: ["admin-service-configs"] });
    },
    onError: (error) => toast.error(errMsg(error, "保存失败")),
  });
  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <PageHeader
        title="外部服务"
        description="联网搜索、图片生成与天气的凭证；模型类配置统一在模型提供商与模型路由"
      />
      {configs.error ? (
        <ErrorState error={configs.error} onRetry={() => configs.refetch()} />
      ) : configs.isPending ? (
        <div className="space-y-5">
          {SERVICES.map((service) => (
            <Skeleton key={service.name} className="h-64 rounded-lg" />
          ))}
        </div>
      ) : (
        SERVICES.map((service) => (
          <ServiceSection
            key={service.name}
            service={service}
            payload={configs.data?.[service.name] ?? {}}
            saving={save.isPending}
            onSave={(payload) => save.mutate({ name: service.name, payload })}
          />
        ))
      )}
    </div>
  );
}
