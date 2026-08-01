"""知识问答赞/踩反馈:请求体校验 + 端点行为(mock)+ 真 DB 插入(live)。"""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nicekit.api.deps import OrgContext
from nicekit.api.v1 import kb_feedback
from nicekit.api.v1.kb_feedback import AnswerFeedbackBody
from nicekit.models.kb_feedback import KnowledgeAnswerFeedback
from nicekit.models.tenancy import Role

_VALID = {
    "query": "冰岛冬季自驾要注意什么",
    "answer_text": "冬季自驾需关注路况与日照时间……[1]",
    "rating": "up",
    "sources": [
        {"ref": 1, "kind": "chunk", "layer": "tenant", "source": "服务手册.pdf"},
    ],
}


def _body(**overrides) -> dict:
    return {**_VALID, **overrides}


# ---------- 请求体校验 ----------


def test_valid_body_parses_and_defaults() -> None:
    body = AnswerFeedbackBody.model_validate(_body())
    assert body.rating == "up"
    assert body.comment is None
    assert body.sources[0].source_doc_id is None


@pytest.mark.parametrize("rating", ["like", "UP", "", None, 1])
def test_rating_must_be_up_or_down(rating) -> None:
    with pytest.raises(ValidationError):
        AnswerFeedbackBody.model_validate(_body(rating=rating))


@pytest.mark.parametrize(
    "overrides",
    [
        {"query": ""},
        {"query": "长" * 1001},
        {"answer_text": ""},
        {"comment": "长" * 501},
        {"sources": [{"kind": "chunk"}]},  # 缺 ref/layer/source
        {"sources": [{"ref": -1, "kind": "chunk", "layer": "tenant", "source": "x"}]},
        {"sources": [_VALID["sources"][0]] * 51},  # 超出快照条数上限
    ],
)
def test_invalid_bodies_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        AnswerFeedbackBody.model_validate(_body(**overrides))


def test_boundary_lengths_are_accepted() -> None:
    body = AnswerFeedbackBody.model_validate(
        _body(rating="down", query="长" * 1000, comment="长" * 500)
    )
    assert body.comment is not None and len(body.comment) == 500


# ---------- 端点行为(mock session,不落库) ----------


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


async def test_create_answer_feedback_uses_ctx_identity_not_body() -> None:
    ctx = OrgContext(user_id=uuid4(), org_id=uuid4(), role=Role.MEMBER)
    session = _mock_session()

    body = AnswerFeedbackBody.model_validate(
        _body(
            rating="down",
            comment="没有提到退款口径",
            sources=[
                {
                    "ref": 2,
                    "kind": "fact",
                    "layer": "platform",
                    "source": "签证须知",
                    "source_doc_id": str(uuid4()),
                }
            ],
        )
    )
    result = await kb_feedback.create_answer_feedback(body, ctx, session)

    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, KnowledgeAnswerFeedback)
    assert row.org_id == ctx.org_id
    assert row.user_id == ctx.user_id
    assert row.rating == "down"
    assert row.comment == "没有提到退款口径"
    # sources 存入的是最小快照 dict(exclude_none),而非 pydantic 对象
    assert row.sources == [body.sources[0].model_dump(exclude_none=True)]
    assert isinstance(result.id, UUID) and result.id == row.id


async def test_up_feedback_source_snapshot_drops_absent_doc_id() -> None:
    ctx = OrgContext(user_id=uuid4(), org_id=uuid4(), role=Role.MEMBER)
    session = _mock_session()

    body = AnswerFeedbackBody.model_validate(_body())
    await kb_feedback.create_answer_feedback(body, ctx, session)

    row = session.add.call_args.args[0]
    assert row.sources == [
        {"ref": 1, "kind": "chunk", "layer": "tenant", "source": "服务手册.pdf"}
    ]


def test_feedback_route_is_registered() -> None:
    """router 聚合器属 P4 装配阶段;这里只断言 router 自身挂载与 openapi 可展开。"""
    assert "/kb/answer/feedback" in {route.path for route in kb_feedback.router.routes}

    app = FastAPI()
    app.include_router(kb_feedback.router, prefix="/api/v1")
    assert "/api/v1/kb/answer/feedback" in app.openapi()["paths"]


# ---------- 真 DB 插入路径(默认跳过,-m live 显式运行) ----------


@pytest.mark.live
async def test_answer_feedback_persists_via_api() -> None:
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import select

    from nicekit.core.config import get_settings
    from nicekit.core.db import org_session
    from nicekit.core.security import create_access_token, hash_password
    from nicekit.models.tenancy import Membership, Organization, User

    engine = create_async_engine(get_settings().database_url, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id, user_id = uuid4(), uuid4()
    async with factory() as seed:
        seed.add(
            Organization(
                id=org_id,
                name="Answer Feedback Live",
                slug=f"answer-feedback-{org_id.hex[:8]}",
            )
        )
        seed.add(
            User(
                id=user_id,
                email=f"feedback-{user_id.hex[:8]}@nicekit-qa.dev",
                password_hash=hash_password("pw-answer-feedback"),
                full_name="feedback",
            )
        )
        await seed.flush()
        seed.add(Membership(org_id=org_id, user_id=user_id, role=Role.MEMBER))
        await seed.commit()

    app = FastAPI()
    app.include_router(kb_feedback.router, prefix="/api/v1")
    headers = {
        "Authorization": f"Bearer {create_access_token(user_id, org_id, Role.MEMBER.value)}"
    }
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/api/v1/kb/answer/feedback",
                json=_body(rating="down", comment="来源不够新"),
                headers=headers,
            )
        assert resp.status_code == 201, resp.text
        feedback_id = UUID(resp.json()["id"])

        session = org_session(factory, org_id)
        async with session:
            row = (
                await session.execute(
                    select(KnowledgeAnswerFeedback).where(
                        KnowledgeAnswerFeedback.id == feedback_id
                    )
                )
            ).scalar_one()
            assert row.org_id == org_id
            assert row.user_id == user_id
            assert row.rating == "down"
            assert row.comment == "来源不够新"
            assert row.sources[0]["ref"] == 1
            assert row.created_at is not None
    finally:
        await engine.dispose()
