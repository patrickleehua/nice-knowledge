"""搜索正文压缩:把多页长正文压进给定字符预算,同时保住来源多样性。

职责:web_fetch 抓回的正文动辄上万字,5 个页面直接进上下文就爆了。
本模块做「文本进、文本出」的纯计算——切块、打分、按预算选取、拼回,
不碰数据库、不读 get_settings()、不 import KB 模块。rerank 能力一律由
调用方以 score_fn 注入,于是单测无需任何外部服务,主线集成时把 KB 的
reranker 适配成 ScoreFn 传进来即可,换打分实现不必动本模块。

为什么用轮询(select_round_robin)而不是全局 top-N:按分数全局取前 N 个
片段时,一个长页面往往能包揽全部名额,模型最终只看到单一来源。政策、
法规、资质这类问题必须多方求证,来源多样性比单点相关性更重要,
所以名额按来源轮流发——每个来源都保证有露出,再谈组内谁分高。

降级约定(与 provider 契约一致):压缩属于锦上添花的优化,任何异常路径
(score_fn 缺失 / 抛异常 / 返回长度对不上)都记 warning 后回落定长截断,
绝不向上抛——压缩失败不该让整个 agent 回合失败。
"""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from nicekit.capabilities.websearch.base import FetchedPage

logger = logging.getLogger(__name__)

# 句末边界:CJK 句号叹号问号独立成界;ASCII .!? 须后接空白或结尾,
# 避免把 "17.5 欧" 这类数字从中间切开(与 KB chunker 同口径)。
_SENTENCE_RE = re.compile(r"[。！？]|[.!?](?=\s|$)")
_PARAGRAPH_SEP = "\n\n"

# 截断标记:让模型知道后面还有内容被裁掉了,避免它把残句当完整事实。
_TRUNCATED_SUFFIX = "\n\n…(内容已压缩)"
# 片段拼接分隔符:显式分隔提示模型这些片段在原文里并不相邻。
_CHUNK_JOINER = "\n\n---\n\n"

CompressionMethod = Literal["none", "cutoff", "rerank"]
_METHODS: frozenset[str] = frozenset({"none", "cutoff", "rerank"})

# score_fn(query, chunks) -> 每块一个分数,顺序与入参对齐(与 KB Reranker 同口径)
ScoreFn = Callable[[str, list[str]], Awaitable[list[float]]]


@dataclass(frozen=True)
class CompressionConfig:
    method: CompressionMethod = "cutoff"
    total_chars: int = 12000  # 全部页面正文合计的字符预算
    chunk_chars: int = 800  # rerank 模式下的分块大小
    chunk_overlap: int = 80
    per_source_max: int = 3  # rerank 模式下单个来源最多保留几个片段
    min_chunk_chars: int = 120  # 小于此长度的尾块并入前一块,避免碎片


_DEFAULT_CONFIG = CompressionConfig()


def _coerce_int(value: object, *, default: int, minimum: int) -> int:
    """管理端 dict 可能塞进任何东西,读不出合法整数就回落默认,绝不抛。"""
    # bool 是 int 的子类,但 True/False 当块大小显然是配置写错了,直接回落
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(value)  # 非数值类型(list/dict/乱码字符串)由下面的异常兜底
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def load_config(overrides: dict | None) -> CompressionConfig:
    """从管理端 overrides 读配置:缺失或非法一律回落默认值,绝不抛异常。"""
    data = overrides if isinstance(overrides, dict) else {}
    raw_method = data.get("method")
    method: CompressionMethod = raw_method if raw_method in _METHODS else _DEFAULT_CONFIG.method
    config = CompressionConfig(
        method=method,
        total_chars=_coerce_int(
            data.get("total_chars"), default=_DEFAULT_CONFIG.total_chars, minimum=1
        ),
        chunk_chars=_coerce_int(
            data.get("chunk_chars"), default=_DEFAULT_CONFIG.chunk_chars, minimum=1
        ),
        chunk_overlap=_coerce_int(
            data.get("chunk_overlap"), default=_DEFAULT_CONFIG.chunk_overlap, minimum=0
        ),
        per_source_max=_coerce_int(
            data.get("per_source_max"), default=_DEFAULT_CONFIG.per_source_max, minimum=1
        ),
        min_chunk_chars=_coerce_int(
            data.get("min_chunk_chars"), default=_DEFAULT_CONFIG.min_chunk_chars, minimum=1
        ),
    )
    # overlap >= chunk_chars 会让切分原地打转,配置层就掐掉,不留给运行时
    if config.chunk_overlap >= config.chunk_chars:
        safe_overlap = min(_DEFAULT_CONFIG.chunk_overlap, config.chunk_chars - 1)
        config = replace(config, chunk_overlap=max(0, safe_overlap))
    return config


def select_round_robin[T](groups: Sequence[Sequence[T]], limit: int) -> list[T]:
    """按组轮询取元素:每轮各组取 1 个,直到取满 limit 或所有组耗尽。

    组内顺序由调用方保证(已按相关度排好),本函数只负责发名额的次序:
    先保证每个来源都露出一个,再发第二轮。空组自动跳过。
    """
    if limit <= 0 or not groups:
        return []
    buckets = [list(group) for group in groups if group]
    picked: list[T] = []
    round_index = 0
    while len(picked) < limit:
        advanced = False
        for bucket in buckets:
            if round_index >= len(bucket):
                continue
            picked.append(bucket[round_index])
            advanced = True
            if len(picked) >= limit:
                return picked
        if not advanced:  # 所有组都耗尽,提前收工
            break
        round_index += 1
    return picked


def _find_cut(text: str, start: int, end: int, floor: int) -> int:
    """在 [floor, end) 窗口里找最靠后的自然边界;找不到返回 end(硬切)。"""
    paragraph = text.rfind(_PARAGRAPH_SEP, floor, end)
    if paragraph != -1:
        return paragraph + len(_PARAGRAPH_SEP)
    last_sentence = -1
    for match in _SENTENCE_RE.finditer(text, floor, end):
        last_sentence = match.end()
    if last_sentence != -1:
        return last_sentence
    return end


def split_chunks(text: str, *, chunk_chars: int, overlap: int, min_chunk_chars: int) -> list[str]:
    """按字符切块:段落边界优先,其次句子边界,都没有才硬切。

    相邻块重叠 overlap 个字符(避免边界处的事实被腰斩);过短的尾块并入
    前一块,免得给模型一堆无上下文的碎片。不引第三方分词库——正文语种
    不定,规则切分反而稳定且零依赖。
    """
    if not text or not text.strip():
        return []
    if chunk_chars <= 0:
        return [text.strip()]
    safe_overlap = max(0, min(overlap, chunk_chars - 1))
    length = len(text)

    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < length:
        end = min(pos + chunk_chars, length)
        if end >= length:
            cut = length
        else:
            # 边界至少要留出半块,否则宁可硬切,避免切出一堆超短块
            floor = min(pos + max(1, min(min_chunk_chars, chunk_chars // 2)), end)
            cut = _find_cut(text, pos, end, floor)
        spans.append((pos, cut))
        if cut >= length:
            break
        pos = max(cut - safe_overlap, pos + 1)

    # 尾块过短则并回前一块:合并的是区间而非字符串,避免把重叠段拼两遍
    if len(spans) >= 2 and spans[-1][1] - spans[-1][0] < min_chunk_chars:
        tail = spans.pop()
        spans[-1] = (spans[-1][0], tail[1])

    return [chunk for chunk in (text[s:e].strip() for s, e in spans) if chunk]


def _budget_per_page(pages: Sequence[FetchedPage], config: CompressionConfig) -> int:
    """预算按有正文的页面数平均分:与其让首页吃满,不如每页都能说上话。"""
    payload_pages = sum(1 for page in pages if page.status == "ok" and page.content)
    if payload_pages <= 0:
        return config.total_chars
    return max(config.min_chunk_chars, config.total_chars // payload_pages)


def compress_cutoff(pages: list[FetchedPage], config: CompressionConfig) -> list[FetchedPage]:
    """定长截断(默认策略,也是所有降级路径的落点)。

    纯函数:一律 model_copy 产出新对象,入参不被就地修改。
    """
    per_page = _budget_per_page(pages, config)
    compressed: list[FetchedPage] = []
    for page in pages:
        if page.status != "ok" or len(page.content) <= per_page:
            compressed.append(page.model_copy())  # 非 ok 或本就不超预算:原样保留
            continue
        content = page.content[:per_page].rstrip() + _TRUNCATED_SUFFIX
        compressed.append(page.model_copy(update={"content": content, "truncated": True}))
    return compressed


async def _score_chunks(query: str, chunks: list[str], score_fn: ScoreFn) -> list[float] | None:
    """调打分函数并校验结果;任何不可信情况返回 None 交给上层降级。"""
    try:
        scores = await score_fn(query, chunks)
    except Exception as exc:  # 打分是外部能力,失败只降级不阻塞 agent 回合
        logger.warning("websearch_compress_score_failed error=%s", exc)
        return None
    if not isinstance(scores, Sequence) or len(scores) != len(chunks):
        logger.warning(
            "websearch_compress_score_mismatch expected=%s got=%s",
            len(chunks),
            len(scores) if isinstance(scores, Sequence) else type(scores).__name__,
        )
        return None
    try:
        return [float(score) for score in scores]
    except (TypeError, ValueError) as exc:
        logger.warning("websearch_compress_score_invalid error=%s", exc)
        return None


async def compress_by_rerank(
    pages: list[FetchedPage],
    *,
    query: str,
    config: CompressionConfig,
    score_fn: ScoreFn | None = None,
) -> list[FetchedPage]:
    """按相关度重排选片:每页切块打分,组内取前 per_source_max,再跨页轮询填预算。

    一个块都没选上的页面仍保留(截到 min_chunk_chars),因为标题与 URL
    还要供模型引用;凭空丢页面会让引用编号对不上。
    """
    if score_fn is None:
        logger.warning("websearch_compress_no_score_fn method=rerank fallback=cutoff")
        return compress_cutoff(pages, config)

    # 页面下标 → 该页的块列表;只有 ok 且有正文的页面参与重排
    chunks_by_page: dict[int, list[str]] = {}
    for index, page in enumerate(pages):
        if page.status != "ok" or not page.content.strip():
            continue
        page_chunks = split_chunks(
            page.content,
            chunk_chars=config.chunk_chars,
            overlap=config.chunk_overlap,
            min_chunk_chars=config.min_chunk_chars,
        )
        if page_chunks:
            chunks_by_page[index] = page_chunks

    flat_chunks = [chunk for page_chunks in chunks_by_page.values() for chunk in page_chunks]
    if not flat_chunks:
        return compress_cutoff(pages, config)

    # 一次性打分:reranker 按请求计费/计时,逐页调用既慢又贵
    scores = await _score_chunks(query, flat_chunks, score_fn)
    if scores is None:
        return compress_cutoff(pages, config)

    # 组内按分数降序取前 per_source_max;元素带上原文序号,后面要按原序拼回
    groups: list[list[tuple[int, int, str]]] = []
    cursor = 0
    for page_index, page_chunks in chunks_by_page.items():
        scored = [
            (page_index, order, chunk, scores[cursor + order])
            for order, chunk in enumerate(page_chunks)
        ]
        cursor += len(page_chunks)
        scored.sort(key=lambda item: item[3], reverse=True)  # 稳定排序:同分保持原序
        groups.append([(item[0], item[1], item[2]) for item in scored[: config.per_source_max]])

    # 先按轮询排好名额次序,再按字符预算逐个收下:预算花光时靠前的轮次已覆盖各来源
    ordered = select_round_robin(groups, limit=sum(len(group) for group in groups))
    selected: dict[int, list[tuple[int, str]]] = {}
    used = 0
    for page_index, order, chunk in ordered:
        cost = len(chunk) + (len(_CHUNK_JOINER) if selected.get(page_index) else 0)
        if used + cost > config.total_chars:
            break
        used += cost
        selected.setdefault(page_index, []).append((order, chunk))

    compressed: list[FetchedPage] = []
    for index, page in enumerate(pages):
        if index not in chunks_by_page:
            compressed.append(page.model_copy())
            continue
        picked = selected.get(index)
        if not picked:
            # 一块没中:留个头部当"名片",保住标题/URL 的可引用性
            content = page.content[: config.min_chunk_chars]
            compressed.append(
                page.model_copy(update={"content": content, "truncated": content != page.content})
            )
            continue
        picked.sort(key=lambda item: item[0])  # 按原文出现顺序拼回,而非分数顺序
        content = _CHUNK_JOINER.join(chunk for _, chunk in picked)
        compressed.append(
            page.model_copy(update={"content": content, "truncated": content != page.content})
        )
    return compressed


async def compress_pages(
    pages: list[FetchedPage],
    *,
    query: str,
    config: CompressionConfig,
    score_fn: ScoreFn | None = None,
) -> list[FetchedPage]:
    """按 config.method 分派压缩策略(rerank 内部已含降级)。"""
    if config.method == "none":
        return list(pages)
    if config.method == "rerank":
        return await compress_by_rerank(pages, query=query, config=config, score_fn=score_fn)
    return compress_cutoff(pages, config)
