"use client";

import {
  BrainCircuit,
  BriefcaseBusiness,
  Database,
  ShieldCheck,
  Users,
} from "lucide-react";
import { AppShell, type NavItem } from "@/components/shell/app-shell";

// 成员与角色仅 org_admin/platform_admin(与后端 members.py 的 require_role 对齐);
// 知识库对区域内全角色可见,写操作由后端 require_write_role 兜底。
const nav: NavItem[] = [
  // 知识库页内含生命周期维护 Tab(操作记录/数据健康),不再单开菜单
  { href: "/org/kb", label: "知识库", icon: Database },
  {
    href: "/org/members",
    label: "成员与角色",
    icon: Users,
    roles: ["org_admin", "platform_admin"],
  },
  {
    href: "/org/settings/agent-permissions",
    label: "Agent 权限",
    icon: ShieldCheck,
    roles: ["org_admin", "platform_admin"],
  },
  // 长期记忆:读全角色可见,但订正/失效是 org_admin 才有的写操作
  // (后端 memory.py require_role(ORG_ADMIN, PLATFORM_ADMIN))。
  // 这一页的主要用途就是审阅与纠正,给只读角色一个改不动的页面没有意义,
  // 因此导航按写权限收口,与「成员与角色」同口径。
  {
    href: "/org/settings/memory",
    label: "长期记忆",
    icon: BrainCircuit,
    roles: ["org_admin", "platform_admin"],
  },
];

const areaSwitch: NavItem = {
  href: "/app/chat",
  label: "进入工作台",
  icon: BriefcaseBusiness,
};

export default function OrgLayout({ children }: { children: React.ReactNode }) {
  return (
    <AppShell nav={nav} areaLabel="租户管理端" areaSwitch={areaSwitch}>
      {children}
    </AppShell>
  );
}
