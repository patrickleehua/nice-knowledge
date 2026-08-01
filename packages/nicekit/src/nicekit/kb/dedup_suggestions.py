"""实体归一合并候选推荐(概率匹配 + 置信分,human-in-the-loop)。

只做"建议",绝不自动合并:候选对由人工在前端确认后走既有
POST /kb/canonical-entities/{src}/merge 完成合并。

信号分层(分值即置信分,取触发信号的最高分,理由全部保留):
- 强信号(>= 0.9):
  - canonical_name_match  双方标准名 normalize 后相同               0.98
  - shared_alias          双方别名 normalize 后存在交集             0.95
  - canonical_is_alias    一方标准名 normalize 后是另一方的别名     0.92
- 中信号(0.6 ~ 0.88):
  - name_substring        短名是长名的子串:0.6 + 0.25 * (len 短/len 长)
  - token_overlap         词元 Dice >= 0.5:0.6 + 0.28 * (dice-0.5)/0.5
                          (中文按字符 bigram 切词元,拉丁按空白词)
  - edit_distance         纯拉丁名且长度 >= 4,相似度 >= 0.8:
                          0.6 + 0.28 * (sim-0.8)/0.2(中文短名不启用)
- 语义加分(可选):双方实体卡片 chunk(source_ref="{type}:{id}")都有
  embedding 时按余弦相似度加分(>=0.9 +0.08 / >=0.8 +0.05 / >=0.7 +0.02),
  最终分封顶 0.99;任一方无向量卡片则跳过该信号,不报错。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.entity_resolution import normalize_alias
from nicekit.models.kb import CanonicalEntity, EntityAlias, KbChunk

# 候选门槛:低于中信号下限的对不产出
MIN_CONFIDENCE = 0.6
# 强信号分值
SCORE_CANONICAL_NAME_MATCH = 0.98
SCORE_SHARED_ALIAS = 0.95
SCORE_CANONICAL_IS_ALIAS = 0.92
# 语义加分档位与封顶
_SEMANTIC_BONUS_TIERS = ((0.9, 0.08), (0.8, 0.05), (0.7, 0.02))
MAX_CONFIDENCE = 0.99
# 阻塞(blocking)后成对打分的安全上限,防大库 O(n^2) 失控
MAX_SCORED_PAIRS = 20_000


@dataclass(frozen=True, slots=True)
class MergeSuggestion:
    """一条合并建议:source 建议并入 target(target 取别名更丰富/更早的一方)。"""

    source: CanonicalEntity
    target: CanonicalEntity
    confidence: float
    reasons: tuple[str, ...]


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def name_tokens(normalized: str) -> frozenset[str]:
    """规范化名 → 词元集:拉丁词保留整词,CJK 连续段展开为字符 bigram。"""
    tokens: set[str] = set()
    for word in normalized.split():
        run: list[str] = []
        latin: list[str] = []
        for ch in word:
            if _is_cjk(ch):
                if latin:
                    tokens.add("".join(latin))
                    latin = []
                run.append(ch)
            else:
                if run:
                    tokens.update(_cjk_bigrams(run))
                    run = []
                latin.append(ch)
        if latin:
            tokens.add("".join(latin))
        if run:
            tokens.update(_cjk_bigrams(run))
    return frozenset(tokens)


def _cjk_bigrams(chars: list[str]) -> set[str]:
    if len(chars) == 1:
        return {chars[0]}
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _blocking_keys(normalized_name: str, alias_keys: frozenset[str]) -> frozenset[str]:
    """成对比较前的粗召回键:去空格全名 bigram + 全名本身 + 别名键。"""
    compact = normalized_name.replace(" ", "")
    keys = {compact}
    keys.update(compact[i : i + 2] for i in range(len(compact) - 1))
    return frozenset(keys | alias_keys)


def levenshtein(a: str, b: str) -> int:
    """标准编辑距离(仅用于拉丁短语,调用方保证规模可控)。"""
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return previous[-1]


def score_name_pair(norm_a: str, norm_b: str) -> tuple[float, list[str]]:
    """中信号:子串 / 词元重叠 / 编辑距离(纯拉丁)。返回 (最高分, 理由列表)。"""
    score = 0.0
    reasons: list[str] = []

    short, long = sorted((norm_a, norm_b), key=len)
    if len(short) >= 2 and short != long and short in long:
        score = max(score, 0.6 + 0.25 * (len(short) / len(long)))
        reasons.append("name_substring")

    tokens_a, tokens_b = name_tokens(norm_a), name_tokens(norm_b)
    if tokens_a and tokens_b:
        overlap = len(tokens_a & tokens_b)
        dice = 2 * overlap / (len(tokens_a) + len(tokens_b))
        if dice >= 0.5:
            score = max(score, 0.6 + 0.28 * (dice - 0.5) / 0.5)
            reasons.append(f"token_overlap:{dice:.2f}")

    compact_a, compact_b = norm_a.replace(" ", ""), norm_b.replace(" ", "")
    latin_only = not any(_is_cjk(ch) for ch in compact_a + compact_b)
    if latin_only and min(len(compact_a), len(compact_b)) >= 4:
        max_len = max(len(compact_a), len(compact_b))
        similarity = 1 - levenshtein(compact_a, compact_b) / max_len
        if similarity >= 0.8:
            score = max(score, 0.6 + 0.28 * (similarity - 0.8) / 0.2)
            reasons.append(f"edit_distance:{similarity:.2f}")

    return score, reasons


def score_candidate_pair(
    norm_a: str,
    norm_b: str,
    aliases_a: frozenset[str],
    aliases_b: frozenset[str],
) -> tuple[float, list[str]]:
    """强信号 + 中信号合成:返回 (置信分, 理由列表);0 分表示不构成候选。"""
    score = 0.0
    reasons: list[str] = []

    if norm_a == norm_b:
        score = SCORE_CANONICAL_NAME_MATCH
        reasons.append("canonical_name_match")
    shared = aliases_a & aliases_b
    if shared:
        score = max(score, SCORE_SHARED_ALIAS)
        reasons.append(f"shared_alias:{sorted(shared)[0]}")
    if norm_a in aliases_b or norm_b in aliases_a:
        score = max(score, SCORE_CANONICAL_IS_ALIAS)
        if "canonical_name_match" not in reasons:
            reasons.append("canonical_is_alias")

    name_score, name_reasons = score_name_pair(norm_a, norm_b)
    score = max(score, name_score)
    reasons.extend(name_reasons)
    return score, reasons


def cosine_similarity(a: list[float], b: list[float]) -> float | None:
    """余弦相似度;维度不一致或零向量返回 None(调用方跳过该信号)。"""
    if len(a) != len(b) or not a:
        return None
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    return dot / (norm_a**0.5 * norm_b**0.5)


def semantic_bonus(similarity: float) -> float:
    for threshold, bonus in _SEMANTIC_BONUS_TIERS:
        if similarity >= threshold:
            return bonus
    return 0.0


def _pair_direction(
    a: CanonicalEntity,
    b: CanonicalEntity,
    alias_count: dict[UUID, int],
) -> tuple[CanonicalEntity, CanonicalEntity]:
    """(source, target):别名更丰富、创建更早的一方作为保留目标(target)。"""

    def keep_rank(entity: CanonicalEntity) -> tuple:
        created = entity.created_at.isoformat() if entity.created_at else "9999"
        return (-alias_count.get(entity.id, 0), created, str(entity.id))

    first, second = sorted((a, b), key=keep_rank)
    return second, first


async def suggest_merge_candidates(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    entity_type: str | None = None,
    limit: int = 50,
) -> list[MergeSuggestion]:
    """产出同 KB 同 entity_type 的相似实体候选对(只读,不做任何合并)。

    候选按置信分降序;同 id 对天然排除,已合并实体(行已删除)不会出现。
    """
    stmt = (
        select(CanonicalEntity)
        .where(CanonicalEntity.org_id == org_id, CanonicalEntity.kb_id == kb_id)
        .order_by(
            CanonicalEntity.entity_type,
            CanonicalEntity.canonical_name,
            CanonicalEntity.id,
        )
    )
    if entity_type:
        stmt = stmt.where(CanonicalEntity.entity_type == entity_type)
    entities = list((await session.execute(stmt)).scalars())
    if len(entities) < 2:
        return []

    alias_rows = (
        await session.execute(
            select(EntityAlias.entity_id, EntityAlias.normalized_alias).where(
                EntityAlias.kb_id == kb_id,
                EntityAlias.entity_id.in_([e.id for e in entities]),
            )
        )
    ).all()
    alias_keys: dict[UUID, set[str]] = {e.id: set() for e in entities}
    for eid, normalized in alias_rows:
        alias_keys[eid].add(normalized)
    alias_sets = {eid: frozenset(keys) for eid, keys in alias_keys.items()}
    alias_count = {eid: len(keys) for eid, keys in alias_keys.items()}
    norm_names = {e.id: normalize_alias(e.canonical_name) for e in entities}

    # 阻塞粗召回:同 entity_type 内,共享任一键(名称 bigram/全名/别名)的实体对才进入打分
    block_index: dict[tuple[str, str], list[int]] = {}
    for idx, entity in enumerate(entities):
        for key in _blocking_keys(norm_names[entity.id], alias_sets[entity.id]):
            block_index.setdefault((entity.entity_type, key), []).append(idx)
    candidate_pairs: set[tuple[int, int]] = set()
    for indexes in block_index.values():
        if len(indexes) < 2:
            continue
        for pos, i in enumerate(indexes):
            for j in indexes[pos + 1 :]:
                candidate_pairs.add((i, j))
                if len(candidate_pairs) >= MAX_SCORED_PAIRS:
                    break
            if len(candidate_pairs) >= MAX_SCORED_PAIRS:
                break

    scored: list[tuple[CanonicalEntity, CanonicalEntity, float, list[str]]] = []
    for i, j in candidate_pairs:
        a, b = entities[i], entities[j]
        score, reasons = score_candidate_pair(
            norm_names[a.id], norm_names[b.id], alias_sets[a.id], alias_sets[b.id]
        )
        if score >= MIN_CONFIDENCE:
            scored.append((a, b, score, reasons))

    # 语义加分:仅对已入围候选查实体卡向量;无卡/无向量时静默跳过
    if scored:
        involved = {e.id: e for pair in scored for e in (pair[0], pair[1])}
        refs = {f"{e.entity_type}:{e.id}": e.id for e in involved.values()}
        vector_rows = (
            await session.execute(
                select(KbChunk.source_ref, KbChunk.embedding).where(
                    KbChunk.kb_id == kb_id,
                    KbChunk.source_ref.in_(list(refs)),
                    KbChunk.embedding.is_not(None),
                )
            )
        ).all()
        vectors: dict[UUID, list[float]] = {}
        for source_ref, embedding in vector_rows:
            if embedding is not None:
                vectors[refs[source_ref]] = [float(v) for v in embedding]

        enriched: list[tuple[CanonicalEntity, CanonicalEntity, float, list[str]]] = []
        for a, b, score, reasons in scored:
            vec_a, vec_b = vectors.get(a.id), vectors.get(b.id)
            if vec_a is not None and vec_b is not None:
                similarity = cosine_similarity(vec_a, vec_b)
                if similarity is not None:
                    bonus = semantic_bonus(similarity)
                    if bonus > 0:
                        score = min(score + bonus, MAX_CONFIDENCE)
                        reasons = [*reasons, f"semantic_similarity:{similarity:.2f}"]
            enriched.append((a, b, score, reasons))
        scored = enriched

    suggestions = []
    for a, b, score, reasons in scored:
        source, target = _pair_direction(a, b, alias_count)
        suggestions.append(
            MergeSuggestion(
                source=source,
                target=target,
                confidence=round(min(score, MAX_CONFIDENCE), 4),
                reasons=tuple(reasons),
            )
        )
    suggestions.sort(
        key=lambda s: (
            -s.confidence,
            s.source.canonical_name,
            s.target.canonical_name,
            str(s.source.id),
        )
    )
    return suggestions[:limit]


__all__ = [
    "MAX_CONFIDENCE",
    "MIN_CONFIDENCE",
    "MergeSuggestion",
    "cosine_similarity",
    "levenshtein",
    "name_tokens",
    "score_candidate_pair",
    "score_name_pair",
    "semantic_bonus",
    "suggest_merge_candidates",
]
