"""Resolve system capability routes from the shared provider inventory.

SDK 化改造(蓝图 §5.3):TF 的四个槽位常量改为模块级"槽位注册表"——
默认注册原名四槽(kb.embedding / kb.search.rerank / kb.image.caption /
llm.default,KB 子包 P3 依赖这些名字),宿主可经 register_capability_slot()
追加自己的系统槽位。对外函数签名与 TF 保持不变。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from nicekit.domain.model_catalog import ModelCapability
from nicekit.llm.model_catalog import effective_model_metadata
from nicekit.llm.runtime_config import runtime_model_route, runtime_providers

# The platform capability slots the system needs in order to run. Each is a
# platform-level ``model_routes`` row keyed by task, so the model choice lives in
# the database rather than in a deployment's ``.env``.
EMBEDDING_ROUTE_TASK = "kb.embedding"
RERANK_ROUTE_TASK = "kb.search.rerank"
CAPTION_ROUTE_TASK = "kb.image.caption"
LLM_ROUTE_TASK = "llm.default"


@dataclass(frozen=True, slots=True)
class CapabilitySlot:
    """一个系统能力槽位:task 名 + 模型必须具备的能力 + 协议限制。"""

    task: str
    capabilities: tuple[ModelCapability, ...]
    # Embedding and rerank speak the OpenAI-compatible ``/embeddings`` and
    # ``/rerank`` shapes only; chat and caption run on either protocol line.
    openai_only: bool = False


_capability_slots: dict[str, CapabilitySlot] = {}


def register_capability_slot(
    task: str,
    capabilities: Sequence[ModelCapability],
    *,
    openai_only: bool = False,
) -> None:
    """登记(或覆盖)一个系统能力槽位。宿主/子包在 import 期调用。"""
    _capability_slots[task] = CapabilitySlot(
        task=task, capabilities=tuple(capabilities), openai_only=openai_only
    )


def capability_slots() -> dict[str, CapabilitySlot]:
    """当前槽位注册表快照(只读用途)。"""
    return dict(_capability_slots)


# 默认注册 TF 原有四槽。Capabilities a model must carry to serve each slot:
# caption needs both — a vision-only entry could not write the description
# it is asked for.
register_capability_slot(EMBEDDING_ROUTE_TASK, ("embedding",), openai_only=True)
register_capability_slot(RERANK_ROUTE_TASK, ("rerank",), openai_only=True)
register_capability_slot(CAPTION_ROUTE_TASK, ("generation", "vision"))
register_capability_slot(LLM_ROUTE_TASK, ("generation",))


def __getattr__(name: str):
    # TF 兼容视图:两个旧常量从注册表动态派生,晚注册的槽位不会读到陈旧副本。
    if name == "SYSTEM_ROUTE_CAPABILITIES":
        return {task: slot.capabilities for task, slot in _capability_slots.items()}
    if name == "SYSTEM_ROUTE_OPENAI_ONLY":
        return frozenset(
            task for task, slot in _capability_slots.items() if slot.openai_only
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    provider: str
    model: str
    api_key: str
    base_url: str


def provider_model_endpoint(
    provider: str,
    model: str,
    *,
    capability: ModelCapability | None = None,
) -> ModelEndpoint | None:
    rows = runtime_providers()
    if rows is None:
        return None

    row = next(
        (
            candidate
            for candidate in rows
            if candidate["name"] == provider and candidate["enabled"]
        ),
        None,
    )
    if row is None or row["protocol"] != "openai":
        return None
    if capability is not None:
        if model not in set(row["models"]):
            return None
        metadata = effective_model_metadata(
            model,
            row["model_metadata"].get(model),
        )
        if capability not in metadata.capabilities:
            return None

    if not row["api_key"]:
        return None
    # 落库 api_key 可能是 SecretBox 密文(enc: 前缀),端点消费方(embedding/
    # rerank 客户端)需要明文;无前缀明文原样返回。
    from nicekit.core.secretbox import get_secret_box

    return ModelEndpoint(
        provider=provider,
        model=model,
        api_key=get_secret_box().decrypt(row["api_key"]),
        base_url=row["base_url"] or "https://api.openai.com/v1",
    )


def capability_route_endpoints(
    task: str,
    capability: ModelCapability,
    *,
    active_primary: tuple[str, str] | None = None,
) -> list[ModelEndpoint]:
    route = runtime_model_route(task)
    if route is None:
        return []
    primary = (route["primary_provider"], route["primary_model"])
    if active_primary is not None and primary != active_primary:
        # Embedding route changes are staged until the reindex campaign cuts
        # over the active fingerprint in service_configs.
        return []
    hops = [
        {"provider": primary[0], "model": primary[1]},
        *route["fallback_chain"],
    ]
    endpoints: list[ModelEndpoint] = []
    for hop in hops:
        endpoint = provider_model_endpoint(
            hop["provider"],
            hop["model"],
            capability=capability,
        )
        if endpoint is not None:
            endpoints.append(endpoint)
    return endpoints


def _model_serves(row: dict, model: str, capabilities: Sequence[ModelCapability]) -> bool:
    if model not in set(row["models"]):
        return False
    metadata = effective_model_metadata(model, row["model_metadata"].get(model))
    return set(capabilities).issubset(metadata.capabilities)


def system_route_selection(task: str) -> tuple[str, str] | None:
    """First hop of a system route that can still serve it, else ``None``.

    Unlike :func:`capability_route_endpoints` this returns the provider/model
    pair only — chat and caption go through ``LLMService``, which resolves
    credentials itself, and they are not restricted to the OpenAI line.

    Each hop is re-checked against the slot's required capabilities, so a route
    that went stale (its model relabelled or dropped from the inventory after
    the route was saved) degrades to the caller's fallback instead of failing at
    request time.
    """
    slot = _capability_slots.get(task)
    if slot is None:
        raise KeyError(f"{task!r} is not a system capability route")
    route = runtime_model_route(task)
    rows = runtime_providers()
    if route is None or rows is None:
        return None
    by_name = {row["name"]: row for row in rows if row["enabled"]}
    hops = [
        {"provider": route["primary_provider"], "model": route["primary_model"]},
        *route["fallback_chain"],
    ]
    for hop in hops:
        row = by_name.get(hop["provider"])
        if row is None:
            continue
        if slot.openai_only and row["protocol"] != "openai":
            continue
        if _model_serves(row, hop["model"], slot.capabilities):
            return hop["provider"], hop["model"]
    return None


def capability_route_timeout(task: str) -> float | None:
    route = runtime_model_route(task)
    if route is None:
        return None
    timeout = route.get("timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    return None


__all__ = [
    "CAPTION_ROUTE_TASK",
    "EMBEDDING_ROUTE_TASK",
    "LLM_ROUTE_TASK",
    "RERANK_ROUTE_TASK",
    # 经模块级 __getattr__ 从注册表动态派生的 TF 兼容视图
    "SYSTEM_ROUTE_CAPABILITIES",  # noqa: F822
    "SYSTEM_ROUTE_OPENAI_ONLY",  # noqa: F822
    "CapabilitySlot",
    "ModelEndpoint",
    "capability_route_endpoints",
    "capability_route_timeout",
    "capability_slots",
    "provider_model_endpoint",
    "register_capability_slot",
    "system_route_selection",
]
