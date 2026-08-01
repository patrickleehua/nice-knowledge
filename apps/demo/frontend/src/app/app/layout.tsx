"use client";

import {
  Bell,
  Bot,
  CalendarClock,
  Search,
  Settings,
} from "lucide-react";
import { AppShell, type NavItem } from "@/components/shell/app-shell";

// 工作台各项对区域内全角色可见(后端 chat/kb/icron/notifications 均 org 内全员)。
// 定时任务是本人视角(只看得到自己创建的任务)。
const nav: NavItem[] = [
  { href: "/app/chat", label: "AI 助手", icon: Bot },
  { href: "/app/kb", label: "知识检索", icon: Search },
  { href: "/app/icron", label: "定时任务", icon: CalendarClock },
  { href: "/app/notifications", label: "通知", icon: Bell },
];

const areaSwitch: NavItem = {
  href: "/org/kb",
  label: "进入租户管理端",
  icon: Settings,
  roles: ["org_admin", "platform_admin"],
};

export default function WorkbenchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AppShell nav={nav} areaLabel="工作台" areaSwitch={areaSwitch}>
      {children}
    </AppShell>
  );
}
