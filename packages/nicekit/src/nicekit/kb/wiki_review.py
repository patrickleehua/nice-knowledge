"""Governed review actions for LLM-generated Wiki drafts."""

from typing import Literal

from nicekit.kb.wiki_gen import PAGE_DRAFT_PENDING, WIKI_PUBLICATION_STATUS_KEY
from nicekit.models.kb import FactClaim, KbPage

WikiPublicationDecision = Literal["published", "rejected"]


class WikiDraftStateError(Exception):
    """The requested page does not have a pending draft."""


def publish_wiki_draft(page: KbPage) -> None:
    if page.draft_status != PAGE_DRAFT_PENDING or not (page.draft_content or "").strip():
        raise WikiDraftStateError("页面没有待发布草稿")
    page.content = page.draft_content
    page.draft_content = None
    page.draft_status = None


def reject_wiki_draft(page: KbPage) -> None:
    if page.draft_status != PAGE_DRAFT_PENDING or page.draft_content is None:
        raise WikiDraftStateError("页面没有待处理草稿")
    page.draft_content = None
    page.draft_status = None


def record_wiki_claim_decision(
    claim: FactClaim, decision: WikiPublicationDecision
) -> None:
    """Persist a review decision in the governed payload for future snapshots."""
    if claim.predicate != "wiki_page":
        raise WikiDraftStateError("页面未关联 Wiki 事实")
    effective = (
        claim.corrected_payload
        if claim.corrected_payload is not None
        else claim.value_json
    )
    claim.corrected_payload = {
        **effective,
        WIKI_PUBLICATION_STATUS_KEY: decision,
    }
