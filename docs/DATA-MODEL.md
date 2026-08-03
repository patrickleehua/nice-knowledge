# NiceKit 数据模型手册

> **对应迁移版本**:`cb3e134da690`(`alembic heads`;链路 `4a6a2d9d8aa4_baseline` → `cb3e134da690_operations`)
> **核实方式**:本文每一条断言都取自 `packages/nicekit/src/nicekit/models/*.py`、
> `packages/nicekit/src/nicekit/migrations/**` 或对已迁移库的 `psql` 系统目录查询
> (`docker exec nice-knowledge-postgres-1 psql -U postgres -d niceknowledge`)。
> **读者**:接入 SDK 的工程师与 AI 编码助手。目标是看完就知道:**这张表干什么、我要不要管它、
> 我这种接入模式下用不用得到、它能不能一行都没有。**
>
> **schema 事实**(表/列/约束/策略/索引)对应 head `cb3e134da690`,与库完全一致。
> **接入侧描述**(§6 三种模式、`SINGLE_TENANT_ORG_ID`、`bootstrap_platform`)反映的是
> 租户接入线**工作区当前状态**,其中部分代码尚未 commit。

---

## 0. 三分钟速览

| 你的问题 | 答案 | 详见 |
|---|---|---|
| 一共几张表? | **64** 张(`public` schema,不含 `zhparser` schema 的扩展自带表) | §1 |
| 隔离怎么做的? | 单库单 schema + `org_id` 分区键 + PostgreSQL RLS(`FORCE ROW LEVEL SECURITY`) | §4 |
| 开了 RLS 的有几张? | **46** 张,全部 `ENABLE` + `FORCE`,策略名统一 `org_isolation` | §4.2 |
| 有几张带 `org_id` 但没开 RLS? | **6** 张,刻意豁免,见清单与理由 | §4.5 |
| `org_id` 真的是外键吗? | **绝大多数不是**。全库只有 **6** 张表的 `org_id` 带指向 `organizations` 的外键 | §4.6 |
| 我要不要往 `organizations` 里写行? | 桥接:**只有用到 agent 权限子系统时才必须**(`ensure_org()`);单租户:**必须**(默认 org 不在迁移 seed 里) | §6 |
| 换 embedding 模型要动什么? | `vector(1024)` 是 DDL 里的常量,必须走 `embedding_migration_campaigns` 全量 reindex | §5.3 |
| 有没有坑要先知道? | 有 7 条已核实的代码/库不一致与设计疑点,其中 2 条是**高**级别 | §8 |

---

## 1. 概览统计(psql 核实)

| 指标 | 数值 | 查询依据 |
|---|---|---|
| `public` schema 基表总数 | **64** | `information_schema.tables where table_type='BASE TABLE'` |
| 其中业务表(除 `alembic_version`) | 63 | — |
| 带 `org_id` 列的表 | **52** | `information_schema.columns where column_name='org_id'` |
| `org_id` 为 `NOT NULL` 的 | 49 | 同上 |
| `org_id` 可为 `NULL` 的 | 3(`agent_cards` / `mcp_servers` / `model_routes`,NULL = 平台级) | 同上 |
| `ENABLE ROW LEVEL SECURITY` 的表 | **46** | `pg_class.relrowsecurity` |
| `FORCE ROW LEVEL SECURITY` 的表 | **46**(与上一行完全重合,无例外) | `pg_class.relforcerowsecurity` |
| 未开 RLS 的表 | **18**(= 12 张无 `org_id` + 6 张豁免) | — |
| RLS 策略总数 | 49(46 条 `org_isolation` + 3 条 `platform_read`) | `pg_policies` |
| 外键约束总数 | **108** | `pg_constraint where contype='f'` |
| 其中多列复合外键 | **45**(几乎全部是携带 `org_id` / `kb_id` / `snapshot_id` 的租户一致性外键) | `array_length(conkey,1) > 1` |
| 指向 `organizations(id)` 的外键 | **6** | `pg_constraint where confrelid='organizations'::regclass` |
| 索引总数 | 343 | `pg_indexes` |
| 需要的 PG 扩展 | `vector` 0.8.5 / `pg_trgm` 1.6 / `zhparser` 2.4 / `pgcrypto` 1.3 | `pg_extension` |
| 向量维度 | `vector(1024)`(3 处:`kb_chunks.embedding` / `kb_chunk_embeddings.embedding` / `memory_items.embedding`) | `pg_attribute` |
| 中文全文检索配置 | `public.nicekit_zhparser`(由 baseline 迁移创建) | `pg_ts_config` |

> `zhparser` 扩展自带的 `zhprs_custom_word` 表位于 `zhparser` schema,**不属于本模型的 64 张表**,
> 也不参与迁移与备份口径。

---

## 2. 域分组总表(扫读用)

| 域 | 源模块 | 表数 | 一句话职责 | 桥接模式典型态度 |
|---|---|---|---|---|
| **D1 租户底座与身份** | `models/tenancy.py` | 8 | 组织/账号/角色/审计/站内信/用量记账 | 身份 5 张多半不用;审计+用量 3 张要用 |
| **D2 LLM 平台与外部服务** | `models/llm.py`、`llm_provider.py`、`service_config.py`、`mcp.py` | 8 | provider 实例、模型路由、Prompt Registry、调用 trace、单价、外部服务配置、MCP 服务器 | 全部要用(平台配置,与租户无关) |
| **D3 运维可观测** | `models/operations.py` | 3 | 进程心跳、AI 能力探测、KB 运维事件 | 用,但都可为空 |
| **D4 Agent 运行时与会话** | `models/chat.py`、`agent_permission.py`、`memory.py`、`icron.py` | 14 | agent 卡与版本、会话/消息/事件流、权限授权、长期记忆、定时任务 | 用到 agent 就全用 |
| **D5 知识库(KB)** | `models/kb.py`、`kb_feedback.py` | 30 | 文档摄入 → 修订 → 抽取 → 事实治理 → 快照 → 投影 → 检索 | 用到 KB 就全用 |
| **D6 迁移元数据** | Alembic | 1 | `alembic_version` | 必有 1 行 |
| | | **64** | | |

---

## 3. 逐表详解

图例:
- **org_id**:`NN` = 有且 NOT NULL;`NULL可` = 有且可空(NULL 表示平台级);`—` = 无此列
- **RLS**:`FORCE` = `ENABLE` + `FORCE ROW LEVEL SECURITY` + `org_isolation` 策略;`FORCE+PR` = 另有 `platform_read` 旁路;`豁免` = 有 `org_id` 但刻意不开;`—` = 无 `org_id`,不适用
- **桥接**:桥接 / 单租户模式(宿主自带身份、不挂 SDK 的 auth/members router)下是否会写到这张表
- **可空**:这张表在一个正常运行的部署里能不能一行都没有

### 3.1 D1 租户底座与身份(`models/tenancy.py`,8 张)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `organizations` | 租户主体登记。**全库唯一被 `org_id` 外键指向的表**;`slug` 唯一 | —(自身即主体) | — | 被 6 张表指向 | 条件用 | **否**:baseline 迁移已 seed 平台 org(`00000000-0000-0000-0000-000000000001`) |
| `users` | 全托管模式的账号表(`email` 唯一 + `password_hash`) | — | — | 被 7 条 FK 指向 | 条件用 | 可空;但**用 agent 权限子系统时必须有对应行**(见 §6 注意事项) |
| `memberships` | 用户在某 org 内的角色绑定,`(org_id,user_id)` 唯一;`role` 是 varchar(32) 开放值 | NN | **豁免** | → `organizations.id`、`users.id` | 不用 | 可空 |
| `invitations` | 邀请加入 org 的一次性令牌(`token_hash` 唯一 + `expires_at`) | NN | **豁免** | → `organizations.id` | 不用 | 可空 |
| `refresh_tokens` | 刷新令牌(哈希存储、可吊销)。登录态载体 | NN | **豁免** | → `organizations.id`、`users.id` | 不用 | 可空 |
| `audit_logs` | 业务操作审计流水:`action` / `entity_type` / `entity_id` / `detail(JSONB)` | NN | FORCE | 无 | 用 | 可空 |
| `notifications` | 站内信收件箱:`kind`(事件键)/ `title` / `body` / `link` / `read_at`;走 `(user_id, read_at)` 复合索引查未读 | NN | FORCE | 无 | 用 | 可空 |
| `usage_daily` | 按 `(org, 日期, task, provider, model)` 聚合的用量:token 进出、缓存读写、调用数、通用计量 `quantity`。**出账原料** | NN | **FORCE+PR** | 无 | 用 | 可空 |

### 3.2 D2 LLM 平台与外部服务(8 张)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `llm_providers` | provider 实例(Cherry Studio 式):`name` 唯一、`protocol`(openai/anthropic)、凭证、`base_url`、模型清单与能力元数据。`model_routes` 按 `name` 引用 | — | — | 无 | 用 | 可空:内置 `openai`/`anthropic` 实例会回落 `.env` 凭证 |
| `model_routes` | 任务级模型路由与降级链(`fallback_chain` JSONB)。**`org_id IS NULL` = 平台默认,org 行覆盖平台行** | NULL可 | **豁免** | 无 | 用 | 可空,但需配 `LLM_DEFAULT_MODEL` 兜底;否则解析失败 |
| `prompts` | Prompt Registry:`(task, version)` 唯一,内容不可改、只发新版;取最高 `is_active` 版本 | — | — | 无 | 用 | **否**:用到的每个 task 都必须有 active 行,缺则抛 `PromptNotFoundError` |
| `prompt_catalog_entries` | 自定义 Prompt 任务的中文名/描述目录(`task` 唯一,可 upsert)。内置任务元信息在源码 `BUILTIN_PROMPT_CATALOG`,不落库 | — | — | 无 | 用 | 可空 |
| `model_prices` | 模型单价(USD / 1M tokens),含缓存读写单价。计费公式的价格侧 | — | — | 无 | 条件用(要算钱才用) | 可空 |
| `llm_traces` | 每次 provider 调用一行(降级链每跳各一行):task/provider/model/attempt/status/token/延迟 | NN | **FORCE+PR** | 无 | 用 | 可空 |
| `service_configs` | 平台级外部服务配置(联网搜索、图片生成等),`name` 唯一,`payload` 自由 JSON,**DB 覆盖 `.env` 兜底** | — | — | 无 | 用 | 可空(全回落 `.env`) |
| `mcp_servers` | MCP 服务器配置:transport(streamable_http/sse/stdio)、endpoint、headers、超时、工具白名单。**`org_id IS NULL` = 平台共享**,部分唯一索引分别约束平台名与 org 内名 | NULL可 | **豁免** | 无 | 用 | 可空 |

### 3.3 D3 运维可观测(`models/operations.py`,3 张)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `service_heartbeats` | api / worker / beat 三类进程的最新心跳,`(role, instance)` 唯一。平台级 | — | — | 无 | 用 | 可空(无进程上报即空) |
| `provider_probe_statuses` | 四个 AI 能力槽位(embedding/rerank/ocr/caption)的周期探测结果。主键就是 `capability`,最多 4 行 | — | — | 无 | 用 | 可空 |
| `kb_operational_incidents` | 脱敏去重后的 KB 一致性 / 媒体投影失败事件,`dedupe_key` 唯一 + `occurrence_count` 累加 | NN | FORCE | → `knowledge_bases.id`;复合 → `kb_image_assets(id,org_id,kb_id)` | 用 | 可空(没出事就空,这是好事) |

> 设计要点:KB 子包不认识这张表,只经 `kb/ports.py::IncidentRecorder` 协议登记事件,
> 装配期由 `nicekit.operations.incidents` 注册默认 SQL 实现。替换实现不需要改 KB 代码。

### 3.4 D4 Agent 运行时与会话(14 张)

#### 3.4.1 Agent 卡(`models/chat.py`)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `agent_cards` | agent 配置卡的**身份与展示层**(name/description/icon/sort_order/is_active/delegatable)。`org_id IS NULL` = 平台默认卡 | NULL可 | **豁免** | 无 | 用 | **否**:`chat_sessions.agent_card_id` 是 NOT NULL 外键;`nicekit.agent.seed` 幂等 seed 一张默认卡 |
| `agent_card_versions` | **不可变能力版本**:system_prompt / model_task / max_turns / timeout / tools / mcp_bindings / skills。`(card_id, version)` 唯一,`is_active` 走部分唯一索引保证每卡至多一个激活版本。改配置=发新版 | — | — | → `agent_cards.id` (CASCADE) | 用 | 否(随卡 seed) |

> ⚠️ `agent_cards` 上那几列能力字段(`system_prompt`/`model_task`/`max_turns`/`timeout_seconds`/`tools`)
> **仅为旧库兼容保留**;运行时配置一律读 active 的 `agent_card_versions` 行。

#### 3.4.2 会话与事件流(`models/chat.py`)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `chat_sessions` | 工作台会话:绑定 agent 卡、`scope_type`+`scope_id`(宿主业务作用域,**无外键**)、`origin_mode`(general/scoped,创建事实不回写)、活跃 run、待批高危调用 `pending_confirmation`、待答澄清 `pending_user_input`、权限档位与策略快照、执行计划 `plan` | NN | FORCE | → `agent_cards.id`;→ `agent_approval_policies.id` (SET NULL) | 用 | 可空 |
| `chat_messages` | 会话消息:`role`=user/assistant/tool;tool 行带 `tool_name`/`tool_call_id`/`tool_input`/`tool_output`;`(session_id, sequence)` 唯一 | NN | FORCE | → `chat_sessions.id` | 用 | 可空 |
| `chat_events` | 持久化的 AgentEvent 信封,供 SSE 断线重放;`(session_id, run_id, seq)` 唯一 | NN | **FORCE+PR** | → `chat_sessions.id` (CASCADE) | 用 | 可空 |
| `chat_session_summaries` | 上下文压缩摘要:`covered_until_sequence` 之前的消息在模型历史中由摘要代替。`status=failed` 行保留失败痕迹用于熔断。**摘要是替身,原始消息一律不删** | NN | FORCE | → `chat_sessions.id` (CASCADE) | 用 | 可空 |
| `chat_run_inputs` | run 进行中的用户输入队列(不再 409):`queued` → `consumed`(幂等转成正式 message,记 `consumed_message_id`)/ `skipped`。可带本轮临时工具 `runtime_tools` | NN | FORCE | → `chat_sessions.id` (CASCADE) | 用 | 可空 |
| `session_goals` | 会话目标与自动续跑:`objective` + 双闸预算(`token_budget` / `max_auto_runs`)。`session_id` 唯一,重设目标原地覆盖 | NN | FORCE | → `chat_sessions.id` (CASCADE, UNIQUE) | 用 | 可空 |

#### 3.4.3 Agent 权限(`models/agent_permission.py`)—— **本域唯一带 `organizations` 外键的三张表**

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `agent_approval_policies` | 组织级审批策略的**不可变版本**:默认/允许档位、硬规则、reviewer 配置、最大作用域与 TTL。`(org_id, version)` 唯一,`is_active` 部分唯一索引保证每 org 至多一个激活版本 | NN | FORCE | **→ `organizations.id` (CASCADE)**;→ `users.id` (SET NULL, 可空) | 条件用 | 可空(无策略走 SDK 默认档位) |
| `agent_permission_preferences` | 用户在某 org 内的默认权限选择,`(org_id,user_id)` 唯一 | NN | FORCE | **→ `organizations.id` (CASCADE)**;**→ `users.id` (CASCADE, NOT NULL)** | 条件用 | 可空 |
| `agent_permission_grants` | 带过期的授权:钉在 `(user, session/scope, tool_name 或 category)` 上,`action_fingerprint` 定长 64。CHECK 保证 tool_name 与 category 二选一、session 档必带 session、resource 档必带 scope_id | NN | FORCE | **→ `organizations.id` (CASCADE)**;**→ `users.id` ×2(`user_id`/`created_by`,均 CASCADE、NOT NULL)**;→ `users.id`(`revoked_by`,SET NULL);→ `chat_sessions.id`;→ `agent_approval_policies.id` | 条件用 | 可空 |

#### 3.4.4 长期记忆与定时任务

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `memory_items` | 跨会话复用的长期记忆:`memory_type`(preference / preference_candidate / constraint / decision / fact / risk)、`scope`+`scope_ref_id`(**无外键**,`scope` 由 `register_memory_scopes()` 注册)、`confidence` / `sightings` / `hit_count` 双信号、`status` 软删除、可选 `vector(1024)` | NN | FORCE | 无 | 用 | 可空 |
| `icron_tasks` | 自主定时任务定义:四档 `schedule_kind`(manual/once/interval/cron)、`instruction`(必须自包含)、`timezone` 只解释 cron 不改存储口径、`next_run_at` 是调度器唯一扫描入口 | NN | FORCE | → `chat_sessions.id` (SET NULL) | 用 | 可空 |
| `icron_task_runs` | 每次触发的执行记录(成功/失败/**没跑**都记一行):要么有 `agent_run_id`,要么有 `skip_reason`/`error` | NN | FORCE | → `icron_tasks.id` (CASCADE)。`agent_run_id`/`session_id` **刻意无外键** | 用 | 可空 |

### 3.5 D5 知识库(30 张)

#### 3.5.1 库与生命周期

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `knowledge_bases` | 知识库一等公民。`(id, org_id)` 唯一(供下游复合外键锚定租户)、`active_snapshot_id`、`lifecycle_status`(active/archived/purge_pending/purged)+ 三条 CHECK 保证归档/清除的审计字段成套、`consumption_epoch`、per-KB 摄入配置 `ingest_profile`、Wiki 导航 `wiki_navigation`。`kb_type` 是**纯展示 tag,SDK 内无任何分支依赖它** | NN | FORCE | **无出向 FK**(见 §8 疑点 1) | 用 | 可空(没建库时) |
| `kb_shares` | 库级只读分享:owner org 把某库分享给 `grantee_org_id` | NN | FORCE | → `knowledge_bases.id` | 用 | 可空 |
| `knowledge_base_lifecycle_operations` | 库级清除(purge)的持久化工单:阶段机 `revalidate_and_quiesce → delete_objects → cleanup_metadata → finalize_audit_shell → completed`,`(org_id, idempotency_key)` 唯一 | NN | FORCE | 复合 → `knowledge_bases(id, org_id)`;→ `outbox_events.id` | 用 | 可空 |
| `knowledge_base_purge_objects` | 单次 purge 的**逐对象幂等进度账本**:`(operation_id, bucket, object_key, object_version)` 唯一,状态 pending/deleted/skipped_shared/failed | NN | FORCE | 复合 → `knowledge_base_lifecycle_operations(id, org_id, kb_id)` | 用 | 可空 |

#### 3.5.2 文档摄入链

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `source_documents` | 上传的源文件:对象存储 key、统一 Markdown 中间表示 key、`sha256`(库内唯一)、`doc_type`(**开放字符串**,可为 `kb_entity_types.type_key`,故无 CHECK)、处理状态与分阶段进度、过期与法务保留(legal hold,CHECK 保证字段成套) | NN | FORCE | → `knowledge_bases.id`。`(id,org_id,kb_id)` 唯一供下游锚定 | 用 | 可空 |
| `document_revisions` | **不可变**源版本与解析产物引用。`(doc_id, revision_no)` 唯一;`(doc_id, sha256)` 在未 tombstone 时唯一 | NN | FORCE | → `source_documents.id`、`knowledge_bases.id` | 用 | 可空 |
| `kb_document_operations` | 文档级撤回/重摄/清除工单,`idempotency_key` 唯一 | NN | FORCE | 复合 → `source_documents(id,org_id,kb_id)`、`document_revisions(id,doc_id,org_id,kb_id)`、`knowledge_snapshots(org_id,kb_id,id)` | 用 | 可空 |
| `ingest_runs` | 可租约、可重试的处理单元:一个 revision 的一个 stage + segment,`(revision_id, stage, segment_no)` 唯一 + `idempotency_key` 唯一。**续跑靠它跳过已 staged 的段,不重复烧 LLM/OCR 的钱** | NN | FORCE | 复合 → `document_revisions(id,org_id,kb_id)`;→ `knowledge_bases.id`、`document_revisions.id` | 用 | 可空 |
| `kb_image_assets` | 文档内每一次图片出现的不可变记录 + 受治理的图片增强(caption/OCR),带审阅状态。原始私有存储定位符**只存在这里,永不进快照** | NN | FORCE | 复合 → `document_revisions(id,org_id,kb_id)`;→ `source_documents.id`、`ingest_runs.id`、`knowledge_bases.id` | 用 | 可空 |
| `kb_image_asset_events` | 单张图片增强与审阅的 append-only 历史 | NN | FORCE | 复合 → `kb_image_assets(id,org_id,kb_id)`;→ `knowledge_bases.id` | 用 | 可空 |

#### 3.5.3 实体、事实与证据(治理层)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `kb_entity_types` | **实体类型注册表**:类型是可注册的 schema 配置而非写死的表。`field_schema` 是 JSON Schema(LLM 抽取与人工编辑共用的强校验层)、`filterable_fields` 声明结构化检索字段、`card_template` 产出实体卡片。SDK 只 seed 兜底类型 `concept` | NN | FORCE | 无 | 用 | **否**:至少有平台 org 下的 `concept` |
| `kb_entities` | **通用实体存储**(SDK 内唯一的结构化实体落表)。挂快照,`attributes` 需通过所属类型的 JSON Schema 校验,`name` 为提升列,结构化过滤走 `attributes` 的 GIN 索引 | NN | FORCE | 复合 → `knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id`、`source_documents.id` | 用 | 可空 |
| `canonical_entities` | KB 内的**规范实体注册表**(被治理事实与图投影引用),带 `tsv` 生成列 + GIN。支持合并(`merged_into_entity_id` 自引用复合外键) | NN | FORCE | 自引用复合 → `canonical_entities(org_id,kb_id,id)`;复合 → `knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id` | 用 | 可空 |
| `entity_aliases` | 归一化别名 → 规范实体,`(kb_id, normalized_alias, locale)` 唯一,带 `tsv` + GIN | NN | FORCE | → `canonical_entities.id`、`knowledge_bases.id` | 用 | 可空 |
| `fact_claims` | 可审阅的事实断言,原始抽取载荷不可变;`review_status` ∈ suggested/confirmed/rejected/orphaned;可带有效期区间 | NN | FORCE | 复合 → `canonical_entities(org_id,kb_id,id)` ×2(subject/object);→ `ingest_runs.id`、`knowledge_bases.id` | 用 | 可空 |
| `evidence_spans` | 字段级引证,锚定到**不可变的 document revision**。`(id, fact_claim_id, revision_id, org_id, kb_id)` 唯一供快照成员表锚定 | NN | FORCE | 复合 → `kb_image_assets(id,org_id,kb_id,revision_id)`;→ `fact_claims.id`、`document_revisions.id`、`kb_chunks.id`(SET NULL)、`knowledge_bases.id` | 用 | 可空 |

#### 3.5.4 快照与投影(不可变发布单元)

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `knowledge_snapshots` | 带指纹的**不可变发布单元**:`(kb_id, revision_set_hash, embedding_fingerprint, config_fingerprint)` 唯一;另有 `(kb_id,id)` 与 `(org_id,kb_id,id)` 唯一,专供下游复合外键 | NN | FORCE | → `knowledge_bases.id` | 用 | 可空 |
| `snapshot_fact_supports` | 某候选快照清单里的 fact/evidence 成员关系(不可变),`(snapshot_id, fact_claim_id, evidence_span_id)` 唯一 | NN | FORCE | 复合 → `knowledge_snapshots(org_id,kb_id,id)`、`evidence_spans(id,fact_claim_id,revision_id,org_id,kb_id)`、`document_revisions(id,doc_id,org_id,kb_id)`、`source_documents(id,org_id,kb_id)` | 用 | 可空 |
| `snapshot_projection_supports` | 由事实派生的每一行投影的**证据溯源**,`(snapshot_id, projection_type, projection_row_id, fact_support_id)` 唯一 | NN | FORCE | 复合 → `snapshot_fact_supports(id,org_id,kb_id,snapshot_id)` (CASCADE)、`knowledge_snapshots(...)`;→ `knowledge_bases.id` | 用 | 可空 |
| `kb_snapshot_entity_nodes` | 冻结在某快照里的规范实体节点成员关系,`(snapshot_id, entity_id)` 唯一 | NN | FORCE | 复合 → `canonical_entities(org_id,kb_id,id)`、`knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id` | 用 | 可空 |
| `kb_snapshot_entity_node_supports` | 支撑一个快照实体节点的证据:`support_type` ∈ fact/edge/pin,CHECK 保证三选一时对应 id 有且仅有一个;三条部分唯一索引分别去重 | NN | FORCE | 复合 → `kb_snapshot_entity_nodes(org_id,kb_id,snapshot_id,entity_id)` (CASCADE)、`kb_graph_edges(id,org_id,kb_id,snapshot_id)`、`snapshot_fact_supports(id,org_id,kb_id,snapshot_id)` | 用 | 可空 |
| `kb_graph_edges` | 快照作用域内、受治理的实体间关系边(predicate/direction/kind),`(id,org_id,kb_id,snapshot_id)` 唯一 | NN | FORCE | 复合 → `canonical_entities` ×2 (CASCADE)、`kb_snapshot_entity_nodes` ×2、`knowledge_snapshots`、`fact_claims` ×2、`evidence_spans` ×2、`document_revisions` ×2;→ `knowledge_bases.id` | 用 | 可空 |
| `kb_snapshot_image_assets` | 冻结在快照里的媒体成员关系。**对象 key 只以 SHA-256 指纹出现**,原始私有定位符留在 `kb_image_assets` | NN | FORCE | 复合 → `kb_image_assets(id,org_id,kb_id)`、`document_revisions(id,org_id,kb_id)`、`knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id` | 用 | 可空 |
| `kb_pages` | 通用 wiki 页节点(concept/rule/faq/policy/… 开放 `page_type`),Markdown 正文可被切 chunk 进检索。新增类型零迁移 | NN | FORCE | 复合 → `knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id`、`source_documents.id` | 用 | 可空 |

#### 3.5.5 检索与向量

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `kb_chunks` | 非结构化文本切片:`content` + `heading_path` + `meta`,`tsv` 生成列(`public.nicekit_zhparser`)+ GIN,内联 `embedding vector(1024)`,`embedding_model` 记 `模型名:版本`。`(org_id,kb_id,id)` 唯一供 embedding 表锚定 | NN | FORCE | 复合 → `document_revisions(id,org_id,kb_id)`、`kb_image_assets(id,org_id,kb_id,revision_id)`、`knowledge_snapshots(org_id,kb_id,id)`;→ `knowledge_bases.id`、`source_documents.id` | 用 | 可空 |
| `kb_chunk_embeddings` | **多版本 embedding**:一个 chunk × 一组 provider/model 指纹一行,`(chunk_id, provider, model, …)` 唯一;HNSW `vector_cosine_ops` 索引在此 | NN | FORCE | 复合 → `kb_chunks(org_id,kb_id,id)` (CASCADE) | 用 | 可空 |
| `embedding_migration_campaigns` | **全局**(无 `org_id`)的 embedding 配置迁移编排记录:源/目标配置与指纹、状态机 queued→running→dual_read→completed | — | — | 无 | 用 | 可空 |
| `embedding_reindex_jobs` | campaign 下的 per-KB 可租约工作单元,`(campaign_id, kb_id)` 唯一 | NN | FORCE | → `embedding_migration_campaigns.id`、`knowledge_bases.id` | 用 | 可空 |

#### 3.5.6 事件与反馈

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `outbox_events` | 事务性发件箱:待投影或待 GC 的幂等事件,`idempotency_key` 唯一,状态 pending/processing/published/dead_letter | NN | FORCE | → `knowledge_bases.id` | 用 | 可空 |
| `knowledge_answer_feedbacks` | `/kb/answer` 的赞/踩反馈。AI 答案是即答即走的流式输出不落库,**因此反馈行自带 `query`/`answer_text`/`sources` 快照**,不依赖任何会话或消息外键 | NN | FORCE | **无**(刻意:反馈作为评测数据独立于账号生命周期) | 用 | 可空 |

### 3.6 D6 迁移元数据

| 表 | 用途 | org_id | RLS | 外键 | 桥接 | 可空 |
|---|---|---|---|---|---|---|
| `alembic_version` | Alembic 当前 head(现为 `cb3e134da690`) | — | — | — | 用 | 否 |

---

## 4. 多租户隔离机制

### 4.1 唯一开关:`app.current_org_id`

隔离的全部输入就是一个事务级 GUC。设置方式(`core/db.py::bind_org_context`):

```sql
SELECT set_config('app.current_org_id', '<org uuid>', true)
```

`true` = 事务级,commit 后即失效,因此 `bind_org_context` 挂在 SQLAlchemy 的 `after_begin`
事件上,保证跨 commit 的后续事务仍带上下文。API 侧由 `get_org_session` 依赖注入;
worker / service 侧用 `core/db.py::org_session()`。

**已核实:整库没有任何触发器(`pg_trigger` 非内部触发器为 0 条)、没有任何 rule
(`pg_rules` 为 0 条),`current_setting` 只在 RLS 策略里出现,且只读 `app.current_org_id` 这一个键。**

### 4.2 `org_isolation` 策略(46 张表,逐字相同)

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON <t>
  USING      (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
  WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);
```

三个必须理解的点:

1. **`NULLIF` 不是洁癖**:`set_config(..., true)` 在事务结束后,GUC 会在连接上残留为**空串而非 NULL**,
   直接 `''::uuid` 会报错。`NULLIF` 让"未设置上下文"退化为 `org_id = NULL` → 恒假。
2. **fail-closed**:没有 org 上下文 = **什么都看不到、什么都写不进**。忘记 `bind_org_context`
   的表现是"查询返回 0 行 / 插入报策略违规",不是"看到了别人的数据"。
3. **`WITH CHECK` 与 `USING` 同式**:写入时 `org_id` 必须等于当前上下文,跨租户写入在数据库层被拒。

**`FORCE` 的意义**:普通 RLS 对表 owner 不生效。部署用双账号(`deploy/postgres-init.sql`):
`niceknowledge_migrator` 是 owner 跑 Alembic,`niceknowledge_app` 是应用连接。加 `FORCE`
之后连 owner 连接也受策略约束,`46 = 46`(`relrowsecurity` 与 `relforcerowsecurity` 完全重合),
没有一张表只 ENABLE 不 FORCE。

策略由 `migrations/rls.py::op_enable_org_rls` 统一施加,并在模块级 set 登记;
baseline 迁移末尾用 `rls_tables_check(metadata)` 自检——**带 `org_id` 却没登记 RLS
又不在豁免清单里的表,迁移当场 `RuntimeError`**。新增带 `org_id` 的表必须走同一个 helper。

### 4.3 `platform_read` 旁路(3 张表)

```sql
CREATE POLICY platform_read ON <t> FOR SELECT
  USING (NULLIF(current_setting('app.current_org_id', true), '')::uuid
         = '<platform_org_id>'::uuid);
```

| 表 | 为什么需要跨租户只读 |
|---|---|
| `usage_daily` | **平台出账**:按租户汇总 token/调用/存储量,必须一次扫全表 |
| `llm_traces` | **平台诊断与成本归因**:降级链每跳一行,排查"哪个租户把哪个 provider 打挂了" |
| `chat_events` | **跨租户运行日志审计**:定位 agent run 的失败模式,不能逐租户切上下文重扫 |

要点:
- PERMISSIVE 策略按 **OR** 合并 —— 租户自己的读写仍走 `org_isolation`,这条只**追加**平台 org 的 SELECT;
- **只放开读,不放开写**(`FOR SELECT`);写入面完全不变;
- 平台 org UUID 由 `settings.platform_org_id` 参数化传入迁移(`platform_read_sql` 会先
  `UUID(str(...))` 规整,杜绝拼接注入),不硬编码。当前库里是
  `00000000-0000-0000-0000-000000000001`(baseline 迁移 seed,`slug='platform'`)。

### 4.4 RLS 豁免清单(6 张,`_RLS_EXEMPT`)

这 6 张表**有 `org_id` 列但刻意不开 RLS**,定义在 `4a6a2d9d8aa4_baseline.py` 的 `_RLS_EXEMPT`:

| 表 | 类别 | 豁免理由 | 谁来兜底 |
|---|---|---|---|
| `memberships` | 身份表 | 登录/切组织发生在 **org 上下文建立之前**;要先查"这个人属于哪些 org"才知道 `org_id` 该设成什么 | 应用层按 `user_id` 过滤 |
| `invitations` | 身份表 | 接受邀请时用户还不属于该 org,RLS 会让他连自己的邀请都查不到 | 应用层 + `token_hash` 唯一 |
| `refresh_tokens` | 身份表 | 刷新令牌的校验发生在拿到身份之前 | 应用层 + `token_hash` 唯一索引 |
| `model_routes` | 平台配置表 | `org_id` **可为 NULL 表示平台级默认**;`org_isolation` 的等值比较会把所有平台行挡在外面,租户就再也读不到默认路由 | API 层 `platform_admin` / `org_admin` 守门 |
| `agent_cards` | 平台配置表 | 同上:`org_id IS NULL` = 平台默认卡,全租户可见 | API 层守门 |
| `mcp_servers` | 平台配置表 | 同上:`org_id IS NULL` = 平台共享 MCP 服务器 | API 层守门(`stdio` transport 仅平台管理员可配) |

> **接入方必须知道**:这 6 张表在数据库层**没有隔离**。宿主如果绕过 SDK 的 API 层直接读写它们,
> 必须自己带 `org_id` 条件。剩下 12 张无 RLS 的表(`organizations` / `users` /
> `agent_card_versions` / `llm_providers` / `model_prices` / `prompts` /
> `prompt_catalog_entries` / `service_configs` / `service_heartbeats` /
> `provider_probe_statuses` / `embedding_migration_campaigns` / `alembic_version`)
> 压根没有 `org_id`,本身就是平台级数据,不存在隔离问题。

### 4.5 `org_id` 外键真相 —— 接入方案的关键事实

**全库 108 条外键里,只有 6 条指向 `organizations(id)`**(psql 核实:
`select count(*) from pg_constraint where contype='f' and confrelid='organizations'::regclass` → `6`):

| 表 | 约束名 | 删除行为 | 归类 |
|---|---|---|---|
| `agent_approval_policies` | `agent_approval_policies_org_id_fkey` | `ON DELETE CASCADE` | agent 权限表 |
| `agent_permission_grants` | `agent_permission_grants_org_id_fkey` | `ON DELETE CASCADE` | agent 权限表 |
| `agent_permission_preferences` | `agent_permission_preferences_org_id_fkey` | `ON DELETE CASCADE` | agent 权限表 |
| `invitations` | `invitations_org_id_fkey` | (无) | 租户自有身份表 |
| `memberships` | `memberships_org_id_fkey` | (无) | 租户自有身份表 |
| `refresh_tokens` | `refresh_tokens_org_id_fkey` | (无) | 租户自有身份表 |

**其余 46 张带 `org_id` 的表,`org_id` 是纯分区键 —— 没有任何外键约束。**

这条事实的直接推论:

- KB、chat、检索、记忆、计量、审计这些**主链路完全不需要 `organizations` 里有对应行**。
  桥接模式下可以直接用 `tenant_uuid(宿主租户 ID)` 派生出的 UUID 当 `org_id` 写数据,
  数据库不会拦。
- 只有当你**用到 agent 权限子系统**(审批策略 / 权限偏好 / 权限授权)时,那 3 条 CASCADE 外键
  才要求 `organizations` 里有行。此时调用 `nicekit.tenancy.orgs.ensure_org()`(`ON CONFLICT DO NOTHING`,
  幂等、并发安全、不 commit)垫一行即可。
- 身份三表(`invitations`/`memberships`/`refresh_tokens`)只在全托管模式下写,桥接/单租户模式不挂
  auth/members router,这 3 条外键自然不触发。
- **为什么不干脆去掉那 3 个外键**:级联删除语义有价值(删租户时权限策略跟着走),
  代价只是宿主一行 `ensure_org()` 调用。

> ⚠️ **补充事实(源码文档未提及,psql 核实)**:那 3 张 agent 权限表除了 `organizations`,
> 还有 **NOT NULL 的 `users` 外键**:`agent_permission_grants.user_id`(CASCADE)、
> `agent_permission_grants.created_by`(CASCADE)、`agent_permission_preferences.user_id`(CASCADE)。
> 也就是说桥接模式下要用权限子系统,光 `ensure_org()` 不够,`users` 表里也得有对应行,
> 而 SDK **没有提供 `ensure_user()` 帮手**。详见 §8 疑点 3。

---

## 5. 关键设计

### 5.1 KB 的复合外键:快照作用域一致性

45 条多列外键几乎全部服务于同一个目的:**让"跨租户/跨库/跨快照的引用"在数据库层就不可能存在**。

三种典型形态:

| 形态 | 例子 | 保证什么 |
|---|---|---|
| `(org_id, kb_id, snapshot_id)` → `knowledge_snapshots(org_id, kb_id, id)` | `kb_chunks` / `kb_pages` / `kb_entities` / `kb_graph_edges` / `snapshot_fact_supports` / `snapshot_projection_supports` / `kb_snapshot_entity_nodes` / `kb_snapshot_image_assets`,另加 `canonical_entities`(列名是 `support_status_snapshot_id`)与 `kb_document_operations`(列名是 `target_snapshot_id`)—— 共 **10** 张 | 一行投影数据只能挂在**同一个租户、同一个库**的快照上 |
| `(org_id, kb_id, snapshot_id, entity_id)` → `kb_snapshot_entity_nodes(...)` | `kb_graph_edges` 的 `src_node` / `dst_node` | 一条图边的两端节点必须都在**同一个快照**里被冻结 |
| `(id, org_id, kb_id)` → `<父表>(id, org_id, kb_id)` | `evidence_spans` / `fact_claims` / `document_revisions` / `source_documents` / `kb_image_assets` / `knowledge_base_lifecycle_operations` 等被引用侧 | 引用父行时租户与库必须一致,不能"我的 chunk 引用你的 revision" |

支撑这些复合外键的是各父表上刻意多加的复合唯一约束(`uq_*_tenant_identity`、
`uq_knowledge_snapshot_kb_id_id` 等)——它们不是给查询用的,是给外键做被引用侧用的。

**接入方注意**:这意味着你**不能**手工往 KB 表里塞数据然后指望"补一个 org_id 就行"。
`org_id` 参与外键,写错会直接违约。请一律走 SDK 的服务层。

`snapshot_id` 的可空性也是有讲究的:

| 可空(草稿态数据,尚未进快照) | 非空(必须属于某个快照) |
|---|---|
| `kb_chunks` / `kb_entities` / `kb_pages` | `kb_graph_edges` / `kb_snapshot_entity_nodes` / `kb_snapshot_entity_node_supports` / `kb_snapshot_image_assets` / `snapshot_fact_supports` / `snapshot_projection_supports` |

### 5.2 `scope_type` + `scope_id`:无外键的泛化引用

SDK 不认识宿主的业务表,所以**所有指向宿主业务对象的引用一律不建外键**
(MIGRATION-PLAN §5.4"4 条硬 FK 泛化")。

| 表 | 列 | 语义 | 长度/类型约束 |
|---|---|---|---|
| `chat_sessions` | `scope_type` + `scope_id` | 会话绑定的宿主业务作用域;两者要么都空(通用会话)要么都有值,由**服务层**保证,刻意不落 CHECK(避免宿主迁移期数据被硬拒) | `varchar(40)` + `uuid` |
| `agent_permission_grants` | `scope_type` + `scope_id` | 授权钉在哪个业务作用域上;`scope='resource'` 时 CHECK 要求 `scope_id IS NOT NULL` | 同上 |
| `icron_tasks` | `scope_type` + `scope_id` | 定时任务归属的业务作用域 | 同上 |
| `memory_items` | `scope` + `scope_ref_id` | 记忆的作用范围。**注意列名不同**:`scope` 是注册制词表值(`register_memory_scopes()`),`scope_ref_id` 是**文本而非 UUID**(宿主的作用域标识不一定是 UUID) | `varchar(20)` + `varchar(200)` |

另外两处刻意无外键的引用:

- `icron_task_runs.agent_run_id` / `.session_id` —— run 不是一张表,是会话事件流上的一个标识符
  (对应 `chat_events.run_id` / `chat_sessions.active_run_id`);
- `knowledge_answer_feedbacks.user_id` —— 反馈作为评测数据独立于账号生命周期留存。

**推论**:这些列上的一致性**没有数据库兜底**。宿主删业务对象时,SDK 侧的会话/授权/任务不会级联清理,
需要宿主自己处理(或接受悬挂引用——`ScopeResolver` 解析失败时会体现为不可见)。

### 5.3 向量与全文检索

**向量维度 `vector(1024)`**,出现在 3 处:

| 列 | 索引 |
|---|---|
| `kb_chunks.embedding` | 无向量索引(内联列,主要供小规模/兼容路径) |
| `kb_chunk_embeddings.embedding` | `ix_kb_chunk_embeddings_embedding_hnsw` — HNSW / `vector_cosine_ops` |
| `memory_items.embedding` | 无向量索引;为 NULL 时召回自动退回关键词打分 |

维度来自 `settings.kb_embedding_dim`(缺省回落 **1024**,bge-m3 维度),
在 `models/kb.py::EMBEDDING_DIM` 与 `models/memory.py::MEMORY_EMBEDDING_DIM` 各读一次同一配置。

> ⚠️ **`vector(D)` 是烧进 DDL 的常量**。部署期一次性确定;换 embedding 模型必须走
> `embedding_migration_campaigns` + `embedding_reindex_jobs` 全量 reindex,
> 不能改配置了事。`memory.py` 刻意不 import `kb.py` 拿常量(避免为一个常量把 KB 全部表
> 拖进 agent 的 import 图),因此**这是两处独立读取同一配置,不是转引**——改配置时两边一起变。

**中文全文检索 `kb_fts_regconfig`**,默认 `public.nicekit_zhparser`:

- 由 **baseline 迁移**创建(不是 initdb 脚本),必须先于任何带 `tsvector` 生成列的表:
  ```sql
  CREATE TEXT SEARCH CONFIGURATION public.nicekit_zhparser (PARSER = public.zhparser);
  ALTER TEXT SEARCH CONFIGURATION public.nicekit_zhparser
    ADD MAPPING FOR n, v, a, i, e, l WITH pg_catalog.simple;
  ```
- 3 个持久化生成列 + GIN 索引:`kb_chunks.tsv`、`canonical_entities.tsv`、`entity_aliases.tsv`;
- `zhparser.multi_short = true` 在 initdb 阶段以 `ALTER DATABASE` 持久化(分词 GUC 是 superuser-only,
  后续非 superuser 的迁移/应用会话必须算出同样的向量,例如"酒店推荐"也要切出"酒店");
- **生成列表达式与检索层必须用同一个 regconfig 值**,改配置意味着重建生成列。

### 5.4 PG 扩展与用途

| 扩展 | 版本 | 用途 | 何处安装 |
|---|---|---|---|
| `vector`(pgvector) | 0.8.5 | `vector(1024)` 列与 HNSW 余弦索引 | `deploy/postgres-extensions.sql`(initdb,以 `postgres` 身份) |
| `pg_trgm` | 1.6 | 模糊/相似度匹配(别名与词面匹配辅助) | 同上 |
| `zhparser` | 2.4 | 中文分词 parser,是 `nicekit_zhparser` 的基础。**untrusted 扩展**,必须在把 owner 交给 migrator 之前装好 | 同上 |
| `pgcrypto` | 1.3 | 检索链路的 `digest()`(嵌入内容指纹) | initdb **和** baseline 迁移各建一次 |

`pgcrypto` 两处都建是刻意的:initdb 只跑一次,已有数据卷的环境升级时不会重跑,
放迁移里才能覆盖存量部署。baseline 的 `_require_extensions()` 会在建表前检查
`vector / pg_trgm / zhparser / pgcrypto` 四个扩展,**缺任何一个当场 RAISE EXCEPTION**,
而不是等 `tsvector`/`vector` 列建到一半才报错。

---

## 6. 三种接入模式下的表使用矩阵

模式定义见 `api/deps.py`(**`PrincipalResolver` 是接入外部身份体系的唯一切换点**):

| 模式 | 怎么开 | `org_id` 从哪来 |
|---|---|---|
| **① 全托管**(默认) | 不做任何事,SDK 自己签发/校验 JWT,挂 auth + members router | JWT claim |
| **② 桥接** | `set_principal_resolver(自己的实现)`,不挂 auth/members router | 宿主租户主键经 `tenancy.mapping.tenant_uuid()` uuid5 派生 |
| **③ 单租户** | `set_principal_resolver(single_tenant_resolver(...))` | 恒定;默认取 `api.deps.SINGLE_TENANT_ORG_ID`(= `tenant_uuid("__single_tenant__")`) |

> ③ 的默认 org **刻意不是** `settings.platform_org_id`:平台 org 的语义是"这份数据对所有组织可见"
> (见 `kb/search.py::_layer` 与 `kb/image_assets.py::visible_kb_filter`),
> 单租户期间两者行为无差别,但接入第二个租户时原先落在平台 org 下的知识会立刻对新租户可见。
> 独立 org 只是一个普通租户,升级多租户时什么都不用改。
> 代价:这个 org **不在迁移 seed 里**,首次启动必须垫行 ——
> `bootstrap_platform(session, single_tenant=True)`,或自己调
> `tenancy.orgs.ensure_org(session, SINGLE_TENANT_ORG_ID)`。

> ③ 单租户模式**不做任何身份校验**,必须前置一层完成认证的网关,或确保 SDK 不直接暴露公网。
> **RLS 仍然生效,别关它** —— 那是代码漏写 `where` 时的最后一道闸。

### 表使用矩阵

| 表组 | ① 全托管 | ② 桥接 | ③ 单租户 | 说明 |
|---|---|---|---|---|
| `organizations` | ✅ 必需 | ⚠️ 条件 | ✅ 必需 | ②:仅用 agent 权限子系统时需要,调 `ensure_org()`。③:`SINGLE_TENANT_ORG_ID` **不在迁移 seed 里**,必须 `bootstrap_platform(single_tenant=True)` 垫一行 |
| `users` | ✅ 必需 | ⚠️ 条件 | ⚠️ 条件 | ②③:仅用 agent 权限子系统时需要(NOT NULL 外键),但 SDK 无 `ensure_user()` |
| `memberships` / `invitations` / `refresh_tokens` | ✅ 必需 | ❌ 不用 | ❌ 不用 | ②③ 不挂 auth/members router |
| `prompts` / `model_routes` / `llm_providers` / `model_prices` / `prompt_catalog_entries` / `service_configs` / `mcp_servers` | ✅ | ✅ | ✅ | 平台配置,与租户模式无关 |
| `llm_traces` / `usage_daily` | ✅ | ✅ | ✅ | 自动写入;③ 用独立 org 后,`platform_read` **不再**恒成立(见 §8 疑点 4) |
| `audit_logs` / `notifications` | ✅ | ✅ | ✅ | `user_id` 在 ②③ 下取 resolver 返回的主体(`single_tenant_resolver` 可用 `subject_of` 从网关头带入,否则全部落到同一固定主体) |
| `service_heartbeats` / `provider_probe_statuses` | ✅ | ✅ | ✅ | 平台级 |
| `agent_cards` / `agent_card_versions` | ✅ | ✅ | ✅ | `nicekit.agent.seed` 幂等 seed 一张中性默认卡 |
| chat 六表(`chat_sessions`/`chat_messages`/`chat_events`/`chat_session_summaries`/`chat_run_inputs`/`session_goals`) | ✅ | ✅ | ✅ | 用到 agent 就用 |
| agent 权限三表 | ✅ | ⚠️ 条件 | ⚠️ 条件 | 唯一要求 `organizations` + `users` 有行的地方 |
| `memory_items` / `icron_tasks` / `icron_task_runs` | ✅ | ✅ | ✅ | `scope` 需先 `register_memory_scopes()` / 服务层保证 |
| KB 30 表 | ✅ | ✅ | ✅ | 用到 KB 就全用;`kb_entity_types` 至少有平台 org 的 `concept` |
| `kb_shares` | ✅ | ⚠️ | ❌ | ③ 单租户没有第二个 org,分享无意义 |
| `embedding_migration_campaigns` / `embedding_reindex_jobs` | ✅ | ✅ | ✅ | 换 embedding 模型时才用 |
| `alembic_version` | ✅ | ✅ | ✅ | — |

### 桥接模式接入清单(照做即可)

```python
from nicekit.api.deps import Principal, set_principal_resolver
from nicekit.tenancy import ensure_org, subject_uuid, tenant_uuid  # 顶层 re-export

async def my_resolver(request) -> Principal:
    me = await my_auth(request)              # 认证失败抛 HTTPException(401),越权 403,别返回 None
    return Principal(
        org_id=tenant_uuid(me.company_id),   # 42 → 固定 UUID,同输入永远同输出,不需要映射表
        user_id=subject_uuid(me.user_id),
        role=my_role_of(me),                 # 内置 platform_admin/org_admin/member 或注册角色
    )

set_principal_resolver(my_resolver)          # 装配期调一次,在 create_app 之前
```

补充规则:

1. `tenant_uuid` / `subject_uuid` 用**独立命名空间**,`tenant_uuid("1")` 与 `subject_uuid("1")` 不会撞;
   多套外部系统接进同一平台时可再传自己的 `namespace`。
2. 映射**单向不可逆**;需要展示外部 ID 时宿主自己在业务侧留对照关系。
3. 空 `external_id` 会被拒绝 —— "没有租户"必须显式走单租户模式,而不是悄悄落到空串派生的分区。
4. 要用 agent 权限子系统就在**首次见到新租户时**调一次 `ensure_org(session, org_id, name=...)`
   (幂等,可以每次请求都调;不 commit,由调用方事务决定何时落盘)。
5. 注册 resolver 后 **SDK 的 auth router 就不该再挂载** —— 两套身份来源并存会让"当前是谁"不可推理。
6. 角色是自由字符串,业务角色经 `tenancy.roles.register_roles()` / `register_write_roles()` 追加,
   `role` 列是 `varchar(32)`,加角色不需要 `ALTER TYPE`,也不需要迁移。

---

## 7. 迁移、扩展与部署前提

### 迁移链

| 版本 | 说明 |
|---|---|
| `4a6a2d9d8aa4_baseline` | 建 **60** 张表 + 中文检索配置 + 45 张表的 RLS + 3 条 `platform_read` + 平台 org seed + RLS 自检 |
| `cb3e134da690_operations` | **当前 head**。加 `provider_probe_statuses` / `service_heartbeats` / `kb_operational_incidents` **3** 张运维表(第 3 张开 RLS) |

60 + 3 = 63,加上 Alembic 自建的 `alembic_version` 共 **64** 张,与 §1 的实测数吻合。

baseline 的 RLS 段有两条重要工程约定:

1. **只处理本迁移自己建出来的表**(`created_tables = set(inspect(bind).get_table_names())`):
   表清单取自 models 聚合器,而聚合器会随后续波次新增表,历史迁移不该去动它没建过的表
   (否则 `upgrade from base` 会在 `ALTER TABLE` 上炸 `UndefinedTable`)。后续表的 RLS
   由各自的增量迁移用同一个 helper 施加(`cb3e134da690` 即如此)。
2. **末尾自检**:`(set(rls_tables_check(metadata)) - _RLS_EXEMPT) & created_tables` 非空即 `RuntimeError`。

### 新增一张带 `org_id` 的表时必须做的四件事

1. 在 `models/<模块>.py` 定义模型;
2. **在 `models/__init__.py` 里登记**,否则 `alembic autogenerate` 看不到
   (`chat` 与 `agent_permission` 有交叉引用,必须成对登记);
3. 在增量迁移里调 `op_enable_org_rls(op, "<表名>")`;
4. 如果它需要跨租户只读,再调 `op_platform_read(op, "<表名>", platform_org)` 并在本文档 §4.3 补一行理由。

> **运行时代码一律从具体模块导入**(`nicekit.models.tenancy` / `.llm` / ...),
> 不要 `import nicekit.models`(顶层聚合器)—— 那会让"触碰任何模型即拖动全库 schema"。
> 聚合器仅供 alembic autogenerate 与测试建表使用。

### 部署前提

- PostgreSQL 镜像必须自带 `vector` / `pg_trgm` / `zhparser`(用 `deploy/postgres/Dockerfile` 构建);
- 双账号:`niceknowledge_migrator`(owner,跑 Alembic)/ `niceknowledge_app`(应用连接,受 RLS 约束);
  `ALTER DEFAULT PRIVILEGES` 让 app 账号自动拿到 migrator 新建表的 DML 权限;
- `zhparser` 是 untrusted 扩展,必须在 initdb 阶段以 `postgres` 身份安装,早于 owner 移交。

### 迁移之外的 seed(迁移只 seed 一行)

**迁移只 seed `organizations` 的平台行**;其余基线数据由 `runtime.bootstrap.bootstrap_platform()`
在装配期幂等写入(需要普通会话,内部各 `ensure_*` 自行 `SET LOCAL` org 上下文):

| 表 | seed 内容 | 由谁 |
|---|---|---|
| `organizations` | 平台 org(`slug='platform'`) | baseline 迁移 |
| `organizations` | 单租户 org(`slug='single-tenant'`)—— **仅 `single_tenant=True` 时** | `bootstrap_platform` |
| `kb_entity_types` | 领域无关的兜底类型 `concept`(`is_builtin=True`,不可删) | `ensure_entity_types` |
| `agent_cards` + `agent_card_versions` | 一张中性默认卡(`assistant`),`model_task='agent.default'`,启用全部内置通用工具 | `ensure_default_agent_card` |
| `prompts` / `model_routes` | KB 问答所需的 prompt 与路由 | `ensure_kb_prompts` / `ensure_kb_answer_route` |

> 默认卡的版本机制:seed 版本号高于库内最高版本才发新版并激活;
> 业务方经 admin 升过更高版本则不动 —— **seed 永远不会把人工改动踩掉**。
> 行业实体类型与业务专员卡属于宿主的 seed,SDK 不内置任何领域词表。

---

## 8. 已知不一致与设计疑点

> 以下每条都经过 psql 或源码核实。它们不是本文档的推测,而是**代码/注释与实际库状态的差异**,
> 接入前建议先确认预期。

### 疑点 1(高):`fk_knowledge_bases_active_snapshot` 在库里根本不存在

- `models/kb.py:263-268` 与 `4a6a2d9d8aa4_baseline.py:190` 都声明了
  `ForeignKeyConstraint(['id','active_snapshot_id'], ['knowledge_snapshots.kb_id','knowledge_snapshots.id'], use_alter=True)`;
- `cb3e134da690_operations.py` 的 docstring 明确写着"该 FK 由 baseline 用 `use_alter=True` 建过,
  autogenerate 每次都会重报(它比对不到 use_alter FK)",并据此**删掉了 autogenerate 提出的创建语句**;
- **实测**:`select conname from pg_constraint where conrelid='knowledge_bases'::regclass and contype='f'`
  → **0 行**。`knowledge_bases` 上一条外键都没有。
- 根因:`use_alter=True` 只对 SQLAlchemy 的 `metadata.create_all()` 拓扑排序生效;
  在 Alembic 的 `op.create_table()` 里,带 `use_alter` 的 FK 会被**静默跳过**,
  且 Alembic 不会补发 `ALTER TABLE ... ADD CONSTRAINT`。
- 后果:① `knowledge_bases.active_snapshot_id` 没有任何引用完整性(可以指向别的库的快照,
  甚至不存在的快照);② autogenerate 会**永远**重报这条 FK,而当前的应对办法是每次人工删掉,
  等于把一个真实缺失当成噪声长期忽略。

### 疑点 2(高):KB 的"平台库 + 分享库可读"策略只存在于注释里,数据库层没有实现

- `models/kb.py` 模块 docstring(第 4-8 行)写:
  > 可见性三层:1. 本 org 自有库(读写);2. 平台 org 的库 = 平台层,全租户可读;
  > 3. 其他 org 通过 `kb_shares` 显式分享给我的库(只读)。
  > **RLS 读策略 = 本 org OR 平台 org OR 已分享;写策略 = 仅本 org(见迁移文件)。**
- **实测**:`knowledge_bases`(以及全部 KB 表)上只有一条 `org_isolation` 策略,
  `USING` 与 `WITH CHECK` 都是纯 `org_id = current_org_id`。**没有平台 org 旁路,没有 `kb_shares` 子查询。**
- **应用层确实写了三层可见性,但被 RLS 挡在下面**,形成一处"两层实现、下层更严"的死代码:
  - `kb/image_assets.py::visible_kb_filter(request_org_id)` 生成
    `org_id = 本 org OR org_id = platform_org_id OR EXISTS(kb_shares 授予本 org)`;
  - `kb/search.py::_layer(org_id, row_org)` 把命中行标成 `tenant` / `platform` / `shared`,
    `_LAYER_ORDER` 还据此做同分破局排序;
  - 但这些查询全部跑在 `Depends(get_org_session)` 的 org 绑定会话上,
    `knowledge_bases` / `kb_shares` / `kb_image_assets` 的 `org_isolation` 会**先**把非本 org 的行滤掉。
    结果:`visible_kb_filter` 的后两个 `OR` 分支永远不可能匹配,`_layer()` 永远只返回 `tenant`。
  - `kb/search.py` 模块 docstring 第 3 行"RLS 保证只能读到本 org、显式共享与平台层"同样与实际策略不符。
- `kb_shares` 表本身是活的:`api/v1/kb.py` 有完整的建/列/删分享端点,
  `kb/lifecycle.py` 在 purge 时会统计并清理它 —— 但**没有任何读路径能让 grantee 真正读到被分享的数据**。
- 接入影响:如果宿主指望"把库建在平台 org 下让全租户读"或"分享库给兄弟租户",按现状**读不到**。
  要启用需二选一:① 在这些 KB 表上追加类似 `platform_read` 的 RLS 旁路策略;
  ② 让跨库读走非 org 绑定的会话并由应用层全权过滤(风险更高)。

### 疑点 3(中):`ensure_org()` 的文档低估了外键面

- `tenancy/orgs.py` docstring 写:"64 张表里带 `org_id` 的有 45+ 张,其中**只有 3 张 agent 权限表**
  带指向 `organizations` 的外键"。
- **实测**:带 `org_id` 的是 **52** 张(不是 45+);指向 `organizations` 的外键是 **6** 条
  —— 除 3 张 agent 权限表外,还有 `memberships` / `invitations` / `refresh_tokens`。
  (后 3 张在桥接/单租户模式下不写,所以结论"调 `ensure_org()` 即可"对**这三张**仍然成立,
  但表述本身不准确,容易让人以为全库只有 3 条 org 外键。)
- 更实际的缺口:那 3 张 agent 权限表还有 **NOT NULL 的 `users` 外键**
  (`agent_permission_grants.user_id`、`agent_permission_grants.created_by`、
  `agent_permission_preferences.user_id`,均 `ON DELETE CASCADE`)。
  `tenancy/` 包里**只有 `ensure_org()`,没有 `ensure_user()`**。
  桥接模式下要用权限子系统,宿主得自己往 `users` 表插行(而 `users.email` 有唯一约束、
  `password_hash` 是 NOT NULL Text),这一步在文档里没有提。

### 疑点 4(中,**已在工作区修复,尚未 commit**):单租户默认 org 曾等于平台 org

- **原状**:`single_tenant_resolver()` 默认 `org_id = settings.platform_org_id`
  = `00000000-0000-0000-0000-000000000001`,而 `usage_daily` / `llm_traces` / `chat_events`
  的 `platform_read` 策略正是以这个 UUID 为条件 —— 单租户部署的默认租户**恒定满足平台旁路**。
  单租户期间无害,但一旦演进成多租户,那个"默认租户"会永久持有 3 张表的跨租户读权限;
  同时它在平台 org 下建的知识会因 `_layer`/`visible_kb_filter` 的 platform 分支对新租户可见。
- **现状**:租户接入线已引入独立的 `api.deps.SINGLE_TENANT_ORG_ID = tenant_uuid("__single_tenant__")`,
  默认值改用它,并提供 `runtime.bootstrap.bootstrap_platform(session, single_tenant=True)` 垫行。
- **遗留代价**:这个 org 不在迁移 seed 里,首次启动漏垫会让 3 张 agent 权限表的写入撞外键。
  部署检查清单里要有这一步。

### 疑点 5(低):三个 GUC 被应用层设置,但数据库里没有任何东西读它们

- 应用代码会设 `app.build_snapshot_id`(`kb/lifecycle.py`、`kb/projection_gc.py`、
  `kb/snapshot.py`、`kb/snapshot_media.py`)、`app.legacy_projection_gc`、
  `app.wiki_review_write`(`api/v1/kb.py`);
- **实测**:整库无触发器(0)、无 rule(0),`pg_policies` 里只有 `org_isolation` 与
  `platform_read` 两种策略,二者只读 `app.current_org_id`。
  **没有任何数据库对象消费上述三个 GUC。**
- 判断:这三个 `set_config` 是从上游系统(TF)带过来的遗留写法。在 TF 里它们对应快照作用域 RLS
  策略,漏设会导致 `DELETE` 静默删 0 行;在 nicekit 里它们目前是空操作。
- 风险:留着不害事,但会误导后来者以为"投影表有快照级 RLS 保护"。
  若后续要恢复快照级策略,需要显式补迁移;若确认不要,应清理这些调用。

### 疑点 6(低):`agent_cards` 的 docstring 描述的 RLS 策略并不存在

- `models/chat.py:3-5` 写:"`agent_cards`… **RLS 读策略同时放行 平台 org 与 NULL 两种平台表示**";
- **实测**:`agent_cards.relrowsecurity = false`,它在 `_RLS_EXEMPT` 里,**根本没开 RLS**,
  自然也没有那条"同时放行"的策略。访问控制完全在 API 层。
- 同类:`agent_cards` 上还残留一组"仅用于旧库兼容"的能力字段
  (`system_prompt` / `model_task` / `max_turns` / `timeout_seconds` / `tools`),
  运行时读的是 `agent_card_versions`。这些列是 `NOT NULL`,新建卡时仍必须填,
  属于双写负担,建议后续迁移清理。

### 疑点 7(低):`outbox_events.kb_id` 是 NOT NULL,发件箱被绑死在 KB 上

- `outbox_events` 定位是通用的事务性发件箱(`aggregate_type` / `event_type` 都是自由字符串),
  但 `kb_id` 列是 `NOT NULL` 且带 `→ knowledge_bases.id` 外键。
- 后果:非 KB 领域的事件无法复用这张表(没有合法的 `kb_id` 可填)。
  如果宿主想用 outbox 模式发业务事件,需要另建表或放宽这一列。
