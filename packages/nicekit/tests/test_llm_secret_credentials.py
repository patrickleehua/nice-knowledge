"""api_key 解密单测(SDK 化新增):凭证解析处经 SecretBox 解密。

覆盖三条路径:.env 内置双线、runtime_providers 落库行、capability_routes
端点;enc: 密文解出明文,无前缀明文原样返回(兼容未加密存量配置)。
"""

from types import SimpleNamespace

import nicekit.llm.capability_routes as route_module
import nicekit.llm.service as service_module
from nicekit.core.secretbox import SecretBox
from nicekit.llm.capability_routes import provider_model_endpoint
from nicekit.llm.service import env_provider_credentials, get_llm_service

BOX = SecretBox("test-master-key")


def _settings(**overrides):
    base = dict(
        openai_api_key="", openai_base_url="",
        anthropic_api_key="", anthropic_base_url="",
        llm_anthropic_thinking_budget=0,
        llm_anthropic_thinking_budgets={},
        llm_openai_effort_map={},
        llm_openai_omit_max_output_tokens=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _provider_row(name: str, api_key: str, *, protocol: str = "openai") -> dict:
    return {
        "name": name, "protocol": protocol, "api_key": api_key,
        "base_url": "https://gw.test/v1", "models": ["m1"],
        "model_metadata": {}, "enabled": True,
    }


def test_env_credentials_decrypt_ciphertext_and_pass_plaintext(monkeypatch) -> None:
    encrypted = BOX.encrypt("sk-openai-secret")
    assert encrypted.startswith("enc:")
    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: _settings(
            openai_api_key=encrypted, anthropic_api_key="sk-anthropic-plain"
        ),
    )
    monkeypatch.setattr(service_module, "get_secret_box", lambda: BOX)

    creds = env_provider_credentials()

    assert creds["openai"][1] == "sk-openai-secret"  # 密文解出明文
    assert creds["anthropic"][1] == "sk-anthropic-plain"  # 无前缀明文原样返回


def test_get_llm_service_decrypts_runtime_provider_rows(monkeypatch) -> None:
    encrypted = BOX.encrypt("sk-db-secret")
    monkeypatch.setattr(service_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(service_module, "get_secret_box", lambda: BOX)
    monkeypatch.setattr(
        service_module,
        "runtime_providers",
        lambda: [
            _provider_row("gw-enc", encrypted),
            _provider_row("gw-plain", "sk-db-plain"),
        ],
    )

    svc = get_llm_service()

    assert svc._providers["gw-enc"]._client.api_key == "sk-db-secret"
    assert svc._providers["gw-plain"]._client.api_key == "sk-db-plain"


def test_provider_model_endpoint_returns_decrypted_key(monkeypatch) -> None:
    encrypted = BOX.encrypt("sk-embed-secret")
    monkeypatch.setattr(
        route_module, "runtime_providers", lambda: [_provider_row("gw", encrypted)]
    )
    import nicekit.core.secretbox as secretbox_module

    monkeypatch.setattr(secretbox_module, "get_secret_box", lambda: BOX)

    endpoint = provider_model_endpoint("gw", "m1")

    assert endpoint is not None
    assert endpoint.api_key == "sk-embed-secret"


def test_empty_provider_table_falls_back_to_env_builtins(monkeypatch) -> None:
    """全新部署:表已加载但为空 → 内置双线仍要能用 .env 凭证起来。

    回归用例。曾经的判据是 `rows is None`(表未加载才回落),于是刚迁移完、
    管理员还没配 provider 的库返回 `[]`,凭证解析结果是空 dict,首次对话
    直接 AllProvidersFailedError,而且失败在选实例阶段、llm_traces 里连
    一条记录都没有,极难排查。
    """
    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: _settings(openai_api_key="sk-env", openai_base_url="https://env.test/v1"),
    )
    monkeypatch.setattr(service_module, "get_secret_box", lambda: BOX)
    monkeypatch.setattr(service_module, "runtime_providers", lambda: [])

    svc = get_llm_service()

    assert "openai" in svc._providers, "空表时内置 openai 应回落 .env"
    assert svc._providers["openai"]._client.api_key == "sk-env"


def test_env_fallback_does_not_resurrect_disabled_table_row(monkeypatch) -> None:
    """管理员显式禁用的实例不该被 .env 复活——那是他的决定,不是缺配置。"""
    row = _provider_row("openai", "sk-db")
    row["enabled"] = False
    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: _settings(openai_api_key="sk-env", openai_base_url="https://env.test/v1"),
    )
    monkeypatch.setattr(service_module, "get_secret_box", lambda: BOX)
    monkeypatch.setattr(service_module, "runtime_providers", lambda: [row])

    svc = get_llm_service()

    assert "openai" not in svc._providers
