# nicekit 使用指南

面向**新项目**:如何用 nicekit 在自己的业务里搭起一个多租户 Agent + 知识库平台。

> 想先跑起来看看?去 `apps/demo/backend/README.md`,那是一个可运行的最小宿主。
> 想知道这套代码怎么来的?去 `docs/MIGRATION-PLAN.md`。

---

## 1. 装上它能得到什么

nicekit 是**平台框架包**,不是工具库。安装并装配后,你的项目直接拥有:

| 能力 | 具体内容 |
|---|---|
| **多租户底座** | 组织/用户/成员/邀请、JWT + 旋转式 refresh token、PostgreSQL RLS 强隔离(FORCE,连表 owner 也受约束) |
| **LLM 多模型层** | OpenAI/Anthropic 双协议归一、45 个模型家族的能力注册表、模型 ID 归一化(聚合器前缀/Bedrock ARN/量化标签/日期戳)、路由与降级链、provider 冷却、org 级预算与并发闸门、四桶 token 计量 |
| **Agent 运行时** | 多轮工具循环、七轴权限审批、AI 复核员、MCP 客户端、SKILL.md 技能、子 agent 委派、长期记忆、会话目标续跑、上下文压缩、定时任务(到点起一个真实 agent run) |
| **知识库** | 文档摄入(解析/分块/上下文化/嵌入)、通用实体抽取与审核、快照发布与回滚、四路混合检索(结构化 + 图谱 + 稀疏 + 向量,RRF 融合)、知识图谱、wiki 生成、多媒体入库与审核、提示注入围栏 |
| **运维** | 健康/就绪探针、Prometheus 指标、服务心跳、provider 连通性探测、孤儿任务恢复、超时清扫、事务性发件箱 |
| **API** | 约 200 个 REST 端点(认证/成员/管理端/chat SSE/KB 全家/记忆/定时/通知/媒体) |

**明确不包含**:任何行业业务逻辑。没有订单、工单、项目、客户这些概念——它们由你定义,通过扩展点接进来。

**技术前提**(不可替换):PostgreSQL(需 pgvector + pg_trgm + zhparser + pgcrypto)、Redis、S3 兼容对象存储、Python 3.13+。Celery 可选(有 inline 兜底)。

---

## 2. 三十分钟起步

### 2.1 装依赖

```toml
# 你的 pyproject.toml
[project]
dependencies = ["nicekit"]

# 本仓库内开发时走 workspace
[tool.uv.sources]
nicekit = { workspace = true }
```

### 2.2 起基础设施

复制本仓库的 `docker-compose.yml` 与 `deploy/`(PostgreSQL 定制镜像,预装三个扩展)。
**不要用官方 postgres 镜像**:zhparser 中文分词与 pgvector 都需要编译进去。

### 2.3 建表

```bash
cd <nicekit 包目录> && uv run alembic upgrade head
```

迁移会建 64 张表、给 46 张带 `org_id` 的表开 FORCE RLS、创建中文检索配置、seed 平台组织。

### 2.4 最小宿主

```python
# main.py
from nicekit.api.v1.router import default_routers
from nicekit.runtime.app_factory import create_app

app = create_app(routers=default_routers())
```

```python
# seed.py —— 幂等,每次部署跑一次
from nicekit.core.db import get_session_factory
from nicekit.runtime.bootstrap import bootstrap_with_factory

await bootstrap_with_factory(get_session_factory())
```

`bootstrap_platform` 做的事:注册 17 个内置工具、seed 实体类型兜底(`concept`)、
seed KB 与 agent 的 prompt、建默认 agent 卡、校验 prompt 资源完整性。

### 2.5 启动

```python
# run.py —— Windows 上必须走这个入口
from nicekit.core.event_loop import use_selector_event_loop_on_windows
use_selector_event_loop_on_windows()
import uvicorn
uvicorn.run("main:app", host="127.0.0.1", port=8000)
```

> **为什么不能直接 `uvicorn main:app`**:Windows 默认的 ProactorEventLoop 不被
> psycopg async 支持。直接跑 uvicorn 时事件循环策略在 loop 建好之后才生效,
> 结果是 HTTP 照常响应、但 lifespan 里所有数据库后台任务**静默失败**。

到这里你已经有一个能登录、能配模型、能建知识库、能和 agent 对话的平台了。

---

## 3. 分层与依赖方向

```
core        config(分域) / db(RLS) / security + secretbox / cache / ratelimit / logging / metrics
  ↑
tenancy     组织·用户·成员·邀请 / JWT / 角色注册表 / 用量计量 / RLS 迁移 helper
  ↑
llm         provider 双协议 / 能力注册表 / ID 归一化 / 路由降级 / sinks
  ↑
kb ── agent ── capabilities        (三者互不依赖,都只向下依赖)
  ↑
operations / runtime               运维可观测性 / 装配·派发·可靠性·celery
  ↑
api                                REST routers
```

**铁律**:SDK 内部不 import 任何宿主代码。所有业务定制走第 4 节的扩展点。

---

## 4. 扩展点全清单

这是 SDK 与你的业务之间的**全部**接口。

### 4.1 让 agent 会做你的业务:自定义工具

```python
from nicekit.agent.tools import ToolRegistry, ToolContext, tool_permission
from nicekit.domain.agent_permission import (
    ToolEffect, ToolRisk, ToolCategory, ToolReversibility,
    ToolDelegation, PermissionScope,
)

my_tools = ToolRegistry("myapp")

@my_tools.register(
    "ticket_close",
    "关闭一个工单",
    {"type": "object", "properties": {"ticket_id": {"type": "string"}},
     "required": ["ticket_id"], "additionalProperties": False},
    permission=tool_permission(
        effect=ToolEffect.TRANSITION, risk=ToolRisk.SENSITIVE,
        categories=(ToolCategory.WORKFLOW,),
        reversibility=ToolReversibility.COMPENSATABLE,
        delegation=ToolDelegation.REVIEWABLE,      # 需 AI 复核员或人工批准
        scope=PermissionScope.RESOURCE,
        material_arguments=("ticket_id",),          # 参与授权指纹的实质参数
    ),
    label="关闭工单", category="工单",
)
async def ticket_close(ctx: ToolContext, args: dict) -> dict:
    ...

app = create_app(routers=default_routers(), tool_registry=my_tools)
```

**schema 约定**:所有字段进 `required`、`additionalProperties: false`。可选参数用
`["string", "null"]` 表达,而不是从 `required` 里拿掉——严格 schema 让模型的参数
幻觉当场失败,而不是静默丢参。

**七轴权限**决定这个工具是自动执行、走 AI 复核、要人工点确认、还是禁止 agent 调用。
七轴:`effect`(读写删/状态转换)、`risk`、`categories`、`reversibility`、
`delegation`、`scope`、`material_arguments`。

### 4.2 让权限层看懂你的业务对象:ResourceResolver

```python
from nicekit.agent.permissions.actions import register_resource_resolver

async def resolve_ticket(session, tool_name: str, arguments: dict) -> UUID | None:
    """把工具参数里的 ticket_id 解析成"作用域根",权限层据此判断跨作用域操作。"""
    ...

register_resource_resolver("ticket_id", resolve_ticket)
```

配套:`ToolPermissionSpec.creates_scope_root=True` 标记"这个工具会创建新的作用域根"。

### 4.3 给 agent 注入业务上下文:ContextProvider

```python
from nicekit.agent.service import register_context_provider, ContextBlock, ContextRequest

@context_provider("ticket_workspace", budget_chars=2500, timeout_seconds=2.0)
async def derive(session, ctx: ContextRequest) -> ContextBlock | None:
    """每轮对话往 system prompt 注入一段"当前工单状态"。"""
    return ContextBlock(block_id="ticket_workspace", text="...")
```

**三铁律**(SDK 强制):不落新表(每轮从业务真相表派生)、agent 不能写、
派生失败或超预算就整段丢弃且不阻塞对话。

### 4.4 会话与记忆的作用域:ScopeResolver

会话通过 `scope_type`(自由字符串,如 `"ticket"`)+ `scope_id`(UUID)绑定业务对象,
**无外键**。记忆的 scope 词表同理:

```python
from nicekit.agent.memory import set_scope_resolver, register_scope_label
from nicekit.models.memory import register_memory_scopes

register_memory_scopes("ticket", "account")
set_scope_resolver(my_resolver)   # (session, org_id, session_id) -> {"ticket": "<id>"}
```

### 4.5 KB 认得你的领域实体:实体类型注册

**不需要写代码建表**。一份 spec 即可,`attributes` 存 JSONB,
`filterable_fields` 声明的字段自动进入结构化检索:

```python
PRODUCT_TYPE = {
    "type_key": "product",
    "display_name": "产品",
    "field_schema": {...},              # JSON Schema Draft 2020-12
    "filterable_fields": [
        {"field": "category", "type": "text",   "label": "品类"},
        {"field": "price",    "type": "number", "label": "价格"},
    ],
    "card_template": "{{name}} · {{category}}",
    "review_policy": "ai",              # auto | ai | human
}

await bootstrap_platform(session, entity_type_specs=[PRODUCT_TYPE])
```

检索时:

```python
from nicekit.kb.search import search_kb, StructuredFilter, StructuredSearchQuery

hits = await search_kb(
    session, org_id, "轻薄本推荐",
    structured_filters=StructuredSearchQuery(
        type_keys=("product",),
        filters=(StructuredFilter("category", "eq", "笔记本"),
                 StructuredFilter("price", "max", 8000)),
    ),
)
```

未在 `filterable_fields` 声明的字段会被拒绝(防任意 JSONB 探测)。

### 4.6 其余扩展点速查

| 协议 | 位置 | 用途 |
|---|---|---|
| `ReferenceScanner` | `kb.ports` | KB 清除前统计外部引用,防误删仍被业务引用的知识 |
| `IncidentRecorder` | `kb.ports` | KB 运维事件记录(默认落 SQL) |
| `Notifier` | `capabilities.notify` | 站内信/邮件,可换成你的通知管道 |
| `TraceSink` / `UsageSink` | `llm.sinks` | LLM trace 与计量落账,可换 OTLP/ClickHouse |
| `RecoverableTask` | `runtime.reliability` | 进程重启后的孤儿任务恢复 |
| `CapabilityProbe` | `agent.service` | 能力就绪探测(不可用则从工具快照里摘掉并告知模型) |
| `BusinessFactsProvider` | `agent.service` | 给 AI 复核员补充业务事实 |
| prompt 多 root | `agent.prompts.loader` | `register_prompt_root(path)`,同 id 时宿主覆盖 SDK |
| 角色注册 | `tenancy.roles` | `register_roles("agent")` / `register_write_roles("editor")` |
| 派发任务 | `runtime.dispatch` | `register_task(name, celery_name, inline_fn)` |
| 周期任务 | `operations.schedules` | `register_system_schedule(...)`,自动进 beat 与运维面板 |
| 能力槽位 | `llm.capability_routes` | `register_capability_slot(task, capabilities)` |
| 结果渲染器(前端) | demo 前端 | `Record<toolName, Renderer>`,自定义工具结果的展示 |

---

## 5. 数据库与迁移

- **你的业务表自己管**:建议独立的 alembic 分支,或在 `env.py` 里合并两份 metadata。
- **凡带 `org_id` 的表都要开 RLS**,用 SDK 提供的 helper:

```python
from nicekit.migrations.rls import op_enable_org_rls, op_platform_read

def upgrade():
    op.create_table("tickets", ...)
    op_enable_org_rls(op, "tickets")          # ENABLE + FORCE + org_isolation 策略
```

- 应用账号与迁移账号分离,**两个都不要给 BYPASSRLS**。
- 请求链路:JWT 携带 `org_id` → `bind_org_context(session, org_id)` →
  每个事务自动 `SET LOCAL app.current_org_id` → RLS 策略过滤。
  未设置上下文时策略恒假(fail-closed,什么都看不到)。

---

## 6. 配置

`Settings` 按域组合:`CoreSettings` + `LlmSettings` + `KbSettings` + `AgentSettings` +
`CapabilitySettings`。全部通过环境变量注入,字段名即变量名(大写)。

必须覆盖的三项:

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | ≥32 字节随机串 |
| `NICEKIT_SECRET_KEY` | 密钥加密的 master key。**不配则密钥明文落库** |
| `DATABASE_URL` / `MIGRATION_DATABASE_URL` | 应用账号 / 迁移账号,分离 |

**嵌入/重排的凭证不走 env**:`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` 只是"首次部署
用了哪个模型"的指纹,换模型要走 reindex campaign。真正的凭证是在管理端建一个
provider 实例,再配 `kb.embedding` / `kb.search.rerank` 系统路由。

> **Base URL 必须带版本段**(如 `https://api.siliconflow.cn/v1`)。SDK 把它当完整
> 前缀直接拼 `/models`、`/embeddings`,少写 `/v1` 会得到一个没有上下文的 404;
> 连通性测试会在这种情况下附带 `hint=base_url_missing_version_path` 提示。

**接入 OpenAI 兼容网关**:`OPENAI_API_KEY` + `OPENAI_BASE_URL` 即可(`llm_providers`
表为空时内置实例自动用这两项;管理端配了表行则以表为准)。若网关不接受 Responses
的 `max_output_tokens`(症状:上游回 400 `upstream_error`),把该实例名加进
`LLM_OPENAI_OMIT_MAX_OUTPUT_TOKENS=["openai"]`——这是按实例生效的兼容开关,
不影响同协议的其他网关。

排查顺序:`/api/v1/ready` 看依赖 → `llm_traces` 表看每跳错误(含上游诊断码)→
服务端日志看 `上游拒绝请求` 的 WARNING(含被脱敏前的 upstream 原文)。

---

## 7. 安全须知

1. **第三方密钥落库前加密**(`core.secretbox`,Fernet)。未配 master key 时降级为明文
   直通并告警;已加密的值遇到缺失 key 时 **fail-closed 抛错**,绝不当明文用。
2. **API 读出面永不返回明文密钥**:统一掩码 `********` + `has_api_key` 布尔。
   "不修改密钥"的表达方式是把掩码原样回传。
3. **提示注入围栏**:入库文档经 `kb.guardrails` 围栏包裹并做可疑指令检测。
4. **审批 bundle 脱敏**:展示给用户的审批卡不含可执行参数,只有 `display_input`。
5. **agent 无人值守时**(定时任务、子 agent)自动把"问用户"降级为"拒绝",
   不会挂起等一个不存在的人。

---

## 8. 常见任务配方

| 我想… | 怎么做 |
|---|---|
| 加一个 agent 能调的业务动作 | §4.1 注册工具 + §4.2 注册 ResourceResolver |
| 让 KB 认识我的领域对象 | §4.5 注册实体类型(无需建表) |
| 换个大模型 | 管理端配 provider → 配模型路由(`/admin/model-route-tasks`);换嵌入模型会自动触发重嵌 campaign |
| 加一个业务角色 | `register_roles("agent")`;要能写 KB 再 `register_write_roles("agent")` |
| 接入 MCP 工具 | 管理端建 MCP server,agent 卡上绑定;权限优先读协议的 `read_only_hint` |
| 给 agent 加技能 | 在 `skills_dir` 下放 `<slug>/SKILL.md`(YAML frontmatter 需 name + description) |
| 定时让 agent 干活 | agent 调 `cronjob` 工具,或直接写 `icron_tasks` |
| 换通知渠道 | 实现 `Notifier` 协议,`set_notifier(...)` |

---

## 9. 边界:这个 SDK 不做什么

- 不提供任何行业业务模型(订单/工单/项目/客户……)
- 不做数据库无关抽象(强绑 PostgreSQL,这是刻意的)
- 不内置行业词表:实体类型、prompt 领域文案、检索过滤字段、联网搜索的领域触发词,
  全部由宿主注册。SDK 只带领域无关的兜底(`concept` 实体类型、通用政务合规词等)
- 不替你做前端。demo 前端是可裁剪的参考实现,不是发布的 npm 包
