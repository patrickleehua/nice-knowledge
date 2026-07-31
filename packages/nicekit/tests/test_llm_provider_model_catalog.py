from uuid import uuid4

import pytest
from openai.types import Model

from nicekit.domain.model_catalog import ManualModelCapabilities
from nicekit.llm.model_capability_registry import REGISTRY_REVISION
from nicekit.llm.model_catalog import (
    ModelCatalogLookupError,
    build_model_catalog,
    lookup_eligible_provider_model,
    lookup_eligible_provider_model_session,
    merge_imported_metadata,
    metadata_from_manual,
    metadata_from_provider_model,
    metadata_from_registry,
)
from nicekit.models.llm_provider import LlmProvider


class _ProviderModel:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, **_kwargs) -> dict[str, object]:
        return self.payload


def test_registry_classifies_known_families_without_matching_embedded_names() -> None:
    for model_id in (
        "gpt-5",
        "gpt-5.1",
        "gpt-5.6",
        "gpt-5.6-pro",
        "openai/gpt-5.6",
        "claude-sonnet-4-5",
        "qwen2.5-vl-72b-instruct",
    ):
        metadata = metadata_from_registry(model_id)
        assert {"generation", "vision"}.issubset(metadata.capabilities)
        assert metadata.input_modalities == ["text", "image"]
        assert metadata.capability_source == "registry"
        assert metadata.registry_revision == REGISTRY_REVISION

    for model_id in (
        "opaque-model",
        "my-gpt-5.6",
    ):
        metadata = metadata_from_registry(model_id)
        assert metadata.capabilities == []
        assert metadata.input_modalities == []
        assert metadata.capability_source == "unclassified"
        assert metadata.registry_revision == REGISTRY_REVISION

    embedding = metadata_from_registry("gpt-5.6-embedding")
    assert embedding.capabilities == ["embedding"]
    assert embedding.input_modalities == ["text"]
    assert embedding.capability_source == "registry"
    assert embedding.registry_revision == REGISTRY_REVISION


@pytest.mark.parametrize(
    ("model_id", "capability"),
    [
        ("BAAI/bge-m3", "embedding"),
        ("qwen3-embedding-8b", "embedding"),
        ("text-embedding-3-large", "embedding"),
        ("BAAI/bge-reranker-v2-m3", "rerank"),
        ("qwen3-reranker-8b", "rerank"),
        ("rerank-multilingual-v3.0", "rerank"),
        ("deepseek-chat", "generation"),
        ("openai/gpt-5.7", "vision"),
    ],
)
def test_registry_distinguishes_chat_embedding_and_rerank(
    model_id: str,
    capability: str,
) -> None:
    metadata = metadata_from_registry(model_id)
    assert capability in metadata.capabilities
    if capability in {"embedding", "rerank"}:
        assert metadata.capabilities == [capability]
        assert metadata.input_modalities == ["text"]


def test_provider_explicit_capabilities_override_registry() -> None:
    model_id, metadata = metadata_from_provider_model(
        _ProviderModel(
            {
                "id": "gpt-5.6",
                "owned_by": "gateway",
                "created": 123,
                "capabilities": ["embedding"],
                "input_modalities": ["text"],
            }
        )
    )

    assert model_id == "gpt-5.6"
    assert metadata.capabilities == ["embedding"]
    assert metadata.input_modalities == ["text"]
    assert metadata.capability_source == "provider"
    assert metadata.registry_revision is None
    assert metadata.provider_metadata == {"owned_by": "gateway", "created": 123}


def test_nested_provider_capability_and_camel_case_modalities_are_normalized() -> None:
    _, nested = metadata_from_provider_model(
        {
            "id": "provider-vision",
            "capabilities": {
                "image_input": {"supported": True},
                "embeddings": {"supported": False},
            },
        }
    )
    _, camel = metadata_from_provider_model(
        {
            "id": "provider-camel",
            "inputModalities": ["text", "image"],
        }
    )

    assert nested.capabilities == ["vision"]
    assert nested.input_modalities == ["image"]
    assert nested.capability_source == "provider"
    assert camel.capabilities == ["vision"]
    assert camel.input_modalities == ["text", "image"]
    assert camel.capability_source == "provider"


def test_openrouter_architecture_modalities_are_explicit_provider_evidence() -> None:
    _, metadata = metadata_from_provider_model(
        {
            "id": "openrouter/vision-model",
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
        }
    )

    assert metadata.capabilities == ["generation", "vision"]
    assert metadata.input_modalities == ["text", "image"]
    assert metadata.capability_source == "provider"


def test_generic_image_endpoint_capability_is_not_visual_input_evidence() -> None:
    _, metadata = metadata_from_provider_model(
        {
            "id": "image-generator",
            "capabilities": ["image", "images"],
        }
    )

    assert metadata.capabilities == []
    assert metadata.input_modalities == []
    assert metadata.capability_source == "provider"


def test_openai_sdk_model_extra_is_preserved_and_normalized() -> None:
    sdk_model = Model.model_validate(
        {
            "id": "gateway-vision",
            "object": "model",
            "created": 123,
            "owned_by": "gateway",
            "capabilities": ["generation", "vision"],
            "input_modalities": ["text", "image"],
        }
    )
    assert sdk_model.model_extra == {
        "capabilities": ["generation", "vision"],
        "input_modalities": ["text", "image"],
    }

    model_id, metadata = metadata_from_provider_model(sdk_model)
    assert model_id == "gateway-vision"
    assert metadata.capabilities == ["generation", "vision"]
    assert metadata.input_modalities == ["text", "image"]
    assert metadata.provider_metadata == {"owned_by": "gateway", "created": 123}


def test_standard_provider_model_uses_registry_then_unknown_is_unclassified() -> None:
    _, registered = metadata_from_provider_model(
        {"id": "gpt-5.6", "owned_by": "gateway", "created": 456}
    )
    _, unknown = metadata_from_provider_model(
        {"id": "opaque-model", "owned_by": "gateway"}
    )

    assert registered.capability_source == "registry"
    assert registered.provider_metadata == {"owned_by": "gateway", "created": 456}
    assert unknown.capability_source == "unclassified"
    assert unknown.capabilities == []
    assert unknown.provider_metadata == {"owned_by": "gateway"}


def test_manual_override_survives_provider_import_and_keeps_provider_metadata() -> None:
    manual = metadata_from_manual(
        ManualModelCapabilities(
            capabilities=["rerank"],
            input_modalities=["text"],
        )
    )
    models, metadata = merge_imported_metadata(
        existing_models=["gpt-5.6"],
        existing_metadata={"gpt-5.6": manual.model_dump(mode="json")},
        imported_models=[
            {
                "id": "gpt-5.6",
                "owned_by": "gateway",
                "capabilities": ["generation", "vision"],
            },
            {"id": "opaque-model"},
        ],
    )

    assert models == ["gpt-5.6", "opaque-model"]
    assert metadata["gpt-5.6"]["capability_source"] == "manual"
    assert metadata["gpt-5.6"]["capabilities"] == ["rerank"]
    assert metadata["gpt-5.6"]["provider_metadata"] == {"owned_by": "gateway"}
    assert metadata["opaque-model"]["capability_source"] == "unclassified"


def test_reimport_replaces_stale_provider_metadata_but_not_manual_override() -> None:
    _, provider_metadata = metadata_from_provider_model(
        {
            "id": "provider-model",
            "capabilities": ["generation", "vision"],
            "input_modalities": ["text", "image"],
        }
    )
    manual = metadata_from_manual(
        ManualModelCapabilities(capabilities=["rerank"])
    )
    models, metadata = merge_imported_metadata(
        existing_models=["provider-model", "manual-model"],
        existing_metadata={
            "provider-model": provider_metadata.model_dump(mode="json"),
            "manual-model": manual.model_dump(mode="json"),
        },
        imported_models=[
            {"id": "provider-model", "owned_by": "gateway"},
        ],
    )

    assert models == ["manual-model", "provider-model"]
    assert metadata["provider-model"]["capability_source"] == "unclassified"
    assert metadata["provider-model"]["capabilities"] == []
    assert metadata["provider-model"]["provider_metadata"] == {
        "owned_by": "gateway"
    }
    assert metadata["manual-model"]["capability_source"] == "manual"
    assert metadata["manual-model"]["capabilities"] == ["rerank"]


def test_catalog_filters_vision_and_search_and_omits_disabled_provider() -> None:
    vision_provider = LlmProvider(
        id=uuid4(),
        name="vision",
        protocol="openai",
        models=["gpt-5.6", "reranker"],
        model_metadata={
            "reranker": metadata_from_manual(
                ManualModelCapabilities(capabilities=["rerank"])
            ).model_dump(mode="json")
        },
    )
    disabled_provider = LlmProvider(
        id=uuid4(),
        name="disabled",
        protocol="openai",
        models=["gpt-5.6"],
        enabled=False,
    )

    vision = build_model_catalog(
        [vision_provider, disabled_provider],
        capability="vision",
    )
    search = build_model_catalog(
        [vision_provider, disabled_provider],
        capability="search",
    )

    assert [item.model for item in vision] == ["gpt-5.6"]
    assert [item.model for item in search] == ["reranker"]
    assert vision[0].model_dump().keys() == {
        "provider",
        "model",
        "vendor",
        "capabilities",
        "input_modalities",
        "capability_source",
        "registry_revision",
        "provider_metadata",
    }


def test_provider_model_requires_id() -> None:
    with pytest.raises(ValueError, match="id"):
        metadata_from_provider_model({"owned_by": "gateway"})


def test_exact_eligible_lookup_returns_stable_codes() -> None:
    provider = LlmProvider(
        id=uuid4(),
        name="catalog",
        protocol="openai",
        models=["gpt-5.6", "search-only"],
        model_metadata={
            "search-only": metadata_from_manual(
                ManualModelCapabilities(capabilities=["embedding"])
            ).model_dump(mode="json")
        },
    )

    entry = lookup_eligible_provider_model(
        [provider],
        provider="catalog",
        model="gpt-5.6",
    )
    assert {"generation", "vision"}.issubset(entry.capabilities)

    cases = (
        ("missing", "gpt-5.6", "caption_provider_unavailable"),
        ("catalog", "missing", "caption_model_unavailable"),
        ("catalog", "search-only", "caption_model_ineligible"),
    )
    for provider_name, model, code in cases:
        with pytest.raises(ModelCatalogLookupError) as exc_info:
            lookup_eligible_provider_model(
                [provider],
                provider=provider_name,
                model=model,
            )
        assert exc_info.value.code == code

    provider.enabled = False
    with pytest.raises(ModelCatalogLookupError) as exc_info:
        lookup_eligible_provider_model(
            [provider],
            provider="catalog",
            model="gpt-5.6",
        )
    assert exc_info.value.code == "caption_provider_unavailable"


async def test_async_exact_lookup_reuses_catalog_contract() -> None:
    provider = LlmProvider(
        id=uuid4(),
        name="catalog",
        protocol="openai",
        models=["gpt-5.6"],
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [provider]

    class _Session:
        async def execute(self, _statement):
            return _Result()

    entry = await lookup_eligible_provider_model_session(
        _Session(),
        provider="catalog",
        model="gpt-5.6",
    )
    assert entry.model == "gpt-5.6"
