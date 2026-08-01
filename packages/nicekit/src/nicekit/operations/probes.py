"""Scheduled bounded AI provider probes; never called by HTTP readiness.

搬自 TF backend/app/services/operations/probes.py。四个被探测的能力
(embedding / rerank / ocr / caption)都是 SDK 自带的通用槽位,没有业务耦合;
探测串到 KB 的 embedding / rerank / image_enrichment 三条执行路径上,保证
"探测报告 ready 的模型 = 摄入真会调用的模型"。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.config import get_settings
from nicekit.core.db import org_session
from nicekit.domain.kb import IngestProfile
from nicekit.kb import metrics
from nicekit.kb.caption import CaptionModelSelection, platform_caption_selection
from nicekit.kb.embedding import EmbeddingUnavailableError
from nicekit.kb.image_enrichment import (
    KbImageEnrichmentError,
    KbImageEnrichmentService,
    get_kb_image_enrichment_service,
)
from nicekit.kb.rerank import RerankError, get_rerank_execution
from nicekit.kb.search import default_embedding_execution
from nicekit.llm.model_catalog import (
    ModelCatalogLookupError,
    lookup_eligible_provider_model_session,
)
from nicekit.models.kb import KnowledgeBase
from nicekit.models.operations import ProviderProbeStatus
from nicekit.models.tenancy import Organization

CAPABILITIES = ("embedding", "rerank", "ocr", "caption")
MAX_CAPTION_PROBE_ROUTES = 20
CAPTION_PROFILE_PAGE_SIZE = 100
PROBE_TEXT = "nicekit provider readiness probe"
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAABfklEQVR4nO3YMYrCQ"
    "BQG4D+/B9B6jqCFxDTKlHoPS89jIQgiwcYzKNqqhZDSC6Sxi42M2LwtwqbPCll+d"
    "r9q3sCD+eG9ZiIzgzJCHCGOEEeII8QR4ghxhDhCHCGOEEeII8QR4ghxhDhCHCGOEE"
    "eII/5ggDRNkyQZjUZJkmw2m/Ky0+mUhzzP4zi+3+9ohtW02+2890VRmFlRFN77w+"
    "FgZu1228xCCN77y+ViTUHdhvF4fD6fq/J0Ok0mkyrAdDpdrVbWINRtcM6FEKoyhOC"
    "cKwPM5/PZbGbN4ucTGEURgPf7vVgsmhv9b7UDdLvdLMuqMsuyXq8HoNVqXa/X5/O"
    "5XC7RJKtpv9977x+PR7XEx+Ox2oE8z51zt9vNmoIf9KzX6ziOh8PhYDBI07S8LAO"
    "Y2Xa77ff7r9fLGhH9/07/MkIcIY4QR4gjxBHiCHGEOEIcIY4QR4gjxBHiCHGEOEI"
    "cIY4QR4gjxBHiCHGEOEIcIY4QR4gjxBHiCHH87Qd86gsn+aW9qG02PgAAAABJRU5"
    "ErkJggg=="
)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProbeExecution:
    capability: str
    provider: str | None
    model: str | None
    config_fingerprint: str
    configuration_ready: bool
    enabled: bool
    unavailable_code: str | None
    call: Callable[[], Awaitable[None]] | None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    capability: str
    provider: str | None
    model: str | None
    config_fingerprint: str
    configuration_ready: bool
    status: str
    latency_ms: float | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _CaptionProbeRoute:
    selection: CaptionModelSelection
    config_fingerprint: str
    ready: bool
    code: str


def _embedding_execution() -> ProbeExecution:
    service, fingerprint, manifest = default_embedding_execution()

    async def call() -> None:
        assert service is not None
        try:
            await service.embed(
                [PROBE_TEXT],
                org_id=get_settings().platform_org_id,
                task="kb.operations.probe.embedding",
            )
        finally:
            await service.close()

    return ProbeExecution(
        capability="embedding",
        provider=str(fingerprint["provider"]),
        model=str(fingerprint["model"]),
        config_fingerprint=_fingerprint({**fingerprint, **manifest}),
        configuration_ready=service is not None,
        enabled=True,
        unavailable_code=None if service is not None else "embedding_credentials_missing",
        call=call if service is not None else None,
    )


def _rerank_execution() -> ProbeExecution:
    service, manifest = get_rerank_execution()
    enabled = bool(manifest.get("enabled"))
    provider = manifest.get("provider")
    model = manifest.get("model")

    async def call() -> None:
        assert service is not None
        await service.rerank(
            org_id=get_settings().platform_org_id,
            query="provider readiness",
            documents=("provider readiness",),
        )

    return ProbeExecution(
        capability="rerank",
        provider=str(provider) if provider else None,
        model=str(model) if model else None,
        config_fingerprint=_fingerprint(manifest),
        configuration_ready=service is not None or not enabled,
        enabled=enabled,
        unavailable_code=None if service is not None else "rerank_credentials_missing",
        call=call if service is not None else None,
    )


def _image_executions() -> tuple[ProbeExecution, ProbeExecution]:
    service = get_kb_image_enrichment_service()
    readiness = service.readiness()
    caption = service.caption_configuration_status()

    async def ocr_call() -> None:
        await service.probe_ocr(_PROBE_PNG)

    async def caption_call() -> None:
        await service.probe_caption(
            _PROBE_PNG,
            org_id=get_settings().platform_org_id,
        )

    ocr_ready = service.ocr_configuration_ready()
    return (
        ProbeExecution(
            capability="ocr",
            provider=readiness.ocr_provider,
            model=readiness.ocr_model,
            config_fingerprint=readiness.ocr_fingerprint,
            configuration_ready=ocr_ready,
            enabled=True,
            unavailable_code=None if ocr_ready else "ocr_provider_unavailable",
            call=ocr_call if ocr_ready else None,
        ),
        ProbeExecution(
            capability="caption",
            provider=caption.provider or None,
            model=caption.model or None,
            config_fingerprint=readiness.config_fingerprint,
            configuration_ready=caption.ready,
            enabled=True,
            unavailable_code=None if caption.ready else caption.code,
            call=caption_call if caption.ready else None,
        ),
    )


def _invalid_execution(capability: str, *, code: str | None = None) -> ProbeExecution:
    return ProbeExecution(
        capability=capability,
        provider=None,
        model=None,
        config_fingerprint=_fingerprint({"capability": capability, "code": code}),
        configuration_ready=False,
        enabled=True,
        unavailable_code=code or f"{capability}_configuration_invalid",
        call=None,
    )


def resolve_probe_executions() -> tuple[ProbeExecution, ...]:
    try:
        embedding = _embedding_execution()
    except Exception:
        embedding = _invalid_execution("embedding")
    try:
        rerank = _rerank_execution()
    except Exception:
        rerank = _invalid_execution("rerank")
    try:
        ocr, caption = _image_executions()
    except Exception:
        ocr, caption = _invalid_execution("ocr"), _invalid_execution("caption")
    return embedding, rerank, ocr, caption


async def _enabled_kb_caption_selections(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[set[CaptionModelSelection | None], bool]:
    """Return unique enabled KB overrides plus the platform-default sentinel."""
    selections: set[CaptionModelSelection | None] = {None}
    invalid_profile = False
    async with session_factory() as session:
        org_ids: list[object] = []
        offset = 0
        while True:
            page = list(
                (
                    await session.scalars(
                        select(Organization.id)
                        .where(Organization.is_active.is_(True))
                        .order_by(Organization.id)
                        .limit(CAPTION_PROFILE_PAGE_SIZE)
                        .offset(offset)
                    )
                ).all()
            )
            org_ids.extend(page)
            if len(page) < CAPTION_PROFILE_PAGE_SIZE:
                break
            offset += CAPTION_PROFILE_PAGE_SIZE
    for org_id in org_ids:
        async with org_session(session_factory, org_id) as session:
            offset = 0
            while True:
                profiles = list(
                    (
                        await session.scalars(
                            select(KnowledgeBase.ingest_profile)
                            .order_by(KnowledgeBase.id)
                            .limit(CAPTION_PROFILE_PAGE_SIZE)
                            .offset(offset)
                        )
                    ).all()
                )
                for raw_profile in profiles:
                    try:
                        profile = (
                            IngestProfile.model_validate(raw_profile)
                            if raw_profile is not None
                            else IngestProfile()
                        )
                    except ValidationError:
                        invalid_profile = True
                        continue
                    if not profile.caption_images:
                        continue
                    if profile.caption_provider is not None and profile.caption_model is not None:
                        selections.add(
                            CaptionModelSelection(
                                provider=profile.caption_provider,
                                model=profile.caption_model,
                            )
                        )
                if len(profiles) < CAPTION_PROFILE_PAGE_SIZE:
                    break
                offset += CAPTION_PROFILE_PAGE_SIZE
                if len(selections) > MAX_CAPTION_PROBE_ROUTES + 1:
                    break
        if len(selections) > MAX_CAPTION_PROBE_ROUTES + 1:
            break
    return selections, invalid_profile


def _effective_caption_selections(
    selections: Iterable[CaptionModelSelection | None],
) -> set[CaptionModelSelection] | None:
    requested = set(selections)
    effective = {selection for selection in requested if selection is not None}
    if None in requested:
        # Same resolution the execution path uses, so the probe can never report
        # readiness for a model ingestion would not actually call.
        provider, model = platform_caption_selection()
        if not provider or not model:
            return None
        effective.add(CaptionModelSelection(provider=provider, model=model))
    return effective


async def _caption_probe_routes(
    session_factory: async_sessionmaker[AsyncSession],
    selections: set[CaptionModelSelection],
    *,
    service: KbImageEnrichmentService,
) -> list[_CaptionProbeRoute]:
    routes: dict[tuple[str, str, str], _CaptionProbeRoute] = {}
    async with session_factory() as session:
        for selection in sorted(
            selections,
            key=lambda item: (item.provider.casefold(), item.model.casefold()),
        ):
            try:
                await lookup_eligible_provider_model_session(
                    session,
                    provider=selection.provider,
                    model=selection.model,
                )
            except ModelCatalogLookupError as exc:
                config_fingerprint = _fingerprint(
                    {
                        "provider": selection.provider,
                        "model": selection.model,
                        "eligibility": exc.code,
                    }
                )
                route = _CaptionProbeRoute(
                    selection=selection,
                    config_fingerprint=config_fingerprint,
                    ready=False,
                    code=exc.code,
                )
            else:
                caption = service.caption_configuration_status(selection)
                readiness = service.readiness(selection)
                route = _CaptionProbeRoute(
                    selection=selection,
                    config_fingerprint=readiness.config_fingerprint,
                    ready=caption.ready,
                    code=caption.code,
                )
            key = (
                route.selection.provider,
                route.selection.model,
                route.config_fingerprint,
            )
            routes.setdefault(key, route)
    return sorted(
        routes.values(),
        key=lambda item: (
            item.selection.provider.casefold(),
            item.selection.model.casefold(),
            item.config_fingerprint,
        ),
    )


async def _scheduled_caption_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> ProbeExecution:
    selections, invalid_profile = await _enabled_kb_caption_selections(session_factory)
    if invalid_profile:
        return _invalid_execution("caption", code="caption_profile_invalid")
    effective_selections = _effective_caption_selections(selections)
    if effective_selections is None:
        return _invalid_execution("caption", code="caption_provider_unconfigured")
    if len(effective_selections) > MAX_CAPTION_PROBE_ROUTES:
        return _invalid_execution("caption", code="caption_probe_route_limit_exceeded")

    service = get_kb_image_enrichment_service()
    routes = await _caption_probe_routes(
        session_factory,
        effective_selections,
        service=service,
    )
    manifest = [
        {
            "provider": route.selection.provider,
            "model": route.selection.model,
            "config_fingerprint": route.config_fingerprint,
            "ready": route.ready,
            "code": route.code,
        }
        for route in routes
    ]
    first_unready = next((route for route in routes if not route.ready), None)

    async def caption_call() -> None:
        await asyncio.gather(
            *(
                service.probe_caption(
                    _PROBE_PNG,
                    org_id=get_settings().platform_org_id,
                    selection=route.selection,
                )
                for route in routes
            )
        )

    only_route = routes[0] if len(routes) == 1 else None
    return ProbeExecution(
        capability="caption",
        provider=(only_route.selection.provider if only_route is not None else None),
        model=only_route.selection.model if only_route is not None else None,
        config_fingerprint=_fingerprint(manifest),
        configuration_ready=first_unready is None,
        enabled=True,
        unavailable_code=first_unready.code if first_unready else None,
        call=caption_call if first_unready is None else None,
    )


def _safe_failure_code(capability: str, exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return f"{capability}_probe_timeout"
    if isinstance(exc, KbImageEnrichmentError):
        return exc.code[:100]
    if isinstance(exc, EmbeddingUnavailableError):
        return "embedding_probe_failed"
    if isinstance(exc, RerankError):
        return "rerank_probe_failed"
    return f"{capability}_probe_failed"


async def _run_probe(execution: ProbeExecution, *, timeout_seconds: float) -> ProbeResult:
    if not execution.enabled:
        return ProbeResult(
            capability=execution.capability,
            provider=execution.provider,
            model=execution.model,
            config_fingerprint=execution.config_fingerprint,
            configuration_ready=True,
            status="disabled",
            latency_ms=None,
            error_code=None,
        )
    if not execution.configuration_ready or execution.call is None:
        return ProbeResult(
            capability=execution.capability,
            provider=execution.provider,
            model=execution.model,
            config_fingerprint=execution.config_fingerprint,
            configuration_ready=False,
            status="unavailable",
            latency_ms=None,
            error_code=execution.unavailable_code,
        )

    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            await execution.call()
    except Exception as exc:
        return ProbeResult(
            capability=execution.capability,
            provider=execution.provider,
            model=execution.model,
            config_fingerprint=execution.config_fingerprint,
            configuration_ready=True,
            status="degraded",
            latency_ms=(time.perf_counter() - started) * 1000,
            error_code=_safe_failure_code(execution.capability, exc),
        )
    return ProbeResult(
        capability=execution.capability,
        provider=execution.provider,
        model=execution.model,
        config_fingerprint=execution.config_fingerprint,
        configuration_ready=True,
        status="healthy",
        latency_ms=(time.perf_counter() - started) * 1000,
        error_code=None,
    )


async def _persist_results(
    session_factory: async_sessionmaker[AsyncSession],
    results: tuple[ProbeResult, ...],
    *,
    now: datetime,
) -> None:
    async with session_factory() as session:
        for result in results:
            is_success = result.status == "healthy"
            is_failure = result.status in {"degraded", "unavailable"}
            statement = insert(ProviderProbeStatus).values(
                capability=result.capability,
                provider=result.provider,
                model=result.model,
                config_fingerprint=result.config_fingerprint,
                configuration_ready=result.configuration_ready,
                status=result.status,
                last_probe_at=now,
                last_success_at=now if is_success else None,
                last_failure_at=now if is_failure else None,
                degraded_since_at=now if is_failure else None,
                latency_ms=result.latency_ms,
                error_code=result.error_code,
                consecutive_failures=1 if is_failure else 0,
                updated_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[ProviderProbeStatus.capability],
                set_={
                    "provider": statement.excluded.provider,
                    "model": statement.excluded.model,
                    "config_fingerprint": statement.excluded.config_fingerprint,
                    "configuration_ready": statement.excluded.configuration_ready,
                    "status": statement.excluded.status,
                    "last_probe_at": statement.excluded.last_probe_at,
                    "last_success_at": case(
                        (
                            statement.excluded.status == "healthy",
                            statement.excluded.last_probe_at,
                        ),
                        else_=ProviderProbeStatus.last_success_at,
                    ),
                    "last_failure_at": case(
                        (
                            statement.excluded.status.in_(("degraded", "unavailable")),
                            statement.excluded.last_probe_at,
                        ),
                        else_=ProviderProbeStatus.last_failure_at,
                    ),
                    "degraded_since_at": case(
                        (statement.excluded.status.in_(("healthy", "disabled")), None),
                        (
                            ProviderProbeStatus.status.in_(("degraded", "unavailable")),
                            ProviderProbeStatus.degraded_since_at,
                        ),
                        else_=statement.excluded.last_probe_at,
                    ),
                    "latency_ms": statement.excluded.latency_ms,
                    "error_code": statement.excluded.error_code,
                    "consecutive_failures": case(
                        (
                            statement.excluded.status.in_(("degraded", "unavailable")),
                            ProviderProbeStatus.consecutive_failures + 1,
                        ),
                        else_=0,
                    ),
                    "updated_at": statement.excluded.updated_at,
                },
            )
            await session.execute(statement)
            status_value = {
                "healthy": 1,
                "degraded": 0,
                "unavailable": 0,
                "disabled": 1,
            }[result.status]
            metrics.KB_PROVIDER_PROBE_HEALTHY.labels(capability=result.capability).set(status_value)
            if result.latency_ms is not None:
                metrics.KB_PROVIDER_PROBE_LATENCY.labels(capability=result.capability).set(
                    result.latency_ms / 1000
                )
        await session.commit()


async def run_scheduled_provider_probes(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[ProbeResult, ...]:
    """Resolve current config once, execute all probes concurrently, and persist."""
    settings = get_settings()
    base_executions = resolve_probe_executions()
    try:
        scheduled_caption = await _scheduled_caption_execution(session_factory)
    except Exception:
        scheduled_caption = _invalid_execution("caption")
    executions = (*base_executions[:-1], scheduled_caption)
    results = tuple(
        await asyncio.gather(
            *(
                _run_probe(
                    execution,
                    timeout_seconds=settings.operations_provider_probe_timeout_seconds,
                )
                for execution in executions
            )
        )
    )
    await _persist_results(session_factory, results, now=datetime.now(UTC))
    return results


__all__ = [
    "CAPABILITIES",
    "CAPTION_PROFILE_PAGE_SIZE",
    "MAX_CAPTION_PROBE_ROUTES",
    "ProbeExecution",
    "ProbeResult",
    "resolve_probe_executions",
    "run_scheduled_provider_probes",
]
