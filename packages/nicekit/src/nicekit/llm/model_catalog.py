"""Normalize and merge provider model capability metadata."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.domain.model_catalog import (
    ManualModelCapabilities,
    ModelCapability,
    ModelCatalogCapabilityFilter,
    ModelInputModality,
    ProviderModelCatalogEntry,
    ProviderModelMetadata,
    canonical_capabilities,
    canonical_input_modalities,
)
from nicekit.llm.model_capability_registry import (
    REGISTRY_REVISION,
    lookup_registry,
    lookup_vendor,
)
from nicekit.models.llm_provider import LlmProvider

_EXPLICIT_CAPABILITY_FIELDS = (
    "capabilities",
    "modalities",
    "input_modalities",
    "inputModalities",
)
_CAPABILITY_TOKENS: dict[str, ModelCapability] = {
    "generation": "generation",
    "text_generation": "generation",
    "chat": "generation",
    "chat_completion": "generation",
    "completion": "generation",
    "completions": "generation",
    "vision": "vision",
    "image_input": "vision",
    "image_understanding": "vision",
    "function_call": "function_call",
    "function_calling": "function_call",
    "tool_call": "function_call",
    "tool_calls": "function_call",
    "tool_use": "function_call",
    "tools": "function_call",
    "reasoning": "reasoning",
    "thinking": "reasoning",
    "chain_of_thought": "reasoning",
    "web_search": "web_search",
    "websearch": "web_search",
    "search": "web_search",
    "online_search": "web_search",
    "browsing": "web_search",
    "embedding": "embedding",
    "embeddings": "embedding",
    "text_embedding": "embedding",
    "rerank": "rerank",
    "reranking": "rerank",
}
_MODALITY_TOKENS: dict[str, ModelInputModality] = {
    "text": "text",
    "image": "image",
    "images": "image",
    "vision": "image",
    "image_input": "image",
}


class ModelCatalogLookupError(LookupError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_model_ids(values: Iterable[str]) -> list[str]:
    normalized: set[str] = set()
    for value in values:
        model_id = value.strip()
        if not model_id:
            raise ValueError("模型 ID 不可为空")
        if len(model_id) > 300:
            raise ValueError("模型 ID 最长 300 个字符")
        normalized.add(model_id)
    return sorted(normalized, key=str.casefold)


def _normalized_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = re_normalize(value)
    return token or None


def re_normalize(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _explicitly_supported(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return re_normalize(value) in {"enabled", "supported", "true", "yes"}
    if isinstance(value, Mapping):
        if "supported" in value:
            return value["supported"] is True
        if "enabled" in value:
            return value["enabled"] is True
    return False


def _declared_tokens(value: object) -> Iterable[str]:
    if isinstance(value, str):
        token = _normalized_token(value)
        if token is not None:
            yield token
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _declared_tokens(item)
        return
    if isinstance(value, Mapping):
        for key, support in value.items():
            token = _normalized_token(key)
            if token is not None and _explicitly_supported(support):
                yield token


def _provider_mapping(model: object) -> dict[str, object]:
    if isinstance(model, Mapping):
        return {str(key): value for key, value in model.items()}
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        value = dump(mode="python", exclude_none=True)
        if isinstance(value, dict):
            return {str(key): nested for key, nested in value.items()}
    result: dict[str, object] = {}
    for field in ("id", "owned_by", "created"):
        value = getattr(model, field, None)
        if value is not None:
            result[field] = value
    extra = getattr(model, "model_extra", None)
    if isinstance(extra, dict):
        result.update(extra)
    return result


def _provider_standard_metadata(payload: Mapping[str, object]) -> dict[str, str | int]:
    metadata: dict[str, str | int] = {}
    owned_by = payload.get("owned_by")
    if isinstance(owned_by, str) and owned_by.strip():
        metadata["owned_by"] = owned_by.strip()[:300]
    created = payload.get("created")
    if isinstance(created, int) and not isinstance(created, bool):
        metadata["created"] = created
    return metadata


def _modalities_for_capabilities(
    capabilities: Iterable[ModelCapability],
) -> list[ModelInputModality]:
    modalities: list[ModelInputModality] = []
    for capability in capabilities:
        # ``function_call`` / ``reasoning`` are behaviours of a text model, not
        # extra input channels — they still imply text, so a manual override
        # that only ticks them does not end up with an empty modality set.
        if capability != "vision":
            modalities.append("text")
        else:
            modalities.append("image")
    return canonical_input_modalities(modalities)


def metadata_from_registry(model_id: str) -> ProviderModelMetadata:
    match = lookup_registry(model_id)
    if match is None:
        return ProviderModelMetadata(
            capability_source="unclassified",
            registry_revision=REGISTRY_REVISION,
        )
    return ProviderModelMetadata(
        capabilities=list(match.capabilities),
        input_modalities=list(match.input_modalities),
        capability_source="registry",
        registry_revision=REGISTRY_REVISION,
    )


def metadata_from_provider_model(
    model: object,
) -> tuple[str, ProviderModelMetadata]:
    payload = _provider_mapping(model)
    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("模型目录项缺少有效 id")
    model_id = normalize_model_ids([raw_id])[0]
    standard_metadata = _provider_standard_metadata(payload)
    explicit_fields: dict[str, object] = {
        field: payload[field]
        for field in _EXPLICIT_CAPABILITY_FIELDS
        if field in payload and payload[field] is not None
    }
    architecture = payload.get("architecture")
    if isinstance(architecture, Mapping):
        for field in (
            "input_modalities",
            "inputModalities",
            "output_modalities",
            "outputModalities",
        ):
            if field in architecture and architecture[field] is not None:
                explicit_fields[f"architecture.{field}"] = architecture[field]
    if not explicit_fields:
        metadata = metadata_from_registry(model_id)
        metadata.provider_metadata = standard_metadata
        return model_id, metadata

    capability_values: list[ModelCapability] = []
    modality_values: list[ModelInputModality] = []
    for field, value in explicit_fields.items():
        tokens = list(_declared_tokens(value))
        if field == "capabilities":
            capability_values.extend(
                _CAPABILITY_TOKENS[token]
                for token in tokens
                if token in _CAPABILITY_TOKENS
            )
        elif field in {
            "modalities",
            "input_modalities",
            "inputModalities",
            "architecture.input_modalities",
            "architecture.inputModalities",
        }:
            modality_values.extend(
                _MODALITY_TOKENS[token]
                for token in tokens
                if token in _MODALITY_TOKENS
            )
            if any(_MODALITY_TOKENS.get(token) == "image" for token in tokens):
                capability_values.append("vision")
            if field == "modalities":
                capability_values.extend(
                    _CAPABILITY_TOKENS[token]
                    for token in tokens
                    if token in _CAPABILITY_TOKENS
                )
        elif field in {
            "architecture.output_modalities",
            "architecture.outputModalities",
        } and "text" in tokens:
            capability_values.append("generation")
    capabilities = canonical_capabilities(capability_values)
    modalities = canonical_input_modalities(
        [*modality_values, *_modalities_for_capabilities(capabilities)]
    )
    return model_id, ProviderModelMetadata(
        capabilities=capabilities,
        input_modalities=modalities,
        capability_source="provider",
        provider_metadata=standard_metadata,
    )


def metadata_from_manual(
    value: ManualModelCapabilities,
    *,
    provider_metadata: Mapping[str, str | int] | None = None,
) -> ProviderModelMetadata:
    capabilities = canonical_capabilities(value.capabilities)
    modalities = (
        canonical_input_modalities(value.input_modalities)
        if value.input_modalities is not None
        else _modalities_for_capabilities(capabilities)
    )
    return ProviderModelMetadata(
        capabilities=capabilities,
        input_modalities=modalities,
        capability_source="manual",
        provider_metadata=dict(provider_metadata or {}),
    )


def _stored_metadata(value: object) -> ProviderModelMetadata | None:
    try:
        return ProviderModelMetadata.model_validate(value)
    except ValidationError:
        return None


def effective_model_metadata(
    model_id: str,
    value: object,
) -> ProviderModelMetadata:
    stored = _stored_metadata(value)
    if stored is not None and stored.capability_source in {"manual", "provider"}:
        return stored
    resolved = metadata_from_registry(model_id)
    if stored is not None:
        resolved.provider_metadata = stored.provider_metadata
    return resolved


def reconcile_provider_metadata(
    models: Iterable[str],
    stored: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    normalized_models = normalize_model_ids(models)
    source = stored or {}
    return {
        model_id: effective_model_metadata(model_id, source.get(model_id)).model_dump(
            mode="json"
        )
        for model_id in normalized_models
    }


def merge_imported_metadata(
    *,
    existing_models: Iterable[str],
    existing_metadata: Mapping[str, object] | None,
    imported_models: Iterable[object],
) -> tuple[list[str], dict[str, dict[str, object]]]:
    models = set(normalize_model_ids(existing_models))
    source = existing_metadata or {}
    merged: dict[str, dict[str, object]] = {}
    for model_id in models:
        current = _stored_metadata(source.get(model_id))
        if current is not None and current.capability_source == "manual":
            merged[model_id] = current.model_dump(mode="json")
        else:
            merged[model_id] = metadata_from_registry(model_id).model_dump(mode="json")
    for provider_model in imported_models:
        model_id, imported = metadata_from_provider_model(provider_model)
        models.add(model_id)
        current = _stored_metadata(source.get(model_id))
        if current is not None and current.capability_source == "manual":
            current.provider_metadata = {
                **imported.provider_metadata,
                **current.provider_metadata,
            }
            merged[model_id] = current.model_dump(mode="json")
        else:
            merged[model_id] = imported.model_dump(mode="json")
    normalized_models = normalize_model_ids(models)
    return normalized_models, {
        model_id: merged.get(
            model_id,
            metadata_from_registry(model_id).model_dump(mode="json"),
        )
        for model_id in normalized_models
    }


def provider_metadata_for_read(
    provider: LlmProvider,
) -> dict[str, ProviderModelMetadata]:
    stored = provider.model_metadata or {}
    return {
        model_id: effective_model_metadata(model_id, stored.get(model_id))
        for model_id in normalize_model_ids(provider.models or [])
    }


def catalog_entry(
    provider_name: str,
    model_id: str,
    metadata: ProviderModelMetadata,
) -> ProviderModelCatalogEntry:
    """Single construction point, so ``vendor`` is always derived, never passed in."""
    return ProviderModelCatalogEntry(
        provider=provider_name,
        model=model_id,
        vendor=lookup_vendor(model_id),
        **metadata.model_dump(),
    )


def provider_model_catalog_entry(
    provider: LlmProvider,
    model_id: str,
) -> ProviderModelCatalogEntry:
    metadata = provider_metadata_for_read(provider).get(model_id)
    if metadata is None:
        raise LookupError("模型不存在")
    return catalog_entry(provider.name, model_id, metadata)


def build_model_catalog(
    providers: Iterable[LlmProvider],
    *,
    capability: ModelCatalogCapabilityFilter | None = None,
) -> list[ProviderModelCatalogEntry]:
    entries: list[ProviderModelCatalogEntry] = []
    for provider in providers:
        if not provider.enabled:
            continue
        for model_id, metadata in provider_metadata_for_read(provider).items():
            if capability == "search":
                if not {"embedding", "rerank"}.intersection(metadata.capabilities):
                    continue
            elif capability is not None and capability not in metadata.capabilities:
                continue
            entries.append(provider_model_catalog_entry(provider, model_id))
    return sorted(
        entries,
        key=lambda entry: (entry.provider.casefold(), entry.model.casefold()),
    )


def lookup_eligible_provider_model(
    providers: Iterable[LlmProvider],
    *,
    provider: str,
    model: str,
) -> ProviderModelCatalogEntry:
    provider_row = next(
        (
            row
            for row in providers
            if row.name == provider and row.enabled
        ),
        None,
    )
    if provider_row is None:
        raise ModelCatalogLookupError("caption_provider_unavailable")
    if model not in set(provider_row.models or []):
        raise ModelCatalogLookupError("caption_model_unavailable")
    entry = provider_model_catalog_entry(provider_row, model)
    if not {"generation", "vision"}.issubset(entry.capabilities):
        raise ModelCatalogLookupError("caption_model_ineligible")
    return entry


async def lookup_eligible_provider_model_session(
    session: AsyncSession,
    *,
    provider: str,
    model: str,
) -> ProviderModelCatalogEntry:
    rows = (
        await session.execute(
            select(LlmProvider).where(LlmProvider.name == provider)
        )
    ).scalars().all()
    return lookup_eligible_provider_model(
        rows,
        provider=provider,
        model=model,
    )


__all__ = [
    "ModelCatalogLookupError",
    "build_model_catalog",
    "catalog_entry",
    "effective_model_metadata",
    "lookup_eligible_provider_model",
    "lookup_eligible_provider_model_session",
    "merge_imported_metadata",
    "metadata_from_manual",
    "metadata_from_provider_model",
    "metadata_from_registry",
    "normalize_model_ids",
    "provider_model_catalog_entry",
    "provider_metadata_for_read",
    "reconcile_provider_metadata",
]
