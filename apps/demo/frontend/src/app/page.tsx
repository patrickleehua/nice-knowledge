import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default async function Home() {
  // Next 16:cookies() 为异步 Request API
  const store = await cookies();
  const token = store.get("nk_token")?.value;
  const role = store.get("nk_role")?.value;
  if (!token) redirect("/login");
  if (role === "platform_admin") redirect("/admin/orgs");
  if (role === "org_admin") redirect("/org/kb");
  // 宿主自定义角色一律回落工作台(与 lib/auth.ts::homeForRole 同口径)
  redirect("/app/chat");
}
