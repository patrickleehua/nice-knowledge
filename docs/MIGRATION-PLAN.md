# nice-knowledge 平台 SDK 迁移改造蓝图

> 版本:v1.0 · 日期:2026-07-31
> 源仓库:`e:\workplace\TravelFlow AI`(下文简称 TF,git 基线 `50b8c86`)
> 目标仓库:`E:\workplace\nice-knowledge`(本仓库)
> 本文档是全部迁移子任务的唯一规格真源。所有 file:line 引用均指 TF 仓库 `backend/app/` 与 `frontend/src/` 下的路径。

---

## 1. 目标

把 TF 中已验证的通用能力抽取为可复用的多租户 Agent+KB 平台 SDK,供后续新项目直接安装使用:

- **Agent 子系统**:agent loop、工具注册、权限审批(七轴)、MCP、skills、子 agent、长期记忆、定时任务(icron)、会话目标续跑、上下文压缩。
- **KB 子系统**:文档摄入(解析/分块/上下文化/嵌入)、通用实体抽取、事实审核、快照发布/回滚、四路混合检索、图谱、wiki、多媒体、防注入围栏。
- **LLM 多模型层**:双协议 Provider、模型能力注册表、模型 ID 归一化、路由/降级/冷却/预算、四桶计量。
- **租户底座**:组织/用户/成员/邀请、JWT+旋转 refresh、PostgreSQL RLS 强隔离。
- **前端 demo**:登录 + chat 全交互 + KB 工作台 + admin 配置区,作为 SDK 的可视化测试台。

**不搬运**(留在 TF,永不出现在本仓库):旅游业务 pipeline(demand/itinerary/quote 节点与状态机)、报价引擎、OTA、渲染文档工厂、履约、客户 CRM、线路资产、5 张旅游 KB 专表、44 个旅游业务工具、working_context.py、旅游 prompt 文案。

## 2. 三个架构决策(已确认)

1. **新仓库抽取式**:从 TF 复制通用代码并清理,不在 TF 原地做手术。无存量数据 → 5 张专表的数据迁移成本归零,直接写全新 baseline schema。
2. **uv workspace 单 SDK 包 + demo 宿主**:一个分层 SDK 包 `nicekit`(pip 可安装)+ `apps/demo`(组装示例)。不拆多包,内部模块边界清晰,未来可再拆。
3. **平台框架包形态,PostgreSQL-only**:SDK 自带 models、alembic 迁移、FastAPI routers、workers;宿主用 `app_factory()` 组装。明确依赖 PG(RLS/pgvector/zhparser)、Redis、MinIO、Celery,不伪装数据库无关。

## 3. 目标仓库结构

```
nice-knowledge/
├── pyproject.toml                  # uv workspace 根
├── docker-compose.yml              # postgres(pgvector+zhparser)/redis/minio,复制自 TF
├── deploy/                         # PG 定制镜像与 init SQL,复制自 TF deploy/
├── docs/
│   └── MIGRATION-PLAN.md           # 本文档
├── packages/
│   └── nicekit/                    # SDK 包
│       ├── pyproject.toml
│       └── src/nicekit/
│           ├── core/               # config/db/security/cache/ratelimit/logging/metrics/event_loop
│           ├── tenancy/            # models + auth/members API + RLS helper + Role 注册
│           ├── llm/                # providers/normalizer/capability_registry/catalog/routing/
│           │                       # connectivity/service(TraceSink/UsageSink 协议)
│           ├── kb/                 # ingest/extract/entities/graph/retrieve/media/wiki/guardrails/storage
│           ├── agent/              # loop/permissions/tools(Registry+通用工具)/mcp/skills/subagents/
│           │                       # memory/icron/session_goal/prompts/compression
│           ├── capabilities/       # websearch/weather/imagegen/notify
│           ├── runtime/            # app_factory/dispatch 注册表/reliability 框架/celery 装配
│           ├── api/                # 通用 routers(auth/members/admin/chat/kb*/memory/icron/…)
│           ├── domain/             # 通用契约(agent_permission/agent_plan/kb/kb_media/model_catalog/common)
│           ├── models/             # SQLModel 表定义(分模块,聚合器仅供 alembic)
│           └── migrations/         # 全新 squashed baseline + RLS helper
├── apps/
│   └── demo/
│       ├── backend/                # 组装 nicekit + 示例扩展(自定义工具/实体类型/seed)+ run.py
│       └── frontend/               # 裁剪后的 Next.js demo(P5 从 TF frontend 复制裁剪)
└── README.md
```

**Python 包命名**:`nicekit`(如需改名全局替换,当前以此为准)。TF 中 `app.xxx` 的 import 全部改写为 `nicekit.xxx`。

## 4. 扩展点协议(SDK ↔ 宿主边界)

SDK 内部禁止 import 任何宿主/业务代码。业务定制一律走以下协议(定义在 `nicekit/` 各子包的 `ports.py` 或 `protocols.py`):

| 协议 | 替代的 TF 现状 | 规格 |
|---|---|---|
| `ToolRegistry`(类实例) | `tools.py:224` 模块级 `TOOLS` dict + `validate_builtin_tool_registry`(:947)三方完全相等校验 | `register_tool()` 装饰器挂在 registry 实例上;校验改为"每个已注册工具必须有元数据",允许多来源合并;label/category 并入 `ToolDef` 本体 |
| `ContextProvider` | `service.py:429-452` 硬接线 working_context | `async derive(session, scope) -> ContextBlock \| None`,带字符预算与超时,失败不阻塞(参照 working_context.py:7-12 三铁律) |
| `ScopeResolver` | `services/customer.py` 的 `resolve_session_scope_refs/resolve_project_scope_refs` 被 `agent/memory.py:46`、`agent/service.py:138` 回调 | 会话/记忆 scope 解析;`MemoryScope` 词表泛化为 `org / {custom}`(models/memory.py:37) |
| `ResourceResolver` | `permissions/actions.py:58-66` RESOURCE_ARGUMENTS + `:223-274` `_resource_project` 七分支 SQL | 宿主注册 `{参数名: async resolver}` 映射;`creates_scope_root` 改为 `ToolPermissionSpec` 布尔字段(替代 `:327` 的工具名硬编码) |
| `ReferenceScanner` | `kb/reference_registry.py:13-25`、`kb/projection_gc.py:11-19` import 业务表 | KB purge/GC 前的外部引用计数,宿主注册实现;通用 JSON 递归扫描器 `reference_kinds`(:83)保留在 SDK |
| `TraceSink` / `UsageSink` | `llm/service.py:501 _record` 直落 LlmTrace/UsageDaily | Protocol + 默认 SQLModel 实现;`_check_budget`/`_record` 中手写 `set_config('app.current_org_id')`(service.py:479-482/521-524)移除,由注入的 session provider 负责 |
| `Notifier` | `services/notify.py` 被 `agent/icron.py:53`、`kb/expiry.py:875-891` 硬依赖 | `async notify(org_id, user_ids, kind, title, body, link)`;链接与文案参数化(如 icron.py:74 `NOTIFY_LINK`) |
| `RecoverableTask` | `services/reliability.py`(348 行,深度业务耦合) | 框架化:`list_orphans/fail/requeue` 协议 + `_list_org_ids`+`org_session` 逐租户骨架保留;业务任务类型由宿主注册 |
| Prompt 多 root | `prompts/loader.py:22` 固定包内 resources/ | 支持宿主资源目录叠加搜索;`assembly.py:11-16` 全局块清单可配置 |
| 实体类型 seed | `kb/entity_types.py:138-404` BUILTIN_ENTITY_TYPES 九个旅游类型 | `ensure_builtin_entity_types(:407)` 改为接受外部 spec 列表;SDK 默认只 seed `concept` 兜底类型 |
| 声明式检索过滤 | `kb/search.py:163-183` StructuredSearchFilters(hotel_star 等) | 泛化为 `StructuredFilter(field, op, value)` 列表,按 `KbEntityType.filterable_fields` 校验(复用 `entity_lookup.build_entity_filters`) |
| LLM task 注册 | `prompt_catalog.py` 内置目录 | `task -> prompt + route` 开放注册,非内置标 `builtin=False`(机制已存在) |

## 5. 各子系统迁移规格

### 5.1 core(P0/P1)

来源 `TF backend/app/core/`,除 config/metrics 外基本原样搬运:

| 文件 | 处置 |
|---|---|
| `event_loop.py`(10 行) | 原样(Windows psycopg selector loop 必需) |
| `db.py`(52 行) | 原样搬;`bind_org_context`(:32)/`org_session`(:48)是租户隔离核心;engine 增加 pool_size/max_overflow/pool_recycle 可配参数 |
| `security.py`(48 行) | 原样搬 + **新增密钥加密模块 `secretbox.py`**:Fernet + 环境变量 master key(`NICEKIT_SECRET_KEY`),提供 `encrypt_secret/decrypt_secret`;预留 KMS 接口。`llm_providers.api_key`、`service_configs.payload` 密钥字段、`mcp_servers.headers/env` 落库前加密 |
| `cache.py`(119 行) | 原样(Redis 熔断 `_down_until` 设计保留);加可选 namespace 前缀参数 |
| `ratelimit.py`(102 行) | 原样;`_EXEMPT_PATHS` 改为可配置 |
| `logging.py`(132 行) | 原样(纯 ASGI,不缓冲 SSE) |
| `metrics.py`(250 行) | **只搬骨架 ~15 行**(registry/render_metrics/metrics_endpoint + SERVICE_HEARTBEAT_AGE);全部 KB_* 业务指标改由 kb 子包自行注册到传入的 registry |
| `config.py`(332 行) | **按域拆分**:`CoreSettings`(db/redis/jwt/cors/log/task_dispatch_mode/platform_org_id)、`LlmSettings`(llm_* 全部)、`KbSettings`(kb_* 全部,剔除无用项)、`AgentSettings`(skills_dir/icron/agent 相关)、`CapabilitySettings`(websearch/imagegen/weather;删除 ota_provider/rapidapi_booking_host 等旅游项)。组合进 `Settings`,保持 `get_settings()` 兼容入口,env 变量名不变 |

**platform_org_id 参数化**:TF 硬编码 `00000000-0000-0000-0000-000000000001`(config.py:22,且写死进迁移 SQL)。新 baseline 里由 settings 提供,迁移/策略 SQL 用绑定参数或 seed 时读配置。

### 5.2 tenancy(P1)

来源 `models/tenancy.py`(184 行)、`api/deps.py`(56 行)、`api/v1/auth.py`(255 行)、`api/v1/members.py`(228 行)、`migrations/versions/ae23a2c746c9_tenancy.py` 的 RLS 策略 SQL。

改造点:
1. **Role 泛化**:内置只留 `platform_admin`/`org_admin`/`member`;提供 `register_roles()` 注册机制,`ASSIGNABLE_ROLES`(members.py:28)由注册表派生。删除 sales/operator/pricer/reviewer。
2. **RLS helper**:新增 `nicekit/migrations/rls.py` 提供 `op_enable_org_rls(table)`(ENABLE + FORCE + org_isolation 策略,含 `NULLIF(current_setting('app.current_org_id', true), '')::uuid` fail-closed 写法)与 `op_platform_read(table)`(平台只读旁路,参照 TF `d4a7e0b2c913_usage_platform_read.py`);一致性检查脚本:凡带 org_id 的表必须已开 RLS。
3. 双 DB 角色沿用:`database_url`(app)/`migration_database_url`(migrator),都无 BYPASSRLS。
4. 搬 `tests/test_rls_isolation.py` 并适配。
5. `AuditLog`/`Notification`/`UsageDaily` 三张 RLS 表随 tenancy 走;`usage.py`(78 行)原样搬(修包级 import)。

### 5.3 llm(P1)

来源 `services/llm/` 全部 + `models/llm.py`/`llm_provider.py` + `domain/model_catalog.py`。

| 文件 | 处置 |
|---|---|
| `providers.py`(1171 行) | 原样(零 app 依赖)。注意保留文件头"勿回退"注释:不用服务端约束解码、必须流式、伪 400 重试(`_is_gateway_upstream_failure` :242) |
| `model_id_normalizer.py`(302 行) | 原样(零依赖) |
| `model_capability_registry.py`(355 行) | 原样(REGISTRY_REVISION 机制保留) |
| `model_catalog.py` / `connectivity.py` | 原样(它们已正确用模块级 import) |
| `registry.py` | **修 `:16` 包级 `from app.models import` → `from nicekit.models.llm import ModelRoute, Prompt`** |
| `service.py` | **修 `:25` 包级 import**;`_record`/`_check_budget` 抽 TraceSink/UsageSink 协议(默认实现仍落表);`_org_semaphores`/`_provider_cooldowns` 模块级全局改为 LLMService 实例属性;`org_id` 参数保留(本 SDK 就是多租户定位) |
| `capability_routes.py` | 四个槽位常量 `kb.embedding`/`kb.search.rerank`/`kb.image.caption`/`llm.default` 改为槽位注册表,默认注册这四个(名字保持,KB 子包依赖) |
| `runtime_config.py`(139 行) | 模块级全局(`_overrides/_providers/_model_routes`)改为 `RuntimeConfig` 类 + 默认单例,保持现有函数式入口兼容 |
| `prompt_catalog.py` | 只保留目录机制与 `agent.approval_review` 一条;开放注册 |

搬运 TF 的 `tests/test_llm_*.py`(9 个)+ `test_prompt_catalog*.py`。

### 5.4 agent(P2)

来源 `services/agent/` 全目录 + `models/{chat,agent_permission,mcp,memory,icron}.py` + `domain/{agent_permission,agent_plan}.py` + `api/v1/{chat,agent_permissions,memory,icron}.py`。

**近乎原样搬运**(约 3200 行):`loop.py`(注意 :818/:1118 的 `image_generate` 特判改为 `ToolDef.emit_start_progress` 标志位)、`context_budget.py`、`mcp_manager.py`、`skills.py`、`sub_agents.py`(:306-309 项目 ID 注入泛化为 scope 注入)、`run_inputs.py`、`user_input.py`、`terminal.py`、`tool_trace.py`(噪音工具名单可配)、`runtime_tools.py`(NOT_RUNTIME_GRANTABLE 改 ToolDef 标志位)、`session_goal.py`(GOAL_CONTINUATION_TEXT 文案中性化)、`icron.py`(notify 走 Notifier 协议、NOTIFY_LINK 可配)、`permissions/` 七文件(见下)、`domain/agent_permission.py`、`domain/agent_plan.py`。

**tools.py 拆分**(TF :1-3837):
- 进 SDK 的 17 个通用工具(TF 的 63 个内置工具中,通用部分。最初调研称 19 个,其中 `kb_retrieve`(返回业务分桶结果)与 `business_media_select`(业务选图桥接)经业务耦合复核判定为业务工具,故为 17):`kb_list/kb_search/kb_image_inspect/retrieval_get`、`web_search/web_fetch/image_generate`、`skills_list/skill_view`、`ask_user`、`plan_update/update_goal/cronjob`、`memory_search/memory_write/memory_forget`、`weather_get`(描述文案去旅游化)。注:`kb_retrieve`/`business_media_select` 是业务工具,不搬。
- `ToolRegistry` 类替代模块级 `TOOLS`(:224);`TOOL_META`(:227-291)并入 ToolDef;`BUILTIN_TOOL_PERMISSIONS`(:315-904)只保留这 17 个通用工具的声明;`validate_tool_permission_spec`(:907)保留。
- `ToolContext`(:144-163):`chat_session` 保留;`pipeline_run_ids` 泛化为 `cancellable_resources: set` + 终结回调;角色检查(`PRICE_ROLES`/`_require_pricer` :119/:2284)删除,提供 `RoleChecker` 注入点。

**permissions/ 改造**:
- `actions.py`:RESOURCE_ARGUMENTS(:58-66)与 `_resource_project`(:223-274)删除,换 `ResourceResolver` 注册;`creates_project = tool.name == "project_create"`(:327)改 `ToolPermissionSpec.creates_scope_root`;业务模型 import(:17-27)删除;MATERIAL_ARGUMENT_NAMES(:44-57)去掉 offer_index/unit_cost,支持宿主追加。
- `policy.py`:`mutates_existing_project_from_general`(:627-638)泛化为"scope root 不匹配"规则。
- `reviewer.py`:APPROVAL_REVIEW_SYSTEM_PROMPT(:28-46)模板化。
- `management.py`:`_known_tools`(:90)改查注入的 ToolRegistry。

**prompts 模板化**(`prompts/resources/` 7 文件 + `assembly.py` + `loader.py`):
- `global_intro.md`:产品身份改 `{{product_identity}}` 变量,默认中性文案("多租户 AI 工作台助手")。
- `global_core_rules.md`:保留通用诚实性规则(不编造、引用来源、金额带币种改为"数值精确引用"),删旅行社口吻/酒店航班/签证条目。
- `global_tool_usage.md`/`global_output_style.md`:高危动作示例改由宿主 extra_sections 注入,默认给通用示例。
- `agent_role_generic.md`:领域无关兜底角色。
- `memory_extraction.md`:模板化,示例换通用场景(去掉签证/票价/大巴)。
- `compression.py:43-50` SUMMARY_SYSTEM_PROMPT 移入 prompt 资源并中性化;`SUMMARY_TASK`/`MEMORY_TASK`(compression.py:41、memory.py:91)默认 `agent.default`,可配。
- `seed.py`:WORKBENCH_SYSTEM_PROMPT(:81-147)等旅游 SOP 不搬;SDK 提供一个中性默认卡(通用助手,启用全部 17 个通用工具),demo 的 seed 在 apps/demo。

**service.py 改造**:
- `stage.update` 事件(:1362-1378 按工具名前缀推导旅游阶段)删除;改为 `ToolDef.stage` 自声明 + 宿主事件钩子(前端对应删 stage 分支)。
- `:1339-1341` 直读 Project 状态删除。
- `_resolve_working_context`(:429-452)改为遍历注册的 ContextProvider。
- `_resolve_memory_recall`(:480-529)走 ScopeResolver。
- `business_facts`(:1272-1285 conversation_mode/bound_project_id)改为宿主注入的 `Callable[[ChatSession], dict]`。
- 缺省卡名 `workbench`(:233/:243)可配,默认 `assistant`。
- websearch/imagegen 就绪度探测(:580-632)改 `CapabilityProbe` 注册表(capabilities 子包默认注册)。

**4 条硬 FK 泛化**(models 层):`chat_sessions.project_id/customer_id`(chat.py:181/:194)、`agent_approval_policies` 的 project FK(agent_permission.py:277)、`icron_jobs.project_id`(icron.py:135)→ 统一改为 `scope_type: str | None` + `scope_id: UUID | None`,无 FK;`origin_mode` 取值 `general/scoped`。

### 5.5 kb(P3)

来源 `services/kb/`(60 文件)+ `models/kb.py`/`kb_feedback.py` + `domain/kb.py`/`kb_media.py` + `api/v1/kb*.py`。

**原样搬运**(通用主体):`ingest_runs.py`、`ingestion.py`(删 EXTRACTION_SPECS 后)、`parsers/` 全部、`chunker.py`、`contextualizer.py`、`embedding.py`、`embedding_reindex.py`、`snapshot.py`、`effective_scope.py`、`eligibility.py`、`retrieval_projection.py`、`rerank.py`、`evidence_locator.py`、`entity_types.py`(除 BUILTIN 数据)、`entity_lookup.py`、`entity_resolution.py`、`fact_review_ai.py`、`review_sweep.py`、`review_settlement.py`、`dedup_suggestions.py`、`graph_projection.py`、`graph_search.py`、`wiki_review.py`、`guardrails.py`、`storage.py`、全部 `image_*.py` + `snapshot_media.py` + `caption.py`、`outbox.py`、`document_lifecycle.py`/`lifecycle.py`/`document_purge.py`/`document_reingestion.py`/`purge_due.py`、`projection_gc.py`(引用检查走 ReferenceScanner)、`orphan_audit.py`、`health_sweep.py`、`status_board.py`、`expiry.py`(通知走 Notifier)、`lint.py`(:161-165 五模型实体名解析改查 KbEntity.name)、`prompts_seed.py`(只留通用 task)。

**删除/重写**(编号对应调研报告 B 清单):
- B1:5 张专表 `Destination/Poi/HotelPoolEntry/CostReference/RouteTemplate`(models/kb.py:717-903)不搬,统一走 `KbEntity`(:955)。
- B2/B3:`KbType` 枚举(:47-57)删除(字段保留为自由 tag 或直接移除);`DocType`(:59-65)收敛为 `{unclassified, general}` + 任意注册类型 key。
- B4/B5:`EXTRACTION_SPECS`(ingestion.py:685-695)删除,`_resolve_extraction_spec`(:773)永远走 `kb.extract.generic`;domain/kb.py 只保留 `ConfidentItem/GenericExtractedItem/GenericEntityExtraction`(:28/:163/:188)与 `IngestProfile`(预设换中性示例)。
- B6/B7:`SqlProjectionBuilder` 五段业务分支(projections.py:483-579)删除,只保留 `is_custom` 通用路径(:580-603);`normalize_route_template_payload`(:189-204)删除。
- B8:`entity_cards.py` 整文件删除,保留 `card_source_ref/parse_card_ref` 骨架,渲染走 `entity_types.render_entity_card`(:96)。
- B9-B14:`search.py` 重写业务部分——`StructuredSearchFilters/QuoteFilters`(:163-183)换泛化 `StructuredFilter` 列表;`_structured_candidates` 六段硬编码召回(:1367-1648)重写为对 KbEntity 的单一泛化召回(按 type_key 分组 + filterable_fields 声明式打分);酒店星级全套(:126-128/:141-143/:1166-1187/:1291-1335)、线路天数容差(:131/:1583-1596)、报价可答性(:1220-1290)删除。**四路 RRF 框架、稀疏容错、双读迁移、rerank 链全部保留**。
- B15/B16:`travelflow_zhparser` regconfig 名(search.py:111、graph_search.py:22)参数化为 `settings.kb_fts_regconfig`;同义词表(:122-125)可配置。
- B17/B18:BUILTIN_ENTITY_TYPES 九个旅游类型移到 demo 的示例 seed;entity_binding 白名单(:45-54)默认只留 `concept`,其余从注册表读。
- B19:`graph.py _TYPE_AFFINITY`(:31-47)改注入参数,默认全 0.5。
- B20:`wiki_gen.py VALID_PAGE_TYPES`(:82-84)改开放字符串。
- B22:`answer.py KNOWLEDGE_ANSWER_SYSTEM_PROMPT`(:42-55)中性化 + 可配。
- B26:`business_media.py` 不搬(业务桥接层)。
- B29:`api/v1/kb.py` 五类专属 CRUD(:1146-1173/:1224)删除,统一走 `kb_entity_types.py` 的通用实体 CRUD。
- B32:`EMBEDDING_DIM = 1024`(models/kb.py:44)参数化(初始化时指定,换模型走 reindex campaign)。

### 5.6 capabilities(P2 随 agent)

`services/websearch/`、`services/weather/`、`services/imagegen/`、`services/notify.py` 整体搬运(多 provider 适配层,架构一致:契约 + provider + 可降级)。`websearch/service.py:489` 对 kb.rerank 的依赖保留(SDK 内部)。`notify` 抽为 Notifier 协议默认实现。

### 5.7 runtime + api 装配(P4)

- `main.py` → `runtime/app_factory.py`:`create_app(settings, *, routers=[], background_loops=[], startup_hooks=[], tools=..., context_providers=..., ...)`;中间件顺序保留(CORS 最外→请求日志→限流最内,纯 ASGI);lifespan 的 9 个业务扫描器改为 `background_loops` 注册;修复停机 `finally` 用 `asyncio.gather(*tasks, return_exceptions=True)`。
- `services/dispatch.py` → 任务注册表:`register_task(name, celery_name, inline_fn)`,保留"celery 失败回退 BackgroundTasks"语义。
- `services/reliability.py` → RecoverableTask 框架(见第 4 节)。
- `workers/` → celery 装配保留("延迟导入 + asyncio.run"模式),beat schedule 构建器接受注册的周期任务。
- `api/`:搬通用 routers——`auth/members/admin(含 mcp_router)/chat/agent_permissions/kb 全家/memory/icron/notifications/media/health`;不搬 `projects/customers/itineraries/quotes/ota/renders/render_kinds/templates/shares/public_shares/quality/route_assets`。
- `models/__init__.py` 聚合器只供 alembic autogenerate 使用(`migrations/env.py` 引用),运行时代码一律模块级 import。
- 迁移:全新 squashed baseline(约 60 张通用表),RLS 用 helper 统一施加;不继承 TF 的 99 个历史迁移。

### 5.8 前端 demo(P5)

从 TF `frontend/` 复制后裁剪。目标形态:

```
/login                         登录(邮箱+密码+org_slug)
/app/chat                      agent 全交互(会话/工具卡/计划/审批/反问/权限/思考等级/模型/联网/输入队列/目标)
/app/kb                        知识检索 + AI 问答流
/app/icron                     定时任务(保留,通用能力展示)
/app/notifications             通知
/org/kb, /org/kb/[id]          知识库管理 + 9 视图工作台
/org/members                   成员与角色
/org/settings/agent-permissions 组织级 Agent 权限策略
/admin/*                       全区保留(租户/提供商/模型路由/MCP/Agent卡/技能/Prompt/诊断/运行日志/用量/价格/服务)
```

**删除**:`app/app/{projects,customers,exports}`、`app/org/{route-assets,templates}`、`app/share`、`app/org/settings/customer-memory`、`components/{projects,customers}`、`components/org/template-*`、`components/agent/{business-result*,project-picker,stage-panel,general-panel}.tsx`、`lib/{stage-b,customer}.ts`(stage-b 的 `errMsg()` 移入 lib/utils.ts)。

**改造**:
- `app/app/chat/page.tsx`(1724 行):删 project 模式/ProjectPicker/深链,目标 ~1100 行;顺手拆 SSE 状态机/队列缓存/权限延迟应用三个 hook。
- `components/agent/run-sections.tsx`:删 STAGE_LABELS 与 stage 时间线分支;`<BusinessResult>` 换可注册结果渲染器(`Record<toolName, Renderer>` 由宿主注入,demo 提供 JSON 默认渲染)。
- `components/agent/tool-presentation.ts`:删 BUSINESS_KINDS/逐工具文案,改通用 JSON 摘要 + 注册表。
- `components/agent/tool-run-card.tsx`:删 `components/projects/selected-knowledge-media` import。
- `components/agent/agent-conversation.tsx`:EMPTY_PRESETS 换通用示例(props 可传)。
- `lib/chat.ts`:删 `stage.update` 事件成员与旅游 TOOL_LABELS;`lib/agent-events.ts`:删 stage 分支(~15 行)。
- `lib/types.ts`(42KB):拆分,保留 KB/auth/模型部分;`KB_TYPE_PRESETS/DOC_TYPES` 改后端下发或通用预设。
- `lib/auth.ts`:Role 收敛为 `platform_admin | org_admin | member`。
- `components/kb/route-ingestion.ts` + `views/sources-view.tsx`:删线路重分类;`search/frontline-*`、`entity-hits.tsx`:回落通用 chunk/entity 命中展示;`org/kb/page.tsx` KB_TYPE_VISUALS 换通用图标。

保留的核心契约层(不动):`lib/api.ts`(fetch+单飞 refresh+postSse)、`lib/agent-events.ts`(事件投影+测试)、`lib/{agent-permissions,org-agent-permissions,approval-decisions,chat-session-state}.ts` 及全部 `.test.mjs`。

## 5.9 跨波次接线约定(执行中沉淀,后续波次必读)

前序波次留下的、必须由后续波次接上的口子。每完成一项就在此打勾。

| # | 约定 | 责任波次 |
|---|---|---|
| A1 ✅ | ~~`nicekit/models/operations.py` 与 `services/operations/` 尚未迁移~~ **P4 完成**:`models/operations.py`(三表)+ `nicekit/operations/`(runtime/incidents/probes/schedules/diagnostics/readiness/worker_heartbeat);`SqlIncidentRecorder` 由 `runtime.bootstrap.install_default_ports()` 注册;`SERVICE_HEARTBEAT_AGE` 在心跳写入与诊断采集两处刷新;新表走**增量迁移** `cb3e134da690`(不改 baseline),`kb_operational_incidents` 已开 FORCE RLS。`schedules` 由元组改注册表,删业务任务 `fulfillment.daily_reminders`。 | P4 |
| A2 ✅ | ~~装配期必须显式 `import nicekit.agent.builtin_tools`~~ **P4 完成**:收进 `runtime.bootstrap.install_default_ports()`(幂等,纯内存)。 | P4 |
| A3 ✅ | ~~装配期必须调用 ensure_entity_types / ensure_kb_answer_route / register_prompt_catalog_entries / kb prompts seed~~ **P4 完成**:全部收进 `runtime.bootstrap.bootstrap_platform(session)`(另含 `agent.default` 路由与默认 agent 卡、`validate_prompt_resources()`),demo seed 与测试共用。 | P4 |
| A4 | MCP 工具注册(TF `service.py:708-730`)由 P2c 的 `service.py` 完成,权限调 `mcp_manager.mcp_tool_permission(server, spec)`(已实现,优先读协议原生 `annotations.read_only_hint`)。 | P2c |
| A5 | `sub_agents.build_delegation_tool()/run_sub_agent()` 已删 `project_id` 参数,scope 从 `ToolContext.chat_session` 自取;`icron.create_task/update_task` 用 `scope_type`+`scope_id`,角色参数是 `str` 非 Role 枚举。service 接线按此。 | P2c |
| A6 | `projections.py` 文件头临时定义了 `PAGE_ORIGIN_LLM`/`PAGE_DRAFT_PENDING`/`WIKI_PUBLICATION_STATUS_KEY`;`wiki_gen.py`/`wiki_review.py` 搬入后改为从那里 import(值须一致)。 | P3c |
| A7 | `snapshot.py::_snapshot_media()` 是可选加载器,`snapshot_media.py` 搬入后自动注册为必需 builder(`required_manifest()` 届时从 graph/retrieval/sql/wiki 变为含 media);`review_settlement.py` 对 `image_ingestion` 同理。新仓库无存量快照,无需兼容处理。 | P3c |
| A8 | **lifecycle 契约已变更**:预检 payload 删 `project_ids`/`project_references`/`retrieval_snapshot_ids`/`route_asset_ids`,新增 `external_reference_count`/`counts.external_references`;`archive_knowledge_base` 参数 `acknowledge_project_unlink` → `acknowledge_external_unlink`;blocker 码 `current_project_references` → `external_scope_references`(公开码 `EXTERNAL_SCOPE_REFERENCE`),`historical_retrieval_references` 删除。API 侧已按新契约实现(P3c),**P5 前端待改**。 | P3c/P5 |
| A9 | 待补测试(源文件内已留标记):websearch 工具门控、runtime_tools 工具快照、icron/session_goal API 层、sub_agents 专员 seed、compression 的 `rows_to_history`;approval reviewer 的评测集需自备中性语料。**P4 已补** runtime 四件套(dispatch/reliability/app_factory/bootstrap)、operations、router 聚合与写角色的专测,其余仍挂账。 | P2c/P4/P6 |
| A11 ✅ | ~~kb.py / kb_media.py 里的 dispatch shim~~ **P4 完成**:shim 删除,两处改回顶层 `from nicekit.runtime.dispatch import ...`;测试 monkeypatch 目标同步改为 `dispatch_kb_ingest_run`。 | P4 |
| A12 ✅ | ~~admin.py 的两处 KB 触点与 SERVICE_CONFIG_NAMES~~ **P4 完成**:`_prepare_embedding_route_campaign()`(路由落库前建 campaign,失败即整请求失败,不留"路由已切、向量还旧"的半途状态)与 `_resolved_embedding_config()` 均已接线;`SERVICE_CONFIG_NAMES = ("websearch","imagegen","weather")`。另补齐 orgs / 模型路由 / providers / model-catalog / usage / billing / model-prices / llm-traces / operations 诊断,共 46 个 admin 端点;provider api_key、service_config 密钥字段、MCP headers+env 落库前经 `core.secretbox` 加密,读出面一律掩码。 | P4 |
| A13 ✅ | ~~`_KB_WRITERS` 各一份~~ **P4 完成**:收进 `tenancy/roles.py` 的 `register_write_roles()` / `write_roles()`,守卫改 `api/deps.py::require_write_role()`(**请求期**读注册表,装配顺序无关);`ports.set_kb_notify_roles(write_roles())` 在 `install_default_ports()` 里调用。 | P4 |
| A14 ✅ | ~~ingestion.py 的四处延迟 import~~ **P4 完成**:caption/image_enrichment/image_ingestion/wiki_gen 全部提回模块级,`TYPE_CHECKING` 块删除;`test_kb_wiki_unit.py` 的 monkeypatch 目标改打在消费方 `kb.ingestion` 上(A10 纪律)。celery_app 的延迟 import 保留 —— 顶层引它会构成 runtime → operations → kb → runtime 的导入环。 | P4 |
| A10 | 测试写法纪律:替换可选依赖模块一律 `monkeypatch.setattr(包对象, "模块名", 替身)`,**不要** `monkeypatch.setitem(sys.modules, ...)`——`from pkg import mod` 优先取包属性,真实模块被别的用例 import 过后 sys.modules 替身会被绕开(已踩坑一次)。 | 全部 |

## 6. 阶段计划与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 骨架** | workspace 改造、目录骨架、docker-compose+deploy 复制、core 搬运(§5.1) | `uv sync` 通过;`docker compose up` 三件套健康;core 单测通过 |
| **P1 底座** | tenancy(§5.2)+ llm(§5.3)+ 新 baseline 迁移 + 密钥加密 | alembic upgrade head 成功;RLS 隔离测试过;登录/邀请/成员 API 可用;llm 测试套过;admin 配 provider+路由联通探测通过 |
| **P2 Agent** | agent(§5.4)+ capabilities(§5.6) | chat SSE 全事件流跑通;审批/反问/计划/子agent/MCP/skills/记忆/icron 逐项验证;demo 注册 1 个自定义工具成功 |
| **P3 KB** | kb(§5.5) | 上传→解析→分块→嵌入→generic 抽取→审核→快照发布→检索→问答 e2e;自定义实体类型走通结构化召回 |
| **P4 装配** | runtime/api/workers(§5.7)+ demo 后端 seed | inline 与 celery 双模式可跑;重启孤儿回收正常;`create_app()` 组装的 demo 后端全 API 可用 |
| **P5 前端** | demo 前端(§5.8) | 登录→chat 全交互→KB 工作台→admin 配置 e2e(playwright) |
| **P6 收尾** | 新项目试装演练、README、扩展点文档、旅游实体类型作为"行业模板示例" | "新项目 30 分钟起平台"演练通过 |

依赖关系:P0 → P1 → (P2 ∥ P3) → P4 → P5 → P6。P2 与 P3 相互独立可并行。

## 7. 工程规约

- commit 消息:`<类型>:<中文描述>`(feat/fix/update/refactor/docs),每完成一个功能大点即 commit。
- 所有子任务交付前必须:`uv run ruff check` + `uv run pytest`(相关测试)通过;未验证代码不 commit。
- 搬运原则:**先原样复制再最小改造**,保留 TF 源文件中的设计注释(尤其"勿回退"级别的裁决记录);import 路径 `app.` → `nicekit.`;禁止顺手重构未列入规格的部分。
- 每个搬运模块同时搬对应测试文件(TF `backend/tests/`)并适配。
- SDK 内出现任何 `project/customer/itinerary/quote/ota/render` 字样的残留即视为未完成(工具描述、prompt、注释、错误文案均包括)。

## 8. 风险与对策

1. `search.py` 泛化召回重写是唯一"重写级"任务 → 先写针对 KbEntity 的检索测试集再动手。
2. prompt 去旅游化后 agent 行为漂移 → 保留变量声明校验机制;P2 验收含行为回归清单。
3. 密钥加密新增 → Fernet + env master key 起步,预留 KMS;demo .env.example 明确说明。
4. zhparser/pgvector 定制 PG 镜像 → 直接复用 TF `deploy/` 的 Dockerfile 与 init SQL。
5. Windows 开发:必须经 `use_selector_event_loop_on_windows()`;后端用 `uv run python run.py` 启动(TF 经验:直接 uvicorn 会让 DB 后台任务静默失败)。
