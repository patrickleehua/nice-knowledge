# AGENTS.md

给在本仓库工作的 AI 编码助手。人类贡献者读 [CONTRIBUTING.md](CONTRIBUTING.md)。

> **接入 NiceKit 到别的项目**(不是改本仓库)?那份说明在
> [docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) —— 全部 import 路径、函数签名、
> 执行步骤表与反模式清单都在那里,**照抄它,不要凭记忆写 nicekit 的 API**。

## 这是什么

`nicekit` 是一个**多租户 Agent + 知识库平台 SDK**(Python 3.13+、FastAPI、
PostgreSQL-only)。`apps/demo` 是随附的示例宿主 Nice Knowledge,演示如何组装它。

```text
packages/nicekit/       SDK 包 —— 发到 PyPI 的就是它
apps/demo/backend/      示例宿主:create_app 装配 + 四个扩展点的最小实现
apps/demo/frontend/     示例前端(Next.js)
deploy/                 PostgreSQL 定制镜像(pgvector + pg_trgm + zhparser)
docs/                   全部文档,索引见 docs/README.md
```

uv workspace 单仓,members = `packages/nicekit` 与 `apps/demo/backend`。

## 常用命令

```bash
uv sync --all-packages                                    # 装依赖
docker compose up -d                                      # 起 postgres 5433 / redis 6380 / minio 9010
cd packages/nicekit && uv run --package nicekit alembic upgrade head   # 建表

uv run ruff check .                                       # lint(仓库根)
uv run ruff check --fix .

# 测试:必须 cd 到 packages/nicekit,见下方说明
cd packages/nicekit
uv run --package nicekit pytest                           # 1967 passed / 4 skipped / 53 deselected,~45s
uv run --package nicekit pytest -m live                   # 需要真实 DB / LLM API 的用例
uv run --package nicekit pytest tests/test_agent_loop.py -x

cd apps/demo/backend && uv run --package nicekit-demo-backend python run.py   # 起示例宿主
```

> **pytest 必须在 `packages/nicekit/` 下执行。** 配置(`asyncio_mode = "auto"`、`testpaths`、
> `addopts = "-m 'not live'"`)写在 `packages/nicekit/pyproject.toml`,仓库根没有
> `[tool.pytest.ini_options]`。从根目录跑会把根当 rootdir、一条配置都读不到,
> 结果是 **598 failed / 50 errors** —— 这不是代码坏了,是跑错了目录,**不要去"修"这些失败**。

改完代码,**至少**跑一遍 `uv run ruff check .` 和 `packages/nicekit` 下的 `pytest` 再交付。

## 架构约束(违反了就是错的)

**1. SDK 内部不 import 任何宿主代码。** `packages/nicekit/` 里不许出现订单、工单、
项目、客户这类行业概念。业务定制一律走扩展点(见 [docs/SDK-GUIDE.md §4](docs/SDK-GUIDE.md#4-扩展点全清单))。

**2. 分层依赖只能自下而上:**

```text
core → tenancy → llm → (kb | agent | capabilities) → operations/runtime → api
```

`kb`、`agent`、`capabilities` 三者**互不依赖**。反向 import 会成环。

**3. 凡带 `org_id` 的新表都要开 RLS**,用 SDK 的 helper,不要手写策略:

```python
from nicekit.migrations.rls import op_enable_org_rls
op_enable_org_rls(op, "your_table")   # ENABLE + FORCE + org_isolation 策略
```

**永远不要**写 `DISABLE ROW LEVEL SECURITY`,不要给数据库账号 `BYPASSRLS`,
不要为了"让测试能读到数据"绕过 RLS —— 正确做法是用
`org_session(factory, org_id)` 或 `get_org_session` 依赖拿一个绑好上下文的会话。
裸 `factory()` 会话没有 `app.current_org_id`,RLS 恒假,你会读到 0 行然后误判为"数据没写进去"。

**4. 第三方密钥落库前过 `core.secretbox`**,API 读出面永不返回明文密钥
(统一掩码 `********` + `has_api_key` 布尔)。

**5. 工具的 JSON Schema 必须严格**:所有字段进 `required`、`additionalProperties: false`,
可选参数用 `["string", "null"]` 表达而不是从 `required` 里拿掉。宽松 schema 会让模型的
参数幻觉静默丢参,而不是当场失败。

## 容易踩的坑

| 症状 | 原因 | 正确做法 |
|---|---|---|
| pytest 大面积失败(598 failed / 50 errors) | 在仓库根跑 pytest,读不到 `packages/nicekit/pyproject.toml` 里的配置 | `cd packages/nicekit` 再跑 |
| HTTP 200 但所有数据库后台任务静默失败(Windows) | uvicorn 在 win32 硬编码 `ProactorEventLoop`,psycopg async 不支持 | 走 `run.py`;脚本顶部调 `use_selector_event_loop_on_windows()` |
| 数出 18 条路由而不是 182 | FastAPI 0.141 起 `include_router` 不展开,`app.routes` 里是 `_IncludedRouter` | 用 `app.openapi()["paths"]` |
| `ToolRegistrationError: duplicate tool names` | 脚本里重复调 `build_app()` | `from myhost.main import app`,别再装配一次 |
| LLM provider 连通性 404 | `base_url` 漏了版本段 | base_url 必须带 `/v1`,如 `https://api.siliconflow.cn/v1` |
| `RuntimeError: 身份接线自相矛盾` | 注册了自定义 resolver 又挂了 auth router | `default_routers(exclude=AUTH_ROUTER_NAMES)` |
| 测试之间互相污染身份 | `principal_resolver` 是进程级全局 | teardown 里 `set_principal_resolver(None)` |

完整清单见 [docs/AGENT-BRIEF.md §4](docs/AGENT-BRIEF.md#4-不要这样做)。

## 代码风格

- ruff,`line-length = 100`,`target-version = "py313"`,规则集 `E,F,I,UP,B,SIM`
- 类型注解用 3.13 语法:`X | None`、`list[str]`,不要 `Optional` / `List`
- 注释写"为什么",不写"做了什么";代码讲不清的取舍才值得一行注释
- 文档与注释用中文,标识符与技术术语保持原文

## 提交约定

格式 `<类型>:<做了什么>`,冒号后不空格,描述用中文。
前缀限定 `feat:` / `fix:` / `update:` / `refactor:` / `docs:`。

例:`fix:修复分页在空数据时崩溃的问题`

**未经明确要求不要 commit,更不要 push。**

## 文档必须同步

`docs/` 里的数字与签名都声称"取自实际代码并已核实"。改了下面这些东西,连带改文档,
否则文档会变成假的:

| 改了 | 同步 |
|---|---|
| API 路由(增删端点 / router) | [docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) §2.3 §6、[docs/INTEGRATION.md](docs/INTEGRATION.md) §8 |
| 数据库表 / RLS 策略 | [docs/DATA-MODEL.md](docs/DATA-MODEL.md) |
| 扩展点签名 | [docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) §4、[docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) §2 |
| 对外行为 / 破坏性变更 | [CHANGELOG.md](CHANGELOG.md) 的「未发布」段落 |

## 前端

`apps/demo/frontend/` 有独立的 [AGENTS.md](apps/demo/frontend/AGENTS.md),
在那个目录下工作时以它为准(它用的 Next.js 版本与你的训练数据可能不一致,
写代码前先读 `node_modules/next/dist/docs/`)。
