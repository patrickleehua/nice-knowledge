"""知识问答反馈 API:AI 解答的赞/踩落库。

独立于 kb.py 成文件——反馈闭环与检索/摄入无共享逻辑,拆开可避免
与并行迭代中的 kb.py 相互牵连。任何 org 成员都可反馈(检索页对全员
开放),故只要求登录态(get_org_context),不加角色门槛。
"""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.models.kb_feedback import KnowledgeAnswerFeedback

router = APIRouter(prefix="/kb")


class AnswerFeedbackSource(BaseModel):
    """答案引用来源的最小快照——只存评测回溯所需的定位字段。"""

    ref: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=50)
    layer: str = Field(min_length=1, max_length=20)
    source: str = Field(max_length=300)
    source_doc_id: str | None = Field(default=None, max_length=64)


class AnswerFeedbackBody(BaseModel):
    """长度上限与 knowledge_answer_feedbacks 列定义一致,超限在校验层拦下。"""

    query: str = Field(min_length=1, max_length=1000)
    answer_text: str = Field(min_length=1)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)
    # 上限对齐检索返回规模,防御异常客户端灌入超大快照
    sources: list[AnswerFeedbackSource] = Field(default_factory=list, max_length=50)


class AnswerFeedbackCreated(BaseModel):
    id: UUID


@router.post(
    "/answer/feedback",
    response_model=AnswerFeedbackCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_answer_feedback(
    body: AnswerFeedbackBody,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> AnswerFeedbackCreated:
    """org_id / user_id 一律取自鉴权上下文,不信任请求体。"""
    # id 在应用侧显式生成:响应无需依赖 flush 期的列默认值,单测也无需真 DB
    row = KnowledgeAnswerFeedback(
        id=uuid4(),
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        query=body.query,
        answer_text=body.answer_text,
        rating=body.rating,
        comment=body.comment,
        sources=[source.model_dump(exclude_none=True) for source in body.sources],
    )
    session.add(row)
    await session.commit()
    return AnswerFeedbackCreated(id=row.id)
