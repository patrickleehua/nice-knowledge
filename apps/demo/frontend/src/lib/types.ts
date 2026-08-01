// 前端 DTO 的统一入口(桶文件)。
//
// 实际定义按域拆在三个模块里(MIGRATION-PLAN §5.8:lib/types.ts 42KB 需拆分):
//   - lib/kb-types.ts       知识库:摄入 / 实体 / 检索 / 图谱 / wiki / 图片资产
//   - lib/admin-types.ts    平台管理端:模型 / 诊断 / Prompt / Agent 卡 / 技能
//   - lib/tenancy-types.ts  租户底座:登录 / 组织 / 成员 / 邀请 / 角色
//
// 这里只做 re-export,调用方继续 `from "@/lib/types"` 即可;需要收窄依赖时
// 直接 import 具体模块。

export * from "@/lib/kb-types";
export * from "@/lib/admin-types";
export * from "@/lib/tenancy-types";
