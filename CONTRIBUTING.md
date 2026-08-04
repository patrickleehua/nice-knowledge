# 参与贡献

感谢你愿意为 NiceKit 出力。本文说明如何把开发环境跑起来、代码要满足什么标准、
以及一个改动从提交到合并需要走完哪些步骤。

## 目录

- [开发环境](#开发环境)
- [仓库结构](#仓库结构)
- [代码规范](#代码规范)
- [测试](#测试)
- [提交与 PR](#提交与-pr)
- [文档](#文档)
- [发布](#发布)
- [协议](#协议)

## 开发环境

**前提**:Python 3.13+、[uv](https://docs.astral.sh/uv/)、Docker、Node.js 20+ 与 pnpm(仅前端需要)。

```bash
git clone https://github.com/patrickleehua/nice-knowledge.git
cd nice-knowledge

# 1) 起基础设施:postgres 5433 / redis 6380 / minio 9010
docker compose up -d

# 2) 安装 workspace 全部依赖
uv sync --all-packages

# 3) 建表
cd packages/nicekit && uv run --package nicekit alembic upgrade head && cd ../..

# 4) seed + 启动示例宿主
cp apps/demo/backend/.env.example apps/demo/backend/.env
cd apps/demo/backend
uv run --package nicekit-demo-backend python -m demo_backend.seed
uv run --package nicekit-demo-backend python run.py
```

> **PostgreSQL 必须用 `deploy/` 里的定制镜像**,官方 `postgres` 镜像不带
> `pgvector` / `pg_trgm` / `zhparser`,迁移会直接失败。
>
> **Windows 上必须走 `run.py` 启动**,不要直接 `uvicorn demo_backend.main:app`。
> uvicorn 在 win32 硬编码 `ProactorEventLoop`,psycopg async 不支持它,
> 症状是 HTTP 正常响应、但 lifespan 里所有数据库后台任务静默失败。

## 仓库结构

```text
packages/nicekit/       SDK 包(发布到 PyPI 的就是它)
apps/demo/backend/      示例宿主:create_app 装配 + 四个扩展点的最小实现
apps/demo/frontend/     示例前端(Next.js,不发布 npm 包)
deploy/                 PostgreSQL 定制镜像与初始化 SQL
docs/                   全部文档,索引见 docs/README.md
```

**铁律:SDK 内部不 import 任何宿主代码。** 所有业务定制走
[docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) 第 4 节的扩展点。往 `packages/nicekit/` 里加
行业概念(订单、工单、客户……)的 PR 一律不收 —— 它们属于宿主。

分层依赖只能自下而上:

```text
core → tenancy → llm → (kb | agent | capabilities) → operations/runtime → api
```

## 代码规范

工具链是 [ruff](https://docs.astral.sh/ruff/),配置在 `packages/nicekit/pyproject.toml`
(`line-length = 100`,`target-version = "py313"`,规则集 `E,F,I,UP,B,SIM`)。

```bash
uv run ruff check .          # 检查
uv run ruff check --fix .    # 自动修
uv run ruff format .         # 格式化
```

其他约定:

- **类型注解用 3.13 语法**:`X | None` 而不是 `Optional[X]`,`list[str]` 而不是 `List[str]`。
- **工具的 JSON Schema 必须严格**:所有字段进 `required`、`additionalProperties: false`,
  可选参数用 `["string", "null"]` 表达。严格 schema 让模型的参数幻觉当场失败,
  而不是静默丢参。
- **凡带 `org_id` 的新表都要开 RLS**,用 `nicekit.migrations.rls.op_enable_org_rls(op, "表名")`,
  不要手写策略。任何 `DISABLE ROW LEVEL SECURITY` 或给账号 `BYPASSRLS` 的改动都不会被合并。
- **第三方密钥落库前必须过 `core.secretbox`**,API 读出面永不返回明文密钥。
- 注释写"为什么",不写"做了什么";代码本身讲不清的取舍才值得一行注释。

## 测试

> **必须在 `packages/nicekit/` 目录下跑。** pytest 配置(`asyncio_mode = "auto"`、
> `testpaths`、`addopts = "-m 'not live'"`)写在 `packages/nicekit/pyproject.toml`,
> 而仓库根的 `pyproject.toml` 没有 `[tool.pytest.ini_options]`。从根目录执行时 pytest
> 会把根当 rootdir、一条配置都读不到,结果是 **598 failed / 50 errors** ——
> 这不是代码坏了,是跑错了目录。CI 里也用 `working-directory: packages/nicekit`。

```bash
cd packages/nicekit

# 默认套件:1967 passed / 4 skipped / 53 deselected,约 45s,不需要任何外部服务
uv run --package nicekit pytest

# 单个文件
uv run --package nicekit pytest tests/test_agent_loop.py -x

# 需要真实数据库 / LLM API 的用例,显式开启
uv run --package nicekit pytest -m live
```

`live` marker 标记的用例依赖真实外部服务(LLM API / 数据库),默认跳过(53 个)。
新增这类用例时务必打上 marker,否则会让别人的 CI 红掉。

改了行为就要有对应用例。修 bug 的 PR 请先写一个能复现该 bug 的失败用例,再修它。

## 提交与 PR

**提交信息格式**:`<类型>:<做了什么>`,冒号后不空格,描述用中文。

| 前缀 | 用于 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 修复 bug |
| `update:` | 改进 / 调整现有功能 |
| `refactor:` | 重构(不改变行为) |
| `docs:` | 文档 |

示例:`fix:修复分页在空数据时崩溃的问题`

**PR 检查清单**:

- [ ] `uv run ruff check .` 通过(仓库根)
- [ ] `cd packages/nicekit && uv run --package nicekit pytest` 通过
- [ ] 涉及数据库变更时附带 alembic 迁移,且带 `org_id` 的表已开 RLS
- [ ] 改动影响到的文档已同步(见下节)
- [ ] 破坏性变更已在 PR 描述里写明,并在 [CHANGELOG.md](CHANGELOG.md) 的「未发布」段落登记

## 文档

文档不是附属品:`docs/` 下的数字与签名都声称"取自实际代码并已核实"。
改了下列东西,请连带更新对应文档,否则文档会变成假的:

| 你改了 | 需要同步 |
|---|---|
| API 路由(增删端点 / router) | [docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) §2.3 §6、[docs/INTEGRATION.md](docs/INTEGRATION.md) §8 的端点数 |
| 数据库表 / RLS 策略 | [docs/DATA-MODEL.md](docs/DATA-MODEL.md) |
| 扩展点签名 | [docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) §4、[docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) §2 |
| 租户接入相关行为 | [docs/INTEGRATION.md](docs/INTEGRATION.md) 与 [docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) |
| 面向 AI 编码助手的仓库约定 | [AGENTS.md](AGENTS.md) |

## 发布

发版流程见 [docs/RELEASING.md](docs/RELEASING.md)。只有维护者能打 tag 触发发布。

## 协议

提交 PR 即表示你同意你的贡献以 [Apache License 2.0](LICENSE) 授权。
