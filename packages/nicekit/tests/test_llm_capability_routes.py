"""Capability route resolution tests; provider calls never leave the process."""

import nicekit.llm.capability_routes as route_module
from nicekit.llm.capability_routes import (
    EMBEDDING_ROUTE_TASK,
    capability_route_endpoints,
    provider_model_endpoint,
)


def _provider(
    name: str,
    *,
    capability: str = "embedding",
    enabled: bool = True,
    protocol: str = "openai",
    api_key: str = "secret",
) -> dict:
    model = f"{name}-model"
    return {
        "name": name,
        "protocol": protocol,
        "api_key": api_key,
        "base_url": f"https://{name}.test/v1",
        "models": [model],
        "model_metadata": {
            model: {
                "capabilities": [capability],
                "input_modalities": ["text"],
                "capability_source": "manual",
                "registry_revision": None,
                "provider_metadata": {},
            }
        },
        "enabled": enabled,
    }


def test_provider_endpoint_requires_enabled_openai_provider_and_capability(
    monkeypatch,
) -> None:
    providers = [
        _provider("eligible"),
        _provider("disabled", enabled=False),
        _provider("wrong-capability", capability="rerank"),
        _provider("anthropic", protocol="anthropic"),
        _provider("credentialless", api_key=""),
    ]
    monkeypatch.setattr(route_module, "runtime_providers", lambda: providers)
    endpoint = provider_model_endpoint(
        "eligible",
        "eligible-model",
        capability="embedding",
    )

    assert endpoint is not None
    assert endpoint.provider == "eligible"
    assert endpoint.model == "eligible-model"
    assert (
        provider_model_endpoint(
            "disabled",
            "disabled-model",
            capability="embedding",
        )
        is None
    )
    assert (
        provider_model_endpoint(
            "wrong-capability",
            "wrong-capability-model",
            capability="embedding",
        )
        is None
    )
    assert (
        provider_model_endpoint(
            "anthropic",
            "anthropic-model",
            capability="embedding",
        )
        is None
    )
    assert (
        provider_model_endpoint(
            "credentialless",
            "credentialless-model",
            capability="embedding",
        )
        is None
    )


def test_capability_route_skips_ineligible_hops(monkeypatch) -> None:
    monkeypatch.setattr(
        route_module,
        "runtime_providers",
        lambda: [
            _provider("disabled", enabled=False),
            _provider("wrong-capability", capability="rerank"),
            _provider("eligible"),
        ],
    )
    monkeypatch.setattr(
        route_module,
        "runtime_model_route",
        lambda task: (
            {
                "primary_provider": "disabled",
                "primary_model": "disabled-model",
                "fallback_chain": [
                    {
                        "provider": "wrong-capability",
                        "model": "wrong-capability-model",
                    },
                    {"provider": "eligible", "model": "eligible-model"},
                ],
            }
            if task == EMBEDDING_ROUTE_TASK
            else None
        ),
    )

    endpoints = capability_route_endpoints(
        EMBEDDING_ROUTE_TASK,
        "embedding",
    )

    assert [(endpoint.provider, endpoint.model) for endpoint in endpoints] == [
        ("eligible", "eligible-model")
    ]
