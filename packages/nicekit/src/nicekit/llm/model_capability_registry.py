"""Versioned, operator-reviewed model capability registry.

The standard OpenAI ``/v1/models`` response does not describe model purpose.
Provider-declared metadata still wins; this registry is only used when that
metadata is absent.

Every rule is matched against the canonical stem produced by
:func:`nicekit.llm.model_id_normalizer.normalize_model_id`, so a family
needs one rule rather than one per gateway spelling. Rules stay anchored to
known families: an arbitrary ID that merely contains ``gpt`` is not silently
classified, and a family whose purpose we cannot support (image/video/audio
generation, moderation) is deliberately left unclassified rather than being
treated as a chat model.

``function_call`` is biased towards under-claiming: hiding a usable model from
a picker is recoverable, whereas claiming tool support the upstream lacks
surfaces as a request-time 400.
"""

import re
from dataclasses import dataclass
from re import Pattern

from nicekit.domain.model_catalog import (
    ModelCapability,
    ModelInputModality,
    canonical_capabilities,
    canonical_input_modalities,
)
from nicekit.llm.model_id_normalizer import (
    normalize_model_id,
    normalize_model_id_detailed,
)

REGISTRY_REVISION = "2026-07-25.5"
REGISTRY_SOURCE = "operator-reviewed"


@dataclass(frozen=True, slots=True)
class RegistryMatch:
    capabilities: tuple[ModelCapability, ...]
    input_modalities: tuple[ModelInputModality, ...]
    source: str


@dataclass(frozen=True, slots=True)
class _RegistryRule:
    pattern: Pattern[str]
    match: RegistryMatch


def _text(*capabilities: ModelCapability) -> RegistryMatch:
    return RegistryMatch(
        capabilities=capabilities,
        input_modalities=("text",),
        source=REGISTRY_SOURCE,
    )


def _visual(*capabilities: ModelCapability) -> RegistryMatch:
    return RegistryMatch(
        capabilities=capabilities,
        input_modalities=("text", "image"),
        source=REGISTRY_SOURCE,
    )


_CHAT = _text("generation")
_CHAT_TOOL = _text("generation", "function_call")
_CHAT_REASON = _text("generation", "function_call", "reasoning")
_CHAT_REASON_ONLY = _text("generation", "reasoning")
_VISION = _visual("generation", "vision", "function_call")
_VISION_ONLY = _visual("generation", "vision")
_VISION_REASON = _visual("generation", "vision", "function_call", "reasoning")
_VISION_REASON_ONLY = _visual("generation", "vision", "reasoning")
_CHAT_SEARCH = _text("generation", "web_search")
_EMBEDDING = _text("embedding")
_RERANK = _text("rerank")


# Family tail: the rest of an SKU may start with a hyphen (``qwen-max``) or run
# straight into a version digit (``qwen2-5-instruct``, ``abab6-5s-chat``), but
# never into another letter — that would make ``seed`` swallow ``seedream``.
_TAIL = r"(?:[-\d].*)?$"


def _rule(family: str, match: RegistryMatch) -> _RegistryRule:
    return _RegistryRule(re.compile(f"^(?:{family}){_TAIL}", re.IGNORECASE), match)


def _exact(pattern: str, match: RegistryMatch) -> _RegistryRule:
    """A rule whose pattern is spelled out in full (no implicit family tail)."""
    return _RegistryRule(re.compile(pattern, re.IGNORECASE), match)


# ── Purpose-exclusive families ───────────────────────────────────────────────
# Checked before every chat rule so ``qwen3-embedding`` and ``bge-reranker``
# can never fall through into a generation rule.

_RERANK_TOKEN = re.compile(r"(?:^|-)rerank(?:er|ing)?(?:-|$)", re.IGNORECASE)
_EMBEDDING_TOKEN = re.compile(r"(?:^|-)embed(?:ding|dings)?(?:-|$)", re.IGNORECASE)
# Embedding families whose name never spells "embed".
_EMBEDDING_FAMILY = re.compile(
    r"^(?:"
    r"(?:bge|gte|e5|m3e|mxbai|conan|piccolo|stella|acge|text2vec|xiaobu)(?:-.*)?"
    r"|multilingual-e5(?:-.*)?"
    r"|jina-clip(?:-.*)?"
    r"|voyage(?:-.*)?"
    r"|snowflake-arctic(?:-.*)?"
    r")$",
    re.IGNORECASE,
)

# Families whose purpose this platform has no capability slot for. Leaving them
# unclassified is the point: an image generator must never look like a chat
# model that can accept a picture.
_UNSUPPORTED_PURPOSE = re.compile(
    r"(?:^|-)(?:"
    r"tts|whisper|speech|asr|transcribe|transcription|translate|realtime"
    r"|audio|voice|sovits|music|moderation|image|video"
    r")(?:-|$)"
    r"|^(?:"
    r"dall-e|gpt-image|sora|flux|stable-diffusion|sd\d|sdxl|kolors"
    r"|cogview|cogvideo|wan|wanx|seedream|seedance|kling|veo|imagen|vidu|luma"
    r"|midjourney|suno|recraft|ideogram|irag|nano-banana|z-image|hailuo|bagel"
    r")(?:[-.]|\d|$)",
    re.IGNORECASE,
)

# ── Generation families ──────────────────────────────────────────────────────
# Ordered: the most specific SKU first, then the family fallback. Matched with
# ``fullmatch`` so a vendor token has to start the id.

_FAMILY_RULES: tuple[_RegistryRule, ...] = (
    # OpenAI
    _rule(r"gpt-oss", _CHAT_REASON),
    _rule(r"o1-mini|o1-preview", _CHAT_REASON_ONLY),
    _rule(r"o1", _VISION_REASON),
    _rule(r"o3-mini", _CHAT_REASON),
    _rule(r"o[34]", _VISION_REASON),
    # ``gpt-5-chat`` is the non-reasoning SKU of the same line.
    _rule(r"gpt-5(?:-\d+)?-chat", _VISION),
    _rule(r"gpt-5(?:-\d+)?", _VISION_REASON),
    _rule(r"chatgpt-4o", _VISION_ONLY),
    _rule(r"gpt-4o|gpt-4-1|gpt-4-vision", _VISION),
    _rule(r"gpt-4|gpt-3-5", _CHAT_TOOL),
    # Anthropic — every Claude line from 3 onwards reads images and calls tools.
    _rule(r"claude-instant|claude-2", _CHAT),
    _rule(r"claude-3-7|claude-4|claude-(?:sonnet|opus|haiku)-[4-9]", _VISION_REASON),
    _rule(r"claude", _VISION),
    # Google
    _rule(r"gemini-2-5|gemini-[3-9]", _VISION_REASON),
    _rule(r"gemini", _VISION),
    _rule(r"gemma\d*", _CHAT),
    # Alibaba Qwen
    _rule(r"qvq", _VISION_REASON_ONLY),
    _rule(r"qwq", _CHAT_REASON),
    _rule(r"qwen[3-9]", _CHAT_REASON),
    _rule(r"qwen\d*|tongyi|marco", _CHAT_TOOL),
    # DeepSeek
    _exact(rf"^deepseek-vl\d*{_TAIL}", _VISION_ONLY),
    _rule(r"deepseek-r1|deepseek-reasoner", _CHAT_REASON_ONLY),
    _rule(r"deepseek", _CHAT_TOOL),
    # Zhipu GLM — the ``v`` SKUs (``glm-4v``, ``glm-4-5v``) are the multimodal ones.
    _exact(rf"^glm-\d+(?:-\d+)?v{_TAIL}", _VISION),
    _rule(r"glm-4-[5-9]|glm-[5-9]|glm-z1", _CHAT_REASON),
    _rule(r"glm\d*|chatglm\d*|charglm\d*|codegeex\d*", _CHAT_TOOL),
    # ByteDance Doubao / Seed
    _rule(r"doubao-seed-\d+", _VISION_REASON),
    _rule(r"doubao|seed", _CHAT_TOOL),
    # Tencent Hunyuan
    _rule(r"hunyuan-t1", _CHAT_REASON),
    _rule(r"hunyuan|hy-\w+|hy\d+", _CHAT_TOOL),
    # Moonshot Kimi
    _rule(r"kimi|moonshot", _CHAT_TOOL),
    # MiniMax
    _rule(r"minimax|abab\d*\w*", _CHAT_TOOL),
    # StepFun — the ``1v`` / ``1o`` SKUs are the multimodal ones.
    _exact(rf"^step-\d+[vo]{_TAIL}", _VISION_ONLY),
    _rule(r"step", _CHAT),
    # Perplexity — the sonar line is retrieval-augmented by construction.
    _rule(r"sonar", _CHAT_SEARCH),
    # Baidu Ernie
    _rule(r"ernie\d*", _CHAT_TOOL),
    # xAI Grok
    _rule(r"grok-[4-9]", _VISION_REASON),
    _rule(r"grok-3", _CHAT_REASON),
    _rule(r"grok", _CHAT_TOOL),
    # Mistral
    _rule(r"pixtral", _VISION),
    _rule(r"magistral", _CHAT_REASON),
    _rule(
        r"(?:open-|labs-)?(?:mistral|mixtral|codestral|ministral|devstral)\w*",
        _CHAT_TOOL,
    ),
    # Meta Llama
    _rule(r"llama\d*|code-llama|nemotron", _CHAT_TOOL),
    # Cohere
    _rule(r"command", _CHAT_TOOL),
    _rule(r"c4ai|aya", _CHAT),
    # Remaining open-weight and regional families
    _rule(r"internvl\d*", _VISION_ONLY),
    _rule(r"internlm\d*|interns\d*", _CHAT),
    _rule(r"mimo", _CHAT),
    _rule(r"yi\d*", _CHAT),
    _exact(rf"^phi-?\d+{_TAIL}", _CHAT),
    _rule(r"ling|ring|bailing", _CHAT_TOOL),
    _rule(r"longcat", _CHAT_TOOL),
    _rule(r"baichuan\d*", _CHAT),
    _rule(r"spark\d*|generalv\d*", _CHAT),
    _rule(r"sense\w*", _CHAT),
    _rule(r"skywork", _CHAT),
    _rule(r"solar", _CHAT),
)

# ── Cross-family refinements ─────────────────────────────────────────────────

# A vendor-agnostic marker that the SKU takes images. Applied on top of the
# family rule so ``hunyuan-vision``, ``doubao-1-5-vision-pro`` and
# ``ernie-4-5-turbo-vl`` do not each need their own rule. ``image`` / ``video``
# are deliberately absent — those mark generators, which are excluded earlier.
_VISION_TOKEN = re.compile(r"(?:^|-)(?:vision|vl|omni)(?:-|$)", re.IGNORECASE)

# Variant tags the normalizer peeled off. They are the only evidence of which
# behaviour of the base model the gateway selected.
_THINKING_VARIANTS = frozenset({"think", "thinking", "reasoning"})
_NO_THINKING_VARIANTS = frozenset(
    {"nothink", "nothinking", "no-think", "non-think", "non-thinking"}
)
# A ``-search`` / ``-online`` SKU is the same base model with built-in retrieval
# (``gpt-4o-search-preview``, ``deepseek-chat:online``). The normalizer folds the
# tag away, so it is the only remaining evidence.
_WEB_SEARCH_VARIANTS = frozenset({"search", "online"})


def _refine(
    match: RegistryMatch,
    stem: str,
    variants: frozenset[str],
) -> RegistryMatch:
    capabilities = set(match.capabilities)
    modalities = set(match.input_modalities)
    if _VISION_TOKEN.search(stem):
        capabilities.add("vision")
        modalities.add("image")
    if variants & _THINKING_VARIANTS:
        capabilities.add("reasoning")
    if variants & _NO_THINKING_VARIANTS:
        capabilities.discard("reasoning")
    if variants & _WEB_SEARCH_VARIANTS:
        capabilities.add("web_search")
    if capabilities == set(match.capabilities) and modalities == set(match.input_modalities):
        return match
    return RegistryMatch(
        capabilities=tuple(canonical_capabilities(list(capabilities))),
        input_modalities=tuple(canonical_input_modalities(list(modalities))),
        source=match.source,
    )


# ── Vendor identity ──────────────────────────────────────────────────────────
# Used only to group the import dialog. Independent of capability matching, so
# an unclassified model still lands under the right vendor.

_VENDOR_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = tuple(
    (vendor, re.compile(pattern, re.IGNORECASE))
    for vendor, pattern in (
        ("anthropic", r"^claude"),
        (
            "openai",
            r"^(?:gpt|chatgpt|o[134](?:-|$)|codex|dall-e|davinci|babbage"
            r"|whisper|sora|tts-|text-embedding-(?:3|ada)|(?:omni-)?text-moderation"
            r"|omni-moderation)",
        ),
        ("google", r"^(?:gemini|gemma|palm|imagen|veo|lyria|learnlm|text-bison)"),
        ("alibaba", r"^(?:qwen|qwq|qvq|tongyi|marco|gte-|wanx?\d*(?:-|$))"),
        ("deepseek", r"^deepseek"),
        ("zhipu", r"^(?:glm|chatglm|charglm|cogview|cogvideo|codegeex|embedding-\d)"),
        ("bytedance", r"^(?:doubao|seed|seedream|seedance|skylark|ep-|bagel)"),
        ("moonshot", r"^(?:kimi|moonshot)"),
        ("tencent", r"^(?:hunyuan|hy-|hy\d)"),
        ("minimax", r"^(?:minimax|abab|hailuo)"),
        ("stepfun", r"^step-"),
        ("baidu", r"^(?:ernie|irag)"),
        ("xai", r"^grok"),
        (
            "mistral",
            r"^(?:open-|labs-)?(?:mistral|mixtral|pixtral|codestral|ministral"
            r"|devstral|magistral|voxtral)",
        ),
        ("meta", r"^(?:llama|code-llama)"),
        ("microsoft", r"^phi"),
        ("nvidia", r"^nemotron"),
        (
            "cohere",
            r"^(?:command|c4ai|aya|embed-(?:english|multilingual)"
            r"|rerank-(?:english|multilingual))",
        ),
        ("perplexity", r"^sonar"),
        ("baai", r"^bge"),
        ("jina", r"^jina"),
        ("voyage", r"^voyage"),
        ("youdao", r"^bce-"),
        ("nomic", r"^nomic"),
        ("01ai", r"^yi(?:-|\d|$)"),
        ("baichuan", r"^baichuan"),
        ("iflytek", r"^(?:spark|generalv)"),
        ("sensetime", r"^sense"),
        ("shanghai-ai-lab", r"^intern"),
        ("upstage", r"^solar"),
        ("xiaomi", r"^mimo"),
        ("ant", r"^(?:ling|ring|bailing)-"),
        ("meituan", r"^longcat"),
        ("skywork", r"^skywork"),
        ("black-forest-labs", r"^flux"),
        ("stability", r"^(?:stable-diffusion|sd\d|sdxl)"),
    )
)


def lookup_registry(model_id: str) -> RegistryMatch | None:
    parsed = normalize_model_id_detailed(model_id)
    stem = parsed.stem
    if not stem:
        return None
    if _RERANK_TOKEN.search(stem):
        return _RERANK
    if _EMBEDDING_TOKEN.search(stem) or _EMBEDDING_FAMILY.fullmatch(stem):
        return _EMBEDDING
    if _UNSUPPORTED_PURPOSE.search(stem):
        return None
    for rule in _FAMILY_RULES:
        if rule.pattern.fullmatch(stem):
            return _refine(rule.match, stem, parsed.variants)
    return None


def lookup_vendor(model_id: str) -> str | None:
    """Vendor slug for grouping, or ``None`` when the id is unrecognizable."""
    normalized = normalize_model_id(model_id)
    if not normalized:
        return None
    for vendor, pattern in _VENDOR_PATTERNS:
        if pattern.search(normalized):
            return vendor
    return None


__all__ = [
    "REGISTRY_REVISION",
    "REGISTRY_SOURCE",
    "RegistryMatch",
    "lookup_registry",
    "lookup_vendor",
]
