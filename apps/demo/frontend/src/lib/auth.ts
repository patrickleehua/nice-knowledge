// 认证状态:token 与角色存 cookie(供 proxy.ts 路由守卫读取)+ refresh/org 存 localStorage。
// 后端 access token 15 分钟过期,api 客户端在 401 时用 refresh_token 静默续期(旋转式);
// cookie 生命周期与 refresh(后端 refresh_token_expire_days=7)对齐,access 过期由续期兜底。

import { useSyncExternalStore } from "react";

export type Role =
  | "platform_admin"
  | "org_admin"
  | "sales"
  | "operator"
  | "pricer"
  | "reviewer";

export interface AuthOrg {
  id: string;
  slug: string;
  name: string;
  role: Role;
}

const TOKEN_COOKIE = "tf_token";
const ROLE_COOKIE = "tf_role";
const REFRESH_KEY = "tf_refresh";
const ORG_KEY = "tf_org";

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

export function homeForRole(role: Role): string {
  if (role === "platform_admin") return "/admin/orgs";
  if (role === "org_admin") return "/org/kb";
  return "/app/projects";
}
