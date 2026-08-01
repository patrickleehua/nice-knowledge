"""Celery 任务:生产形态的执行端(开发态 API 用 BackgroundTasks)。

搬自 TF backend/app/workers/tasks.py,只保留通用任务;业务任务
(pipeline.run_node / render.execute_job / fulfillment.daily_reminders)不搬,
由宿主在自己的模块里注册到同一个 ``celery_app``。

保留 TF 的"延迟导入 + asyncio.run"模式:worker 进程启动只加载 celery 与 core,
真实依赖(KB / LLM / agent)在任务体内才拉起来,避免 beat/worker 因某条链路的
import 错误整体起不来。
"""

import asyncio
from uuid import UUID

from nicekit.core.config import get_settings
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.core.logging import setup_logging
from nicekit.runtime.celery_app import celery_app

use_selector_event_loop_on_windows()
setup_logging()  # worker 侧日志与 API 同格式(结构化)


@celery_app.task(name="operations.record_heartbeat", max_retries=0)
def record_operational_heartbeat_task(role: str, instance: str | None = None) -> None:
    from nicekit.core.db import get_session_factory
    from nicekit.operations.runtime import record_service_heartbeat

    if role not in {"worker", "beat"}:
        raise ValueError("Celery heartbeat role must be worker or beat")
    asyncio.run(
        record_service_heartbeat(
            get_session_factory(),
            role=role,  # type: ignore[arg-type]
            instance=instance,
        )
    )


@celery_app.task(name="operations.provider_probes", max_retries=0)
def provider_operational_probes_task() -> list[dict]:
    from nicekit.core.db import get_session_factory
    from nicekit.operations.probes import run_scheduled_provider_probes

    results = asyncio.run(run_scheduled_provider_probes(get_session_factory()))
    return [
        {
            "capability": result.capability,
            "status": result.status,
            "error_code": result.error_code,
            "latency_ms": result.latency_ms,
            "config_fingerprint": result.config_fingerprint,
        }
        for result in results
    ]


@celery_app.task(name="kb.ingest_document", max_retries=2, default_retry_delay=30)
def ingest_document_task(run_id: str, org_id: str) -> None:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.ingestion import ingest_document
    from nicekit.kb.search import default_embedder
    from nicekit.llm.service import get_llm_service

    asyncio.run(
        ingest_document(
            UUID(run_id),
            UUID(org_id),
            session_factory=get_session_factory(),
            llm=get_llm_service(),
            embedder=default_embedder(),
        )
    )


@celery_app.task(name="kb.reextract_document", max_retries=2, default_retry_delay=30)
def reextract_document_task(run_id: str, org_id: str) -> None:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.ingestion import reextract_document
    from nicekit.llm.service import get_llm_service

    asyncio.run(
        reextract_document(
            UUID(run_id),
            UUID(org_id),
            session_factory=get_session_factory(),
            llm=get_llm_service(),
        )
    )


@celery_app.task(name="kb.consume_outbox", max_retries=0)
def consume_kb_outbox_task() -> dict[str, int]:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.outbox import consume_outbox_once

    return asyncio.run(consume_outbox_once(get_session_factory())).as_dict()


@celery_app.task(name="kb.reindex_embeddings", max_retries=0)
def reindex_embeddings_task(campaign_id: str) -> dict:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.embedding_reindex import execute_embedding_campaign

    status = asyncio.run(
        execute_embedding_campaign(
            UUID(campaign_id),
            get_session_factory(),
            batch_size=get_settings().embedding_reindex_batch_size,
            lease_seconds=get_settings().embedding_reindex_lease_seconds,
        )
    )
    return status.as_dict()


@celery_app.task(name="kb.reconcile_embedding_campaigns", max_retries=0)
def reconcile_embedding_campaigns_task() -> list[dict]:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.embedding_reindex import (
        finalize_due_embedding_campaigns,
        reconcile_embedding_campaigns,
    )
    from nicekit.llm.runtime_config import load_runtime_overrides

    async def run() -> list[dict]:
        factory = get_session_factory()
        statuses = await reconcile_embedding_campaigns(
            factory,
            batch_size=get_settings().embedding_reindex_batch_size,
            lease_seconds=get_settings().embedding_reindex_lease_seconds,
        )
        finalized = await finalize_due_embedding_campaigns(factory)
        if finalized:
            await load_runtime_overrides(factory)
        return [status.as_dict() for status in statuses]

    return asyncio.run(run())


@celery_app.task(name="kb.finalize_embedding_campaigns", max_retries=0)
def finalize_embedding_campaigns_task() -> list[str]:
    from nicekit.core.db import get_session_factory
    from nicekit.kb.embedding_reindex import finalize_due_embedding_campaigns
    from nicekit.llm.runtime_config import load_runtime_overrides

    async def run() -> list[str]:
        factory = get_session_factory()
        finalized = await finalize_due_embedding_campaigns(factory)
        if finalized:
            await load_runtime_overrides(factory)
        return [str(campaign_id) for campaign_id in finalized]

    return asyncio.run(run())


@celery_app.task(name="reliability.sweep_stale_tasks", max_retries=0)
def sweep_stale_tasks_task() -> dict[str, int]:
    from nicekit.core.db import get_session_factory
    from nicekit.runtime.reliability import sweep_stale_tasks

    return asyncio.run(sweep_stale_tasks(get_session_factory()))


@celery_app.task(name="kb.ai_review_sweep", max_retries=0)
def kb_ai_review_sweep_task() -> dict[str, int]:
    """AI 事实审核定时扫描:逐 org 分批审核 + 例外升级人工通知(同日幂等)。"""
    from nicekit.core.db import get_session_factory
    from nicekit.kb.review_sweep import sweep_pending_fact_reviews

    return asyncio.run(sweep_pending_fact_reviews(get_session_factory()))


@celery_app.task(name="kb.expiry_sweep", max_retries=0)
def kb_expiry_sweep_task() -> int:
    """KB 文档过期提醒单轮扫描(纯 worker 部署形态由 beat 驱动)。"""
    from nicekit.core.db import get_session_factory
    from nicekit.kb.expiry import sweep_expired_kb_docs

    return asyncio.run(sweep_expired_kb_docs(get_session_factory()))


@celery_app.task(name="kb.health_sweep", max_retries=0)
def kb_health_sweep_task() -> dict[str, int]:
    """KB 系统健康巡检:逐 org 刷新水位 Gauge + 阈值超标站内信(同日幂等)。"""
    from nicekit.core.db import get_session_factory
    from nicekit.kb.health_sweep import sweep_kb_health

    return asyncio.run(sweep_kb_health(get_session_factory()))


@celery_app.task(name="icron.tick", max_retries=0)
def icron_tick_task() -> dict[str, int]:
    """iCron 单轮调度:扫到期任务 → CAS 抢占 → 逐个起 agent run。

    max_retries=0 且不抛错:到期任务的补偿手段是下一跳,celery 重试只会
    让同一批任务被重复抢占(CAS 兜得住,但白白排队)。
    """
    from nicekit.agent.icron import tick
    from nicekit.core.db import get_session_factory

    return asyncio.run(tick(get_session_factory()))


__all__ = [
    "consume_kb_outbox_task",
    "finalize_embedding_campaigns_task",
    "icron_tick_task",
    "ingest_document_task",
    "kb_ai_review_sweep_task",
    "kb_expiry_sweep_task",
    "kb_health_sweep_task",
    "provider_operational_probes_task",
    "record_operational_heartbeat_task",
    "reconcile_embedding_campaigns_task",
    "reextract_document_task",
    "reindex_embeddings_task",
    "sweep_stale_tasks_task",
]
