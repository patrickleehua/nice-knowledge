"""配置中心:按域拆分的 Settings(迁移自 TF backend/app/core/config.py)。

拆分原则(MIGRATION-PLAN §5.1):
- CoreSettings:db/redis/jwt/cors/log/task_dispatch/platform_org_id/密钥/限流/minio;
- LlmSettings:llm_* 与双协议 provider 凭证;
- KbSettings:kb_* 与 embedding/rerank(为 KB 检索链服务);
- AgentSettings:skills/icron/agent 运行时;
- CapabilitySettings:websearch/imagegen/weather/smtp(通知)。

组合类 `Settings` 多继承五个域,字段名与 env 变量名与 TF 完全一致,
迁移过来的代码可直接引用同名字段;`get_settings()` 保持 lru_cache 入口。
源仓库中的旅游业务专属配置项(比价 / 履约提醒 / 业务流水线时限)不迁移。
"""

from functools import lru_cache
from typing import Annotated, Self
from uuid import UUID

from pydantic import AliasChoices, BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_debug(value: object) -> object:
    """Accept common deployment words from ambient DEBUG env vars."""
    if not isinstance(value, str):
        return value
    normalized = value.strip().casefold()
    if normalized in {"release", "production", "prod", "false", "0", "off", "no"}:
        return False
    if normalized in {"debug", "development", "dev", "true", "1", "on", "yes"}:
        return True
    return value


class _DomainSettings(BaseSettings):
    """各域 Settings 的公共基类:统一 .env 读取与未知项忽略策略。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class CoreSettings(_DomainSettings):
    app_name: str = "NiceKit"
    deployment_environment: str = "development"
    debug: Annotated[bool, BeforeValidator(_parse_debug)] = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    database_url: str = (
        "postgresql+psycopg://niceknowledge_app:app-dev-secret@localhost:5433/niceknowledge"
    )
    migration_database_url: str = (
        "postgresql+psycopg://niceknowledge_migrator:migrator-dev-secret"
        "@localhost:5433/niceknowledge"
    )
    # 数据库连接池(默认 None = 交给 SQLAlchemy 默认值,不显式传参)
    db_pool_size: int | None = Field(default=None, gt=0)
    db_max_overflow: int | None = Field(default=None, ge=0)
    db_pool_recycle: int | None = Field(default=None, gt=0)
    db_pool_timeout: float | None = Field(default=None, gt=0)

    platform_org_id: UUID = UUID("00000000-0000-0000-0000-000000000001")

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # 密钥加密 master key(core/secretbox.py):空 = 明文直通模式(开发零配置)。
    # env 首选 NICEKIT_SECRET_KEY,同时兼容 SECRET_KEY_MASTER。
    secret_key_master: str = Field(
        default="",
        validation_alias=AliasChoices("nicekit_secret_key", "secret_key_master"),
    )

    redis_url: str = "redis://localhost:6380/0"
    # redis 缓存 key 命名空间前缀(多套部署共用 redis 时隔离用);空 = 不加前缀
    cache_namespace: str = ""

    # 可观测性(prod-readiness-1 D1)
    log_level: str = "INFO"
    log_format: str = "console"  # console(开发)/ json(生产结构化单行)

    # Runtime identity and durable API/worker/beat operational heartbeats.
    service_version: str = Field(default="0.1.0", min_length=1, max_length=100)
    service_instance_id: str = Field(default="", max_length=160)
    operations_heartbeat_interval_seconds: int = Field(default=15, gt=0)
    operations_heartbeat_expiry_seconds: int = Field(default=60, gt=0)
    operations_provider_probe_interval_seconds: int = Field(default=300, ge=0)
    operations_provider_probe_timeout_seconds: float = Field(default=15.0, gt=0)
    operations_provider_degraded_seconds: int = Field(default=900, gt=0)

    # 任务可靠性(D2/D3):派发模式与 stale 回收
    task_dispatch_mode: str = "inline"  # inline(BackgroundTasks)/ celery(生产主路径)
    stale_run_timeout_seconds: int = 1800  # running 超过该时长视为卡死,周期清扫回收
    stale_sweep_interval_seconds: int = 60  # 0 = 关闭周期清扫(启动恢复仍执行)

    # API 限流(D5):每客户端(令牌/IP)每分钟请求数,0 = 关
    rate_limit_per_minute: int = 240
    rate_limit_backend: str = "memory"  # memory(单副本)/ redis(多副本共享计数)
    # 限流豁免路径(TF 中为硬编码 frozenset,SDK 化后改为可配置)
    rate_limit_exempt_paths: list[str] = ["/api/v1/health"]

    minio_endpoint: str = "localhost:9010"
    minio_access_key: str = "niceknowledge"
    minio_secret_key: str = "niceknowledge-dev-secret"
    minio_bucket: str = "niceknowledge"
    minio_secure: bool = False

    @model_validator(mode="after")
    def validate_operations_heartbeat(self) -> Self:
        if self.operations_heartbeat_interval_seconds >= self.operations_heartbeat_expiry_seconds:
            raise ValueError("operations heartbeat interval must be shorter than expiry")
        return self


class LlmSettings(_DomainSettings):
    openai_api_key: str = ""
    openai_base_url: str = ""  # 空 = SDK 官方默认;网关形如 http://host:port/v1
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""  # 空 = SDK 官方默认;网关形如 http://host:port

    # 无 model_routes 命中时的全局默认路由与降级
    llm_default_provider: str = "openai"
    llm_default_model: str = ""
    llm_fallback_provider: str = ""
    llm_fallback_model: str = ""
    llm_default_max_tokens: int = 8192
    llm_timeout_seconds: float = 120.0
    # Anthropic 线主动开启思考(agent 工具调用一跳):0 = 不开启(被动接收上游返回),
    # >0 = thinking budget_tokens,思考过程稳定输出。开启前需实测网关/模型支持
    # 且不强制回传 thinking block(tool 循环两跳);必须 < max_tokens。
    llm_anthropic_thinking_budget: int = 0
    # ---- 思考等级(agent 工作台可调,全部可经 env 覆盖,env 值为 JSON):
    # 有序档位表,前端下拉按此渲染(默认对齐 GPT-5.1/Codex:none~xhigh 已实测网关接受)
    llm_thinking_levels: list[str] = ["off", "minimal", "low", "medium", "high", "xhigh"]
    # 档位展示名覆盖(缺省用内置中文名,再缺省用档位原值)
    llm_thinking_level_labels: dict[str, str] = {}
    llm_thinking_default_level: str = "medium"
    # openai 线:档位 → reasoning.effort(未列出的档位原样透传)
    llm_openai_effort_map: dict[str, str] = {"off": "none"}
    # 兼容性开关:列出的 openai 实例名(llm_providers.name)不发送 max_output_tokens。
    # 部分聚合网关的上游是 ChatGPT/Codex 协议代理,不认这个参数,带上就整个请求 400
    # (任何值都一样)。默认空 = 所有实例照常发送,官方 OpenAI 行为不受影响。
    llm_openai_omit_max_output_tokens: list[str] = []
    # anthropic 线:档位 → thinking budget_tokens(未列出 = 不开启;实际值会
    # 钳制到 max_tokens-1024 以内,低于 1024 视为不开启)
    llm_anthropic_thinking_budgets: dict[str, int] = {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "xhigh": 16384,
    }
    # provider 失败冷却:某条 provider:model 线 retryable 失败后,冷却窗口内的
    # 后续调用直接跳过它从降级链下一位开始(避免主线故障时每跳都白等失败);
    # 窗口过后自动恢复尝试。0 = 关闭。
    llm_provider_cooldown_seconds: int = 120

    # LLM org 级守门(D4):0 = 不限
    llm_org_max_concurrency: int = 3
    llm_org_daily_token_budget: int = 0  # 当日 tokens_in+tokens_out 总额上限

    # LLM registry 缓存(排期项 4/4):prompt/路由解析结果 TTL,0 = 关缓存
    llm_registry_cache_ttl_seconds: int = 60


class KbSettings(_DomainSettings):
    # 向量维度:必须与 baseline 迁移里的 vector(D) 及所用嵌入模型一致。
    # 换模型需走 embedding_reindex campaign 全量重建,不能只改这个值。
    kb_embedding_dim: int = Field(default=1024, gt=0)
    # 中文全文检索配置名(zhparser),由 baseline 迁移创建。改名需同步迁移里的
    # CREATE TEXT SEARCH CONFIGURATION 与各 tsvector 生成列表达式。
    kb_fts_regconfig: str = "public.nicekit_zhparser"

    # KB ingestion leases are shared by inline and celery execution paths.
    kb_ingest_lease_seconds: int = Field(default=120, gt=0)
    kb_ingest_heartbeat_seconds: int = Field(default=30, gt=0)
    kb_upload_max_file_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    kb_upload_max_batch_files: int = Field(default=50, gt=0)
    kb_image_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    kb_image_max_dimension: int = Field(default=20_000, gt=0)
    kb_image_max_pixels: int = Field(default=40_000_000, gt=0)
    # 装饰性图标下限:低于任一下限的插图(项目符号/箭头/logo)不入库也不富化。
    # 实测一份研报 230 张插图中,任一边 <64 或面积 <128x128 的 98 张 OCR 命中全为空,
    # 却各占一次 OCR + 一次 VLM caption。置 0 关闭该过滤。
    kb_image_min_dimension: int = Field(default=64, ge=0)
    kb_image_min_pixels: int = Field(default=16_384, ge=0)
    # 单文档内图片富化的并发张数。OCR 侧 CPU 已饱和(实测多实例并发仅 1.1x),
    # 并发的收益来自让 caption 的网络等待与下一张图的 OCR 重叠。
    kb_image_enrich_concurrency: int = Field(default=4, gt=0, le=32)
    # 单文档内抽取分段的并发数(结构化事实与实体图谱各自适用)。
    # 实际并发还会被 llm_org_max_concurrency 二次收敛。
    kb_extract_concurrency: int = Field(default=3, gt=0, le=16)
    # 图片审核自动化:富化成功(OCR/caption 均无 failure_code)且非重复图的插图
    # 直接判 accepted,只有失败、重复、caption 缺失的才升级人工。一份研报能有
    # 上百张图,逐张点通过不具可维护性。置 False 回到全量人工审核。
    kb_image_auto_accept: bool = True
    # Wiki 草稿自动发布:能进投影的 wiki_page 事实必然已通过事实审核(AI/人工),
    # 再压一道页面级人工发布是双重审核 —— 实测内容全部卡在 pending_review,
    # 而 wiki 页只读已发布正文,用户看到的永远是空白。开关开时 confirmed 即发布,
    # 人工显式 rejected 的仍然不投影;置 False 回到"确认后仍需逐页点发布"。
    kb_wiki_auto_publish: bool = True
    kb_image_thumbnail_max_dimension: int = Field(default=512, gt=0)
    kb_image_ocr_timeout_seconds: float = Field(default=120.0, gt=0)
    kb_image_ocr_max_chars: int = Field(default=4000, gt=0, le=20_000)
    kb_image_caption_max_chars: int = Field(default=2000, gt=0, le=2000)
    kb_caption_timeout_seconds: float = Field(default=120.0, gt=0)

    # KB outbox:0 poll interval disables the local loop / celery beat schedule.
    kb_outbox_poll_interval_seconds: int = Field(default=2, ge=0)
    kb_outbox_batch_size: int = Field(default=50, gt=0)
    kb_outbox_concurrency: int = Field(default=4, gt=0)
    kb_outbox_lease_seconds: int = Field(default=60, gt=0)
    kb_outbox_max_attempts: int = Field(default=5, gt=0)
    kb_outbox_retry_base_seconds: int = Field(default=5, gt=0)
    kb_outbox_retry_max_seconds: int = Field(default=300, gt=0)
    kb_document_purge_retention_days: int = Field(default=30, ge=0)
    kb_lifecycle_purge_worker_enabled: bool = False

    # KB 陈旧度(prod-readiness-4):文档过期提醒扫描间隔,0 = 关
    kb_expiry_sweep_interval_seconds: int = 86400

    # KB 孤儿数据巡检(report-only,对标 OWASP RAG 审计):间隔秒,0 = 关
    kb_orphan_audit_interval_seconds: int = Field(default=86400, ge=0)

    # KB 归档保留期到期清理候选提醒(只通知不删):扫描间隔秒,0 = 关
    kb_purge_due_interval_seconds: int = Field(default=21600, ge=0)

    # KB 事实 AI 审核定时扫描(自动化优先):间隔秒,0 = 关;每轮每 org 处理上限
    kb_ai_review_sweep_interval_seconds: int = Field(default=300, ge=0)
    kb_ai_review_sweep_batch_size: int = Field(default=200, ge=1, le=500)

    # KB 文档解析后端(KB-1):fast / docling;配置未知后端时自动回退 fast,启动不报错
    kb_parser_backend: str = "docling"

    # Contextual Retrieval(分块上下文增强):摄入 GENERAL 文档时为每个 chunk
    # 生成 1-2 句"该块在全文档中的定位"上下文,随 chunk 进 embedding 与 BM25。
    # 默认关闭:nDCG 检索线已封版、评测链路重建中,待评测恢复后 A/B 验证增益再开。
    # LLM 失败/限流时摄入侧整体降级为无 context(与 caption 同姿态,不阻塞摄入)。
    kb_contextual_chunking_enabled: bool = False
    # 每次 LLM 调用批量处理的 chunk 数(批内带 index 对齐校验)
    kb_contextual_chunk_batch_size: int = Field(default=10, gt=0)

    # KB 图片视觉 caption(VLM):必须同时显式配置专用 provider/model。
    # 执行始终使用 exact model_override,不进入任务路由的 provider/model 降级链。
    kb_caption_provider: str = ""
    kb_caption_model: str = ""

    # 首次部署的 active embedding 指纹;凭证和后续模型切换只走提供商/系统路由。
    embedding_provider: str = "siliconflow"
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_max_items: int = Field(default=32, gt=0)
    embedding_batch_max_estimated_tokens: int = Field(default=8192, gt=0)
    embedding_reindex_batch_size: int = Field(default=64, gt=0)
    embedding_reindex_lease_seconds: int = Field(default=300, gt=0)
    embedding_dual_read_seconds: int = Field(default=86400, gt=0)
    embedding_migration_poll_interval_seconds: int = Field(default=60, ge=0)

    # KB cross-encoder rerank 全局开关;模型、凭证与超时来自系统路由。
    rerank_enabled: bool = True
    rerank_timeout_seconds: float = Field(default=3.0, gt=0)

    # KB 向量检索的 cosine 距离上限(KB-5B):bge-m3 余弦距离口径下,
    # 语义相关内容通常 <0.55,乱码/无意义查询对任意 chunk 普遍 >0.68;
    # 超过该阈值的向量命中丢弃(检索为空如实返回,不给乱码查询编造 top-k 最近邻)。
    # PostgreSQL FTS sparse 通道不受该距离阈值影响。
    kb_vector_max_distance: float = 0.62

    # KB 检索缓存(两层,均可单独关闭;redis 不可用时自动降级为直查库)。
    # 结果缓存的键压了一枚"当前可见 (kb, active_snapshot) 集合"的指纹,发布新
    # 快照、切换快照指针或可见范围变化都会立刻换键,因此 TTL 只是兜底上限,不是
    # 数据新鲜度的唯一保障。0 = 关闭该层。
    # 取 60s 的理由:同一个人反复刷新/翻页是主要重复来源,而摄入→发布链路远长于
    # 一分钟,拿不到"刚写完就该看到"的场景;要严格实时可设为 0。
    kb_search_cache_ttl_seconds: int = Field(default=60, ge=0)
    # query 向量是 (文本, 模型, 维度) 的确定映射,与知识库内容无关,可长期缓存。
    # 换嵌入模型不需要清理:模型名与维度已经在缓存键里。
    kb_query_vector_cache_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=0)
    # 口径澄清(2026-07-23):search.py manifest 里的 enable_gate_min_gain_points:
    # 5.0 只是文档性声明(参与 config_fingerprint,不可改动),代码中没有自动
    # gain 门控。真正的门 = 本开关默认 False + 评测实测增益 >= 5pt 后人工改 True。
    #
    # 2026-08-03 记:这道门曾长期是**空的**——图谱一路整体空转(结果恢复只认检索
    # 卡片,而普通文档只产出 entity_mention 与关系事实、按设计不建卡),开与关结果
    # 完全一致,增益无从谈起;配套评测工具也从未实现。断链修复 + nicekit.kb.graph_eval
    # 补齐后,门槛才从"不可执行"变成"可执行"。
    #
    # 2026-08-04 按实测改为默认 True。评测语料见 docs/graph-eval-corpus/
    # (六篇专门构造的跨文档多跳语料 + 10 条分层标注),连跑三次结果一致:
    #     multi_hop   Recall 0.800→0.900 (+10.0pt),nDCG +6.1pt   达标
    #     single_hop  Recall 1.000→1.000 (+0.0pt)                不伤简单查询
    # 增益量级与 HippoRAG2 公开报告的 +7~13pt 吻合。典型贡献:纯多跳问题
    # (其它通道够不着答案文档)off 恒 0 → on 恒 0.5;另有两条 off 在 0/1 间跳变
    # 的查询在开启后稳定为 1.0——图谱反而消除了波动。
    #
    # 这个默认值的**依据边界**要清楚:10 条样例、专门构造的语料,够证明链路有效,
    # 不足以代表任意业务语料。换语料请按 docs/graph-eval-corpus/README.md 复测,
    # 并且必须按 query 类型**分层**看、不要只看总均值:公开评测(GraphRAG-Bench
    # arXiv:2506.05690、RAG vs. GraphRAG arXiv:2502.11371)一致显示图谱增益高度
    # 依赖问题类型,简单单跳事实检索上它常因引入冗余而低于纯向量+稀疏,混合
    # query 集的总均值会把两侧同时掩盖。长期更优解是按 query 意图路由。
    kb_graph_search_enabled: bool = True
    kb_graph_max_hops: int = Field(default=2, ge=1, le=2)

    # KB 健康巡检(监控双轨之二:系统内主动告警):间隔秒,0 = 关;
    # 各阈值 0 = 该项不告警(指标 Gauge 仍照常刷新)
    kb_health_sweep_interval_seconds: int = Field(default=3600, ge=0)
    kb_health_outbox_backlog_threshold: int = Field(default=100, ge=0)
    kb_health_vectorless_chunks_threshold: int = Field(default=50, ge=0)
    kb_health_failed_docs_threshold: int = Field(default=5, ge=0)
    kb_health_pending_claims_threshold: int = Field(default=200, ge=0)
    # PARSING 超过该时长视为卡死,计入文档异常项(与 failed 文档共用阈值)
    kb_health_stuck_parsing_seconds: int = Field(default=1800, gt=0)
    # Low-volume but old lifecycle work must alert even below count thresholds.
    kb_health_outbox_old_min_count: int = Field(default=6, gt=0)
    kb_health_outbox_old_age_seconds: int = Field(default=900, gt=0)
    kb_health_uploaded_old_age_seconds: int = Field(default=900, gt=0)
    kb_health_processing_old_age_seconds: int = Field(default=1800, gt=0)
    kb_health_ingest_lease_old_age_seconds: int = Field(default=180, gt=0)
    kb_health_image_enrichment_old_age_seconds: int = Field(default=900, gt=0)
    kb_health_snapshot_build_old_age_seconds: int = Field(default=1800, gt=0)
    kb_health_incident_old_age_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def validate_kb_ingest_lease(self) -> Self:
        if self.kb_ingest_heartbeat_seconds >= self.kb_ingest_lease_seconds:
            raise ValueError("kb_ingest_heartbeat_seconds must be shorter than the lease")
        return self


class AgentSettings(_DomainSettings):
    # Agent Skill packages (relative to backend working directory by default).
    skills_dir: str = "skills"

    # iCron 自主定时任务:调度扫描间隔秒,0 = 关闭(任务仍可手动运行)。
    # 默认 60 秒 = cron 表达式的最小粒度,再密没有意义(cron 分辨率就是分钟)。
    icron_tick_interval_seconds: int = Field(default=60, ge=0)
    # 单轮每个 org 最多领取的到期任务数;积压的下一轮接着扫
    icron_tick_batch_size: int = Field(default=20, ge=1, le=200)

    # Agent native 工具的总墙钟时限。与 HTTP connect/read/write/pool timeout
    # 分层:即使上游流持续有数据,整个工具调用也不能无限延长。
    agent_native_tool_timeout_seconds: float = Field(default=300.0, gt=0)
    # 工具运行期间协作式检查 chat.cancel_requested 的间隔。
    agent_cancellation_poll_interval_seconds: float = Field(default=0.25, gt=0)

    agent_kb_image_inspection_enabled: bool = False
    agent_kb_image_inspection_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        gt=0,
    )


class CapabilitySettings(_DomainSettings):
    # 联网搜索:auto = 按 tavily → bocha 顺序取有 key 的组成故障转移链,
    # 都没 key 时工具如实返回 unavailable(不报错不阻塞)
    websearch_provider: str = "auto"  # auto / tavily / bocha
    tavily_api_key: str = ""
    bocha_api_key: str = ""
    websearch_timeout_seconds: float = 15.0
    websearch_max_results: int = 5  # 工具未指定时的默认条数(上限 10)
    # 多检索式:一次工具调用可并发跑几条 query(比较类问题拆成多条比串行多轮 loop 快)
    websearch_max_queries: int = 3
    # 结果缓存(redis,不可达自动降级直查):限时效的查询 TTL 短,常识性查询 TTL 长。
    # 命中缓存不计 usage——外部搜索按请求计费,复用不该重复扣。
    websearch_cache_ttl_seconds: int = 1800
    websearch_cache_ttl_fresh_seconds: int = 300
    websearch_cache_enabled: bool = True

    # 网页正文抓取(web_fetch):URL 来自模型,SSRF 防护在 websearch/fetch.py 内做,
    # 这里只控成本与体量。max_chars 是单页喂给模型的上限,压缩在其之后再收一次。
    websearch_fetch_timeout_seconds: float = 15.0
    websearch_fetch_max_chars: int = 8000
    websearch_fetch_max_pages: int = 5  # 单次工具调用最多抓几页
    websearch_fetch_concurrency: int = 4
    # 正文压缩:none 原样 / cutoff 平均截断 / rerank 分块重排(复用 KB reranker)。
    # 默认 cutoff——零外部依赖且够用;rerank 精度更高但要多打一次 rerank 路由。
    websearch_compression_method: str = "cutoff"
    websearch_compression_total_chars: int = 12000

    # 模型内置联网搜索(provider 服务端执行):与自建搜索互斥,由会话选择决定走哪条。
    # Anthropic 的工具版本号要与模型支持的版本对得上,默认取兼容面最广的 GA 版;
    # SDK 0.116 另支持 web_search_20260209 / web_search_20260318。
    anthropic_web_search_tool: str = "web_search_20250305"
    # 内置搜索的单轮次数上限(Anthropic max_uses;OpenAI 侧无此参数)
    builtin_web_search_max_uses: int = 5

    # 图片生成:显式配置 GPT-Image-2 兼容网关,无 key = 本轮不暴露工具。
    # images = /v1/images/generations;chat = /v1/chat/completions SSE 正文提图。
    imagegen_api_key: str = ""
    imagegen_base_url: str = "https://api.v36.cm"
    imagegen_model: str = "gpt-image-2-c"
    imagegen_api_mode: str = "chat"  # images / chat
    imagegen_timeout_seconds: float = 300.0

    # 天气多源:weatherapi 主全球源 / 和风(qweather)中国增强 /
    # open-meteo 免 key 兜底;key/host 为空 = 对应 provider 不启用(available()=False)
    weatherapi_api_key: str = ""
    qweather_api_key: str = ""
    qweather_api_host: str = ""  # 和风每账号专属 API Host,形如 xxx.qweatherapi.com

    # 邮件通知(prod-readiness-3):smtp_host 为空(默认)= 只站内信不发邮件
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@niceknowledge.local"
    smtp_starttls: bool = True


class Settings(CoreSettings, LlmSettings, KbSettings, AgentSettings, CapabilitySettings):
    """组合入口:字段并集,env 变量名与 TF 完全兼容。"""

    @model_validator(mode="after")
    def validate_cross_domain(self) -> Self:
        # agent 侧图片审读的体积上限不能超过 KB 图片入库上限(跨域约束,
        # 只能在组合类上校验;各域内部约束在各自的 validator 里)
        if self.agent_kb_image_inspection_max_bytes > self.kb_image_max_bytes:
            raise ValueError("agent_kb_image_inspection_max_bytes cannot exceed kb_image_max_bytes")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
