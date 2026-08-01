// wiki 页 page_type 的展示元数据单一真源:左栏分组、详情元信息卡、新建下拉共用。
// page_type 是开放字符串,未知类型回退 FileText 图标 + 原文标签(零迁移)。

import {
  BedDouble,
  BookOpen,
  FileStack,
  FileText,
  Handshake,
  MapPin,
  Route,
  Tag,
  type LucideIcon,
} from "lucide-react";

export interface PageTypeMeta {
  label: string;
  icon: LucideIcon;
}

/** 已知类型(顺序即左栏分组顺序) */
export const PAGE_TYPE_META: Record<string, PageTypeMeta> = {
  overview: { label: "总览", icon: BookOpen },
  destination: { label: "目的地", icon: MapPin },
  supplier: { label: "供应商", icon: Handshake },
  hotel: { label: "酒店", icon: BedDouble },
  route: { label: "线路", icon: Route },
  topic: { label: "主题", icon: Tag },
  source_summary: { label: "来源摘要", icon: FileStack },
};

export const PAGE_TYPE_ORDER = Object.keys(PAGE_TYPE_META);

export function pageTypeMeta(type: string): PageTypeMeta {
  return PAGE_TYPE_META[type] ?? { label: type, icon: FileText };
}
