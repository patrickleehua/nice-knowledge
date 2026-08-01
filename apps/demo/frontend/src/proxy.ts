// Next.js 16:Middleware 更名为 Proxy(node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md)。
// 乐观路由守卫:仅做 cookie 级检查,真正的鉴权在后端 JWT + RLS。
//
// 角色是自由字符串(见 lib/auth.ts):这里只对**内置三角色**做区域收口,
// 宿主注册的自定义角色一律按 `member` 处理(能进 /app,进不了 /org、/admin)。
// 少放行一个自定义角色只是多一次跳转,放错一个才是安全问题。

import { NextResponse, type NextRequest } from "next/server";

const AREA_ROLES: Record<string, string[]> = {
  "/admin": ["platform_admin"],
  "/org": ["platform_admin", "org_admin"],
  // /app 对所有已登录角色开放(含宿主自定义角色),写操作由后端 require_role 兜底
  "/app": [],
};

const FALLBACK_HOME = "/app/chat";

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const area = Object.keys(AREA_ROLES).find((prefix) =>
    pathname.startsWith(prefix),
  );
  if (!area) return NextResponse.next();

  const token = request.cookies.get("nk_token")?.value;
  if (!token) {
    const login = new URL("/login", request.url);
    // 带上 query(?session=/?tab= 等 URL 状态),登录后深链完整回跳
    login.searchParams.set("next", pathname + request.nextUrl.search);
    return NextResponse.redirect(login);
  }
  const allowed = AREA_ROLES[area];
  const role = request.cookies.get("nk_role")?.value ?? "";
  // 空白名单 = 区域对全角色开放
  if (allowed.length > 0 && !allowed.includes(role)) {
    return NextResponse.redirect(new URL(FALLBACK_HOME, request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/app/:path*", "/org/:path*", "/admin/:path*"],
};
