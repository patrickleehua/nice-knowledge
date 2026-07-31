"""配置拆分单测:域组合完整性、env 变量名兼容、校验器保留、旅游项已删除。"""

import pytest

from nicekit.core.config import (
    AgentSettings,
    CapabilitySettings,
    CoreSettings,
    KbSettings,
    LlmSettings,
    Settings,
    get_settings,
)


def test_settings_composes_all_domains() -> None:
    s = Settings()
    # 每个域抽一个代表字段,确认组合类字段并集完整
    assert s.database_url.startswith("postgresql+psycopg://")  # Core
    assert s.llm_default_provider == "openai"  # Llm
    assert s.kb_ingest_lease_seconds == 120  # Kb
    assert s.skills_dir == "skills"  # Agent
    assert s.websearch_provider == "auto"  # Capability


def test_env_variable_names_unchanged(monkeypatch) -> None:
    # TF 迁移过来的代码直接引用同名字段,env 变量名必须保持不变
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "gpt-x")
    monkeypatch.setenv("KB_PARSER_BACKEND", "fast")
    monkeypatch.setenv("ICRON_TICK_BATCH_SIZE", "7")
    monkeypatch.setenv("WEBSEARCH_MAX_RESULTS", "9")
    s = Settings()
    assert s.database_url == "postgresql+psycopg://u:p@h:5432/db"
    assert s.llm_default_model == "gpt-x"
    assert s.kb_parser_backend == "fast"
    assert s.icron_tick_batch_size == 7
    assert s.websearch_max_results == 9


def test_secret_key_master_env_aliases(monkeypatch) -> None:
    monkeypatch.setenv("NICEKIT_SECRET_KEY", "from-nicekit-env")
    assert Settings().secret_key_master == "from-nicekit-env"
    monkeypatch.delenv("NICEKIT_SECRET_KEY")
    monkeypatch.setenv("SECRET_KEY_MASTER", "from-legacy-env")
    assert Settings().secret_key_master == "from-legacy-env"


def test_kb_ingest_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValueError, match="heartbeat"):
        KbSettings(kb_ingest_lease_seconds=30, kb_ingest_heartbeat_seconds=30)


def test_operations_heartbeat_must_be_shorter_than_expiry() -> None:
    with pytest.raises(ValueError, match="heartbeat"):
        CoreSettings(
            operations_heartbeat_interval_seconds=60,
            operations_heartbeat_expiry_seconds=60,
        )


def test_agent_kb_image_inspection_cross_domain_validator() -> None:
    with pytest.raises(ValueError, match="agent_kb_image_inspection_max_bytes"):
        Settings(
            kb_image_max_bytes=1024,
            agent_kb_image_inspection_max_bytes=2048,
        )


def test_domain_settings_instantiable_standalone() -> None:
    # 各域可独立实例化(域内校验不跨域取字段)
    for cls in (CoreSettings, LlmSettings, KbSettings, AgentSettings, CapabilitySettings):
        cls()


def test_business_only_fields_are_gone() -> None:
    fields = Settings.model_fields
    for name in (
        "ota_provider",
        "ota_timeout_seconds",
        "rapidapi_key",
        "rapidapi_booking_host",
        "fulfillment_reminder_interval_seconds",
        "pipeline_llm_timeout_seconds",
    ):
        assert name not in fields, f"旅游业务配置项 {name} 不应迁移进 SDK"


def test_get_settings_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_rate_limit_exempt_paths_default() -> None:
    assert Settings().rate_limit_exempt_paths == ["/api/v1/health"]
