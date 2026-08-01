"""Embedding 指纹与 reindex 探针单测(P3a 搬运适配版)。

TF 原文件末尾的 RRF 用例被测对象是 `nicekit.kb.search`(P3b 才搬,且是本次
迁移里唯一的"重写级"任务),这里先标 skip,随 search 一并恢复。
"""

from uuid import uuid4

import pytest

from nicekit.kb.embedding import (
    EmbeddingFingerprint,
    chunk_embedding_text,
    embedding_content_hash,
    normalize_embedding_config,
)
from nicekit.kb.embedding_reindex import (
    EmbeddingDimensionMismatchError,
    _probe_target,
)
from nicekit.models.kb import EMBEDDING_DIM


class _FakeEmbedder:
    def __init__(self, dim: int):
        self.dim = dim
        self.calls = 0

    async def embed(self, texts, *, org_id, task=None):
        self.calls += 1
        return [[0.0] * self.dim for _ in texts]


def test_embedding_config_normalizes_fingerprint_identifiers() -> None:
    config = normalize_embedding_config(
        {"provider": "  OPENAI\u3000Gateway ", "model": "  bge-m3  ", "dim": 1024}
    )

    assert config["provider"] == "openai gateway"
    assert config["model"] == "bge-m3"
    assert EmbeddingFingerprint.from_config(config).label == "openai gateway:bge-m3"


def test_content_hash_covers_the_exact_embedding_text() -> None:
    original = chunk_embedding_text("body", "section")

    assert original == "section\nbody"
    assert embedding_content_hash(original) != embedding_content_hash(
        chunk_embedding_text("changed", "section")
    )
    assert embedding_content_hash(original) != embedding_content_hash(
        chunk_embedding_text("body", "renamed section")
    )


@pytest.mark.asyncio
async def test_probe_rejects_configured_dimension_before_network_call() -> None:
    fake = _FakeEmbedder(EMBEDDING_DIM + 1)

    with pytest.raises(EmbeddingDimensionMismatchError) as caught:
        await _probe_target(
            {"provider": "test", "model": "different-dim", "dim": EMBEDDING_DIM + 1},
            org_id=uuid4(),
            embedding_service_factory=lambda _config: fake,
        )

    assert caught.value.as_dict()["code"] == "embedding_dimension_mismatch"
    assert "rebuild" in caught.value.as_dict()["offline_migration"]
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_probe_rejects_provider_dimension_mismatch() -> None:
    fake = _FakeEmbedder(EMBEDDING_DIM + 1)

    with pytest.raises(EmbeddingDimensionMismatchError) as caught:
        await _probe_target(
            {"provider": "test", "model": "misreported", "dim": EMBEDDING_DIM},
            org_id=uuid4(),
            embedding_service_factory=lambda _config: fake,
        )

    assert caught.value.actual == EMBEDDING_DIM + 1
    assert fake.calls == 1


@pytest.mark.skip(reason="被测对象 nicekit.kb.search 属 P3b(检索层重写),随其一并恢复")
def test_rrf_fuses_model_rankings_without_comparing_native_scores() -> None:
    from nicekit.kb.search import reciprocal_rank_fusion  # noqa: PLC0415

    old_first, new_first, shared = uuid4(), uuid4(), uuid4()

    scores = reciprocal_rank_fusion(
        [[old_first, shared], [new_first, shared]],
    )

    assert scores[shared] > scores[old_first]
    assert scores[shared] > scores[new_first]
    assert scores[old_first] == scores[new_first]
