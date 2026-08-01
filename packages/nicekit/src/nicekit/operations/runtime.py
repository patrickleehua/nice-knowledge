"""Durable process heartbeats and age-based runtime diagnostics.

搬自 TF backend/app/services/operations/runtime.py。SDK 化补充:诊断采集时顺手
刷新 ``core.metrics.SERVICE_HEARTBEAT_AGE``(A1 遗留:该 Gauge 此前无人刷新),
心跳写入成功后把自身 role 的年龄归零。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.core.metrics import SERVICE_HEARTBEAT_AGE
from nicekit.models.operations import ServiceHeartbeat

logger = logging.getLogger(__name__)

ServiceRole = Literal["api", "worker", "beat"]
SERVICE_ROLES: tuple[ServiceRole, ...] = ("api", "worker", "beat")


def runtime_instance_id(role: ServiceRole, supplied: str | None = None) -> str:
    """Return a bounded, non-secret identity stable for this process lifetime."""
    configured = (supplied or get_settings().service_instance_id).strip()
    if configured:
        return configured[:200]
    return f"{socket.gethostname()}:{os.getpid()}"[:200]


async def record_service_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: ServiceRole,
    instance: str | None = None,
    version: str | None = None,
    now: datetime | None = None,
) -> None:
    if role not in SERVICE_ROLES:
        raise ValueError("unsupported service heartbeat role")
    recorded_at = now or datetime.now(UTC)
    identity = runtime_instance_id(role, instance)
    release = (version or get_settings().service_version).strip()
    if not release:
        raise ValueError("service version must not be empty")
    statement = insert(ServiceHeartbeat).values(
        role=role,
        instance=identity,
        version=release[:100],
        recorded_at=recorded_at,
        started_at=recorded_at,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_service_heartbeat_role_instance",
        set_={
            "version": statement.excluded.version,
            "recorded_at": statement.excluded.recorded_at,
        },
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()
    # 写入即代表本进程存活:把自己这一路的年龄归零,抓取端不必等下一次诊断。
    SERVICE_HEARTBEAT_AGE.labels(role=role).set(0.0)


async def run_api_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Persist immediately, then at a fixed interval until shutdown."""
    settings = get_settings()
    while not stop_event.is_set():
        try:
            await record_service_heartbeat(session_factory, role="api")
        except Exception:
            logger.error(
                "API operational heartbeat write failed",
                extra={"failure_code": "api_heartbeat_write_failed"},
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.operations_heartbeat_interval_seconds,
            )
            return
        except TimeoutError:
            pass


@dataclass(frozen=True, slots=True)
class HeartbeatDiagnostic:
    role: ServiceRole
    required: bool
    status: Literal["healthy", "expired", "missing"]
    instance: str | None
    version: str | None
    recorded_at: datetime | None
    age_seconds: float | None

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "required": self.required,
            "status": self.status,
            "instance": self.instance,
            "version": self.version,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "age_seconds": self.age_seconds,
        }


async def collect_heartbeat_diagnostics(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[HeartbeatDiagnostic, ...]:
    settings = get_settings()
    current = now or datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ServiceHeartbeat).order_by(
                    ServiceHeartbeat.role,
                    ServiceHeartbeat.recorded_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    newest: dict[str, ServiceHeartbeat] = {}
    for row in rows:
        newest.setdefault(row.role, row)
    required_roles: set[str] = {"api"}
    if settings.task_dispatch_mode == "celery":
        required_roles.update(("worker", "beat"))

    diagnostics: list[HeartbeatDiagnostic] = []
    for role in SERVICE_ROLES:
        row = newest.get(role)
        if row is None or row.recorded_at is None:
            diagnostics.append(
                HeartbeatDiagnostic(
                    role=role,
                    required=role in required_roles,
                    status="missing",
                    instance=None,
                    version=None,
                    recorded_at=None,
                    age_seconds=None,
                )
            )
            continue
        age = max(0.0, (current - row.recorded_at).total_seconds())
        SERVICE_HEARTBEAT_AGE.labels(role=role).set(age)
        diagnostics.append(
            HeartbeatDiagnostic(
                role=role,
                required=role in required_roles,
                status=(
                    "expired"
                    if age > settings.operations_heartbeat_expiry_seconds
                    else "healthy"
                ),
                instance=row.instance,
                version=row.version,
                recorded_at=row.recorded_at,
                age_seconds=age,
            )
        )
    return tuple(diagnostics)


async def refresh_heartbeat_metrics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> None:
    """只为刷新 Gauge 的轻量入口(周期任务用;失败不抛,监控不改控制流)。"""
    try:
        async with session_factory() as session:
            await collect_heartbeat_diagnostics(session, now=now)
    except Exception:
        logger.debug("heartbeat 指标刷新失败,已跳过", exc_info=True)


__all__ = [
    "SERVICE_ROLES",
    "HeartbeatDiagnostic",
    "ServiceRole",
    "collect_heartbeat_diagnostics",
    "record_service_heartbeat",
    "refresh_heartbeat_metrics",
    "run_api_heartbeat",
    "runtime_instance_id",
]
