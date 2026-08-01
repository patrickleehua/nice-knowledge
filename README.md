# nice-knowledge

多租户 **Agent + 知识库平台 SDK**(`nicekit`),从 TravelFlow AI 抽取的通用能力底座。
装上即得:租户隔离(PostgreSQL RLS)、LLM 多模型路由、Agent 运行时(工具/权限/MCP/技能/记忆/定时)、
知识库(摄入/实体/图谱/快照/混合检索/wiki/多媒体)、运维可观测性,以及约 180 个 REST 端点。

**不含任何行业业务逻辑** —— 业务通过扩展点接入。

## 文档

| 文档 | 用途 |
|---|---|
| [docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) | **新项目如何复用**:三十分钟起步、扩展点全清单、常见配方 |
| [apps/demo/backend/README.md](apps/demo/backend/README.md) | 跑通 demo:启动步骤、冒烟验证、celery 模式 |
| [docs/MIGRATION-PLAN.md](docs/MIGRATION-PLAN.md) | 这套代码怎么来的:迁移蓝图与逐模块处置 |

## 结构

```
packages/nicekit/     SDK 包(core/tenancy/llm/kb/agent/capabilities/operations/runtime/api)
apps/demo/backend/    示例宿主 + 四个扩展点的最小实现
apps/demo/frontend/   示例前端(Next.js)
deploy/               PostgreSQL 定制镜像(pgvector + pg_trgm + zhparser)
```

## 快速开始

```bash
docker compose up -d                                       # postgres 5433 / redis 6380 / minio 9010
cd packages/nicekit && uv run alembic upgrade head && cd ../..
cp apps/demo/backend/.env.example apps/demo/backend/.env
cd apps/demo/backend
uv run --package nicekit-demo-backend python -m demo_backend.seed
uv run --package nicekit-demo-backend python run.py
```
