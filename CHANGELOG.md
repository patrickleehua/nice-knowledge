# 更新日志

本文件记录 `nicekit` SDK 的显著变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **0.x 阶段约定**:公开 API 尚未冻结,次版本号(`0.Y.0`)可能包含不兼容变更,
> 修订号(`0.0.Z`)只做向后兼容的修复。升级前请读本文件对应条目。

## [未发布]

### 新增
- Apache-2.0 协议、`NOTICE`、PyPI 发布元数据与 GitHub Actions 发布链路
- 文档索引 [docs/README.md](docs/README.md) 与面向 AI 编码助手的仓库说明 [AGENTS.md](AGENTS.md)
- 发布手册 [docs/RELEASING.md](docs/RELEASING.md)

## [0.1.0] - 2026-08-04

首个版本。从生产系统 TravelFlow AI 抽取通用能力,形成独立的多租户 Agent + 知识库平台 SDK。

### 新增
- **多租户底座**:组织 / 用户 / 成员 / 邀请、JWT + 旋转式 refresh token、
  PostgreSQL `FORCE ROW LEVEL SECURITY` 强隔离,三种接入模式(全托管 / 桥接 / 单租户)
- **LLM 多模型层**:OpenAI / Anthropic 双协议归一、模型能力注册表、模型 ID 归一化、
  路由与降级链、provider 冷却、组织级预算与并发闸门、四桶 token 计量
- **Agent 运行时**:多轮工具循环、七轴权限审批、AI 复核员、MCP 客户端、
  `SKILL.md` 技能、子 agent 委派、长期记忆、会话目标续跑、上下文压缩、定时任务
- **知识库**:文档摄入(解析 / 分块 / 上下文化 / 嵌入)、通用实体抽取与审核、
  快照发布与回滚、四路混合检索(结构化 + 图谱 + 稀疏 + 向量,RRF 融合)、
  知识图谱、wiki 生成、多媒体入库与审核、提示注入围栏
- **运维**:健康 / 就绪探针、Prometheus 指标、服务心跳、provider 连通性探测、
  孤儿任务恢复、超时清扫、事务性发件箱
- **API**:`/api/v1` 下 18 个 router、182 个 REST 端点
- **示例宿主**:`apps/demo`(Nice Knowledge)—— FastAPI 后端 + Next.js 前端

[未发布]: https://github.com/patrickleehua/nice-knowledge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/patrickleehua/nice-knowledge/releases/tag/v0.1.0
