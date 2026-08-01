"""知识问答反馈表。

知识检索的 AI 解答(/kb/answer)是即答即走的流式输出,不落库;
要评测答案质量只能靠用户当场的赞/踩。因此反馈行自带 query/answer_text/
sources 快照——不依赖任何会话或消息外键,答案哪怕再也无法复现,
运营与后续评测仍能拿到完整上下文。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


class KnowledgeAnswerFeedback(SQLModel, table=True):
    """AI 答案的赞/踩反馈:rating = up/down;down 时 comment 可附一句话原因。

    sources = 答案引用来源的最小快照 [{ref, kind, layer, source, source_doc_id?}],
    只存定位所需字段而非完整命中——反馈表是评测数据集的原料,轻量优先。
    user_id/org_id 一律取自鉴权上下文(不信任请求体);不加用户外键,
    反馈作为评测数据独立于账号生命周期留存。
    """

    __tablename__ = "knowledge_answer_feedbacks"
    __table_args__ = (
        CheckConstraint("rating IN ('up', 'down')", name="ck_knowledge_answer_feedbacks_rating"),
    )

    id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4))
    org_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False, index=True))
    user_id: UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    query: str = Field(sa_column=Column(String(1000), nullable=False))
    answer_text: str = Field(sa_column=Column(Text, nullable=False))
    rating: str = Field(sa_column=Column(String(8), nullable=False))
    comment: str | None = Field(default=None, sa_column=Column(String(500), nullable=True))
    sources: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
