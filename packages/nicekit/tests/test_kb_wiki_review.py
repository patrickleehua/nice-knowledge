from uuid import uuid4

import pytest

from nicekit.kb.projections import _wiki_content_state
from nicekit.kb.wiki_review import (
    WIKI_PUBLICATION_STATUS_KEY,
    WikiDraftStateError,
    publish_wiki_draft,
    record_wiki_claim_decision,
    reject_wiki_draft,
)
from nicekit.models.kb import FactClaim, KbPage


def _page() -> KbPage:
    return KbPage(
        id=uuid4(), org_id=uuid4(), kb_id=uuid4(), snapshot_id=uuid4(),
        title="巴黎交通", content="人工正文", draft_content="模型建议正文",
        draft_status="pending_review", origin="human",
    )


def _claim() -> FactClaim:
    return FactClaim(
        id=uuid4(), org_id=uuid4(), kb_id=uuid4(), subject_type="source_document",
        subject_id=uuid4(), predicate="wiki_page",
        value_json={"title": "巴黎交通", "content_markdown": "模型建议正文"},
        raw_payload={"title": "巴黎交通", "content_markdown": "模型建议正文"},
    )


def test_publish_preserves_origin_and_records_durable_decision() -> None:
    page = _page()
    claim = _claim()

    record_wiki_claim_decision(claim, "published")
    publish_wiki_draft(page)

    assert page.content == "模型建议正文"
    assert page.draft_content is None and page.draft_status is None
    assert page.origin == "human"
    assert claim.corrected_payload[WIKI_PUBLICATION_STATUS_KEY] == "published"


def test_reject_preserves_published_content_and_records_decision() -> None:
    page = _page()
    claim = _claim()

    record_wiki_claim_decision(claim, "rejected")
    reject_wiki_draft(page)

    assert page.content == "人工正文"
    assert page.draft_content is None and page.draft_status is None
    assert claim.corrected_payload[WIKI_PUBLICATION_STATUS_KEY] == "rejected"


def test_draft_actions_require_pending_draft() -> None:
    page = _page()
    page.draft_content = None
    page.draft_status = None

    with pytest.raises(WikiDraftStateError, match="待发布"):
        publish_wiki_draft(page)
    with pytest.raises(WikiDraftStateError, match="待处理"):
        reject_wiki_draft(page)


@pytest.mark.parametrize(
    ("decision", "auto_publish", "expected"),
    [
        # 自动发布开启(默认):事实审核通过即发布,不再压第二道页面级人工发布
        (None, True, ("模型建议正文", None, None)),
        # 关闭开关:回到"确认后仍需逐页点发布"的旧行为
        (None, False, (None, "模型建议正文", "pending_review")),
        # 显式人工决定永远优先于开关
        ("published", True, ("模型建议正文", None, None)),
        ("published", False, ("模型建议正文", None, None)),
        ("rejected", True, None),
        ("rejected", False, None),
    ],
)
def test_projection_respects_durable_review_decision(
    decision, auto_publish, expected, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nicekit.core.config import get_settings

    # settings 是 lru_cache 单例,patch 实例属性(类属性会被实例值遮蔽)
    monkeypatch.setattr(get_settings(), "kb_wiki_auto_publish", auto_publish)
    payload = {"content_markdown": "模型建议正文"}
    if decision is not None:
        payload[WIKI_PUBLICATION_STATUS_KEY] = decision

    assert _wiki_content_state(payload) == expected
