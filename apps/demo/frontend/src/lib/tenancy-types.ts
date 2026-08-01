// 租户底座(登录/组织/成员/邀请)的前端 DTO。
//
// SDK 化改造:内置角色只有 platform_admin / org_admin / member,宿主可经
// `register_roles()` 注册任意业务角色 —— 因此 Role 在前端是**自由字符串**
// (见 lib/auth.ts),这里的文案表只是兜底,查不到一律回显原值。

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  org: { id: string; slug: string; name: string; role: string };
  orgs: { id: string; slug: string; name: string; role: string }[];
}

export interface AdminOrg {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  member_count: number;
  created_at: string | null;
}


export interface Member {
  membership_id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string | null;
}

export interface InviteOut {
  id: string;
  email: string;
  role: string;
  expires_at: string;
  created_at: string | null;
}

export interface InviteCreated extends InviteOut {
  token: string;
}

export interface InviteInfo {
  email: string;
  role: string;
  org_name: string;
  org_slug: string;
  user_exists: boolean;
}

// 内置角色的展示名兜底表。宿主注册的角色查不到就回显 key 原值
// (统一入口是 lib/auth.ts::roleLabel,不要在组件里再写一份 switch)。
export const ROLE_LABELS: Record<string, string> = {
  platform_admin: "平台管理员",
  org_admin: "组织管理员",
  member: "成员",
};

// 租户内可授予的角色(platform_admin 仅平台侧产生)。
// 后端 members.py 的 ASSIGNABLE_ROLES 由角色注册表派生,宿主注册新角色后
// 这里应改为读接口下发;demo 只用内置两档,先按常量给。
export const ASSIGNABLE_ROLES = ["org_admin", "member"] as const;
