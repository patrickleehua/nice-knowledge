"use client";

import {
  Activity,
  BarChart3,
  Bot,
  Boxes,
  Building2,
  CircleDollarSign,
  Globe,
  Network,
  Route,
  Stethoscope,
  ScrollText,
  Sparkles,
} from "lucide-react";
import { AppShell, type NavItem } from "@/components/shell/app-shell";

// /admin 整区已由 proxy.ts 限定 platform_admin(后端 admin.py 同样整路由 require_role),无需逐项 roles。
const nav: NavItem[] = [
  { href: "/admin/orgs", label: "租户管理", icon: Building2 },
  { href: "/admin/providers", label: "模型提供商", icon: Boxes },
  { href: "/admin/models", label: "模型路由", icon: Route },
  { href: "/admin/prompts", label: "Prompt 注册表", icon: ScrollText },
  { href: "/admin/agents", label: "Agent 管理", icon: Bot },
  { href: "/admin/skills", label: "技能管理", icon: Sparkles },
  { href: "/admin/mcp", label: "MCP 服务器", icon: Network },
  { href: "/admin/services", label: "外部服务", icon: Globe },
  { href: "/admin/diagnostics", label: "系统诊断", icon: Stethoscope },
  { href: "/admin/runs", label: "运行日志", icon: Activity },
  { href: "/admin/usage", label: "使用统计", icon: BarChart3 },
  { href: "/admin/prices", label: "模型价格", icon: CircleDollarSign },
];

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AppShell nav={nav} areaLabel="平台管理端">
      {children}
    </AppShell>
  );
}
