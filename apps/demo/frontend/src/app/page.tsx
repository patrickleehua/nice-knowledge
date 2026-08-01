import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default async function Home() {
  // Next 16:cookies() 为异步 Request API
  const store = await cookies();
  const token = store.get("tf_token")?.value;
  const role = store.get("tf_role")?.value;
  if (!token) redirect("/login");
  if (role === "platform_admin") redirect("/admin/orgs");
  if (role === "org_admin" || role === "operator") redirect("/org/kb");
  redirect("/app/projects");
}
