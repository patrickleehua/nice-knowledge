"""Bounded dependency readiness checks(``GET /api/v1/ready``)。

Liveness stays independent in ``api/v1/health.py``. Readiness performs one
attempt per dependency, runs attempts concurrently, and exposes only stable
codes rather than provider exceptions or configuration values.

SDK 化调整:生产环境的密钥黑名单从 TF 的产品名改为通用弱值(``change-me``/
``dev-secret``),其余判据原样保留。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, text

from nicekit.core import cache
from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory, org_session
from nicekit.kb import storage
from nicekit.models.kb import KnowledgeBase, KnowledgeSnapshot, SnapshotStatus

DEFAULT_DEPENDENCY_TIMEOUT_SECONDS = 2.0

DATABASE = "database"
ACTIVE_SNAPSHOT = "active_snapshot"
REDIS = "redis"
OBJECT_STORAGE = "object_storage"
CONFIGURATION = "configuration"

_CHECK_ORDER = (
    DATABASE,
    ACTIVE_SNAPSHOT,
    REDIS,
    OBJECT_STORAGE,
    CONFIGURATION,
)

_UNAVAILABLE_CODES = {
    DATABASE: "database_unavailable",
    ACTIVE_SNAPSHOT: "active_snapshot_unavailable",
    REDIS: "redis_unavailable",
    OBJECT_STORAGE: "object_storage_unavailable",
    CONFIGURATION: "configuration_invalid",
}

#: 生产环境禁止出现的弱密钥片段(默认值 / 开发口令)
_WEAK_SECRET_MARKERS = ("change-me", "dev-secret")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ready: bool
    code: str | None = None

    def as_dict(self) -> dict[str, str]:
        if self.ready:
            return {"status": "ok"}
        return {"status": "unavailable", "code": self.code or _UNAVAILABLE_CODES[self.name]}


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.ready for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {check.name: check.as_dict() for check in self.checks},
        }


async def check_database() -> ReadinessCheck:
    async with get_session_factory()() as session:
        await session.execute(text("SELECT 1"))
    return ReadinessCheck(DATABASE, True)


async def check_active_snapshot() -> ReadinessCheck:
    """Verify the active-snapshot schema/query path; an empty KB set is valid."""
    settings = get_settings()
    session = org_session(get_session_factory(), settings.platform_org_id)
    try:
        inconsistent = int(
            await session.scalar(
                select(func.count())
                .select_from(KnowledgeBase)
                .outerjoin(
                    KnowledgeSnapshot,
                    and_(
                        KnowledgeSnapshot.id == KnowledgeBase.active_snapshot_id,
                        KnowledgeSnapshot.kb_id == KnowledgeBase.id,
                        KnowledgeSnapshot.org_id == KnowledgeBase.org_id,
                    ),
                )
                .where(
                    KnowledgeBase.active_snapshot_id.is_not(None),
                    or_(
                        KnowledgeSnapshot.id.is_(None),
                        KnowledgeSnapshot.status != SnapshotStatus.ACTIVE.value,
                    ),
                )
            )
            or 0
        )
    finally:
        await session.close()
    if inconsistent:
        return ReadinessCheck(ACTIVE_SNAPSHOT, False, "active_snapshot_inconsistent")
    return ReadinessCheck(ACTIVE_SNAPSHOT, True)


async def check_redis() -> ReadinessCheck:
    if not await cache.ping():
        return ReadinessCheck(REDIS, False, _UNAVAILABLE_CODES[REDIS])
    return ReadinessCheck(REDIS, True)


async def check_object_storage() -> ReadinessCheck:
    if not await storage.bucket_exists():
        return ReadinessCheck(OBJECT_STORAGE, False, _UNAVAILABLE_CODES[OBJECT_STORAGE])
    return ReadinessCheck(OBJECT_STORAGE, True)


def _configuration_is_valid(settings: Any) -> bool:
    required_strings = (
        settings.database_url,
        settings.redis_url,
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.minio_bucket,
    )
    base_valid = (
        all(isinstance(value, str) and bool(value.strip()) for value in required_strings)
        and settings.task_dispatch_mode in {"inline", "celery"}
    )
    if not base_valid:
        return False
    environment = str(getattr(settings, "deployment_environment", "development")).strip().casefold()
    if environment != "production":
        return environment in {"development", "test", "staging"}
    cors_origins = str(getattr(settings, "cors_origins", ""))
    jwt_secret = str(getattr(settings, "jwt_secret", ""))
    secret_values = (
        jwt_secret,
        str(getattr(settings, "minio_access_key", "")),
        str(getattr(settings, "minio_secret_key", "")),
    )
    return (
        getattr(settings, "debug", False) is False
        and settings.task_dispatch_mode == "celery"
        and getattr(settings, "rate_limit_backend", "") == "redis"
        and getattr(settings, "log_format", "") == "json"
        and getattr(settings, "operations_heartbeat_interval_seconds", 0) > 0
        and getattr(settings, "operations_provider_probe_interval_seconds", 0) > 0
        and getattr(settings, "kb_health_sweep_interval_seconds", 0) > 0
        and len(jwt_secret) >= 32
        and not any(
            marker in value for value in secret_values for marker in _WEAK_SECRET_MARKERS
        )
        and "localhost" not in cors_origins
        and "127.0.0.1" not in cors_origins
    )


def _required_provider_configuration_is_valid(settings: Any) -> bool:
    environment = str(getattr(settings, "deployment_environment", "development")).strip().casefold()
    if environment != "production":
        return True
    try:
        from nicekit.operations.probes import resolve_probe_executions

        executions = resolve_probe_executions()
    except Exception:
        return False
    return all(
        not execution.enabled or execution.configuration_ready for execution in executions
    )


async def check_configuration() -> ReadinessCheck:
    settings = get_settings()
    if not _configuration_is_valid(settings) or not _required_provider_configuration_is_valid(
        settings
    ):
        return ReadinessCheck(CONFIGURATION, False, _UNAVAILABLE_CODES[CONFIGURATION])
    return ReadinessCheck(CONFIGURATION, True)


def _probes() -> dict[str, Callable[[], Awaitable[ReadinessCheck]]]:
    # Build this mapping per call so tests and runtime instrumentation can patch
    # individual probes without replacing the readiness orchestrator.
    return {
        DATABASE: check_database,
        ACTIVE_SNAPSHOT: check_active_snapshot,
        REDIS: check_redis,
        OBJECT_STORAGE: check_object_storage,
        CONFIGURATION: check_configuration,
    }


async def _run_probe(
    name: str,
    probe: Callable[[], Awaitable[ReadinessCheck]],
    *,
    timeout_seconds: float,
) -> ReadinessCheck:
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await probe()
    except Exception:
        return ReadinessCheck(name, False, _UNAVAILABLE_CODES[name])
    if result.name != name:
        return ReadinessCheck(name, False, _UNAVAILABLE_CODES[name])
    return result


async def collect_readiness(
    *,
    timeout_seconds: float = DEFAULT_DEPENDENCY_TIMEOUT_SECONDS,
) -> ReadinessReport:
    """Run every required dependency check concurrently, once per request."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    probes = _probes()
    results = await asyncio.gather(
        *(_run_probe(name, probes[name], timeout_seconds=timeout_seconds) for name in _CHECK_ORDER)
    )
    return ReadinessReport(tuple(results))


__all__ = [
    "ACTIVE_SNAPSHOT",
    "CONFIGURATION",
    "DATABASE",
    "DEFAULT_DEPENDENCY_TIMEOUT_SECONDS",
    "OBJECT_STORAGE",
    "REDIS",
    "ReadinessCheck",
    "ReadinessReport",
    "check_active_snapshot",
    "check_configuration",
    "check_database",
    "check_object_storage",
    "check_redis",
    "collect_readiness",
]
