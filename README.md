# NiceKit

![NiceKit logo](images/nicekit.png)

**NiceKit** 是一个多租户 Agent + 知识库平台 SDK，提供从企业级 AI 应用底座中抽取出来的通用能力。

它包含租户隔离(PostgreSQL RLS)、LLM 多模型路由、Agent 运行时(工具/权限/MCP/技能/记忆/定时)、知识库(摄入/实体/图谱/快照/混合检索/wiki/多媒体)、运维可观测性，以及约 180 个 REST 端点。

**Nice Knowledge** 是本仓库随附的演示应用：一个基于 NiceKit 搭建的知识工作台，用来展示 SDK 的宿主接入方式和完整产品体验。


## 文档

| 文档 | 用途 |
|---|---|
| [docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) | 新项目如何复用:三十分钟起步、扩展点清单、常见配方 |
| [apps/demo/backend/README.md](apps/demo/backend/README.md) | 跑通 Nice Knowledge 后端:启动步骤、冒烟验证、celery 模式 |
| [apps/demo/frontend/README.md](apps/demo/frontend/README.md) | 跑通 Nice Knowledge 前端:页面、脚本、宿主扩展点 |
| [docs/MIGRATION-PLAN.md](docs/MIGRATION-PLAN.md) | 这套代码怎么来的:迁移蓝图与逐模块处置 |

## 结构

```text
packages/nicekit/     NiceKit SDK 包(core/tenancy/llm/kb/agent/capabilities/operations/runtime/api)
apps/demo/backend/    Nice Knowledge 示例宿主 + 四个扩展点的最小实现
apps/demo/frontend/   Nice Knowledge 示例前端(Next.js)
images/               品牌资产
deploy/               PostgreSQL 定制镜像(pgvector + pg_trgm + zhparser)
```

## 快速开始

```bash
docker compose up -d
cd packages/nicekit && uv run alembic upgrade head && cd ../..
cp apps/demo/backend/.env.example apps/demo/backend/.env
cd apps/demo/backend
uv run --package nicekit-demo-backend python -m demo_backend.seed
uv run --package nicekit-demo-backend python run.py
```
