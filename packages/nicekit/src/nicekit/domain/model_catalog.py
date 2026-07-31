"""Provider model capability catalog contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 能力词表与 Cherry Studio 的模型类型对齐(视觉/联网/推理/工具/重排/嵌入),
# 另加 generation:它在本平台是承重的——caption 要求 generation+vision、
# llm.default 路由要求 generation,缺了它未分类模型会隐式变成可对话。
ModelCapability = Literal[
    "generation",
    "vision",
    "web_search",
    "reasoning",
    "function_call",
    "embedding",
    "rerank",
]
ModelInputModality = Literal["text", "image"]
CapabilitySource = Literal["manual", "provider", "registry", "unclassified"]
ModelCatalogCapabilityFilter = Literal[
    "generation",
    "vision",
    "web_search",
    "reasoning",
    "function_call",
    "embedding",
    "rerank",
    "search",
]

# 展示顺序与管理端 chip 顺序一致
_CAPABILITY_ORDER = {
    "generation": 0,
    "vision": 1,
    "web_search": 2,
    "reasoning": 3,
    "function_call": 4,
    "embedding": 5,
    "rerank": 6,
}
_MODALITY_ORDER = {"text": 0, "image": 1}


def canonical_capabilities(values: list[ModelCapability]) -> list[ModelCapability]:
    return sorted(set(values), key=_CAPABILITY_ORDER.__getitem__)


def canonical_input_modalities(
    values: list[ModelInputModality],
) -> list[ModelInputModality]:
    return sorted(set(values), key=_MODALITY_ORDER.__getitem__)


class ProviderModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[ModelCapability] = Field(default_factory=list)
    input_modalities: list[ModelInputModality] = Field(default_factory=list)
    capability_source: CapabilitySource = "unclassified"
    registry_revision: str | None = Field(default=None, max_length=100)
    provider_metadata: dict[str, str | int] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def _canonical_capabilities(
        cls,
        values: list[ModelCapability],
    ) -> list[ModelCapability]:
        return canonical_capabilities(values)

    @field_validator("input_modalities")
    @classmethod
    def _canonical_modalities(
        cls,
        values: list[ModelInputModality],
    ) -> list[ModelInputModality]:
        return canonical_input_modalities(values)


class ManualModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[ModelCapability] = Field(default_factory=list)
    input_modalities: list[ModelInputModality] | None = None

    @field_validator("capabilities")
    @classmethod
    def _canonical_capabilities(
        cls,
        values: list[ModelCapability],
    ) -> list[ModelCapability]:
        return canonical_capabilities(values)

    @field_validator("input_modalities")
    @classmethod
    def _canonical_modalities(
        cls,
        values: list[ModelInputModality] | None,
    ) -> list[ModelInputModality] | None:
        if values is None:
            return None
        return canonical_input_modalities(values)


class ProviderModelCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    # Grouping hint derived from the model ID. Never persisted and never
    # overridable — it is a pure function of the ID, so a manual capability
    # override must not be able to move a model under the wrong vendor.
    vendor: str | None
    capabilities: list[ModelCapability]
    input_modalities: list[ModelInputModality]
    capability_source: CapabilitySource
    registry_revision: str | None
    provider_metadata: dict[str, str | int]


__all__ = [
    "CapabilitySource",
    "ManualModelCapabilities",
    "ModelCapability",
    "ModelCatalogCapabilityFilter",
    "ModelInputModality",
    "ProviderModelCatalogEntry",
    "ProviderModelMetadata",
    "canonical_capabilities",
    "canonical_input_modalities",
]
