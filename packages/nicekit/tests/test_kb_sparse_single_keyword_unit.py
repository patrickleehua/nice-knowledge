"""稀疏回退零召回 bad case 回归:"专名 + 一串疑问词"问句(无 DB)。

实证根因(zhparser 配置 ``public.nicekit_zhparser`` 上 ts_debug 实测):

- ``websearch_to_tsquery('modelMemory 是干嘛的')`` → ``'modelmemory' & '是' <-> '干'``
  ——疑问词被拉进合取与相邻短语,primary 通道必然落空;
- 回退通道先丢弃单字词元(是/干/嘛/的),retained 只剩 ``modelmemory`` 一个,
  再撞上 ``min_terms=2`` 门槛直接返回空元组,回退整体放弃;
- zhparser 给所有 ASCII token 的 alias 是 ``e``(非 ``n``),驼峰标识符永远
  当不成 required 锚点。

三者叠加 → sparse/must_include/structured/graph 四路全空 → 拒答门禁把已命中的
dense 结果一并丢弃 → 用户看到"向量检索没生效"。本文件把修复后的行为固化。
"""

from uuid import uuid4

import pytest

from nicekit.kb import search as search_module
from nicekit.kb.search import (
    DEFAULT_SPARSE_FALLBACK_NOISE,
    _controlled_sparse_groups,
    _fallback_quorum_rows,
    _fallback_sparse_chunk_statement,
    _is_ascii_identifier,
    _sparse_group_required,
    _SparseLexeme,
    set_sparse_fallback_noise,
)
from nicekit.models.kb import KbChunk

#: "modelMemory 是干嘛的" 在 public.nicekit_zhparser 上的真实 ts_debug 输出
#: (无 lexemes 的 什么/嘛/的 已被 _sparse_lexemes_statement 的 cardinality>0 过滤)。
_MODEL_MEMORY_LEXEMES = (
    _SparseLexeme(alias="e", value="modelmemory", token="modelMemory"),
    _SparseLexeme(alias="v", value="是", token="是"),
    _SparseLexeme(alias="v", value="干", token="干"),
)


@pytest.fixture(autouse=True)
def _sdk_default_noise():
    """用 SDK 默认停用词跑,并在用例间复位,避免跨文件注册污染。"""
    set_sparse_fallback_noise(None)
    yield
    set_sparse_fallback_noise(None)


def test_single_keyword_question_still_builds_groups() -> None:
    """单实词 + 疑问词的问句必须构造出非空词组(旧实现在此返回 ())。"""
    groups = _controlled_sparse_groups(list(_MODEL_MEMORY_LEXEMES))

    assert [(group.alternatives, group.phrase, group.required) for group in groups] == [
        ((("modelmemory",),), False, True)
    ]
    # 单组时 SQL 侧退化为一次单关键词检索,不会比 primary 更宽
    sql = str(_fallback_sparse_chunk_statement(groups, top_k=8, kb_ids=None).compile())
    assert sql.count("plainto_tsquery") == 2
    assert " || " not in sql and " && " not in sql


def test_camel_case_identifier_becomes_required_anchor() -> None:
    """zhparser 给英文 token 的 alias 是 e 而非 n,锚点判定必须认标识符形态。"""
    # 驼峰 / 下划线 / 字母数字混排 / 全大写缩写:都是 required 锚点
    assert _sparse_group_required("modelmemory", {"e"}, "modelMemory")
    assert _sparse_group_required("modelmemory", {"e"}, "ModelMemory")
    assert _sparse_group_required("model_memory", {"e"}, "model_memory")
    assert _sparse_group_required("model2", {"e"}, "model2")
    assert _sparse_group_required("api", {"e"}, "API")
    # 普通英文长词不是锚点:升成 required 会把整句 AND 死,反而制造新的零召回
    assert not _sparse_group_required("memory", {"e"}, "memory")
    assert not _sparse_group_required("modelmemory", {"e"}, "modelmemory")
    # 单字母/超短噪声不得成为 required(alias 同样是 e)
    assert not _sparse_group_required("v2", {"e"}, "v2")
    assert not _sparse_group_required("4o", {"e"}, "4o")
    # 纯数字词元不做锚点
    assert not _sparse_group_required("2024", {"e"}, "2024")
    # 中文侧锚点规则原样保留
    assert _sparse_group_required("卢浮宫", {"n"})
    assert not _sparse_group_required("巴黎", {"n"})


def test_ascii_identifier_shape_rules() -> None:
    assert _is_ascii_identifier("modelMemory")
    assert _is_ascii_identifier("model_memory")
    assert _is_ascii_identifier("model-memory")
    assert _is_ascii_identifier("gpt4")
    assert _is_ascii_identifier("API")
    assert not _is_ascii_identifier("memory")
    assert not _is_ascii_identifier("Memory")  # 句首大写不算专指信号
    assert not _is_ascii_identifier("2024")
    assert not _is_ascii_identifier("____")


def test_pure_noise_question_still_yields_no_groups() -> None:
    """纯疑问词查询不得因为放宽门槛而变成全库扫描:如实返回空。"""
    # "是干嘛的" —— 是/干 为单字词元,什么/嘛/的 无 lexemes
    assert _controlled_sparse_groups(
        [
            _SparseLexeme(alias="v", value="是", token="是"),
            _SparseLexeme(alias="v", value="干", token="干"),
        ]
    ) == ()
    # "介绍一下这个是什么" —— 全部命中 SDK 默认停用词
    assert _controlled_sparse_groups(
        [
            _SparseLexeme(alias="v", value="介绍", token="介绍"),
            _SparseLexeme(alias="m", value="一下", token="一下"),
            _SparseLexeme(alias="r", value="这个", token="这个"),
            _SparseLexeme(alias="v", value="是什么", token="是什么"),
        ]
    ) == ()
    # "what is the purpose of it" —— 英文功能词同样不构成检索意图
    assert _controlled_sparse_groups(
        [
            _SparseLexeme(alias="e", value="what", token="what"),
            _SparseLexeme(alias="e", value="is", token="is"),
            _SparseLexeme(alias="e", value="the", token="the"),
            _SparseLexeme(alias="e", value="purpose", token="purpose"),
            _SparseLexeme(alias="e", value="of", token="of"),
            _SparseLexeme(alias="e", value="it", token="it"),
        ]
    ) == ()


def test_english_function_words_do_not_inflate_quorum() -> None:
    """英文功能词若不过滤会把 quorum 分母撑大,反向造成零召回。"""
    lexemes = [
        _SparseLexeme(alias="e", value="what", token="what"),
        _SparseLexeme(alias="e", value="is", token="is"),
        _SparseLexeme(alias="e", value="modelmemory", token="modelMemory"),
        _SparseLexeme(alias="e", value="used", token="used"),
        _SparseLexeme(alias="e", value="for", token="for"),
    ]
    groups = _controlled_sparse_groups(lexemes)
    assert [(group.alternatives, group.required) for group in groups] == [
        ((("modelmemory",),), True)
    ]


def test_host_registration_still_overrides_sdk_defaults() -> None:
    """宿主注册整体覆盖默认集;传空可迭代对象即彻底不过滤。"""
    set_sparse_fallback_noise({"仅此一词"})
    assert set(search_module._SPARSE_FALLBACK_NOISE) == {"仅此一词"}
    groups = _controlled_sparse_groups(
        [
            _SparseLexeme(alias="v", value="介绍", token="介绍"),
            _SparseLexeme(alias="n", value="仅此一词", token="仅此一词"),
        ]
    )
    assert [group.alternatives for group in groups] == [(("介绍",),)]

    set_sparse_fallback_noise(())
    assert not search_module._SPARSE_FALLBACK_NOISE

    set_sparse_fallback_noise(None)
    assert search_module._SPARSE_FALLBACK_NOISE == DEFAULT_SPARSE_FALLBACK_NOISE


def test_single_anchor_group_quorum_requires_literal_hit() -> None:
    """单组回退仍要过 Python 侧原文复核:词面不在原文的 chunk 一律剔除。"""
    groups = _controlled_sparse_groups(list(_MODEL_MEMORY_LEXEMES))
    hit = KbChunk(id=uuid4(), content="Agent 的 modelMemory 用于跨轮记忆", heading_path="")
    miss = KbChunk(id=uuid4(), content="完全无关的一段话", heading_path="")

    kept = _fallback_quorum_rows([(hit, 0.9), (miss, 0.8)], groups, top_k=8)

    assert [chunk.id for chunk, _rank in kept] == [hit.id]
