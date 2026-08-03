"""中文分词 bad-case 固化回归(无 DB)。

自然问句在 `websearch_to_tsquery(<zhparser 配置>, ...)` 下会因过严的相邻短语
而零召回。根治方案为受控词组构造(`_controlled_sparse_groups`):长名词锚点
AND + 非锚点组按 quorum 0.6 松绑 OR + Python 侧原文子串复核。

本文件把若干原始问句的 zhparser `ts_debug` 词元(TF 实测捕获,属**分词器行为
基线**而非业务语料)与期望词组结构固化为回归基线:锚点选取、名词复合词备选、
同义词扩展与 quorum 计算任一退化都会在此失败。

SDK 只内置一份通用停用词默认集(疑问词/功能词),语料相关的词表与同义词表仍是
注册项(MIGRATION-PLAN B16),因此本文件用 fixture 显式注册一份语料相关的词表
——这同时也是该扩展点(注册即整体覆盖)的回归。
"""

import math

import pytest

from nicekit.kb.search import (
    _SPARSE_FALLBACK_QUORUM,
    DEFAULT_SPARSE_FALLBACK_NOISE,
    _controlled_sparse_groups,
    _sparse_group_required,
    _SparseLexeme,
    set_sparse_fallback_noise,
    set_sparse_fallback_synonyms,
)

_NOISE = ("需要", "时候", "最好")
_SYNONYMS = {"第一次": ("首次", "首访"), "买票": ("购票",)}


@pytest.fixture(autouse=True)
def _registered_sparse_vocabulary():
    """注册语料相关的噪声/同义词表(SDK 默认为空,由宿主注册)。"""
    set_sparse_fallback_noise(_NOISE)
    set_sparse_fallback_synonyms(_SYNONYMS)
    yield
    set_sparse_fallback_noise(None)
    set_sparse_fallback_synonyms(None)


def test_sparse_vocabulary_defaults_are_sdk_stopwords_only() -> None:
    """默认只含 SDK 通用停用词:语料相关词(需要/买票)照常进词组、同义词不扩展。"""
    set_sparse_fallback_noise(None)
    set_sparse_fallback_synonyms(None)
    # SDK 默认覆盖疑问词/功能词,但绝不碰承载检索意图的实词
    assert {"什么", "干嘛", "为什么", "the", "what"} <= DEFAULT_SPARSE_FALLBACK_NOISE
    assert not {"需要", "买票", "时候", "最好"} & DEFAULT_SPARSE_FALLBACK_NOISE
    groups = _controlled_sparse_groups(
        [_SparseLexeme(alias="v", value="需要"), _SparseLexeme(alias="v", value="买票")]
    )
    assert [group.alternatives for group in groups] == [(("买票",),), (("需要",),)]

# (原始问句, zhparser ts_debug 词元, 期望词组 (alternatives, phrase, required),
#  期望 quorum 需命中数)
_BAD_CASES = (
    pytest.param(
        "巴黎戴高乐机场到市区坐什么，多久，票价多少？",
        [
            ("n", "巴黎戴高乐机场"),
            ("n", "巴黎"),
            ("n", "戴高"),
            ("v", "到"),
            ("v", "坐"),
            ("n", "票价"),
        ],
        [
            # 长名词锚点必须命中;全名精确 OR 解析器子词(巴黎 AND 戴高)兜底
            ((("巴黎戴高乐机场",), ("巴黎", "戴高")), False, True),
            ((("票价",),), False, False),
        ],
        1,  # ceil(1 * 0.6)
        id="cdg-airport-transfer",
    ),
    pytest.param(
        "卢浮宫周几闭馆，常规参观需要多久？",
        [
            ("n", "卢浮宫"),
            ("n", "卢浮"),
            ("v", "闭馆"),
            ("n", "常规"),
            ("v", "参观"),
            ("v", "需要"),  # 噪声词:不得进入词组
        ],
        [
            # 单一子词(卢浮宫→卢浮)收敛为同一锚点组的 OR 备选
            ((("卢浮宫",), ("卢浮",)), False, True),
            ((("参观",),), False, False),
            ((("常规",),), False, False),
            ((("闭馆",),), False, False),
        ],
        2,  # ceil(3 * 0.6)
        id="louvre-closing-day",
    ),
    pytest.param(
        "埃菲尔铁塔日落前什么时候登塔，哪里拍照最好？",
        [
            ("n", "埃菲尔铁塔"),
            ("n", "埃菲"),
            ("v", "日落"),
            ("n", "时候"),  # 噪声词
            ("v", "登"),  # 单字词元:丢弃
            ("n", "塔"),  # 单字词元:丢弃
            ("v", "拍照"),
            ("n", "最好"),  # 噪声词
        ],
        [
            ((("埃菲尔铁塔",), ("埃菲",)), False, True),
            ((("拍照",),), False, False),
            ((("日落",),), False, False),
        ],
        2,  # ceil(2 * 0.6)
        id="eiffel-sunset-photo",
    ),
    pytest.param(
        "第一次去展馆适合白天还是晚上,旺季每天大概多少人?",
        [
            ("n", "展馆"),
            ("v", "适合"),
            ("n", "第一次"),
            ("v", "去"),
            ("n", "白天"),
            ("n", "旺季"),
        ],
        [
            # 第一次 走受控同义词扩展;无锚点(展馆仅 2 字)
            ((("第一次",), ("首次",), ("首访",)), False, False),
            ((("展馆",),), False, False),
            ((("旺季",),), False, False),
            ((("白天",),), False, False),
            ((("适合",),), False, False),
        ],
        3,  # ceil(5 * 0.6)
        id="synonym-expansion-no-anchor",
    ),
    pytest.param(
        "伦敦西区看歌剧魅影需要提前买票吗，在哪个剧院？",
        [
            ("n", "伦敦"),
            ("n", "西区"),
            ("v", "看"),  # 单字词元:丢弃
            ("n", "歌剧"),
            ("n", "影"),  # 品牌被拆出的单字词元:丢弃
            ("v", "需要"),  # 噪声词
            ("v", "提前"),
            ("v", "买票"),
            ("n", "剧院"),
        ],
        [
            # 全部为 2 字词或同义词扩展词:无锚点,SQL 侧 OR + Python 侧 quorum 复核
            ((("买票",), ("购票",)), False, False),
            ((("伦敦",),), False, False),
            ((("剧院",),), False, False),
            ((("提前",),), False, False),
            ((("歌剧",),), False, False),
            ((("西区",),), False, False),
        ],
        4,  # ceil(6 * 0.6)
        id="west-end-phantom",
    ),
)


@pytest.mark.parametrize(("query", "lexemes", "expected", "quorum_need"), _BAD_CASES)
def test_bad_case_controlled_sparse_groups_stay_stable(
    query: str,
    lexemes: list[tuple[str, str]],
    expected: list[tuple[tuple[tuple[str, ...], ...], bool, bool]],
    quorum_need: int,
) -> None:
    groups = _controlled_sparse_groups(
        [_SparseLexeme(alias=alias, value=value) for alias, value in lexemes]
    )

    assert [(group.alternatives, group.phrase, group.required) for group in groups] == expected

    optional = [group for group in groups if not group.required]
    assert math.ceil(len(optional) * _SPARSE_FALLBACK_QUORUM) == quorum_need
    # 锚点组(若有)必须把完整专名放在首个备选,防止子词分支反客为主
    for group in groups:
        if group.required:
            anchor = group.alternatives[0]
            assert len(anchor) == 1 and len(anchor[0]) >= 3
            assert anchor[0] in query


def test_anchor_rule_matches_r4_bad_case_expectations() -> None:
    """锚点判定本身固化:长名词是锚,短名词/动词/同义词展开词不是。"""
    assert _sparse_group_required("巴黎戴高乐机场", {"n"})
    assert _sparse_group_required("埃菲尔铁塔", {"n"})
    assert _sparse_group_required("卢浮宫", {"n"})
    # 2 字名词不足以做锚(巴黎/伦敦/西区任缺其一不应整句零召回)
    assert not _sparse_group_required("巴黎", {"n"})
    assert not _sparse_group_required("伦敦", {"n"})
    # 动词词性不是锚,即便够长
    assert not _sparse_group_required("提前买票", {"v"})
    # 同义词展开词保持 optional,避免锚点 AND 吃掉受控 OR 松绑
    assert not _sparse_group_required("第一次", {"n"})
