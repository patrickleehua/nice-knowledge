"""长期记忆表(memory_items):从对话沉淀的偏好 / 约束 / 决策。

为什么单独一张表而不复用 chat_session_summaries:摘要是"某次会话的替身",
随会话生灭;记忆是跨会话、跨作用域复用的组织资产,生命周期与单条会话无关,
还要能被人工审阅、纠正与删除。二者的读写路径与治理要求都不一样。

三条硬纪律:
- **agent 输出不直接写库**:抽取产物与 memory_write 工具一律先过
  nicekit/agent/memory.py 的校验管道(防污染 → 去重 → 打分 → 提升);
- **候选与正式分层**:新沉淀先落 preference_candidate,同一事实重复出现
  且置信达标才提升为 preference,单次表态不足以成为长期偏好;
- **软删除**:否定一条记忆是置 status=rejected 而非物理删,来源与判断痕迹
  必须留存,否则同一条脏记忆会被下一次抽取原样再写一遍。

**scope 泛化**(MIGRATION-PLAN §4 ScopeResolver):TF 的 org/project/customer
三档词表收敛为内置 `org` + 宿主注册的任意作用域名(register_memory_scopes())。
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from nicekit.core.config import get_settings

# 记忆向量维度。与 KB 侧 embedding 维度同源(models/kb.py::EMBEDDING_DIM 读同一
# 配置项):换 embedding 模型时两边一起变并重建向量,不留两套口径。
# 刻意不 import kb 模型——memory 属于 agent 子系统,不该为一个常量把 KB 全部表
# 拖进 agent 的 import 图(TF models/__init__ 捆绑全库的教训),因此这里复述
# 同一个配置读取而不是转引常量。
MEMORY_EMBEDDING_DIM: int = int(getattr(get_settings(), "kb_embedding_dim", 1024))


class MemoryScope(StrEnum):
    """内置记忆范围。宿主的业务范围经 register_memory_scopes() 追加。"""

    ORG = "org"  # 全组织通用


# scope 落库 varchar(20):小写字母开头,字母/数字/下划线
_SCOPE_MAX_LENGTH = 20
_BUILTIN_SCOPES: frozenset[str] = frozenset({MemoryScope.ORG.value})
_registered_scopes: set[str] = set()


class MemoryScopeError(ValueError):
    """记忆范围不在内置词表且未被宿主注册。"""


def register_memory_scopes(*names: str) -> None:
    """启动期注册宿主的记忆范围(幂等)。

    范围名同时是 scope 列的落库值,因此受长度与字符集约束;scope_ref_id 的
    含义由宿主自行定义(通常是该范围内某个对象的标识)。
    """
    for name in names:
        if name in _BUILTIN_SCOPES:
            continue
        if (
            not name
            or len(name) > _SCOPE_MAX_LENGTH
            or not name[0].isalpha()
            or not name.replace("_", "").isalnum()
            or name != name.lower()
        ):
            raise MemoryScopeError(f"非法记忆范围名 {name!r}")
        _registered_scopes.add(name)


def known_memory_scopes() -> frozenset[str]:
    """内置 + 宿主注册的全部记忆范围。"""
    return _BUILTIN_SCOPES | frozenset(_registered_scopes)


def normalize_memory_scope(value: object) -> str:
    """校验并归一化一个记忆范围值;未知范围直接报错(fail-closed)。"""
    name = str(value or "").strip()
    if name not in known_memory_scopes():
        raise MemoryScopeError(
            f"未知记忆范围 {name!r};已知范围:{sorted(known_memory_scopes())}"
        )
    return name


def reset_registered_memory_scopes() -> None:
    """清空宿主注册的范围(测试 / 重新装配用;不影响内置范围)。"""
    _registered_scopes.clear()


class MemoryType(StrEnum):
    """记忆类型。preference_candidate 是唯一的"未定级"类型,其余均为正式记忆。"""

    PREFERENCE = "preference"  # 已确立的稳定偏好
    PREFERENCE_CANDIDATE = "preference_candidate"  # 偏好信号,尚未达到提升门槛
    CONSTRAINT = "constraint"  # 硬约束(预算上限/时间窗/禁忌)
    DECISION = "decision"  # 已确认的决策
    FACT = "fact"  # 有来源的客观事实
    RISK = "risk"  # 风险提示


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # 被更新的同主题记忆取代
    REJECTED = "rejected"  # 人工或 agent 判定错误/失效,永不再召回


class MemoryItem(SQLModel, table=True):
    """一条长期记忆。

    scope_ref_id 用文本而非 UUID:宿主的作用域标识不一定是 UUID(可能是外部
    系统的编号或复合键),用 UUID 列会把一部分范围直接堵死。org 范围留空。

    hit_count / last_hit_at 是召回侧的反馈信号:被反复命中的记忆更可能真的有用,
    长期零命中的记忆是人工清理的第一批候选。它们由 recall 异步累加,不参与排序
    的主项(否则先被召回的会滚雪球式霸占预算)。
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        # 召回的主查询形状:定 org → 定 scope 与其 ref → 只要 active
        Index(
            "ix_memory_items_scope_lookup",
            "org_id",
            "scope",
            "scope_ref_id",
            "status",
        ),
    )

    id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    )
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True)
    )
    scope: str = Field(
        default=MemoryScope.ORG.value,
        sa_column=Column(String(_SCOPE_MAX_LENGTH), nullable=False),
    )
    scope_ref_id: str | None = Field(
        default=None, sa_column=Column(String(200), nullable=True)
    )
    memory_type: str = Field(
        default=MemoryType.PREFERENCE_CANDIDATE.value,
        sa_column=Column(String(30), nullable=False),
    )
    title: str = Field(sa_column=Column(String(200), nullable=False))
    content: str = Field(sa_column=Column(Text, nullable=False))
    # 来源:会话 id 字符串或写入工具名(memory_write / memory_extraction)。
    # 无来源的记忆不允许存在——排障与人工复核都要能回到原始对话。
    source: str = Field(sa_column=Column(String(200), nullable=False))
    source_message_id: UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    confidence: float = Field(
        default=0.5, sa_column=Column(Float, nullable=False, server_default=text("0.5"))
    )
    status: str = Field(
        default=MemoryStatus.ACTIVE.value,
        sa_column=Column(String(20), nullable=False, server_default=text("'active'")),
    )
    hit_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    # 这条事实被独立观察到的次数(每次去重合并 +1)。候选提升规则要求"同一事实
    # 重复出现 N 次",没有计数列就只能靠 confidence 反推,而 confidence 会被
    # 单次强表态拉满——两个信号必须分开存,否则"稳定偏好"这道闸形同虚设。
    sightings: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default=text("1"))
    )
    last_hit_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # 语义召回的可选加速:embedding 服务未配置时保持 NULL,召回自动退回关键词打分。
    embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(MEMORY_EMBEDDING_DIM), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=text("now()")
        ),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
            # onupdate 必须是客户端可知值:服务端表达式会让属性在 UPDATE 后过期,
            # commit 后序列化访问触发同步惰性刷新 → async 下 MissingGreenlet
            onupdate=lambda: datetime.now(UTC),
        ),
    )
