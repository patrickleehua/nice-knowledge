"""长期记忆:从对话沉淀偏好/约束/决策,并在后续对话按相关性召回注入。

与 ContextProvider(宿主注入的"此刻状态"上下文段)的分工:后者是运行时从宿主
业务真相派生、不落库的当前快照;本模块是"这个作用域长期是什么样"(跨会话沉淀、
要落库、要能人工审阅纠正)。两段并列注入 system prompt,互不替代。

迁移自 TF backend/app/services/agent/memory.py。SDK 化改造(§4/§5.4):
- **scope 泛化**:TF 的 org/project/customer 三档硬编码词表收敛为内置 `org`
  + 宿主经 `models.memory.register_memory_scopes()` 注册的任意作用域;
- **ScopeResolver 扩展点**:TF 直接 import `services/customer.py` 解析会话的
  业务 ref,SDK 改为宿主注册 `set_scope_resolver()`(签名见 `ScopeResolver`);
  未注册时解析为空,只有 org 范围记忆参与沉淀与召回;
- `MEMORY_TASK` 默认 `agent.default`(TF 写死 agent.workbench),可经
  `set_memory_task()` 改。

对齐 TripBoss docs/agent-loop-architecture.md §14 的核心裁决:

**agent 输出不直接写库。** LLM 抽取产物与 memory_write 工具走的是同一条管道
(ingest_candidate):防污染筛查 → 去重合并 → 置信打分 → 候选提升。工具层没有
任何绕过入口——绕过去重就会让同一句话被写成十行,绕过防污染就会让模型脑补的
偏好变成组织资产。

**候选与正式分层。** 偏好类候选一律先落 preference_candidate;同一事实被独立
观察到 PROMOTION_MIN_SIGHTINGS 次且置信达 PROMOTION_MIN_CONFIDENCE 才提升为
preference。单次表态成不了长期偏好——用户随口一句"这次先用方案 B"不该让之后
每一次都默认方案 B。constraint/decision/fact/risk 不走候选期:它们描述的是已经
发生的事实与硬约束,重复观察不会让它们"更真",拿证据说话即可。

**召回失败绝不阻塞对话。** 与 ContextProvider 同一容错口径:异常就地捕获、
本轮不注入、运行时快照记一条 note。记忆是锦上添花,不是对话的必要输入。
"""

import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.db import org_session
from nicekit.models.chat import ChatMessage
from nicekit.models.memory import (
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    known_memory_scopes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 扩展点:会话 → 作用域 ref 解析(MIGRATION-PLAN §4 ScopeResolver)
# ---------------------------------------------------------------------------

# 宿主实现:给定会话,返回 {记忆范围名: 该范围的 ref 字符串}。范围名必须已经过
# register_memory_scopes() 注册;org 范围不需要出现在返回值里(它天然没有具体
# 指向对象)。解析失败应返回空 dict 而不是抛异常——记忆是锦上添花,不能因为
# 宿主的一次查询失败让对话中断。
ScopeResolver = Callable[[AsyncSession, UUID, UUID], Awaitable[Mapping[str, str]]]


async def _null_scope_resolver(
    session: AsyncSession, org_id: UUID, session_id: UUID
) -> Mapping[str, str]:
    """默认实现:不解析任何宿主作用域(只有 org 范围记忆可用)。"""
    return {}


_scope_resolver: ScopeResolver = _null_scope_resolver


def set_scope_resolver(resolver: ScopeResolver | None) -> None:
    """注入宿主的作用域解析(传 None 恢复"只有 org 范围"的默认行为)。"""
    global _scope_resolver
    _scope_resolver = resolver or _null_scope_resolver


def get_scope_resolver() -> ScopeResolver:
    return _scope_resolver

# 注入段在 system prompt 块清单里的 id(service.py 传参与快照排障共用)
MEMORY_RECALL_BLOCK_ID = "memory_recall"

# 注入段总长上限。比宿主上下文段(2500)更紧:记忆是"背景",当前状态才是
# "前景",背景段挤占历史窗口的性价比更低。
MEMORY_SECTION_MAX_CHARS = 1500

# 单条记忆进 prompt 的最大字符数:超长的一条会把预算一口吃光
_ITEM_MAX_CHARS = 220
# 一次召回最多注入几条:条数上限与字符预算双闸,防止十几条短记忆刷屏
RECALL_MAX_ITEMS = 8
# 与本轮话题的最低关联度(关键词覆盖率与语义相似度取大者)。
# 这是**硬闸而非加分项**:类型权重/置信/新鲜度这些"质量分"与提问无关,任何
# 记忆都拿得到,只把它们求和会让一条毫不相干的旧偏好轻松越过任何总分门槛。
# 关联度先做准入,质量分只负责在相关的那些之间排序。
RECALL_MIN_RELEVANCE = 0.15
# 总分下限(准入之后的二道保险,防止勉强擦线的低质量记忆挤占预算)
RECALL_MIN_SCORE = 1.0
# 召回打分里的范围贴合两档权重(见 score_memory)
SCOPE_ORG_WEIGHT = 0.3
SCOPE_SPECIFIC_WEIGHT = 0.7

# —— 写入管道阈值 ——
# 低于此置信度的候选直接丢弃并记日志(LLM 自己都不确定的东西不配进长期记忆)
MIN_WRITE_CONFIDENCE = 0.5
# 判定"同一事实"的相似度门槛(标题与正文加权 Jaccard);达到即合并不新增行
DEDUPE_SIMILARITY = 0.55
# 候选提升为正式 preference 所需的独立观察次数
PROMOTION_MIN_SIGHTINGS = 2
# 候选提升所需的置信度(合并后的值)
PROMOTION_MIN_CONFIDENCE = 0.7
# 每次重复观察给置信度的加成(合并后取 max(旧,新) 再加这个增量,封顶 0.99)
SIGHTING_CONFIDENCE_BOOST = 0.1

# 抽取时喂给模型的最近消息条数。太少抽不出跨轮结论,太多会把早已被摘要压缩过的
# 内容重复抽一遍(同一事实会靠去重合并兜住,但白烧 token)。
EXTRACT_RECENT_MESSAGES = 20
# 消息数不足时不触发抽取:两三句寒暄里没有值得长期保留的东西
EXTRACT_MIN_MESSAGES = 4
# 单条消息进抽取上下文的字符上限(工具结果尤其长)
_TRANSCRIPT_ITEM_MAX = 600

# 复用默认会话路由(同 compression.SUMMARY_TASK):抽取与会话同模型域,
# 免去为它单独 seed 一条 ModelRoute,也自动复用降级链与用量审计。
# (TF 写死 agent.workbench;SDK 默认 agent.default,宿主可经 set_memory_task 改。)
DEFAULT_MEMORY_TASK = "agent.default"
MEMORY_TASK = DEFAULT_MEMORY_TASK


def set_memory_task(task: str | None) -> None:
    """改记忆抽取用的 LLM task 名(传 None 恢复默认 agent.default)。"""
    global MEMORY_TASK
    MEMORY_TASK = task or DEFAULT_MEMORY_TASK

# prompt 资源包未就位时的兜底抽取指令(见 _extraction_system_prompt)
_FALLBACK_EXTRACTION_PROMPT = (
    "你是长期记忆抽取器。从给定对话中抽取值得跨会话保留的记忆,"
    "输出 JSON 数组,每项含 memory_type/scope/title/content/confidence/evidence。"
    "只抽有对话原文佐证的内容:evidence 必须是原文摘录;"
    "不抽推测、一次性情绪与过程性细节;抽不到就输出空数组 []。"
)

# 走 memory_write 工具写入时的 source 前缀,便于人工回溯"这条是 agent 主动记的"
SOURCE_AGENT_TOOL = "memory_write"
SOURCE_EXTRACTION = "memory_extraction"

# 不参与候选期、直接以本型落库的记忆类型(理由见模块 docstring)
_DIRECT_TYPES = frozenset(
    {
        MemoryType.CONSTRAINT.value,
        MemoryType.DECISION.value,
        MemoryType.FACT.value,
        MemoryType.RISK.value,
    }
)

# 防污染:低置信推测的语言标记。命中且置信度不够高时丢弃——
# 模型经常一边写"可能/也许"一边给 0.8 的置信度,文本信号比它的自评更可信。
_HEDGE_MARKERS = (
    "可能",
    "也许",
    "大概",
    "应该是",
    "我猜",
    "似乎",
    "看起来",
    "推测",
    "估计",
    "maybe",
    "perhaps",
    "probably",
    "i guess",
)
# 命中对冲词后仍可放行的置信度下限
_HEDGE_CONFIDENCE_FLOOR = 0.85

# 防污染:一次性情绪。这些词出现在一条"偏好"里,基本可以断定抽的是当下情绪。
_EMOTION_MARKERS = (
    "生气",
    "很气",
    "不高兴",
    "不满意这次",
    "抱怨",
    "着急",
    "太慢了",
    "催了",
    "无语",
    "失望",
)

# 记忆类型在召回打分里的基础权重:硬约束与风险最该被看见,
# 未提升的候选权重压到最低(它还没被证明是稳定的)
_TYPE_WEIGHT = {
    MemoryType.CONSTRAINT.value: 1.2,
    MemoryType.RISK.value: 1.1,
    MemoryType.DECISION.value: 1.0,
    MemoryType.PREFERENCE.value: 0.9,
    MemoryType.FACT.value: 0.6,
    MemoryType.PREFERENCE_CANDIDATE.value: 0.3,
}

_TYPE_LABEL = {
    MemoryType.CONSTRAINT.value: "约束",
    MemoryType.RISK.value: "风险",
    MemoryType.DECISION.value: "决策",
    MemoryType.PREFERENCE.value: "偏好",
    MemoryType.FACT.value: "事实",
    MemoryType.PREFERENCE_CANDIDATE.value: "偏好(待确认)",
}

# 内置范围的显示名;宿主注册的范围默认直接用范围名(见 scope_label())
_SCOPE_LABEL = {MemoryScope.ORG.value: "全组织"}


def scope_label(scope: str) -> str:
    """记忆范围的注入显示名(宿主范围未登记显示名时回落范围名本身)。"""
    return _SCOPE_LABEL.get(scope, scope)


def register_scope_label(scope: str, label: str) -> None:
    """宿主给自己的记忆范围登记显示名(可选)。"""
    _SCOPE_LABEL[scope] = label

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_NON_HAN_RE = re.compile(r"[^一-鿿]+")


class MemoryValidationError(ValueError):
    """候选字段非法(类型/范围/标题缺失等),由调用方转成对模型可读的报错。"""


# ---------------------------------------------------------------------------
# 纯函数:分词 / 相似度 / 筛查 / 打分(全部无 IO,便于单测)
# ---------------------------------------------------------------------------


def tokenize(text: str) -> set[str]:
    """中英混排的轻量分词:ASCII 单词 + 汉字二元组。

    刻意不引入分词器依赖:记忆条目是短文本,二元组对"海景房""不接受"这类
    业务词的召回已经够用,而多一个中文分词模型就多一处部署与版本负担。
    单字汉字串(如"雨")保留原字,否则会被二元组切没。
    """
    lowered = (text or "").lower()
    tokens = set(_ASCII_WORD_RE.findall(lowered))
    for run in _NON_HAN_RE.sub(" ", lowered).split():
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def similarity(left: str, right: str) -> float:
    """两段文本的 Jaccard 相似度(0~1);任一为空返回 0。"""
    left_tokens, right_tokens = tokenize(left), tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def coverage(query: str, text: str) -> float:
    """query 的 token 被 text 覆盖的比例(0~1)。

    召回用覆盖率而不是 Jaccard:一条 200 字的记忆与 10 字的提问天然 Jaccard 很低,
    但只要它把提问里的关键词都讲到了,它就是相关的。
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    return len(query_tokens & tokenize(text)) / len(query_tokens)


def cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """余弦相似度;任一向量缺失或为零向量时返回 0(退回纯关键词打分)。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待入库的候选记忆(抽取器产物或 memory_write 工具入参的统一形态)。"""

    memory_type: str
    scope: str
    title: str
    content: str
    confidence: float = 0.6
    evidence: str = ""

    @property
    def text(self) -> str:
        return f"{self.title} {self.content}"


def normalize_candidate(raw: dict) -> MemoryCandidate:
    """把任意来源的原始字典规整成 MemoryCandidate;非法字段直接抛错。

    抛错而非静默兜底:抽取侧会把异常转成"丢弃 + 记原因",工具侧会把它转成对
    模型可读的报错。两条路径都需要知道"哪一条为什么没写进去",静默改写只会
    让脏数据以另一种形态落库。
    """
    memory_type = str(raw.get("memory_type") or raw.get("type") or "").strip()
    if memory_type not in {item.value for item in MemoryType}:
        raise MemoryValidationError(
            f"memory_type 非法:{memory_type!r};可选 "
            + "/".join(sorted(item.value for item in MemoryType))
        )
    scope = str(raw.get("scope") or MemoryScope.ORG.value).strip()
    known = known_memory_scopes()
    if scope not in known:
        raise MemoryValidationError(
            f"scope 非法:{scope!r};可选 " + "/".join(sorted(known))
        )
    title = " ".join(str(raw.get("title") or "").split())
    content = str(raw.get("content") or raw.get("body") or "").strip()
    if not title or not content:
        raise MemoryValidationError("title 与 content 都不能为空")
    try:
        confidence = float(raw.get("confidence", 0.6))
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError("confidence 必须是 0~1 的小数") from exc
    if not 0.0 <= confidence <= 1.0:
        raise MemoryValidationError("confidence 必须落在 0~1 之间")
    return MemoryCandidate(
        memory_type=memory_type,
        scope=scope,
        title=title[:200],
        content=content[:2000],
        confidence=confidence,
        evidence=str(raw.get("evidence") or "").strip()[:500],
    )


def screen_candidate(candidate: MemoryCandidate) -> str | None:
    """防污染筛查(纯函数)。返回丢弃原因;通过返回 None。

    这是防污染清单在代码里的第二道闸——第一道写在 memory_extraction.md 的
    prompt 里。只有 prompt 是不够的:模型会违反自己的指令,而防污染是长期
    记忆能不能被信任的地基,必须有不依赖模型自觉的硬校验。
    (第三道闸"被用户否定过的建议"依赖库里的 rejected 记录,见 ingest_candidate)
    """
    if candidate.confidence < MIN_WRITE_CONFIDENCE:
        return f"confidence {candidate.confidence:.2f} 低于阈值 {MIN_WRITE_CONFIDENCE}"
    lowered = candidate.text.lower()
    if candidate.confidence < _HEDGE_CONFIDENCE_FLOOR:
        hit = next((marker for marker in _HEDGE_MARKERS if marker in lowered), None)
        if hit is not None:
            return f"含低置信推测措辞「{hit}」且置信度不足 {_HEDGE_CONFIDENCE_FLOOR}"
    hit = next((marker for marker in _EMOTION_MARKERS if marker in lowered), None)
    if hit is not None:
        return f"含一次性情绪表述「{hit}」,不作为长期记忆"
    # evidence 是"这条不是脑补"的唯一凭证。抽取器被明确要求给原文摘录,
    # 给不出来的一律按脑补处理;工具写入不强制(agent 有对话上下文可自证)。
    return None


def initial_memory_type(candidate: MemoryCandidate) -> str:
    """入库时的实际类型:偏好类一律先落候选,其余类型按原样落库。"""
    if candidate.memory_type in _DIRECT_TYPES:
        return candidate.memory_type
    return MemoryType.PREFERENCE_CANDIDATE.value


def merged_confidence(existing: float, incoming: float, sightings: int) -> float:
    """合并后的置信度:取两者较大值,再按重复观察次数给增量,封顶 0.99。

    不用平均值:一次高置信观察 + 一次低置信观察,说明这件事至少被明确说过一次,
    平均会把它稀释掉。封顶 0.99 是为了留出"永远不是 100% 确定"的余地。
    """
    base = max(existing, incoming)
    boost = SIGHTING_CONFIDENCE_BOOST * max(sightings - 1, 0)
    return min(0.99, round(base + boost, 4))


def should_promote(memory_type: str, sightings: int, confidence: float) -> bool:
    """候选提升规则:仅 preference_candidate 适用,需同时满足次数与置信双条件。

    两个条件缺一不可:只看次数会让模型的重复脑补被扶正,只看置信会让一次性的
    强表态直接变成长期偏好。默认 2 次 / 0.7 置信,由上方常量集中调。
    """
    return (
        memory_type == MemoryType.PREFERENCE_CANDIDATE.value
        and sightings >= PROMOTION_MIN_SIGHTINGS
        and confidence >= PROMOTION_MIN_CONFIDENCE
    )


def relevance_of(
    item: MemoryItem,
    *,
    query_text: str,
    query_embedding: Sequence[float] | None = None,
) -> float:
    """这条记忆与本轮提问的关联度(0~1),两条通路取大者。

    关键词覆盖是兜底通路(永远可用),语义相似是加速通路(仅在双方都有
    embedding 时有值)。取大者而不是加权和:embedding 缺失时不该被平均拉低,
    这正是"必须有无 embedding 的关键词兜底"这条要求在打分侧的落点。
    """
    keyword = coverage(query_text, f"{item.title} {item.content}")
    semantic = cosine(query_embedding, item.embedding)
    return max(keyword, semantic)


def score_memory(
    item: MemoryItem,
    *,
    query_text: str,
    query_embedding: Sequence[float] | None = None,
    now: datetime | None = None,
) -> tuple[float, list[str]]:
    """给一条记忆打召回分,返回 (分值, 命中理由)。

    分两层(全部可解释,不用黑盒模型——排障时要能回答"它凭什么被选中"):
      **准入**:关联度 = max(关键词覆盖, 语义相似),低于 RECALL_MIN_RELEVANCE
                的在 rank_and_select 里直接丢弃,不进排序。
      **排序**:关联度 × 4.0 为主项,其余为质量分:
        类型权重 × 1.0   约束/风险 > 决策 > 偏好 > 事实 > 未提升候选
        置信度   × 1.0   模型/人工对这条记忆的把握
        范围贴合 × 0.8   宿主作用域级 > 全组织级(越具体越相关)
        新鲜度   × 0.5   半衰期 60 天的指数衰减
        历史命中 × 0.3   被反复用到的记忆更可能有用(封顶 5 次,防滚雪球)
    """
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    score = 0.0

    keyword = coverage(query_text, f"{item.title} {item.content}")
    semantic = cosine(query_embedding, item.embedding)
    score += 4.0 * max(keyword, semantic)
    if keyword > 0:
        reasons.append(f"关键词覆盖 {keyword:.2f}")
    if semantic > 0:
        reasons.append(f"语义相似 {semantic:.2f}")

    type_weight = _TYPE_WEIGHT.get(item.memory_type, 0.5)
    score += type_weight
    reasons.append(f"类型权重 {type_weight:.1f}")

    score += float(item.confidence or 0.0)

    # 宿主注册的具体作用域比全组织更贴合当下语境。TF 曾按两级业务层级给两档
    # 不同权重,那依赖宿主的领域语义,SDK 无从判定,一律统一给 0.7。
    scope_weight = (
        SCOPE_ORG_WEIGHT
        if item.scope == MemoryScope.ORG.value
        else SCOPE_SPECIFIC_WEIGHT
    )
    score += scope_weight

    updated = item.updated_at or item.created_at
    if updated is not None:
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age_days = max((now - updated).total_seconds() / 86400.0, 0.0)
        freshness = 0.5 * math.exp(-age_days / 60.0)
        score += freshness
        if freshness >= 0.25:
            reasons.append("近期沉淀")

    hits = min(int(item.hit_count or 0), 5)
    score += 0.3 * hits / 5

    return round(score, 4), reasons


@dataclass
class DroppedMemory:
    """一条被丢弃的召回候选:进运行时快照,回答"它为什么没被注入"。"""

    memory_id: str
    title: str
    reason: str
    score: float = 0.0


@dataclass
class RecallResult:
    selected: list[MemoryItem] = field(default_factory=list)
    dropped: list[DroppedMemory] = field(default_factory=list)
    # 打分理由(memory_id → 理由列表),同样只服务排障
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def snapshot_notes(self) -> list[str]:
        """压成运行时快照的 extra_notes(照 working_notes 的写法:短句、可读)。"""
        notes = [
            f"memory_recall_selected:{len(self.selected)} 条"
            + (
                ":" + "、".join(item.title for item in self.selected)
                if self.selected
                else ""
            )
        ]
        if self.dropped:
            notes.append(
                "memory_recall_dropped:"
                + "、".join(
                    f"{row.title}({row.reason})" for row in self.dropped[:8]
                )
            )
        return notes


def rank_and_select(
    items: Sequence[MemoryItem],
    *,
    query_text: str,
    budget_chars: int = MEMORY_SECTION_MAX_CHARS,
    query_embedding: Sequence[float] | None = None,
    max_items: int = RECALL_MAX_ITEMS,
    min_score: float = RECALL_MIN_SCORE,
    min_relevance: float = RECALL_MIN_RELEVANCE,
    now: datetime | None = None,
) -> RecallResult:
    """排序 + 预算内选择(纯函数,recall 的可测内核)。

    丢弃原因逐条记录:排障时"为什么这条没进 prompt"必须能答上来,只给
    selected 会让预算调优变成盲猜。
    """
    result = RecallResult()
    scored: list[tuple[float, list[str], MemoryItem]] = []
    for item in items:
        # 双保险:rejected/superseded 永不召回。SQL 侧已经过滤,这里再拦一次——
        # 纯函数会被工具与测试直接调用,不能假设调用方一定过滤干净。
        if item.status != MemoryStatus.ACTIVE.value:
            result.dropped.append(
                DroppedMemory(str(item.id), item.title, f"状态 {item.status} 不可召回")
            )
            continue
        relevance = relevance_of(
            item, query_text=query_text, query_embedding=query_embedding
        )
        if relevance < min_relevance:
            result.dropped.append(
                DroppedMemory(str(item.id), item.title, "与本轮话题无关", relevance)
            )
            continue
        score, reasons = score_memory(
            item, query_text=query_text, query_embedding=query_embedding, now=now
        )
        result.reasons[str(item.id)] = reasons
        scored.append((score, reasons, item))

    scored.sort(key=lambda row: (-row[0], row[2].title))
    used = 0
    for score, _reasons, item in scored:
        if score < min_score:
            result.dropped.append(
                DroppedMemory(str(item.id), item.title, "相关性低于阈值", score)
            )
            continue
        if len(result.selected) >= max_items:
            result.dropped.append(
                DroppedMemory(str(item.id), item.title, "超出条数上限", score)
            )
            continue
        cost = len(render_memory_line(item)) + 1
        if used + cost > budget_chars:
            result.dropped.append(
                DroppedMemory(str(item.id), item.title, "超出字符预算", score)
            )
            continue
        result.selected.append(item)
        used += cost
    return result


def _clip(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_memory_line(item: MemoryItem) -> str:
    """单条记忆的注入行:类型标签 + 范围 + 正文 + 来源说明。

    来源必须写进 prompt:模型据此判断这条记忆的分量(用户原话 vs agent 自记),
    也让它在与用户当面确认时能说清"我这条是从哪来的"。
    """
    type_label = _TYPE_LABEL.get(item.memory_type, item.memory_type)
    label = scope_label(item.scope)
    return (
        f"- [{type_label}·{label}] {_clip(item.title, 40)}:"
        f"{_clip(item.content, _ITEM_MAX_CHARS)}"
        f"(置信 {float(item.confidence or 0):.2f},来源 {_clip(item.source, 40)})"
    )


def render_memory_section(
    items: Sequence[MemoryItem], *, max_chars: int = MEMORY_SECTION_MAX_CHARS
) -> str:
    """【相关记忆】注入段;无记忆时返回空串(空段不该占 prompt 位置)。

    段首免责声明与 working_context 同理:记忆是历史沉淀而非当前真相,任何要
    落笔改数据的动作仍须以工具实时查询为准;记忆与用户当下的说法冲突时,
    以用户当下说法为准——这一条不写清楚,模型会拿三个月前的偏好去驳用户。
    """
    if not items:
        return ""
    header = (
        "【相关记忆】以下为系统从历史对话沉淀、按本轮话题召回的长期记忆,"
        "仅供参考:与用户本轮说法冲突时一律以用户当下说法为准,"
        "涉及具体数据仍以工具实时查询为准;"
        "记忆有误时可用 memory_forget 标记失效。"
    )
    text = header
    for item in items:
        line = render_memory_line(item)
        candidate = f"{text}\n{line}"
        if len(candidate) > max_chars:
            break
        text = candidate
    return text


# ---------------------------------------------------------------------------
# 写入管道(agent 产物与工具写入的唯一入口)
# ---------------------------------------------------------------------------


@dataclass
class IngestOutcome:
    """一次候选入库的结果。action:created / merged / promoted / dropped。"""

    action: str
    reason: str
    item: MemoryItem | None = None

    @property
    def written(self) -> bool:
        return self.action != "dropped"


async def _scope_siblings(
    session: AsyncSession,
    *,
    org_id: UUID,
    scope: str,
    scope_ref_id: str | None,
) -> list[MemoryItem]:
    """同 scope 下的既有记忆(含 rejected)。

    带上 rejected 是刻意的:防污染第三条"被用户否定过的建议不得再写"必须
    看得到否定记录,只查 active 会让同一条脏记忆被下一轮抽取原样复活。
    """
    return list(
        (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.org_id == org_id,
                    MemoryItem.scope == scope,
                    MemoryItem.scope_ref_id == scope_ref_id,
                )
            )
        )
        .scalars()
        .all()
    )


def find_duplicate(
    candidate: MemoryCandidate, siblings: Sequence[MemoryItem]
) -> MemoryItem | None:
    """在同 scope 记忆里找"讲的是同一件事"的那一条(纯函数)。

    标题权重 0.6 / 正文 0.4:标题是人给这条记忆起的名字,同名基本就是同一件事;
    正文补一票,防止两条不同的事被起了相近的短标题。
    """
    best: tuple[float, MemoryItem] | None = None
    for row in siblings:
        score = 0.6 * similarity(candidate.title, row.title) + 0.4 * similarity(
            candidate.content, row.content
        )
        if score >= DEDUPE_SIMILARITY and (best is None or score > best[0]):
            best = (score, row)
    return None if best is None else best[1]


async def ingest_candidate(
    session: AsyncSession,
    *,
    org_id: UUID,
    candidate: MemoryCandidate,
    source: str,
    scope_ref_id: str | None = None,
    source_message_id: UUID | None = None,
) -> IngestOutcome:
    """候选入库的唯一入口:防污染 → 去重合并 → 打分 → 候选提升。

    抽取器与 memory_write 工具都必须走这里(见模块 docstring 的第一条纪律)。
    不 commit:调用方决定事务边界(工具在自己的 session 里提交,抽取任务批量提交)。
    """
    scope = candidate.scope
    # org 范围没有具体指向对象,ref 一律置空,避免同一条全组织规则按作用域存很多份
    ref = None if scope == MemoryScope.ORG.value else scope_ref_id

    reason = screen_candidate(candidate)
    if reason is not None:
        logger.info(
            "记忆候选被防污染筛查丢弃 org=%s title=%s reason=%s",
            org_id,
            candidate.title,
            reason,
        )
        return IngestOutcome("dropped", reason)

    siblings = await _scope_siblings(
        session, org_id=org_id, scope=scope, scope_ref_id=ref
    )
    duplicate = find_duplicate(candidate, siblings)

    if duplicate is not None and duplicate.status == MemoryStatus.REJECTED.value:
        # 防污染第三条:被否定过的结论不得借"新一轮抽取"复活
        logger.info(
            "记忆候选命中已否定记忆,丢弃 org=%s title=%s memory=%s",
            org_id,
            candidate.title,
            duplicate.id,
        )
        return IngestOutcome("dropped", "同主题记忆此前已被否定(rejected)")

    now = datetime.now(UTC)
    if duplicate is not None:
        # 同一事实再次被观察到:合并到既有行而不是新增一行。
        # 新增行会让同一条偏好在召回里占好几个名额,把预算耗在重复信息上。
        duplicate.sightings += 1
        duplicate.confidence = merged_confidence(
            duplicate.confidence, candidate.confidence, duplicate.sightings
        )
        # 正文取更长的那份:后一次表述通常更完整,但短表述若是补充则不该覆盖长的
        if len(candidate.content) > len(duplicate.content):
            duplicate.content = candidate.content
        duplicate.updated_at = now
        action, reason = "merged", (
            f"合并到既有记忆(第 {duplicate.sightings} 次观察,"
            f"置信 {duplicate.confidence:.2f})"
        )
        if should_promote(
            duplicate.memory_type, duplicate.sightings, duplicate.confidence
        ):
            duplicate.memory_type = MemoryType.PREFERENCE.value
            action, reason = "promoted", (
                f"重复观察 {duplicate.sightings} 次且置信 {duplicate.confidence:.2f},"
                "候选提升为正式偏好"
            )
        # superseded 的行被同一事实再次观察到即复活:说明它并没有过时
        if duplicate.status == MemoryStatus.SUPERSEDED.value:
            duplicate.status = MemoryStatus.ACTIVE.value
        session.add(duplicate)
        return IngestOutcome(action, reason, duplicate)

    item = MemoryItem(
        # id 显式生成:调用方要在 commit 前拿它拼工具返回值与事件载荷
        id=uuid4(),
        org_id=org_id,
        scope=scope,
        scope_ref_id=ref,
        memory_type=initial_memory_type(candidate),
        title=candidate.title,
        content=candidate.content,
        source=source[:200],
        source_message_id=source_message_id,
        confidence=candidate.confidence,
        status=MemoryStatus.ACTIVE.value,
        sightings=1,
        created_at=now,
        updated_at=now,
    )
    session.add(item)
    return IngestOutcome("created", "新建记忆", item)


# ---------------------------------------------------------------------------
# 召回
# ---------------------------------------------------------------------------


async def recall(
    session: AsyncSession,
    org_id: UUID,
    scope_ref_id: str | None,
    query_text: str,
    budget_chars: int = MEMORY_SECTION_MAX_CHARS,
    *,
    extra_scope_refs: Sequence[str] = (),
    max_items: int = RECALL_MAX_ITEMS,
) -> RecallResult:
    """按 scope 过滤 active 记忆 → 打分排序 → 预算内选择。

    候选集 = 全组织记忆(scope=org) + 指定 ref 的宿主作用域记忆。org 范围永远参与:
    组织级硬规则与当下落在哪个作用域无关,漏掉它就是事故。
    只读、不写、不 commit;命中计数由 record_recall_hits 单独负责。
    """
    refs = {ref for ref in (scope_ref_id, *extra_scope_refs) if ref}
    scope_filter = MemoryItem.scope == MemoryScope.ORG.value
    if refs:
        scope_filter = or_(scope_filter, MemoryItem.scope_ref_id.in_(sorted(refs)))
    rows = list(
        (
            await session.execute(
                select(MemoryItem).where(
                    MemoryItem.org_id == org_id,
                    MemoryItem.status == MemoryStatus.ACTIVE.value,
                    scope_filter,
                )
            )
        )
        .scalars()
        .all()
    )
    return rank_and_select(
        rows,
        query_text=query_text,
        budget_chars=budget_chars,
        max_items=max_items,
    )


async def record_recall_hits(
    session: AsyncSession, memory_ids: Sequence[UUID]
) -> None:
    """累加召回命中计数(best-effort 统计,不影响本轮对话)。

    单独成函数并由调用方紧接着 commit:这段写入必须在 LLM 流式调用**之前**
    结清,否则主 session 会带着一个未提交事务挂上几分钟(idle-in-transaction),
    与 _execute_turn 刻意先 commit 再进模型调用的设计冲突。
    """
    if not memory_ids:
        return
    await session.execute(
        update(MemoryItem)
        .where(MemoryItem.id.in_(list(memory_ids)))
        .values(
            hit_count=MemoryItem.hit_count + 1,
            last_hit_at=datetime.now(UTC),
            # updated_at 显式写回原值,压掉列上的 onupdate。"被召回过"不等于
            # "被更新过":让命中刷新 updated_at 会把新鲜度分喂给刚被选中的那几条,
            # 形成"越被召回越新鲜、越新鲜越被召回"的滚雪球,把预算永久锁死在同一批。
            updated_at=MemoryItem.updated_at,
        )
    )


# ---------------------------------------------------------------------------
# 抽取(turn 结束后台跑)
# ---------------------------------------------------------------------------


@dataclass
class ExtractionReport:
    """一次抽取的结果:写了什么、丢了什么、为什么丢。"""

    candidates: int = 0
    created: int = 0
    merged: int = 0
    promoted: int = 0
    dropped: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def parse_extraction_output(text: str) -> list[dict]:
    """解析抽取器输出的 JSON;裸 JSON、```json 包裹、前后带闲话都能吃下。

    宽松解析是必要的:模型偶尔会在 JSON 前后加一句"好的,以下是结果"。
    解析不出来就返回空列表(抽取失败不是错误路径,下一轮还会再抽)。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence is not None:
        raw = fence.group(1).strip()
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("记忆抽取输出不是合法 JSON,已跳过本次抽取")
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        items = payload.get("candidates")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def build_transcript(rows: Sequence[ChatMessage]) -> str:
    """把消息行压成抽取器的输入文本(与 compression._segment_text 同口径)。

    tool 行只取截断的 content:抽取面向"用户说了什么、定了什么",完整工具输出
    是数据备份不是记忆素材,喂进去只会让模型把检索结果当成用户偏好。
    """
    lines: list[str] = []
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        if row.role == "tool":
            lines.append(f"[工具 {row.tool_name}] {content[:_TRANSCRIPT_ITEM_MAX]}")
        else:
            lines.append(f"[{row.role}] {content[:_TRANSCRIPT_ITEM_MAX]}")
    return "\n".join(lines)


async def resolve_scope_refs(
    session: AsyncSession, org_id: UUID, session_id: UUID
) -> dict[str, str]:
    """取会话在各记忆范围下的 ref,形如 {"ticket": "<uuid>"}。

    转发给宿主注册的 ScopeResolver;未注册或解析失败一律返回空 dict——
    此时只有 org 范围的记忆能被沉淀与召回,对话本身不受影响。
    未注册的范围名会被丢弃并告警:让脏范围落库等于绕过 register_memory_scopes
    这道闸。
    """
    try:
        raw = await _scope_resolver(session, org_id, session_id)
    except Exception:  # noqa: BLE001 - 宿主解析失败不阻塞对话
        logger.warning("作用域解析失败,本次只用 org 范围记忆 session=%s", session_id)
        return {}
    known = known_memory_scopes()
    refs: dict[str, str] = {}
    for scope, ref in (raw or {}).items():
        name = str(scope)
        if name == MemoryScope.ORG.value:
            continue  # org 没有具体指向对象
        if name not in known:
            logger.warning("ScopeResolver 返回了未注册的记忆范围 %r,已忽略", name)
            continue
        text = str(ref or "").strip()
        if text:
            refs[name] = text
    return refs


def _extraction_system_prompt() -> str:
    """记忆抽取的 system prompt。

    延迟 import prompt 资源包(随 §5.4"prompts 模板化"单独交付);未就位时
    退化为内置的最小中性指令,抽取仍可跑,只是缺少完整的防污染清单。
    """
    try:
        from nicekit.agent.prompts.loader import render
    except ImportError:
        return _FALLBACK_EXTRACTION_PROMPT
    return render("memory_extraction")


async def extract_candidates(
    session: AsyncSession,
    org_id: UUID,
    session_id: UUID,
    llm,
) -> ExtractionReport:
    """从最近若干条消息抽取候选记忆并走管道入库。turn 结束后在后台跑。

    抽取失败(模型报错 / 输出不是 JSON / 单条候选非法)一律降级为"这次没抽到",
    绝不上抛:少沉淀一次无碍,下一个 turn 还会再抽;而让记忆抽取炸掉一次对话
    是不可接受的。
    """
    report = ExtractionReport()
    rows = list(
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.sequence.desc())
                .limit(EXTRACT_RECENT_MESSAGES)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()  # 取最近 N 条要倒序查,喂给模型时再正过来
    if len(rows) < EXTRACT_MIN_MESSAGES:
        report.skipped_reason = "消息过少,无需抽取"
        return report

    transcript = build_transcript(rows)
    if not transcript.strip():
        report.skipped_reason = "对话内容为空"
        return report

    scope_refs = await resolve_scope_refs(session, org_id, session_id)

    try:
        turn = await llm.generate_with_tools(
            task=MEMORY_TASK,
            system=_extraction_system_prompt(),
            messages=[{"role": "user", "content": transcript}],
            tools=[],
            org_id=org_id,
        )
    except Exception as exc:  # noqa: BLE001 - 抽取失败不是错误路径
        logger.warning("记忆抽取模型调用失败 session=%s: %s", session_id, exc)
        report.skipped_reason = f"模型调用失败:{exc}"
        return report

    raw_candidates = parse_extraction_output(getattr(turn, "text", None) or "")
    report.candidates = len(raw_candidates)
    last_message_id = rows[-1].id if rows else None

    for raw in raw_candidates:
        try:
            candidate = normalize_candidate(raw)
        except MemoryValidationError as exc:
            report.dropped.append(f"字段非法:{exc}")
            continue
        # 抽取器必须给原文摘录:给不出来的按脑补处理(工具写入不强制,见 screen_candidate)
        if not candidate.evidence:
            report.dropped.append(f"{candidate.title}:缺少对话原文佐证")
            continue
        scope_ref = scope_refs.get(candidate.scope)
        if candidate.scope != MemoryScope.ORG.value and not scope_ref:
            report.dropped.append(
                f"{candidate.title}:会话未绑定 {candidate.scope} 作用域,无处归属"
            )
            continue
        outcome = await ingest_candidate(
            session,
            org_id=org_id,
            candidate=candidate,
            source=f"{SOURCE_EXTRACTION}:{session_id}",
            scope_ref_id=scope_ref,
            source_message_id=last_message_id,
        )
        if outcome.action == "created":
            report.created += 1
        elif outcome.action == "merged":
            report.merged += 1
        elif outcome.action == "promoted":
            report.promoted += 1
        else:
            report.dropped.append(f"{candidate.title}:{outcome.reason}")

    await session.commit()
    logger.info(
        "记忆抽取完成 session=%s 候选=%s 新建=%s 合并=%s 提升=%s 丢弃=%s",
        session_id,
        report.candidates,
        report.created,
        report.merged,
        report.promoted,
        len(report.dropped),
    )
    return report


# fire-and-forget 任务需持强引用,否则可能在完成前被 GC(asyncio 官方告诫)
_background_tasks: set = set()


def schedule_memory_extraction(
    session_factory: async_sessionmaker,
    *,
    org_id: UUID,
    session_id: UUID,
    llm,
):
    """turn 完成后的异步触发入口(照 compression.schedule_compression 模式)。

    不阻塞响应、异常吞掉只记日志:少沉淀一次记忆无碍,下一个 turn 会再抽。
    """
    import asyncio

    async def _run() -> None:
        session = org_session(session_factory, org_id)
        try:
            await extract_candidates(session, org_id, session_id, llm)
        except Exception:
            logger.exception("记忆抽取后台任务失败 session=%s", session_id)
        finally:
            await session.close()

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
