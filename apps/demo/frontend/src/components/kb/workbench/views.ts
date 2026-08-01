// 知识工作台视图注册表(纯数据,供顶部 Tab、侧栏、深链校验共用)。
// 分组顺序即工作流顺序:内容(采集与维护)→ 治理(审核与洞察)→ 发布(快照上线)。
// 设置独立成尾组,在 Tab 条右端以齿轮呈现。

import {
  BookOpen,
  ClipboardCheck,
  Database,
  FileText,
  Images,
  LayoutDashboard,
  Rocket,
  Settings,
  Waypoints,
  type LucideIcon,
} from "lucide-react";

export type KbViewGroup = "content" | "governance" | "release" | "config";

export interface KbViewMeta {
  key: string;
  label: string;
  icon: LucideIcon;
  group: KbViewGroup;
  /** 左侧导航栏(文档树 / 页面列表);缺省表示该视图不需要侧栏 */
  sidebar?: "documents" | "pages";
}

export const KB_VIEWS = [
  { key: "overview", label: "概览", icon: LayoutDashboard, group: "content" },
  {
    key: "sources",
    label: "资料",
    icon: FileText,
    group: "content",
    sidebar: "documents",
  },
  { key: "images", label: "图片", icon: Images, group: "content" },
  // 实体视图内部已自带目的地列(entities-tab.tsx),不再重复给一个左栏
  { key: "entities", label: "实体", icon: Database, group: "content" },
  {
    key: "wiki",
    label: "Wiki",
    icon: BookOpen,
    group: "content",
    sidebar: "pages",
  },
  {
    key: "review",
    label: "审核",
    icon: ClipboardCheck,
    group: "governance",
  },
  { key: "graph", label: "图谱", icon: Waypoints, group: "governance" },
  { key: "release", label: "发布", icon: Rocket, group: "release" },
  { key: "settings", label: "设置", icon: Settings, group: "config" },
] as const satisfies readonly KbViewMeta[];

export type KbViewKey = (typeof KB_VIEWS)[number]["key"];

export function isKbViewKey(value: string | null): value is KbViewKey {
  return KB_VIEWS.some((view) => view.key === value);
}

export function kbView(key: KbViewKey): KbViewMeta {
  return KB_VIEWS.find((view) => view.key === key) as KbViewMeta;
}

/** Tab 条按组渲染:组间插分隔线,设置单独放右端。 */
export const KB_TAB_GROUPS: KbViewGroup[] = [
  "content",
  "governance",
  "release",
];
