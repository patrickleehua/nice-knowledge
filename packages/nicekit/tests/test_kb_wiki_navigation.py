import pytest
from pydantic import ValidationError

from nicekit.api.v1.kb import WikiNavigationBody
from nicekit.models.kb import KnowledgeBase


def test_wiki_navigation_serializes_stable_page_keys_for_jsonb() -> None:
    page_keys = [
        'snapshot:["destination","伦敦参考酒店"]',
        "page:598a0137-a489-50e5-acd1-25ef8a12f519",
    ]

    navigation = WikiNavigationBody(page_order=page_keys)

    assert navigation.model_dump(mode="json") == {"page_order": page_keys}
    assert "wiki_navigation" in KnowledgeBase.__table__.columns


def test_wiki_navigation_rejects_duplicate_pages() -> None:
    page_key = 'snapshot:["overview","知识库总览"]'

    with pytest.raises(ValidationError, match="不能包含重复页面"):
        WikiNavigationBody(page_order=[page_key, page_key])


@pytest.mark.parametrize("page_key", ["", "x" * 701])
def test_wiki_navigation_rejects_invalid_page_keys(page_key: str) -> None:
    with pytest.raises(ValidationError, match="无效页面标识"):
        WikiNavigationBody(page_order=[page_key])
