"""Operator-only, secret-free deep operational diagnostics.

搬自 TF backend/app/services/operations/diagnostics.py。全部探测项都是通用能力
(心跳 / 系统能力槽位 / provider 探测 / 技能包 / MCP / Agent 卡 / 周期任务 /
逐租户 KB 健康水位),没有业务专属项;工具目录从 ``TOOLS`` 模块级 dict 改查
注入的 ``ToolRegistry``(§4 扩展点)。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.agent.mcp_manager import mcp_client_manager
from nicekit.agent.skills import inspect_skill_packages
from nicekit.agent.tools import ToolRegistry, default_registry
from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.kb.health_sweep import OrgHealthSnapshot, build_alerts, collect_org_health
from nicekit.llm.capability_routes import capability_slots, system_route_selection
from nicekit.models.chat import AgentCard, AgentCardVersion, ChatSession
from nicekit.models.icron import IcronTask, IcronTaskRun
from nicekit.models.llm import ModelRoute
from nicekit.models.mcp import McpServer
from nicekit.models.operations import ProviderProbeStatus
from nicekit.models.tenancy import Organization


def _provider_payload(row: ProviderProbeStatus, *, now: datetime) -> dict:
    probe_age = (
        max(0.0, (now - row.last_probe_at).total_seconds()) if row.last_probe_at else None
    )
    degraded_age = (
        max(0.0, (now - row.degraded_since_at).total_seconds()) if row.degraded_since_at else None
    )
    return {
        "capability": row.capability,
        "provider": row.provider,
        "model": row.model,
        "config_fingerprint": row.config_fingerprint,
        "configuration_ready": row.configuration_ready,
        "status": row.status,
        "last_probe_at": row.last_probe_at.isoformat() if row.last_probe_at else None,
        "last_success_at": (row.last_success_at.isoformat() if row.last_success_at else None),
        "last_failure_at": (row.last_failure_at.isoformat() if row.last_failure_at else None),
        "probe_age_seconds": probe_age,
        "degraded_age_seconds": degraded_age,
        "latency_ms": row.latency_ms,
        "consecutive_failures": row.consecutive_failures,
        "error_code": row.error_code,
    }


def _org_snapshot_payload(snapshot: OrgHealthSnapshot) -> dict:
    return {
        "outbox": {
            "pending_count": snapshot.outbox_pending,
            "oldest_pending_age_seconds": snapshot.outbox_oldest_pending_age_seconds,
            "dead_letter_count": snapshot.outbox_dead_letter,
            "oldest_dead_letter_age_seconds": (snapshot.outbox_oldest_dead_letter_age_seconds),
        },
        "documents": {
            "uploaded_count": snapshot.uploaded_docs,
            "oldest_uploaded_age_seconds": snapshot.oldest_uploaded_doc_age_seconds,
            "processing_count": snapshot.processing_docs,
            "oldest_processing_age_seconds": (snapshot.oldest_processing_doc_age_seconds),
            "failed_count": snapshot.failed_docs,
        },
        "ingest_leases": {
            "count": snapshot.ingest_leases,
            "oldest_age_seconds": snapshot.oldest_ingest_lease_age_seconds,
        },
        "image_enrichment": {
            "count": snapshot.image_enrichment_pending,
            "oldest_age_seconds": snapshot.oldest_image_enrichment_age_seconds,
        },
        "snapshot_builds": {
            "count": snapshot.snapshot_builds,
            "oldest_age_seconds": snapshot.oldest_snapshot_build_age_seconds,
        },
        "object_metadata_inconsistencies": {
            "count": snapshot.object_metadata_inconsistencies,
            "oldest_age_seconds": snapshot.oldest_object_inconsistency_age_seconds,
        },
        "media_projection_failures": {
            "count": snapshot.media_projection_failures,
            "oldest_age_seconds": (snapshot.oldest_media_projection_failure_age_seconds),
        },
        "vectorless_chunks": snapshot.vectorless_chunks,
        "pending_claims": snapshot.pending_claims,
    }


def _capability_slot_payload(routes: dict[str, ModelRoute]) -> list[dict]:
    """Configuration readiness of the registered system capability slots.

    Deliberately network-free: it answers "did the operator configure enough
    external capability for the system to run", which is a different question
    from "is the upstream healthy right now" (that is what ``providers`` /
    ``ProviderProbeStatus`` answer). Keeping them apart means this section costs
    nothing to compute and never spends tokens.

    ``resolved`` re-runs the capability check, so a route pointing at a model
    that has since lost its label — or whose provider was disabled — reads as
    not ready even though the row still exists.
    """
    payload: list[dict] = []
    for task, slot in capability_slots().items():
        route = routes.get(task)
        selection = system_route_selection(task)
        payload.append(
            {
                "task": task,
                "required_capabilities": list(slot.capabilities),
                "configured": route is not None,
                "is_active": bool(route.is_active) if route is not None else False,
                "primary_provider": route.primary_provider if route else None,
                "primary_model": route.primary_model if route else None,
                "fallback_hops": len(route.fallback_chain or []) if route else 0,
                "resolved_provider": selection[0] if selection else None,
                "resolved_model": selection[1] if selection else None,
                "ready": selection is not None,
            }
        )
    return payload


def _skill_payload() -> tuple[dict, set[str]]:
    skills, issues = inspect_skill_packages()
    return (
        {
            "status": "healthy" if not issues else "degraded",
            "total": len(skills),
            "invalid": [{"slug": issue.slug, "code": issue.code} for issue in issues],
        },
        {skill.slug for skill in skills},
    )


def _binding_server_id(binding: dict) -> UUID | None:
    try:
        return UUID(str(binding["server_id"]))
    except (KeyError, TypeError, ValueError):
        return None


async def _collect_org_runtime(
    session: AsyncSession,
    *,
    org_id: UUID,
    org_name: str,
    skill_slugs: set[str],
    tool_names: frozenset[str],
    current: datetime,
    icron_tick_interval_seconds: int,
) -> tuple[dict, dict, list[McpServer], set[tuple[UUID, UUID]]]:
    servers = list((await session.execute(select(McpServer).order_by(McpServer.name))).scalars())
    server_by_id = {server.id: server for server in servers}
    card_rows = (
        await session.execute(
            select(AgentCard, AgentCardVersion)
            .outerjoin(
                AgentCardVersion,
                (AgentCardVersion.card_id == AgentCard.id) & AgentCardVersion.is_active,
            )
            .order_by(AgentCard.sort_order, AgentCard.created_at)
        )
    ).all()

    running_sessions = (
        await session.execute(
            select(func.count())
            .select_from(ChatSession)
            .where(
                ChatSession.org_id == org_id,
                ChatSession.active_run_id.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).scalar_one()

    agent_issues: list[dict] = []
    ready_cards = 0
    enabled_cards = 0
    binding_usage: set[tuple[UUID, UUID]] = set()
    for card, version in card_rows:
        if not card.is_active:
            continue
        enabled_cards += 1
        card_issue_codes: list[str] = []
        if version is None:
            card_issue_codes.append("agent_active_version_missing")
        else:
            if unknown_tools := sorted(set(version.tools or []) - tool_names):
                card_issue_codes.append("agent_tool_missing")
                agent_issues.append(
                    {
                        "code": "agent_tool_missing",
                        "card": card.name,
                        "items": unknown_tools,
                    }
                )
            if unknown_skills := sorted(set(version.skills or []) - skill_slugs):
                card_issue_codes.append("agent_skill_missing")
                agent_issues.append(
                    {
                        "code": "agent_skill_missing",
                        "card": card.name,
                        "items": unknown_skills,
                    }
                )
            for binding in version.mcp_bindings or []:
                server_id = _binding_server_id(binding)
                if server_id is None or server_id not in server_by_id:
                    card_issue_codes.append("agent_mcp_missing")
                    continue
                binding_usage.add((card.id, server_id))
                if not server_by_id[server_id].enabled:
                    card_issue_codes.append("agent_mcp_disabled")
        if version is None:
            agent_issues.append(
                {
                    "code": "agent_active_version_missing",
                    "card": card.name,
                    "items": [],
                }
            )
        if "agent_mcp_missing" in card_issue_codes:
            agent_issues.append({"code": "agent_mcp_missing", "card": card.name, "items": []})
        if "agent_mcp_disabled" in card_issue_codes:
            agent_issues.append({"code": "agent_mcp_disabled", "card": card.name, "items": []})
        if not card_issue_codes:
            ready_cards += 1

    tasks = list((await session.execute(select(IcronTask))).scalars())
    task_statuses = Counter(task.status for task in tasks)
    overdue_before = current - timedelta(seconds=max(120, icron_tick_interval_seconds * 2))
    overdue_count = sum(
        task.status == "active"
        and task.next_run_at is not None
        and task.next_run_at < overdue_before
        for task in tasks
    )
    recent_failed_runs = (
        await session.execute(
            select(func.count())
            .select_from(IcronTaskRun)
            .where(
                IcronTaskRun.org_id == org_id,
                IcronTaskRun.status == "failed",
                IcronTaskRun.started_at >= current - timedelta(hours=24),
            )
        )
    ).scalar_one()

    return (
        {
            "org_id": str(org_id),
            "org_name": org_name,
            "status": "healthy" if not agent_issues else "degraded",
            "total_cards": len(card_rows),
            "enabled_cards": enabled_cards,
            "ready_cards": ready_cards,
            "running_sessions": running_sessions,
            "issues": agent_issues,
        },
        {
            "org_id": str(org_id),
            "org_name": org_name,
            "active_count": task_statuses["active"],
            "paused_count": task_statuses["paused"],
            "archived_count": task_statuses["archived"],
            "overdue_count": overdue_count,
            "failed_runs_24h": recent_failed_runs,
        },
        servers,
        binding_usage,
    )


async def _mcp_payload(
    servers: list[McpServer],
    *,
    binding_usage: set[tuple[UUID, UUID]],
    refresh: bool,
) -> list[dict]:
    if refresh:
        statuses = await asyncio.gather(
            *(mcp_client_manager.status(server, refresh=True) for server in servers)
        )
    else:
        statuses = [mcp_client_manager.cached_status(server) for server in servers]
    bound_counts = Counter(server_id for _, server_id in binding_usage)
    payload: list[dict] = []
    for server, status in zip(servers, statuses, strict=True):
        checked_at = status.get("checked_at")
        payload.append(
            {
                "server_id": str(server.id),
                "org_id": str(server.org_id) if server.org_id else None,
                "name": server.name,
                "transport": server.transport,
                "enabled": server.enabled,
                "status": status["status"],
                "latency_ms": status.get("latency_ms"),
                "checked_at": (
                    datetime.fromtimestamp(checked_at, tz=UTC).isoformat()
                    if isinstance(checked_at, (int, float))
                    else None
                ),
                "tools_count": len(status.get("tools") or []),
                "bound_agents": bound_counts[server.id],
                "error_code": ("mcp_connection_failed" if status["status"] == "offline" else None),
            }
        )
    return payload


async def collect_operator_diagnostics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    refresh_mcp: bool = False,
    tool_registry: ToolRegistry | None = None,
) -> dict:
    # 延迟 import:schedules/runtime 是同包模块,但 runtime 会拉 core.metrics
    from nicekit.operations.runtime import collect_heartbeat_diagnostics
    from nicekit.operations.schedules import collect_system_schedule_diagnostics

    current = now or datetime.now(UTC)
    settings = get_settings()
    slots = capability_slots()
    registry = tool_registry or default_registry
    async with session_factory() as session:
        heartbeats = await collect_heartbeat_diagnostics(session, now=current)
        slot_rows = (
            (
                await session.execute(
                    select(ModelRoute).where(
                        ModelRoute.task.in_(tuple(slots)),  # type: ignore[attr-defined]
                        ModelRoute.org_id.is_(None),  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
        providers = (
            (
                await session.execute(
                    select(ProviderProbeStatus).order_by(ProviderProbeStatus.capability)
                )
            )
            .scalars()
            .all()
        )
        organizations = (
            await session.execute(
                select(Organization.id, Organization.name).order_by(Organization.created_at)
            )
        ).all()

    skill_payload, skill_slugs = _skill_payload()
    tool_names = registry.names()
    org_payloads: list[dict] = []
    agent_payloads: list[dict] = []
    custom_schedule_payloads: list[dict] = []
    mcp_servers: dict[UUID, McpServer] = {}
    mcp_binding_usage: set[tuple[UUID, UUID]] = set()
    for org_id, org_name in organizations:
        session = org_session(session_factory, org_id)
        try:
            snapshot = await collect_org_health(session, org_id, now=current)
            agent_payload, custom_schedule_payload, visible_servers, binding_usage = (
                await _collect_org_runtime(
                    session,
                    org_id=org_id,
                    org_name=org_name,
                    skill_slugs=skill_slugs,
                    tool_names=tool_names,
                    current=current,
                    icron_tick_interval_seconds=settings.icron_tick_interval_seconds,
                )
            )
            org_payloads.append(
                {
                    "org_id": str(org_id),
                    "org_name": org_name,
                    "health": _org_snapshot_payload(snapshot),
                    "remediation": build_alerts(snapshot, settings),
                }
            )
            agent_payloads.append(agent_payload)
            custom_schedule_payloads.append(custom_schedule_payload)
            mcp_servers.update({server.id: server for server in visible_servers})
            mcp_binding_usage.update(binding_usage)
        finally:
            await session.close()

    heartbeat_payload = [heartbeat.as_dict() for heartbeat in heartbeats]
    beat_status = next(
        (
            heartbeat["status"]
            for heartbeat in heartbeat_payload
            if heartbeat["role"] == "beat"
        ),
        None,
    )
    return {
        "generated_at": current.isoformat(),
        "runtime": {
            "dispatch_mode": settings.task_dispatch_mode,
            "service_version": settings.service_version,
            "heartbeat_expiry_seconds": settings.operations_heartbeat_expiry_seconds,
        },
        "capability_slots": _capability_slot_payload({row.task: row for row in slot_rows}),
        "heartbeats": heartbeat_payload,
        "providers": [_provider_payload(provider, now=current) for provider in providers],
        "skills": skill_payload,
        "mcp_servers": await _mcp_payload(
            list(mcp_servers.values()),
            binding_usage=mcp_binding_usage,
            refresh=refresh_mcp,
        ),
        "agents": agent_payloads,
        "schedules": {
            "system": collect_system_schedule_diagnostics(settings, beat_status=beat_status),
            "custom": custom_schedule_payloads,
        },
        "organizations": org_payloads,
    }


__all__ = ["collect_operator_diagnostics"]
