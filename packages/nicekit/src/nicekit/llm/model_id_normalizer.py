"""Fold a gateway model ID down to one canonical stem.

Compatible gateways re-list the same upstream model under many spellings:
aggregator prefixes (``aihubmix-gpt-5``), namespaces (``Qwen/Qwen2.5-7B``),
Bedrock cross-vendor ARNs (``us.anthropic.claude-sonnet-4-5-v1:0``), release
date stamps (``gpt-4o-2024-08-06``), quantization tags (``qwen3-32b-awq``),
routing variants (``deepseek-v3:free``, ``glm-4-6-nothink``) and parameter
sizes (``qwen3-235b-a22b``).

Capability rules must not carry a branch per spelling, so every ID is folded
here first. Stripping is run to a fixpoint on purpose: one pass is
order-dependent, because a trailing date stamp hides the variant suffix behind
it (``qwen3-235b-thinking-2507`` only exposes ``-thinking`` once ``-2507`` is
gone), so a single pass would not be idempotent.
"""

import re
from dataclasses import dataclass

# Routing prefixes that aggregators bolt onto someone else's model id. Only
# vendor-unambiguous ones are listed — a short generic prefix (``s-``, ``cc-``)
# would eat the head of a legitimate id for no real gain.
_AGGREGATOR_PREFIXES: tuple[str, ...] = (
    "aihubmix-",
    "aihub-",
    "ahm-",
    "openrouter-",
    "dmxapi-",
    "dmxapi_",
    "siliconflow-",
    "sf-",
    "deepinfra-",
    "groq-",
    "chutes-",
    "nvidia-",
    "sophnet-",
    "aistudio_",
    "azure-",
    "alicloud-",
    "aliyun-",
    "huoshan-",
    "volcengine-",
    "baidu-",
    "web-",
    # ``zai-org-`` must precede ``zai-`` so the longer form wins.
    "zai-org-",
    "zai-",
    "openai-",
    "meta-",
    "cohere-",
    "perplexity-",
    "ai21-",
)

# Vendor shorthands an aggregator may use. Expanded rather than stripped, so
# the family rules still see the vendor token.
_PREFIX_EXPANSIONS: tuple[tuple[str, str], ...] = (("mm-", "minimax-"),)

# Routing/variant tags that select a behaviour of the same underlying model.
_COLON_VARIANT_SUFFIXES: tuple[str, ...] = (
    ":free",
    ":nitro",
    ":extended",
    ":beta",
    ":preview",
    ":thinking",
    ":latest",
    ":cloud",
    ":online",
    ":exacto",
)
# Order matters: the first match wins, and ``-no-think`` is only reachable
# after ``-think`` has been rejected by the compound guard below.
_HYPHEN_VARIANT_SUFFIXES: tuple[str, ...] = (
    "-free",
    "-search",
    "-online",
    "-think",
    "-thinking",
    "-nothink",
    "-nothinking",
    "-no-think",
    "-non-think",
    "-non-thinking",
    "-reasoning",
    "-classic",
    "-latest",
    "-preview",
    "-minimal",
    "-low",
    "-high",
    "-ssvip",
    "-aliyun",
    "-huoshan",
    "-reverse",
)
_PAREN_VARIANT_SUFFIXES: tuple[str, ...] = (
    "(free)",
    "(beta)",
    "(preview)",
    "(thinking)",
)
# A tail that turns the suffix behind it into a different word. ``-think``
# must not be peeled off ``deepseek-v3-no-think`` — the whole ``-no-think``
# is the tag. The guard only fires when the tail is its own token, so
# ``inferno-search`` still strips.
_PROTECTED_COMPOUND_TAILS: tuple[str, ...] = ("no", "non", "pre", "anti", "post")

# Precision markers denote the same logical model at a different quantization.
_QUANTIZATION_SUFFIXES: tuple[str, ...] = (
    "-fp8",
    "-fp16",
    "-bf16",
    "-awq",
    "-gptq",
    "-gguf",
    "-int4",
    "-int8",
    "-w4a16",
    "-w8a8",
)

# ``-70b``, ``-1.5b``, ``-8x7b``. Anchored to a token boundary so a version
# (``-v3``) or an expert count (``-a22b`` — leading letter) is left alone.
_PARAMETER_SIZE = re.compile(r"-(\d+(?:[.x]\d+)*b)(?=-|$)", re.IGNORECASE)

# A trailing release stamp: ``-2024-08-06``, ``-20250929``, ``-250905``,
# ``-2507`` (YYMM) or ``-0929`` (MMDD). Every form validates the month and
# day, so a size or context length (``-235b``, ``-4096``) is never mistaken
# for a date.
_DATE_SNAPSHOT = re.compile(
    r"-20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$"
    r"|-20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$"
    r"|-2\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$"
    r"|-2\d(?:0[1-9]|1[0-2])$"
    r"|-(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$"
)

# Bedrock/Vertex re-list other creators' models as cross-vendor ARNs: a
# ``[region.]vendor.`` dotted prefix plus a ``-v1:0`` / ``:0`` revision.
_BEDROCK_VENDORS = (
    "anthropic|amazon|meta|google|mistral|mistralai|cohere|openai|ai21"
    "|microsoft|nvidia|deepseek|minimax|moonshot|moonshotai|qwen|writer|xai|zai"
)
_BEDROCK_DOTTED_PREFIX = re.compile(rf"^(?:[a-z]+\.)*(?:{_BEDROCK_VENDORS})\.")
_BEDROCK_DASH_PREFIX = re.compile(
    r"^(?:anthropic|amazon|meta|google|mistralai|cohere|openai|ai21)-{1,2}"
)
_BEDROCK_REVISION = re.compile(r"(?:[-_]v?\d+)?:\d+$", re.IGNORECASE)

# A registry-tag colon payload that carries a size or quantization
# (``gpt-oss:20b``, ``qwen2.5:7b``, ``llama3:8b-instruct-q4_K_M``). Realigned
# to the hyphen spelling instead of dropped, so the size stays a normal token.
_COLON_SIZE_TAG = re.compile(
    r"^(?:\d+(?:[.x]\d+)*b(?:$|[-.])|q\d|iq\d|fp16|bf16|f16)",
    re.IGNORECASE,
)

# A trailing ``@001``-style deployment revision (``gemini-2.0@001``).
_AT_REVISION = re.compile(r"@.*$")


def _strip_namespace(model_id: str) -> str:
    """Keep the last path segment: ``accounts/x/models/y`` → ``y``."""
    return model_id.rsplit("/", 1)[-1]


def _strip_aggregator_prefix(model_id: str) -> str:
    for prefix in _AGGREGATOR_PREFIXES:
        if model_id.startswith(prefix) and len(model_id) > len(prefix):
            return model_id[len(prefix) :]
    return model_id


def _expand_prefix(model_id: str) -> str:
    for shorthand, canonical in _PREFIX_EXPANSIONS:
        if model_id.startswith(shorthand):
            return canonical + model_id[len(shorthand) :]
    return model_id


def _strip_bedrock_wrapping(model_id: str) -> str:
    stripped = _BEDROCK_DOTTED_PREFIX.sub("", model_id)
    stripped = _BEDROCK_DASH_PREFIX.sub("", stripped)
    return _BEDROCK_REVISION.sub("", stripped)


def _colon_size_tag_to_hyphen(model_id: str) -> str:
    index = model_id.rfind(":")
    if index > 0 and _COLON_SIZE_TAG.match(model_id[index + 1 :]):
        return f"{model_id[:index]}-{model_id[index + 1 :]}"
    return model_id


def _strip_variant_suffix(model_id: str) -> tuple[str, str | None]:
    """Peel one variant tag, returning the remainder and the tag it removed."""
    index = model_id.rfind(":")
    if index > 0 and model_id[index:] in _COLON_VARIANT_SUFFIXES:
        return model_id[:index], model_id[index + 1 :]

    for suffix in _HYPHEN_VARIANT_SUFFIXES:
        if not model_id.endswith(suffix) or len(model_id) == len(suffix):
            continue
        remaining = model_id[: -len(suffix)]
        if any(
            remaining == tail or remaining.endswith(f"-{tail}")
            for tail in _PROTECTED_COMPOUND_TAILS
        ):
            continue
        return remaining, suffix[1:]

    for suffix in _PAREN_VARIANT_SUFFIXES:
        if model_id.endswith(suffix) and len(model_id) > len(suffix):
            return model_id[: -len(suffix)].rstrip(" -"), suffix.strip("()")

    return model_id, None


def _strip_quantization(model_id: str) -> str:
    for suffix in _QUANTIZATION_SUFFIXES:
        if model_id.endswith(suffix) and len(model_id) > len(suffix):
            return model_id[: -len(suffix)]
    return model_id


def _normalize_version_separators(model_id: str) -> str:
    # ``2.5`` / ``2,5`` / ``2p5`` / ``2_5`` all spell the same version.
    return re.sub(r"(\d)[,._p](?=\d)", r"\1-", model_id)


@dataclass(frozen=True, slots=True)
class NormalizedModelId:
    """A canonical stem plus the variant tags that were peeled off it.

    The tags matter: folding ``kimi-k2-thinking`` and ``glm-4.5-air-nothink``
    onto their base stems is what lets one family rule cover every spelling,
    but the tag itself is the only evidence of the behaviour the gateway
    selected. Capability rules read them back as a refinement.
    """

    stem: str
    variants: frozenset[str]


def normalize_model_id_detailed(
    model_id: str,
    *,
    keep_parameter_size: bool = False,
) -> NormalizedModelId:
    normalized = _strip_namespace(model_id.strip()).casefold()
    if not normalized:
        return NormalizedModelId(stem="", variants=frozenset())
    normalized = _strip_aggregator_prefix(normalized)
    normalized = _strip_bedrock_wrapping(normalized)
    normalized = _expand_prefix(normalized)
    normalized = _AT_REVISION.sub("", normalized)
    # A registry-tag size/quant payload is realigned to the hyphen spelling in
    # both modes, so ``qwen2.5:7b`` stays a normal token instead of a colon the
    # variant list does not recognize.
    normalized = _colon_size_tag_to_hyphen(normalized)

    # Variant / quantization / date / size stripping all feed each other, so
    # the whole stage iterates until stable.
    variants: set[str] = set()
    while True:
        stripped, variant = _strip_variant_suffix(normalized)
        if variant is not None:
            variants.add(variant)
        stripped = _strip_quantization(stripped)
        stripped = _DATE_SNAPSHOT.sub("", stripped)
        if not keep_parameter_size:
            stripped = _PARAMETER_SIZE.sub("", stripped)
        if stripped == normalized:
            break
        normalized = stripped

    normalized = _normalize_version_separators(normalized)
    # ``_`` is an interchangeable separator in Hugging Face style ids.
    return NormalizedModelId(
        stem=normalized.replace("_", "-").strip("-"),
        variants=frozenset(variants),
    )


def normalize_model_id(model_id: str, *, keep_parameter_size: bool = False) -> str:
    """Return the canonical stem the capability rules are written against.

    ``keep_parameter_size`` produces the size-preserving key, so
    ``gpt-oss-20b`` and ``gpt-oss-120b`` stay distinct. A future exact-match
    catalog wants that key; the family rules do not.
    """
    return normalize_model_id_detailed(
        model_id,
        keep_parameter_size=keep_parameter_size,
    ).stem


__all__ = [
    "NormalizedModelId",
    "normalize_model_id",
    "normalize_model_id_detailed",
]
