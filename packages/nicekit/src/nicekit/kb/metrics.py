"""KB 子系统的 Prometheus 指标(MIGRATION-PLAN §5.1:core/metrics 只留骨架,
业务指标由各子包自行注册到共享 registry)。

原则:埋点只做可见性,绝不改变业务控制流——失败仍按既有路径静默降级,
这里只是让降级次数/积压水位变得可观测。

计数器(Counter)由各埋点处同步递增;水位(Gauge)由 KB 健康巡检
(nicekit/kb/health_sweep.py)每轮逐 org 刷新。
"""

from prometheus_client import Counter, Gauge, Histogram

from nicekit.core.metrics import get_registry

registry = get_registry()

# ---- rerank(nicekit/kb/rerank.py) ----
KB_RERANK_CALLS = Counter(
    "kb_rerank_calls_total",
    "KB rerank 调用次数,outcome=ok|timeout|error",
    ["outcome"],
    registry=registry,
)
KB_RERANK_LATENCY = Histogram(
    "kb_rerank_latency_seconds",
    "KB rerank 单次调用时延(含超时/失败)",
    registry=registry,
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
)

# ---- 检索(nicekit/kb/search.py) ----
KB_SEARCH_DENSE_DEGRADED = Counter(
    "kb_search_dense_degraded_total",
    "KB 检索 dense 通道整体降级次数(embedding 不可用/预算超限,退化纯词面)",
    registry=registry,
)
KB_SEARCH_RERANK_DEGRADED = Counter(
    "kb_search_rerank_degraded_total",
    "KB 检索 rerank 降级为纯 RRF 次数,reason=config|call",
    ["reason"],
    registry=registry,
)
KB_SEARCH_EMPTY = Counter(
    "kb_search_empty_total",
    "KB 检索空结果次数(各通道均无候选)",
    registry=registry,
)
KB_SEARCH_REFUSALS = Counter(
    "kb_search_refusals_total",
    "KB 检索负例拒答次数(仅 dense 命中、无词面/结构化佐证,如实拒答)",
    registry=registry,
)

# ---- outbox(nicekit/kb/outbox.py) ----
KB_OUTBOX_EVENTS = Counter(
    "kb_outbox_events_total",
    "KB outbox 事件处理结局计数,outcome=published|retried|dead_lettered|recovered",
    ["outcome"],
    registry=registry,
)
KB_OUTBOX_CLAIMED = Counter(
    "kb_outbox_claimed_total",
    "KB outbox 累计 claim 事件数",
    registry=registry,
)
KB_OUTBOX_ROUND_CLAIMED = Gauge(
    "kb_outbox_round_claimed",
    "KB outbox 最近一轮 claim 事件数",
    registry=registry,
)

# ---- embedding(nicekit/kb/embedding.py) ----
KB_EMBEDDING_CALLS = Counter(
    "kb_embedding_calls_total",
    "KB embedding 受治理批次调用次数",
    registry=registry,
)
KB_EMBEDDING_FAILURES = Counter(
    "kb_embedding_failures_total",
    "KB embedding 批次调用失败次数(EmbeddingUnavailableError 口径)",
    registry=registry,
)
KB_EMBEDDING_OVERSIZE_RETRIES = Counter(
    "kb_embedding_oversize_retries_total",
    "KB embedding 输入超长重试次数(批次二分或单条截半)",
    registry=registry,
)

# ---- KB 健康巡检水位(nicekit/kb/health_sweep.py,逐 org 刷新) ----
KB_OUTBOX_PENDING_BACKLOG = Gauge(
    "kb_outbox_pending_backlog",
    "KB outbox PENDING 积压数(按 org)",
    ["org"],
    registry=registry,
)
KB_OUTBOX_OLDEST_PENDING_AGE = Gauge(
    "kb_outbox_oldest_pending_age_seconds",
    "KB outbox 最老 PENDING 事件年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_OUTBOX_DEAD_LETTER = Gauge(
    "kb_outbox_dead_letter",
    "KB outbox DEAD_LETTER 事件数(按 org)",
    ["org"],
    registry=registry,
)
KB_CHUNKS_MISSING_EMBEDDING = Gauge(
    "kb_chunks_missing_embedding",
    "活跃投影内无向量且未隔离的 kb_chunks 数(按 org)",
    ["org"],
    registry=registry,
)
KB_DOCS_FAILED = Gauge(
    "kb_docs_failed",
    "状态为 FAILED 的源文档数(按 org)",
    ["org"],
    registry=registry,
)
KB_DOCS_STUCK_PARSING = Gauge(
    "kb_docs_stuck_parsing",
    "卡在 PARSING 超过阈值时长的源文档数(按 org)",
    ["org"],
    registry=registry,
)
KB_FACT_CLAIMS_PENDING = Gauge(
    "kb_fact_claims_pending_review",
    "SUGGESTED 待审 FactClaim 积压数(按 org)",
    ["org"],
    registry=registry,
)

KB_DOCS_UPLOADED = Gauge(
    "kb_docs_uploaded",
    "状态为 UPLOADED 的源文档数(按 org)",
    ["org"],
    registry=registry,
)
KB_DOCS_OLDEST_UPLOADED_AGE = Gauge(
    "kb_docs_oldest_uploaded_age_seconds",
    "最老 UPLOADED 源文档年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_DOCS_PROCESSING = Gauge(
    "kb_docs_processing",
    "状态为 PARSING 的源文档数(按 org)",
    ["org"],
    registry=registry,
)
KB_DOCS_OLDEST_PROCESSING_AGE = Gauge(
    "kb_docs_oldest_processing_age_seconds",
    "最老 PARSING 源文档处理年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_INGEST_LEASES = Gauge(
    "kb_ingest_leases",
    "RUNNING ingest leases 数量(按 org)",
    ["org"],
    registry=registry,
)
KB_INGEST_OLDEST_LEASE_AGE = Gauge(
    "kb_ingest_oldest_lease_age_seconds",
    "最老 RUNNING ingest lease 心跳年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_IMAGE_ENRICHMENT_PENDING = Gauge(
    "kb_image_enrichment_pending",
    "pending/processing 图片 enrichment 数量(按 org)",
    ["org"],
    registry=registry,
)
KB_IMAGE_ENRICHMENT_OLDEST_AGE = Gauge(
    "kb_image_enrichment_oldest_age_seconds",
    "最老 pending/processing 图片 enrichment 年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_SNAPSHOT_BUILDS = Gauge(
    "kb_snapshot_builds",
    "BUILDING knowledge snapshot 数量(按 org)",
    ["org"],
    registry=registry,
)
KB_SNAPSHOT_OLDEST_BUILD_AGE = Gauge(
    "kb_snapshot_oldest_build_age_seconds",
    "最老 BUILDING knowledge snapshot 年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_OUTBOX_OLDEST_DEAD_LETTER_AGE = Gauge(
    "kb_outbox_oldest_dead_letter_age_seconds",
    "最老 outbox DEAD_LETTER 年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_OPERATIONAL_INCONSISTENCIES = Gauge(
    "kb_operational_inconsistencies",
    "未解决对象/元数据一致性事件数(按 org)",
    ["org"],
    registry=registry,
)
KB_OPERATIONAL_OLDEST_INCONSISTENCY_AGE = Gauge(
    "kb_operational_oldest_inconsistency_age_seconds",
    "最老未解决对象/元数据一致性事件年龄秒数(按 org)",
    ["org"],
    registry=registry,
)
KB_MEDIA_PROJECTION_FAILURES = Gauge(
    "kb_media_projection_failures",
    "未解决媒体投影失败事件数(按 org)",
    ["org"],
    registry=registry,
)

KB_PROVIDER_PROBE_HEALTHY = Gauge(
    "kb_provider_probe_healthy",
    "最近一次定时 provider probe 是否健康或已显式禁用",
    ["capability"],
    registry=registry,
)
KB_PROVIDER_PROBE_LATENCY = Gauge(
    "kb_provider_probe_latency_seconds",
    "最近一次定时 provider probe 时延秒数",
    ["capability"],
    registry=registry,
)
