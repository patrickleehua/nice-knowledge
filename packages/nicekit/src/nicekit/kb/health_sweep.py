"""KB 系统健康巡检(监控双轨:Gauge 刷新 + 阈值超标站内信告警)。

全仓无外部监控栈,KB 的静默降级(embedding 失败无向量入库、outbox 死信、
文档解析失败/卡死、事实待审积压)只落日志会无声腐烂。本巡检逐 org
(过 FORCE RLS org_session)统计各水位:

- outbox PENDING 积压数、最老 pending 年龄、DEAD_LETTER 数;
- 活跃投影内 embedding IS NULL 且未隔离的 kb_chunks 数;
- SourceDocument FAILED 数、卡在 PARSING 超阈值时长数;
- FactClaim SUGGESTED 待审积压数。

每项写入 kb/metrics.py 的 Gauge(带 org 标签,供 /metrics 抓取);
超过 config 阈值(0=关)时给 KB 治理角色(ports.kb_notify_roles())发站内信
(kind=kb.health_alert,同日按标题幂等,email=False,正文列超标明细与
建议动作)。单 org 失败记日志跳过,不阻断其他 org。
巡检只读业务表 + 写通知,绝不改变任何业务状态。
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.domain.kb_media import ImageEnrichmentStatus
from nicekit.kb import metrics, ports
from nicekit.kb.projections import active_projection_filter
from nicekit.models.kb import (
    DocStatus,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbChunk,
    KbImageAsset,
    KnowledgeSnapshot,
    OutboxEvent,
    OutboxStatus,
    SnapshotStatus,
    SourceDocument,
)
from nicekit.models.tenancy import Notification, Organization

logger = logging.getLogger(__name__)

NOTIFY_KIND = "kb.health_alert"
_OBJECT_FAILURE_CODES = (
    "image_asset_unavailable",
    "missing_object",
    "object_unavailable",
    "object_missing_after_write",
)


@dataclass(frozen=True, slots=True)
class OrgHealthSnapshot:
    """单 org 单轮健康水位(全部为只读统计)。"""

    outbox_pending: int = 0
    outbox_oldest_pending_age_seconds: float = 0.0
    outbox_dead_letter: int = 0
    outbox_oldest_dead_letter_age_seconds: float = 0.0
    uploaded_docs: int = 0
    oldest_uploaded_doc_age_seconds: float = 0.0
    processing_docs: int = 0
    oldest_processing_doc_age_seconds: float = 0.0
    ingest_leases: int = 0
    oldest_ingest_lease_age_seconds: float = 0.0
    image_enrichment_pending: int = 0
    oldest_image_enrichment_age_seconds: float = 0.0
    snapshot_builds: int = 0
    oldest_snapshot_build_age_seconds: float = 0.0
    object_metadata_inconsistencies: int = 0
    oldest_object_inconsistency_age_seconds: float = 0.0
    media_projection_failures: int = 0
    oldest_media_projection_failure_age_seconds: float = 0.0
    vectorless_chunks: int = 0
    failed_docs: int = 0
    stuck_parsing_docs: int = 0
    pending_claims: int = 0


async def _count(session: AsyncSession, statement) -> int:
    value = (await session.execute(statement)).scalar_one()
    return int(value or 0)


async def _count_and_oldest_age(
    session: AsyncSession,
    statement,
    *,
    now: datetime,
) -> tuple[int, float]:
    count, oldest = (await session.execute(statement)).one()
    age = max(0.0, (now - oldest).total_seconds()) if oldest is not None else 0.0
    return int(count or 0), age


async def collect_org_health(
    session: AsyncSession, org_id: UUID, *, now: datetime | None = None
) -> OrgHealthSnapshot:
    """统计单 org 各健康水位;session 已过 RLS,org_id 条件为双保险。"""
    now = now or datetime.now(UTC)
    pending_count, oldest_age = await _count_and_oldest_age(
        session,
        select(func.count(), func.min(OutboxEvent.created_at)).where(
                OutboxEvent.org_id == org_id,
                OutboxEvent.status == OutboxStatus.PENDING,
        ),
        now=now,
    )
    dead_letter, oldest_dead_letter_age = await _count_and_oldest_age(
        session,
        select(func.count(), func.min(OutboxEvent.created_at)).where(
            OutboxEvent.org_id == org_id,
            OutboxEvent.status == OutboxStatus.DEAD_LETTER,
        ),
        now=now,
    )
    uploaded_docs, oldest_uploaded_age = await _count_and_oldest_age(
        session,
        select(func.count(), func.min(SourceDocument.created_at)).where(
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.UPLOADED.value,
        ),
        now=now,
    )
    processing_docs, oldest_processing_age = await _count_and_oldest_age(
        session,
        select(
            func.count(),
            func.min(
                func.coalesce(
                    SourceDocument.parsing_started_at,
                    SourceDocument.created_at,
                )
            ),
        ).where(
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.PARSING.value,
        ),
        now=now,
    )
    ingest_leases, oldest_ingest_lease_age = await _count_and_oldest_age(
        session,
        select(
            func.count(),
            func.min(
                func.coalesce(
                    IngestRun.heartbeat_at,
                    IngestRun.started_at,
                    IngestRun.created_at,
                )
            ),
        ).where(
            IngestRun.org_id == org_id,
            IngestRun.status == IngestRunStatus.RUNNING.value,
        ),
        now=now,
    )
    image_enrichment, oldest_image_enrichment_age = await _count_and_oldest_age(
        session,
        select(
            func.count(),
            func.min(func.coalesce(KbImageAsset.updated_at, KbImageAsset.created_at)),
        ).where(
            KbImageAsset.org_id == org_id,
            KbImageAsset.enrichment_status.in_(
                (
                    ImageEnrichmentStatus.PENDING.value,
                    ImageEnrichmentStatus.PROCESSING.value,
                )
            ),
        ),
        now=now,
    )
    snapshot_builds, oldest_snapshot_build_age = await _count_and_oldest_age(
        session,
        select(func.count(), func.min(KnowledgeSnapshot.created_at)).where(
            KnowledgeSnapshot.org_id == org_id,
            KnowledgeSnapshot.status == SnapshotStatus.BUILDING.value,
        ),
        now=now,
    )
    # 运维事件表属 operations 子系统(不在 KB schema):走 IncidentRecorder,
    # 无宿主实现时计 0,KB 自有的资产失败码统计照常合并进来。
    inconsistencies, oldest_inconsistency_age = await ports.count_open_incidents(
        session, org_id=org_id, category="object_metadata_inconsistency"
    )
    failed_assets, oldest_failed_asset_age = await _count_and_oldest_age(
        session,
        select(
            func.count(),
            func.min(func.coalesce(KbImageAsset.updated_at, KbImageAsset.created_at)),
        ).where(
            KbImageAsset.org_id == org_id,
            KbImageAsset.failure_code.in_(_OBJECT_FAILURE_CODES),
        ),
        now=now,
    )
    inconsistencies += failed_assets
    oldest_inconsistency_age = max(
        oldest_inconsistency_age,
        oldest_failed_asset_age,
    )
    projection_failures, oldest_projection_failure_age = await ports.count_open_incidents(
        session, org_id=org_id, category="media_projection_failure"
    )
    vectorless = await _count(
        session,
        select(func.count())
        .select_from(KbChunk)
        .where(
            KbChunk.org_id == org_id,
            KbChunk.embedding.is_(None),  # type: ignore[union-attr]
            KbChunk.quarantined.is_(False),  # type: ignore[attr-defined]
            active_projection_filter(KbChunk),
        ),
    )
    failed_docs = await _count(
        session,
        select(func.count()).where(
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.FAILED.value,
        ),
    )
    stuck_parsing = await _count(
        session,
        select(func.count()).where(
            SourceDocument.org_id == org_id,
            SourceDocument.status == DocStatus.PARSING.value,
            SourceDocument.parsing_started_at.is_not(None),  # type: ignore[union-attr]
            SourceDocument.parsing_started_at
            <= now - timedelta(seconds=get_settings().kb_health_stuck_parsing_seconds),
        ),
    )
    pending_claims = await _count(
        session,
        select(func.count()).where(
            FactClaim.org_id == org_id,
            FactClaim.review_status == FactReviewStatus.SUGGESTED.value,
        ),
    )
    return OrgHealthSnapshot(
        outbox_pending=int(pending_count or 0),
        outbox_oldest_pending_age_seconds=oldest_age,
        outbox_dead_letter=dead_letter,
        outbox_oldest_dead_letter_age_seconds=oldest_dead_letter_age,
        uploaded_docs=uploaded_docs,
        oldest_uploaded_doc_age_seconds=oldest_uploaded_age,
        processing_docs=processing_docs,
        oldest_processing_doc_age_seconds=oldest_processing_age,
        ingest_leases=ingest_leases,
        oldest_ingest_lease_age_seconds=oldest_ingest_lease_age,
        image_enrichment_pending=image_enrichment,
        oldest_image_enrichment_age_seconds=oldest_image_enrichment_age,
        snapshot_builds=snapshot_builds,
        oldest_snapshot_build_age_seconds=oldest_snapshot_build_age,
        object_metadata_inconsistencies=inconsistencies,
        oldest_object_inconsistency_age_seconds=oldest_inconsistency_age,
        media_projection_failures=projection_failures,
        oldest_media_projection_failure_age_seconds=oldest_projection_failure_age,
        vectorless_chunks=vectorless,
        failed_docs=failed_docs,
        stuck_parsing_docs=stuck_parsing,
        pending_claims=pending_claims,
    )


def _set_org_gauges(org_id: UUID, snapshot: OrgHealthSnapshot) -> None:
    org = str(org_id)
    metrics.KB_OUTBOX_PENDING_BACKLOG.labels(org=org).set(snapshot.outbox_pending)
    metrics.KB_OUTBOX_OLDEST_PENDING_AGE.labels(org=org).set(
        snapshot.outbox_oldest_pending_age_seconds
    )
    metrics.KB_OUTBOX_DEAD_LETTER.labels(org=org).set(snapshot.outbox_dead_letter)
    metrics.KB_OUTBOX_OLDEST_DEAD_LETTER_AGE.labels(org=org).set(
        snapshot.outbox_oldest_dead_letter_age_seconds
    )
    metrics.KB_DOCS_UPLOADED.labels(org=org).set(snapshot.uploaded_docs)
    metrics.KB_DOCS_OLDEST_UPLOADED_AGE.labels(org=org).set(
        snapshot.oldest_uploaded_doc_age_seconds
    )
    metrics.KB_DOCS_PROCESSING.labels(org=org).set(snapshot.processing_docs)
    metrics.KB_DOCS_OLDEST_PROCESSING_AGE.labels(org=org).set(
        snapshot.oldest_processing_doc_age_seconds
    )
    metrics.KB_INGEST_LEASES.labels(org=org).set(snapshot.ingest_leases)
    metrics.KB_INGEST_OLDEST_LEASE_AGE.labels(org=org).set(
        snapshot.oldest_ingest_lease_age_seconds
    )
    metrics.KB_IMAGE_ENRICHMENT_PENDING.labels(org=org).set(
        snapshot.image_enrichment_pending
    )
    metrics.KB_IMAGE_ENRICHMENT_OLDEST_AGE.labels(org=org).set(
        snapshot.oldest_image_enrichment_age_seconds
    )
    metrics.KB_SNAPSHOT_BUILDS.labels(org=org).set(snapshot.snapshot_builds)
    metrics.KB_SNAPSHOT_OLDEST_BUILD_AGE.labels(org=org).set(
        snapshot.oldest_snapshot_build_age_seconds
    )
    metrics.KB_OPERATIONAL_INCONSISTENCIES.labels(org=org).set(
        snapshot.object_metadata_inconsistencies
    )
    metrics.KB_OPERATIONAL_OLDEST_INCONSISTENCY_AGE.labels(org=org).set(
        snapshot.oldest_object_inconsistency_age_seconds
    )
    metrics.KB_MEDIA_PROJECTION_FAILURES.labels(org=org).set(
        snapshot.media_projection_failures
    )
    metrics.KB_CHUNKS_MISSING_EMBEDDING.labels(org=org).set(snapshot.vectorless_chunks)
    metrics.KB_DOCS_FAILED.labels(org=org).set(snapshot.failed_docs)
    metrics.KB_DOCS_STUCK_PARSING.labels(org=org).set(snapshot.stuck_parsing_docs)
    metrics.KB_FACT_CLAIMS_PENDING.labels(org=org).set(snapshot.pending_claims)
    # 服务 heartbeat 与 provider 探测水位属 operations 子系统(不在 KB schema),
    # 由运行时装配波次自行刷新 metrics.SERVICE_HEARTBEAT_AGE;KB 巡检不越界统计。


def build_alerts(snapshot: OrgHealthSnapshot, settings) -> list[str]:
    """阈值判定(0=该项关闭),返回超标明细行(空 = 全部健康)。"""
    alerts: list[str] = []
    outbox_threshold = settings.kb_health_outbox_backlog_threshold
    outbox_count_breached = (
        outbox_threshold > 0 and snapshot.outbox_pending > outbox_threshold
    )
    if outbox_threshold > 0:
        if outbox_count_breached:
            alerts.append(
                f"outbox 积压 {snapshot.outbox_pending} 条(阈值 {outbox_threshold},"
                f"最老 pending 已等待 {snapshot.outbox_oldest_pending_age_seconds / 3600:.1f} "
                "小时)。建议:检查 outbox 消费循环(celery beat kb-consume-outbox / "
                "inline 轮询)是否在跑。"
            )
        if snapshot.outbox_dead_letter > 0:
            alerts.append(
                f"outbox 死信 {snapshot.outbox_dead_letter} 条(重试耗尽,不会自动恢复)。"
                "建议:查看 outbox_events.last_error 定位失败原因后人工重放或清理。"
            )
    old_outbox_min_count = getattr(settings, "kb_health_outbox_old_min_count", 0)
    if (
        old_outbox_min_count > 0
        and not outbox_count_breached
        and snapshot.outbox_pending >= old_outbox_min_count
        and snapshot.outbox_oldest_pending_age_seconds
        > getattr(settings, "kb_health_outbox_old_age_seconds", 0)
    ):
        alerts.append(
            f"outbox 有 {snapshot.outbox_pending} 条待处理事件，最老已等待 "
            f"{snapshot.outbox_oldest_pending_age_seconds / 60:.0f} 分钟。"
            "建议:检查 beat/worker heartbeat 与 kb.consume_outbox，再按 runbook 重放。"
        )
    if (
        snapshot.uploaded_docs > 0
        and snapshot.oldest_uploaded_doc_age_seconds
        > getattr(settings, "kb_health_uploaded_old_age_seconds", float("inf"))
    ):
        alerts.append(
            f"{snapshot.uploaded_docs} 篇文档仍停留 uploaded，最老已等待 "
            f"{snapshot.oldest_uploaded_doc_age_seconds / 60:.0f} 分钟。"
            "建议:检查是否缺失 active ingest run，并安全重派摄入。"
        )
    docs_threshold = settings.kb_health_failed_docs_threshold
    docs_count_breached = (
        docs_threshold > 0
        and snapshot.failed_docs + snapshot.stuck_parsing_docs > docs_threshold
    )
    if (
        snapshot.processing_docs > 0
        and not docs_count_breached
        and snapshot.oldest_processing_doc_age_seconds
        > getattr(settings, "kb_health_processing_old_age_seconds", float("inf"))
    ):
        alerts.append(
            f"{snapshot.processing_docs} 篇文档仍在 processing，最老已处理 "
            f"{snapshot.oldest_processing_doc_age_seconds / 60:.0f} 分钟。"
        )
    if (
        snapshot.ingest_leases > 0
        and snapshot.oldest_ingest_lease_age_seconds
        > getattr(settings, "kb_health_ingest_lease_old_age_seconds", float("inf"))
    ):
        alerts.append(
            f"{snapshot.ingest_leases} 个 ingest lease 仍在运行，最老 heartbeat "
            f"已过 {snapshot.oldest_ingest_lease_age_seconds / 60:.0f} 分钟。"
            "建议:先确认 worker，再运行 lease recovery。"
        )
    if (
        snapshot.image_enrichment_pending > 0
        and snapshot.oldest_image_enrichment_age_seconds
        > getattr(
            settings,
            "kb_health_image_enrichment_old_age_seconds",
            float("inf"),
        )
    ):
        alerts.append(
            f"{snapshot.image_enrichment_pending} 个图片 enrichment 未完成，最老已等待 "
            f"{snapshot.oldest_image_enrichment_age_seconds / 60:.0f} 分钟。"
        )
    if (
        snapshot.snapshot_builds > 0
        and snapshot.oldest_snapshot_build_age_seconds
        > getattr(settings, "kb_health_snapshot_build_old_age_seconds", float("inf"))
    ):
        alerts.append(
            f"{snapshot.snapshot_builds} 个 snapshot build 未完成，最老已运行 "
            f"{snapshot.oldest_snapshot_build_age_seconds / 60:.0f} 分钟。"
        )
    if (
        snapshot.object_metadata_inconsistencies > 0
        and snapshot.oldest_object_inconsistency_age_seconds
        > getattr(settings, "kb_health_incident_old_age_seconds", float("inf"))
    ):
        alerts.append(
            f"{snapshot.object_metadata_inconsistencies} 个对象/元数据不一致事件未解决。"
            "建议:隔离受影响资产，校验对象 hash 与 metadata，禁止直接改 object key。"
        )
    if snapshot.media_projection_failures > 0:
        alerts.append(
            f"{snapshot.media_projection_failures} 个媒体投影失败事件未解决。"
            "建议:保持当前 active snapshot 不变并修复候选投影。"
        )
    vectorless_threshold = settings.kb_health_vectorless_chunks_threshold
    if vectorless_threshold > 0 and snapshot.vectorless_chunks > vectorless_threshold:
        alerts.append(
            f"活跃投影内 {snapshot.vectorless_chunks} 个切片缺失向量"
            f"(阈值 {vectorless_threshold}),语义检索对这些内容不可见。"
            "建议:检查 embedding 服务凭证/额度,并发起向量重建。"
        )
    if docs_count_breached:
        alerts.append(
            f"文档异常 {snapshot.failed_docs + snapshot.stuck_parsing_docs} 篇"
            f"(解析失败 {snapshot.failed_docs}、卡在解析中 {snapshot.stuck_parsing_docs},"
            f"阈值 {docs_threshold})。建议:到知识库文档列表查看失败原因并重试摄入。"
        )
    claims_threshold = settings.kb_health_pending_claims_threshold
    if claims_threshold > 0 and snapshot.pending_claims > claims_threshold:
        alerts.append(
            f"事实待审积压 {snapshot.pending_claims} 条(阈值 {claims_threshold})。"
            "建议:检查 AI 审核扫描(kb-ai-review-sweep)是否在跑,或到审核队列人工处理。"
        )
    return alerts


async def _already_notified_today(
    session: AsyncSession, org_id: UUID, title: str
) -> bool:
    row = (
        await session.execute(
            select(Notification.id)
            .where(
                Notification.org_id == org_id,
                Notification.kind == NOTIFY_KIND,
                Notification.title == title,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _notify_health_alert(
    session: AsyncSession, org_id: UUID, alerts: list[str]
) -> bool:
    """站内信告警:同日按标题幂等(履约提醒同款),返回是否新发。"""
    today = datetime.now(UTC).date()
    title = f"KB 系统健康告警({today:%Y-%m-%d})"
    if await _already_notified_today(session, org_id, title):
        return False
    body_lines = [f"{index}. {line}" for index, line in enumerate(alerts, start=1)]
    return bool(
        await ports.notify_org_roles(
            session,
            org_id=org_id,
            kind=NOTIFY_KIND,
            title=title,
            body="KB 健康巡检发现以下超标项:\n" + "\n".join(body_lines),
            email=False,
        )
    )


async def sweep_kb_health(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """全租户单轮巡检(celery task / inline 循环共用),返回汇总计数。"""
    settings = get_settings()
    async with session_factory() as session:
        org_ids = list((await session.execute(select(Organization.id))).scalars().all())
    totals = {"orgs": len(org_ids), "alerting_orgs": 0, "notified_orgs": 0, "failed_orgs": 0}
    for org_id in org_ids:
        session = org_session(session_factory, org_id)
        try:
            snapshot = await collect_org_health(session, org_id)
            _set_org_gauges(org_id, snapshot)
            alerts = build_alerts(snapshot, settings)
            if alerts:
                totals["alerting_orgs"] += 1
                if await _notify_health_alert(session, org_id, alerts):
                    await session.commit()
                    totals["notified_orgs"] += 1
        except Exception:
            await session.rollback()
            logger.exception("KB 健康巡检失败(org=%s),跳过继续", org_id)
            totals["failed_orgs"] += 1
        finally:
            await session.close()
    return totals


async def run_kb_health_sweeper(
    session_factory: async_sessionmaker[AsyncSession], *, stop_event: asyncio.Event
) -> None:
    """常驻循环(仅 inline 模式 lifespan 托管;celery 模式走 beat,避免双跑)。"""
    interval = get_settings().kb_health_sweep_interval_seconds
    while not stop_event.is_set():
        try:
            totals = await sweep_kb_health(session_factory)
            if totals["alerting_orgs"] or totals["failed_orgs"]:
                logger.info("KB 健康巡检完成", extra=totals)
        except Exception:
            logger.exception("KB 健康巡检失败,下轮重试")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
