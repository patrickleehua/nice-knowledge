// wiki 页 page_type 的展示元数据单一真源:左栏分组、详情元信息卡、新建下拉共用。
// page_type 是开放字符串,未知类型回退 FileText 图标 + 原文标签(零迁移)。

import {
  BookOpen,
  FileStack,
  FileText,
  Tag,
  type LucideIcon,
} from "lucide-react";

export interface PageTypeMeta {
  label: string;
  icon: LucideIcon;
}

/**
 * 内置类型(顺序即左栏分组顺序)。
 *
 * 后端 `page_type` 是开放字符串(MIGRATION-PLAN B20):默认不限制,宿主想收紧
 * 就调 `set_valid_page_types()` 注册白名单。SDK 只内置三档 ——
 * `overview`(每库一篇综述)、`topic`(LLM 缺省归一化目标,见
 * wiki_gen.DEFAULT_PAGE_TYPE)、`source_summary`(来源摘要)。
 * 其余类型走 pageTypeMeta 的兜底:FileText + 原文标签。
 */
export const PAGE_TYPE_META: Record<string, PageTypeMeta> = {
  overview: { label: "总览", icon: BookOpen },
  topic: { label: "主题", icon: Tag },
  source_summary: { label: "来源摘要", icon: FileStack },
};

export const PAGE_TYPE_ORDER = Object.keys(PAGE_TYPE_META);

export function pageTypeMeta(type: string): PageTypeMeta {
  return PAGE_TYPE_META[type] ?? { label: type, icon: FileText };
}
