"use client";

import {
  Bot,
  CalendarClock,
  FolderKanban,
  KanbanSquare,
  Search,
  Settings,
  Users,
} from "lucide-react";
import { AppShell, type NavItem } from "@/components/shell/app-shell";

// 工作台各项对区域内全角色可见(后端 chat/kb/exports/icron 均 org 内全员;
// projects 全员可读)。定时任务是本人视角(只看得到自己创建的任务)。
const nav: NavItem[] = [
  { href: "/app/projects", label: "旅行计划", icon: KanbanSquare },
  { href: "/app/customers", label: "客户", icon: Users },
  { href: "/app/chat", label: "AI 助手", icon: Bot },
  { href: "/app/icron", label: "定时任务", icon: CalendarClock },
  { href: "/app/kb", label: "知识检索", icon: Search },
  { href: "/app/exports", label: "导出中心", icon: FolderKanban },
];

const areaSwitch: NavItem = {
  href: "/org/kb",
  label: "返回租户管理端",
  icon: Settings,
  roles: ["org_admin"],
};

export default function WorkbenchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AppShell nav={nav} areaLabel="一线工作台" areaSwitch={areaSwitch}>
      {children}
    </AppShell>
  );
}
