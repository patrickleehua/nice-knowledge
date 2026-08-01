"""搜索正文压缩单元测试:全部离线,不碰数据库/LLM/reranker。

覆盖:轮询选取(来源多样性,本模块存在的意义)/ 分块边界优先级与重叠 /
定长截断的预算分配与纯函数性 / rerank 选片与三条降级路径 / 配置回落。
"""

from nicekit.capabilities.websearch.base import FetchedPage, FetchStatus
from nicekit.capabilities.websearch.compress import (
    CompressionConfig,
    compress_by_rerank,
    compress_cutoff,
    compress_pages,
    load_config,
    select_round_robin,
    split_chunks,
)

_PARA_CHARS = 60


def _para(marker: str, size: int = _PARA_CHARS) -> str:
    """构造定长段落:段落长度 < chunk_chars 时切分结果恰好一段一块,便于断言。"""
    return marker + "填" * (size - len(marker))


def _page(url: str, content: str, *, status: FetchStatus = "ok") -> FetchedPage:
    return FetchedPage(url=url, final_url=url, title=url, content=content, status=status)


# ---------------------------------------------------------------- 轮询选取


def test_select_round_robin_interleaves_groups():
    groups = [["a1", "a2", "a3", "a4"], ["b1"], ["c1", "c2"]]
    assert select_round_robin(groups, 10) == ["a1", "b1", "c1", "a2", "c2", "a3", "a4"]


def test_select_round_robin_respects_limit():
    groups = [["a1", "a2", "a3", "a4"], ["b1"], ["c1", "c2"]]
    # 全局 top-N 会先吃满 a 组;轮询保证 b/c 在前四个名额里就露出
    assert select_round_robin(groups, 4) == ["a1", "b1", "c1", "a2"]


def test_select_round_robin_skips_empty_groups():
    assert select_round_robin([[], ["b1", "b2"], []], 5) == ["b1", "b2"]


def test_select_round_robin_empty_inputs():
    assert select_round_robin([["a"]], 0) == []
    assert select_round_robin([["a"]], -3) == []
    assert select_round_robin([], 5) == []


# -------------------------------------------------------------------- 分块


def test_split_chunks_prefers_paragraph_boundary():
    text = "\n\n".join(["甲" * 300, "乙" * 300, "丙" * 300])
    chunks = split_chunks(text, chunk_chars=400, overlap=0, min_chunk_chars=10)
    assert [set(chunk) for chunk in chunks] == [{"甲"}, {"乙"}, {"丙"}]


def test_split_chunks_falls_back_to_sentence_boundary():
    text = "甲" * 200 + "。" + "乙" * 200 + "。"
    chunks = split_chunks(text, chunk_chars=250, overlap=0, min_chunk_chars=10)
    assert chunks[0] == "甲" * 200 + "。"
    assert chunks[1] == "乙" * 200 + "。"


def test_split_chunks_hard_cuts_without_boundary():
    text = "x" * 1000
    chunks = split_chunks(text, chunk_chars=300, overlap=0, min_chunk_chars=10)
    assert [len(chunk) for chunk in chunks] == [300, 300, 300, 100]


def test_split_chunks_applies_overlap():
    text = "".join(str(index % 10) for index in range(1000))
    chunks = split_chunks(text, chunk_chars=300, overlap=50, min_chunk_chars=10)
    # 重叠是为了防止边界处的事实被腰斩:后一块开头必须复述前一块结尾
    assert chunks[1][:50] == chunks[0][-50:]


def test_split_chunks_merges_short_tail():
    text = "x" * 1000
    chunks = split_chunks(text, chunk_chars=300, overlap=0, min_chunk_chars=150)
    # 尾块只有 100 字符(< 150),并入前一块而不是单独成碎片
    assert [len(chunk) for chunk in chunks] == [300, 300, 400]


def test_split_chunks_empty_text():
    assert split_chunks("", chunk_chars=100, overlap=10, min_chunk_chars=10) == []
    assert split_chunks("   \n\n  ", chunk_chars=100, overlap=10, min_chunk_chars=10) == []


# ---------------------------------------------------------------- 定长截断


def test_compress_cutoff_splits_budget_across_pages():
    pages = [
        _page("https://a.example", "甲" * 1000),
        _page("https://b.example", "乙" * 1000),
    ]
    config = CompressionConfig(total_chars=600, min_chunk_chars=120)
    result = compress_cutoff(pages, config)

    per_page = 600 // 2
    for page in result:
        assert page.truncated is True
        assert page.content.endswith("…(内容已压缩)")
        assert len(page.content.replace("\n\n…(内容已压缩)", "")) == per_page


def test_compress_cutoff_keeps_short_and_non_ok_pages():
    pages = [
        _page("https://a.example", "甲" * 1000),
        _page("https://b.example", "乙" * 50),
        _page("https://c.example", "丙" * 1000, status="blocked"),
    ]
    config = CompressionConfig(total_chars=600, min_chunk_chars=120)
    result = compress_cutoff(pages, config)

    assert result[0].truncated is True
    assert result[1].content == "乙" * 50 and result[1].truncated is False
    # blocked 页面不参与预算分配,也不被截断(状态本身就是给模型看的信息)
    assert result[2].content == "丙" * 1000 and result[2].truncated is False
    assert len(result[0].content.replace("\n\n…(内容已压缩)", "")) == 600 // 2


def test_compress_cutoff_does_not_mutate_inputs():
    pages = [_page("https://a.example", "甲" * 1000)]
    compress_cutoff(pages, CompressionConfig(total_chars=200))
    assert pages[0].content == "甲" * 1000
    assert pages[0].truncated is False


# ------------------------------------------------------------ rerank 选片


def _rerank_config(**overrides) -> CompressionConfig:
    base = {
        "method": "rerank",
        "total_chars": 2000,
        "chunk_chars": 80,
        "chunk_overlap": 0,
        "per_source_max": 3,
        "min_chunk_chars": 20,
    }
    base.update(overrides)
    return CompressionConfig(**base)


async def _keyword_score(query: str, chunks: list[str]) -> list[float]:
    """假打分:命中关键词的块给高分,其余给底分(替代真实 reranker)。"""
    return [1.0 if "签证" in chunk else 0.1 for chunk in chunks]


async def test_compress_by_rerank_keeps_high_score_chunks():
    content = "\n\n".join([_para("P0-闲聊"), _para("P1-签证"), _para("P2-闲聊"), _para("P3-签证")])
    pages = [_page("https://a.example", content)]
    result = await compress_by_rerank(
        pages, query="签证", config=_rerank_config(per_source_max=2), score_fn=_keyword_score
    )
    assert "P1-签证" in result[0].content
    assert "P3-签证" in result[0].content
    assert "P0-闲聊" not in result[0].content
    assert result[0].truncated is True


async def test_compress_by_rerank_preserves_source_diversity():
    # A 页 10 个高分块、B 页 2 个中分块:全局 top-N 会让 A 独占预算,
    # 轮询必须让 B 也进结果——多方求证是本模块存在的理由。
    page_a = _page("https://a.example", "\n\n".join(_para(f"A{i}-签证") for i in range(10)))
    page_b = _page("https://b.example", "\n\n".join(_para(f"B{i}-退税") for i in range(2)))

    async def score_fn(query: str, chunks: list[str]) -> list[float]:
        return [1.0 if "签证" in chunk else 0.5 for chunk in chunks]

    result = await compress_by_rerank(
        [page_a, page_b],
        query="签证 退税",
        config=_rerank_config(total_chars=200),
        score_fn=score_fn,
    )
    assert "A0-签证" in result[0].content
    assert "B0-退税" in result[1].content  # 低分来源仍拿到名额


async def test_compress_by_rerank_orders_chunks_by_source_position():
    content = "\n\n".join([_para("P0"), _para("P1"), _para("P2"), _para("P3")])
    pages = [_page("https://a.example", content)]

    async def score_fn(query: str, chunks: list[str]) -> list[float]:
        # P3 分最高、P1 次之:选中的是 {P3, P1},但拼回必须按原文顺序 P1 → P3
        return [3.0 if "P3" in c else 2.0 if "P1" in c else 0.1 for c in chunks]

    result = await compress_by_rerank(
        pages, query="q", config=_rerank_config(per_source_max=2), score_fn=score_fn
    )
    assert result[0].content.index("P1") < result[0].content.index("P3")


async def test_compress_by_rerank_keeps_unselected_page_as_stub():
    page_a = _page("https://a.example", "\n\n".join(_para(f"A{i}-签证") for i in range(3)))
    page_b = _page("https://b.example", "\n\n".join(_para(f"B{i}-无关") for i in range(3)))
    config = _rerank_config(total_chars=60)  # 预算只够一块

    result = await compress_by_rerank(
        [page_a, page_b], query="签证", config=config, score_fn=_keyword_score
    )
    # B 一块没中也要保留(截到 min_chunk_chars),否则引用编号会对不上
    assert len(result) == 2
    assert len(result[1].content) == config.min_chunk_chars
    assert result[1].truncated is True


async def test_compress_by_rerank_falls_back_when_score_fn_raises():
    pages = [_page("https://a.example", "\n\n".join(_para(f"P{i}") for i in range(6)))]
    config = _rerank_config(total_chars=200)

    async def boom(query: str, chunks: list[str]) -> list[float]:
        raise RuntimeError("reranker down")

    result = await compress_by_rerank(pages, query="q", config=config, score_fn=boom)
    assert [p.content for p in result] == [p.content for p in compress_cutoff(pages, config)]


async def test_compress_by_rerank_falls_back_on_length_mismatch():
    pages = [_page("https://a.example", "\n\n".join(_para(f"P{i}") for i in range(6)))]
    config = _rerank_config(total_chars=200)

    async def short(query: str, chunks: list[str]) -> list[float]:
        return [1.0]

    result = await compress_by_rerank(pages, query="q", config=config, score_fn=short)
    assert [p.content for p in result] == [p.content for p in compress_cutoff(pages, config)]


async def test_compress_by_rerank_falls_back_without_score_fn():
    pages = [_page("https://a.example", "\n\n".join(_para(f"P{i}") for i in range(6)))]
    config = _rerank_config(total_chars=200)

    result = await compress_by_rerank(pages, query="q", config=config, score_fn=None)
    assert [p.content for p in result] == [p.content for p in compress_cutoff(pages, config)]


# ---------------------------------------------------------------- 策略分派


async def test_compress_pages_method_none_keeps_content():
    pages = [_page("https://a.example", "甲" * 1000)]
    result = await compress_pages(
        pages, query="q", config=CompressionConfig(method="none", total_chars=100)
    )
    assert result[0].content == "甲" * 1000
    assert result[0].truncated is False


async def test_compress_pages_method_cutoff():
    pages = [_page("https://a.example", "甲" * 1000)]
    config = CompressionConfig(method="cutoff", total_chars=300)
    result = await compress_pages(pages, query="q", config=config)
    assert [p.content for p in result] == [p.content for p in compress_cutoff(pages, config)]


async def test_compress_pages_method_rerank():
    content = "\n\n".join([_para("P0-签证"), _para("P1-闲聊"), _para("P2-签证")])
    pages = [_page("https://a.example", content)]
    result = await compress_pages(
        pages, query="签证", config=_rerank_config(per_source_max=2), score_fn=_keyword_score
    )
    assert "---" in result[0].content  # 走了拼片路径,而非整段截断
    assert "P1-闲聊" not in result[0].content


# ---------------------------------------------------------------- 配置加载


def test_load_config_defaults():
    assert load_config(None) == CompressionConfig()
    assert load_config({}) == CompressionConfig()


def test_load_config_reads_valid_overrides():
    config = load_config({"method": "rerank", "total_chars": 500, "per_source_max": 5})
    assert config.method == "rerank"
    assert config.total_chars == 500
    assert config.per_source_max == 5


def test_load_config_falls_back_on_invalid_values():
    default = CompressionConfig()
    config = load_config(
        {
            "method": "magic",
            "total_chars": -5,
            "chunk_chars": "abc",
            "per_source_max": True,
            "min_chunk_chars": None,
            "chunk_overlap": [1],
        }
    )
    assert config == default


def test_load_config_clamps_overlap_below_chunk_size():
    # overlap >= chunk_chars 会让切分原地打转,配置层就要掐掉
    config = load_config({"chunk_chars": 50, "chunk_overlap": 400})
    assert config.chunk_chars == 50
    assert config.chunk_overlap < config.chunk_chars
