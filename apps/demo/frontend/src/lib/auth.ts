// 认证状态:token 与角色存 cookie(供 proxy.ts 路由守卫读取)+ refresh/org 存 localStorage。
// 后端 access token 15 分钟过期,api 客户端在 401 时用 refresh_token 静默续期(旋转式);
// cookie 生命周期与 refresh(后端 refresh_token_expire_days=7)对齐,access 过期由续期兜底。

import { useSyncExternalStore } from "react";

/**
 * 角色是**自由字符串**,不是联合字面量。
 *
 * SDK 只内置三个角色(`platform_admin` / `org_admin` / `member`),宿主可以通过
 * `register_roles()` 注册任意业务角色并直接下发到 JWT。前端如果把 Role 写成
 * 联合类型再做穷举 switch,宿主一注册新角色就编译不过/渲染空白,因此这里刻意
 * 保留 `string`,所有分支都必须给未知角色兜底。
 */
export type Role = string;

/** SDK 内置角色(用于 UI 兜底文案与导航可见性,不构成穷举)。 */
export const BUILTIN_ROLES = ["platform_admin", "org_admin", "member"] as const;

export type BuiltinRole = (typeof BUILTIN_ROLES)[number];

const ROLE_LABELS: Record<string, string> = {
  platform_admin: "平台管理员",
  org_admin: "组织管理员",
  member: "成员",
};

/** 角色展示名;未知角色(宿主自定义)原样回显,不隐藏也不报错。 */
export function roleLabel(role: string | null | undefined): string {
  if (!role) return "—";
  return ROLE_LABELS[role] ?? role;
}

export interface AuthOrg {
  id: string;
  slug: string;
  name: string;
  role: Role;
}

const TOKEN_COOKIE = "nk_token";
const ROLE_COOKIE = "nk_role";
const REFRESH_KEY = "nk_refresh";
const ORG_KEY = "nk_org";

// cookie 生命周期对齐后端 refresh_token_expire_days(=7);旧值 8h 会与 15min access 错配,
// 导致 refresh 仍有效却因 cookie 过期被踢登录。
const SESSION_MAX_AGE = 60 * 60 * 24 * 7;

// 模块级订阅:saveSession / clearSession 后通知 useCurrentOrg 等即时重渲染(顶栏 org/角色随之更新)。
type Listener = () => void;
const listeners = new Set<Listener>();

function notify() {
  for (const listener of listeners) listener();
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function setCookie(name: string, value: string, maxAgeSeconds: number) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAgeSeconds}; samesite=lax`;
}

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

export function saveSession(args: {
  accessToken: string;
  refreshToken: string;
  org: AuthOrg;
}) {
  setCookie(TOKEN_COOKIE, args.accessToken, SESSION_MAX_AGE);
  setCookie(ROLE_COOKIE, args.org.role, SESSION_MAX_AGE);
  localStorage.setItem(REFRESH_KEY, args.refreshToken);
  localStorage.setItem(ORG_KEY, JSON.stringify(args.org));
  notify();
}

export function getToken(): string | null {
  return getCookie(TOKEN_COOKIE);
}

export function getCurrentOrg(): AuthOrg | null {
  const raw = localStorage.getItem(ORG_KEY);
  return raw ? (JSON.parse(raw) as AuthOrg) : null;
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function clearSession() {
  setCookie(TOKEN_COOKIE, "", 0);
  setCookie(ROLE_COOKIE, "", 0);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(ORG_KEY);
  notify();
}

const emptySubscribe = () => () => {};

/**
 * SSR 安全地读当前组织(客户端组件用;服务端渲染时为 null)。
 * 真实订阅:saveSession/clearSession(含 401 续期换 org)后即时重渲染。
 */
export function useCurrentOrg(): AuthOrg | null {
  const raw = useSyncExternalStore(
    subscribe,
    () => localStorage.getItem(ORG_KEY),
    () => null,
  );
  return raw ? (JSON.parse(raw) as AuthOrg) : null;
}

/** 客户端挂载标记(替代 useEffect+setState 的 mounted 模式)。 */
export function useMounted(): boolean {
  return useSyncExternalStore(
    emptySubscribe,
    () => true,
    () => false,
  );
}

/** 登录后落地页;未知角色一律回落一线工作台(与 proxy.ts 的区域守卫口径一致)。 */
export function homeForRole(role: Role): string {
  if (role === "platform_admin") return "/admin/orgs";
  if (role === "org_admin") return "/org/kb";
  return "/app/chat";
}
