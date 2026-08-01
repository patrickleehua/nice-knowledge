// Next.js 16:Middleware 更名为 Proxy(node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md)。
// 乐观路由守卫:仅做 cookie 级检查,真正的鉴权在后端 JWT + RLS。

import { NextResponse, type NextRequest } from "next/server";

const AREA_ROLES: Record<string, string[]> = {
  "/admin": ["platform_admin"],
  "/org": ["platform_admin", "org_admin", "operator"],
  "/app": [
    "platform_admin",
    "org_admin",
    "sales",
    "operator",
    "pricer",
    "reviewer",
  ],
};

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const area = Object.keys(AREA_ROLES).find((prefix) =>
    pathname.startsWith(prefix),
  );
  if (!area) return NextResponse.next();

  const token = request.cookies.get("tf_token")?.value;
  if (!token) {
    const login = new URL("/login", request.url);
    // 带上 query(?session=/?tab= 等 URL 状态),登录后深链完整回跳
    login.searchParams.set("next", pathname + request.nextUrl.search);
    return NextResponse.redirect(login);
  }
  const role = request.cookies.get("tf_role")?.value ?? "";
  if (!AREA_ROLES[area].includes(role)) {
    return NextResponse.redirect(new URL("/app/projects", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/app/:path*", "/org/:path*", "/admin/:path*"],
};
