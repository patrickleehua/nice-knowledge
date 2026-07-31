"""Platform capability slots resolve from model_routes, with .env as fallback.

SDK 化适配:TF 原文件末尾三个 platform_caption_selection 用例依赖
kb.caption(P3 才搬运 kb 子包),此处删除,届时随 kb 测试回归;
另补槽位注册表(register_capability_slot)用例。
"""

import nicekit.llm.capability_routes as route_module
from nicekit.llm.capability_routes import (
    CAPTION_ROUTE_TASK,
    EMBEDDING_ROUTE_TASK,
    LLM_ROUTE_TASK,
    RERANK_ROUTE_TASK,
    SYSTEM_ROUTE_CAPABILITIES,
    register_capability_slot,
    system_route_selection,
)


def _provider(
    name: str,
    *,
    model: str,
    capabilities: list[str],
    protocol: str = "openai",
    enabled: bool = True,
) -> dict:
    return {
        "name": name,
        "protocol": protocol,
        "api_key": "secret",
        "base_url": f"https://{name}.test/v1",
        "models": [model],
        "model_metadata": {
            model: {
                "capabilities": capabilities,
                "input_modalities": ["text", "image"]
                if "vision" in capabilities
                else ["text"],
                "capability_source": "manual",
                "registry_revision": None,
                "provider_metadata": {},
            }
        },
        "enabled": enabled,
    }


def _install(monkeypatch, *, route: dict | None, providers: list[dict] | None) -> None:
    monkeypatch.setattr(
        route_module,
        "runtime_model_route",
        lambda _task: dict(route) if route is not None else None,
    )
    monkeypatch.setattr(route_module, "runtime_providers", lambda: providers)


def _route(provider: str, model: str, *, fallback: list[dict] | None = None) -> dict:
    return {
        "primary_provider": provider,
        "primary_model": model,
        "fallback_chain": fallback or [],
        "timeout_seconds": 120.0,
    }


def test_every_capability_slot_declares_its_required_capabilities() -> None:
    # A slot without a capability requirement would accept any model, so the
    # mapping is the contract the admin validator and the resolver share.
    assert set(SYSTEM_ROUTE_CAPABILITIES) == {
        EMBEDDING_ROUTE_TASK,
        RERANK_ROUTE_TASK,
        CAPTION_ROUTE_TASK,
        LLM_ROUTE_TASK,
    }
    # Caption must both read the image and write the description.
    assert set(SYSTEM_ROUTE_CAPABILITIES[CAPTION_ROUTE_TASK]) == {
        "generation",
        "vision",
    }


def test_unknown_task_is_a_programming_error_not_a_silent_none() -> None:
    try:
        system_route_selection("kb.not.a.system.route")
    except KeyError:
        return
    raise AssertionError("an unknown slot must not resolve silently")


def test_caption_slot_resolves_the_primary_hop(monkeypatch) -> None:
    _install(
        monkeypatch,
        route=_route("vision-gateway", "gpt-4o"),
        providers=[
            _provider(
                "vision-gateway",
                model="gpt-4o",
                capabilities=["generation", "vision"],
            )
        ],
    )

    assert system_route_selection(CAPTION_ROUTE_TASK) == ("vision-gateway", "gpt-4o")


def test_a_stale_hop_is_skipped_for_the_next_one(monkeypatch) -> None:
    # The primary lost its vision label after the route was saved. Degrading to
    # the fallback beats failing at request time.
    _install(
        monkeypatch,
        route=_route(
            "text-only",
            "text-model",
            fallback=[{"provider": "vision-gateway", "model": "gpt-4o"}],
        ),
        providers=[
            _provider("text-only", model="text-model", capabilities=["generation"]),
            _provider(
                "vision-gateway",
                model="gpt-4o",
                capabilities=["generation", "vision"],
            ),
        ],
    )

    assert system_route_selection(CAPTION_ROUTE_TASK) == ("vision-gateway", "gpt-4o")


def test_a_disabled_provider_never_serves_a_slot(monkeypatch) -> None:
    _install(
        monkeypatch,
        route=_route("vision-gateway", "gpt-4o"),
        providers=[
            _provider(
                "vision-gateway",
                model="gpt-4o",
                capabilities=["generation", "vision"],
                enabled=False,
            )
        ],
    )

    assert system_route_selection(CAPTION_ROUTE_TASK) is None


def test_embedding_and_rerank_stay_on_the_openai_line(monkeypatch) -> None:
    # Those two slots speak the /embeddings and /rerank wire shapes only.
    _install(
        monkeypatch,
        route=_route("claude-gateway", "some-embedding"),
        providers=[
            _provider(
                "claude-gateway",
                model="some-embedding",
                capabilities=["embedding"],
                protocol="anthropic",
            )
        ],
    )

    assert system_route_selection(EMBEDDING_ROUTE_TASK) is None


def test_chat_and_caption_may_run_on_the_anthropic_line(monkeypatch) -> None:
    _install(
        monkeypatch,
        route=_route("claude-gateway", "claude-sonnet-4-5"),
        providers=[
            _provider(
                "claude-gateway",
                model="claude-sonnet-4-5",
                capabilities=["generation", "vision"],
                protocol="anthropic",
            )
        ],
    )

    assert system_route_selection(CAPTION_ROUTE_TASK) == (
        "claude-gateway",
        "claude-sonnet-4-5",
    )
    assert system_route_selection(LLM_ROUTE_TASK) == (
        "claude-gateway",
        "claude-sonnet-4-5",
    )


def test_no_route_at_all_resolves_to_nothing(monkeypatch) -> None:
    _install(monkeypatch, route=None, providers=[])

    assert system_route_selection(CAPTION_ROUTE_TASK) is None


# ── 槽位注册表(SDK 化新增)──────────────────────────────────────────────────


def test_registered_custom_slot_resolves_like_builtin_ones(monkeypatch) -> None:
    """宿主注册的槽位与内置四槽同一解析路径;openai_only 约束同样生效。"""
    task = "host.custom.slot"
    register_capability_slot(task, ("generation",), openai_only=True)
    try:
        _install(
            monkeypatch,
            route=_route("claude-gateway", "claude-sonnet-4-5"),
            providers=[
                _provider(
                    "claude-gateway",
                    model="claude-sonnet-4-5",
                    capabilities=["generation"],
                    protocol="anthropic",
                )
            ],
        )
        # openai_only 槽位不接受 anthropic 线
        assert system_route_selection(task) is None

        _install(
            monkeypatch,
            route=_route("openai-gateway", "gpt-4o"),
            providers=[
                _provider("openai-gateway", model="gpt-4o", capabilities=["generation"])
            ],
        )
        assert system_route_selection(task) == ("openai-gateway", "gpt-4o")
        # 动态派生的兼容视图跟着注册表走,不是陈旧快照
        assert task in route_module.SYSTEM_ROUTE_CAPABILITIES
        assert task in route_module.SYSTEM_ROUTE_OPENAI_ONLY
    finally:
        route_module._capability_slots.pop(task, None)
