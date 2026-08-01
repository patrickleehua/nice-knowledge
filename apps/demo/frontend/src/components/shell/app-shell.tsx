"use client";

import { LogOut, Menu, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTheme } from "next-themes";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useState } from "react";
import { createPortal } from "react-dom";
import { NotificationBell } from "@/components/shell/notification-bell";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet";
import { clearSession, useCurrentOrg, useMounted, type Role } from "@/lib/auth";
import { cn } from "@/lib/utils";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** 缺省 = 区域内全角色可见;给定则仅列出的角色可见(与后端权限矩阵对齐)。 */
  roles?: Role[];
}

type ShellHeaderPosition = "content" | "centered";

interface ShellHeaderTargets {
  content: HTMLDivElement | null;
  centered: HTMLDivElement | null;
}

const ShellHeaderTargetsContext = createContext<ShellHeaderTargets>({
  content: null,
  centered: null,
});

/** 将页面级导航或操作挂到 AppShell 顶栏，避免正文重复占用一行。 */
export function ShellHeader({
  children,
  position = "content",
}: {
  children: React.ReactNode;
  position?: ShellHeaderPosition;
}) {
  const targets = useContext(ShellHeaderTargetsContext);
  const target = targets[position];

  return target
    ? createPortal(
        <div className="pointer-events-auto h-full">{children}</div>,
        target,
      )
    : null;
}

function Brand() {
  return (
    <div className="flex h-14 items-center gap-2 border-b border-sidebar-border px-4">
      <span className="flex size-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
        N
      </span>
      <span className="text-sm font-semibold text-sidebar-foreground">
        nicekit demo
      </span>
    </div>
  );
}

function ShellNav({
  items,
  pathname,
  onNavigate,
}: {
  items: NavItem[];
  pathname: string;
  onNavigate?: () => void;
}) {
  // 嵌套路由(如 /org/kb 与 /org/kb/lifecycle)只让最长前缀命中者激活,避免双亮
  const matched = items
    .filter(
      (item) =>
        pathname === item.href || pathname.startsWith(`${item.href}/`),
    )
    .reduce<NavItem | null>(
      (best, item) =>
        best === null || item.href.length > best.href.length ? item : best,
      null,
    );
  return (
    <nav className="flex-1 space-y-1 overflow-y-auto p-3">
      {items.map((item) => {
        const active = item.href === matched?.href;
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            className={cn(
              "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
              active
                ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
            )}
          >
            <Icon className="size-4" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function AreaFooter({
  areaLabel,
  areaSwitch,
}: {
  areaLabel: string;
  areaSwitch?: NavItem;
}) {
  const Icon = areaSwitch?.icon;

  return (
    <div className="space-y-2 border-t border-sidebar-border p-3">
      {areaSwitch && Icon && (
        <Link
          href={areaSwitch.href}
          className="flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <Icon className="size-4" />
          {areaSwitch.label}
        </Link>
      )}
      <div className="px-3 text-xs text-muted-foreground">{areaLabel}</div>
    </div>
  );
}

export function AppShell({
  nav,
  areaLabel,
  areaSwitch,
  children,
}: {
  nav: NavItem[];
  areaLabel: string;
  areaSwitch?: NavItem;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { resolvedTheme, setTheme } = useTheme();
  const org = useCurrentOrg();
  const mounted = useMounted();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [headerContentTarget, setHeaderContentTarget] =
    useState<HTMLDivElement | null>(null);
  const [headerCenteredTarget, setHeaderCenteredTarget] =
    useState<HTMLDivElement | null>(null);

  const role = org?.role ?? null;
  // 角色未知(SSR / 首帧)时仅显示无限制项,避免向无权用户闪现受限入口且不引发 hydration 抖动。
  const visibleNav = nav.filter(
    (item) => !item.roles || (role !== null && item.roles.includes(role)),
  );
  const visibleAreaSwitch =
    areaSwitch &&
    (!areaSwitch.roles || (role !== null && areaSwitch.roles.includes(role)))
      ? areaSwitch
      : undefined;

  function logout() {
    clearSession();
    router.push("/login");
  }

  return (
    <div className="flex h-dvh w-full overflow-hidden">
      <aside className="hidden h-full w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
        <Brand />
        <ShellNav items={visibleNav} pathname={pathname} />
        <AreaFooter areaLabel={areaLabel} areaSwitch={visibleAreaSwitch} />
      </aside>

      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent
          side="left"
          className="w-64 gap-0 bg-sidebar p-0 md:hidden"
        >
          <SheetTitle className="sr-only">主导航</SheetTitle>
          <SheetDescription className="sr-only">
            nicekit demo 功能导航
          </SheetDescription>
          <Brand />
          <ShellNav
            items={visibleNav}
            pathname={pathname}
            onNavigate={() => setMobileNavOpen(false)}
          />
          <AreaFooter areaLabel={areaLabel} areaSwitch={visibleAreaSwitch} />
        </SheetContent>
      </Sheet>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="relative flex h-14 shrink-0 items-center gap-1 border-b border-border bg-card px-3 sm:gap-3 sm:px-6">
          <Button
            variant="ghost"
            size="icon"
            className="relative z-10 md:hidden"
            aria-label="打开主导航"
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu />
          </Button>
          <div
            ref={setHeaderCenteredTarget}
            className="pointer-events-none absolute inset-y-0 right-3 left-3 sm:right-6 sm:left-6"
          />
          <div
            ref={setHeaderContentTarget}
            className="pointer-events-none relative z-[1] h-full min-w-0 flex-1 overflow-hidden"
          />
          <div className="relative z-10 ml-auto flex shrink-0 items-center gap-1 sm:gap-3">
            <NotificationBell />
            <Button
              variant="ghost"
              size="icon"
              aria-label="切换主题"
              onClick={() =>
                setTheme(resolvedTheme === "dark" ? "light" : "dark")
              }
            >
              {mounted && resolvedTheme === "dark" ? (
                <Sun className="size-4" />
              ) : (
                <Moon className="size-4" />
              )}
            </Button>
            <div className="hidden text-right sm:block">
              <div className="text-sm font-medium">{org?.name ?? "…"}</div>
              <div className="text-xs text-muted-foreground">{org?.role}</div>
            </div>
            <Button
              variant="ghost"
              size="icon"
              aria-label="退出登录"
              onClick={logout}
            >
              <LogOut className="size-4" />
            </Button>
          </div>
        </header>
        <main className="mx-auto min-h-0 w-full max-w-350 flex-1 overflow-y-auto p-3 sm:p-6">
          <ShellHeaderTargetsContext.Provider
            value={{
              content: headerContentTarget,
              centered: headerCenteredTarget,
            }}
          >
            {children}
          </ShellHeaderTargetsContext.Provider>
        </main>
      </div>
    </div>
  );
}
