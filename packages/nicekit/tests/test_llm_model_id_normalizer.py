"""Canonical model ID folding and the capability rules built on top of it."""

import pytest

from nicekit.llm.model_capability_registry import (
    lookup_registry,
    lookup_vendor,
)
from nicekit.llm.model_id_normalizer import (
    normalize_model_id,
    normalize_model_id_detailed,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Namespaces and aggregator routing prefixes
        ("Qwen/Qwen2.5-VL-72B-Instruct", "qwen2-5-vl-instruct"),
        ("Pro/moonshotai/Kimi-K2-Instruct", "kimi-k2-instruct"),
        ("accounts/fireworks/models/deepseek-v3", "deepseek-v3"),
        ("aihubmix-gpt-5", "gpt-5"),
        ("siliconflow-deepseek-chat", "deepseek-chat"),
        ("zai-org/GLM-4.6", "glm-4-6"),
        # Bedrock / Vertex cross-vendor ARNs
        ("us.anthropic.claude-sonnet-4-5-v1:0", "claude-sonnet-4-5"),
        ("global.meta.llama3-1-70b-instruct-v1:0", "llama3-1-instruct"),
        ("openai.gpt-oss-120b-1:0", "gpt-oss"),
        ("gemini-2.0-flash@001", "gemini-2-0-flash"),
        # Release-date stamps, in all four spellings
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("claude-3-7-sonnet-20250219", "claude-3-7-sonnet"),
        ("deepseek-v3-1-250821", "deepseek-v3-1"),
        ("grok-4-0709", "grok-4"),
        ("qwen3-235b-a22b-2507", "qwen3-a22b"),
        # A version or a context size is not a date
        ("gemini-1.5-pro-002", "gemini-1-5-pro-002"),
        ("gpt-4-32k", "gpt-4-32k"),
        ("step-2-16k", "step-2-16k"),
        # Quantization and parameter size
        ("Qwen/Qwen2.5-72B-Instruct-AWQ", "qwen2-5-instruct"),
        ("glm-4-5-fp8", "glm-4-5"),
        ("mixtral-8x7b-instruct", "mixtral-instruct"),
        ("qwen2.5-1.5b-instruct", "qwen2-5-instruct"),
        # Registry-style colon size tags are realigned, not dropped
        ("qwen2.5:7b", "qwen2-5"),
        ("gpt-oss:20b", "gpt-oss"),
        # Routing variants
        ("deepseek-r1:free", "deepseek-r1"),
        ("gpt-5-chat-latest", "gpt-5-chat"),
        ("gemini-3-pro-preview", "gemini-3-pro"),
        ("glm-4.5-air-nothink", "glm-4-5-air"),
        ("kimi-k2-thinking", "kimi-k2"),
        ("deepseek-v3 (free)", "deepseek-v3"),
        # Version separators and underscores
        ("gpt-4.1", "gpt-4-1"),
        ("abab6.5s-chat", "abab6-5s-chat"),
        ("bce-embedding-base_v1", "bce-embedding-base-v1"),
        # Opaque ids survive untouched
        ("ep-20250101-abcde", "ep-20250101-abcde"),
        ("opaque-model", "opaque-model"),
        ("", ""),
    ],
)
def test_normalizer_folds_gateway_spellings_to_one_stem(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected


def test_normalizer_is_idempotent_because_stripping_runs_to_a_fixpoint() -> None:
    # A trailing date stamp hides the variant suffix behind it, so a
    # single-pass normalizer would not be stable under re-application.
    assert normalize_model_id("qwen3-235b-a22b-thinking-2507") == "qwen3-a22b"
    for raw in (
        "qwen3-235b-a22b-thinking-2507",
        "glm-4.5-air-fp8-nothink",
        "Qwen/Qwen2.5-72B-Instruct-AWQ",
        "us.anthropic.claude-sonnet-4-5-v1:0",
    ):
        once = normalize_model_id(raw)
        assert normalize_model_id(once) == once


def test_normalizer_keeps_a_compound_no_think_tag_intact() -> None:
    # ``-think`` must not be peeled off ``…-no-think`` — the whole tag is one
    # variant. The guard only fires on its own token, so ``-search`` still goes.
    assert normalize_model_id_detailed("glm-4.5-no-think").variants == frozenset({"no-think"})
    assert normalize_model_id("inferno-search") == "inferno"


def test_normalizer_reports_the_variant_tags_it_removed() -> None:
    assert normalize_model_id_detailed("kimi-k2-thinking").variants == frozenset({"thinking"})
    assert normalize_model_id_detailed("deepseek-r1:free").variants == frozenset({"free"})
    assert normalize_model_id_detailed("glm-4.5-air-nothink").variants == frozenset({"nothink"})
    assert normalize_model_id_detailed("deepseek-v3").variants == frozenset()


def test_size_preserving_mode_keeps_sibling_skus_distinct() -> None:
    assert normalize_model_id("gpt-oss:20b", keep_parameter_size=True) == "gpt-oss-20b"
    assert normalize_model_id("gpt-oss-120b", keep_parameter_size=True) == "gpt-oss-120b"
    # Without it both collapse onto the family stem.
    assert normalize_model_id("gpt-oss:20b") == normalize_model_id("gpt-oss-120b")


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        # Vision is recognized across vendors, including the ``-vl`` spelling
        # and SKUs whose family rule alone would not imply it.
        ("Qwen/Qwen2.5-VL-72B-Instruct", {"generation", "vision", "function_call"}),
        ("hunyuan-vision", {"generation", "vision", "function_call"}),
        ("doubao-1.5-vision-pro-32k", {"generation", "vision", "function_call"}),
        ("ernie-4.5-turbo-vl-32k", {"generation", "vision", "function_call"}),
        ("glm-4v-plus", {"generation", "vision", "function_call"}),
        ("step-1v-8k", {"generation", "vision"}),
        ("deepseek-vl2", {"generation", "vision"}),
        # Reasoning comes from the family…
        ("deepseek-r1", {"generation", "reasoning"}),
        ("o3-mini", {"generation", "function_call", "reasoning"}),
        ("magistral-medium-2506", {"generation", "function_call", "reasoning"}),
        # …or from the variant tag the normalizer peeled off.
        ("kimi-k2-thinking", {"generation", "function_call", "reasoning"}),
        ("kimi-k2-instruct", {"generation", "function_call"}),
        # A negative tag removes what the family rule claimed.
        ("glm-4.6", {"generation", "function_call", "reasoning"}),
        ("glm-4.6-nothink", {"generation", "function_call"}),
        # Chat families that gateways commonly expose
        ("MiniMax-M2", {"generation", "function_call"}),
        ("abab6.5s-chat", {"generation", "function_call"}),
        # Perplexity 的 sonar 线内建检索
        ("sonar-pro", {"generation", "web_search"}),
        # `-search` / `:online` 变体标签被规范化剥掉后,是内建检索的唯一证据
        ("gpt-4o-search-preview", {"generation", "vision", "function_call", "web_search"}),
        ("deepseek-chat:online", {"generation", "function_call", "web_search"}),
        ("gpt-4o", {"generation", "vision", "function_call"}),
        ("baichuan4-turbo", {"generation"}),
        ("LongCat-Flash-Chat", {"generation", "function_call"}),
        ("command-r-plus", {"generation", "function_call"}),
        # Purpose-exclusive families never fall through to a chat rule
        ("Qwen/Qwen3-Embedding-8B", {"embedding"}),
        ("gte-Qwen2-7B-instruct", {"embedding"}),
        ("nomic-embed-text-v1.5", {"embedding"}),
        ("BAAI/bge-reranker-v2-m3", {"rerank"}),
        ("voyage-rerank-2", {"rerank"}),
    ],
)
def test_capability_rules_match_the_canonical_stem(
    model_id: str,
    expected: set[str],
) -> None:
    match = lookup_registry(model_id)
    assert match is not None
    assert set(match.capabilities) == expected
    assert ("image" in match.input_modalities) is ("vision" in expected)


@pytest.mark.parametrize(
    "model_id",
    [
        # Generators and audio models have no capability slot here. Leaving them
        # unclassified is the point: an image generator must never look like a
        # chat model that can be handed a picture.
        "gpt-image-1",
        "dall-e-3",
        "black-forest-labs/FLUX.1-dev",
        "cogview-4",
        "Wan-AI/Wan2.2-I2V",
        "gemini-2.5-flash-image",
        "qwen-image-edit",
        "whisper-1",
        "tts-1-hd",
        "gpt-4o-audio-preview",
        "omni-moderation-latest",
        # Unknown ids stay unknown even when they embed a known family name.
        "my-gpt-5.6",
        "opaque-model",
        "ep-20250101-abcde",
    ],
)
def test_unsupported_and_unknown_ids_stay_unclassified(model_id: str) -> None:
    assert lookup_registry(model_id) is None


@pytest.mark.parametrize(
    ("model_id", "vendor"),
    [
        ("us.anthropic.claude-sonnet-4-5-v1:0", "anthropic"),
        ("aihubmix-gpt-5", "openai"),
        ("gemini-2.5-pro", "google"),
        ("Qwen/Qwen2.5-72B-Instruct", "alibaba"),
        ("zai-org/GLM-4.6", "zhipu"),
        ("doubao-seed-1-6-250615", "bytedance"),
        ("Pro/moonshotai/Kimi-K2-Instruct", "moonshot"),
        ("BAAI/bge-m3", "baai"),
        ("netease-youdao/bce-embedding-base_v1", "youdao"),
        # An id we cannot place must group as unknown rather than guess.
        ("opaque-model", None),
        ("", None),
    ],
)
def test_vendor_grouping_is_derived_from_the_id(
    model_id: str,
    vendor: str | None,
) -> None:
    assert lookup_vendor(model_id) == vendor


def test_vendor_is_independent_of_whether_capabilities_were_matched() -> None:
    # ``ep-…`` endpoint ids are opaque, so no capability can be claimed, but the
    # import dialog still needs to file them under the right vendor.
    assert lookup_registry("ep-20250101-abcde") is None
    assert lookup_vendor("ep-20250101-abcde") == "bytedance"
