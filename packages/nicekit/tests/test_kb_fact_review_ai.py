"""事实 AI 审核单测:三档裁决、无证据强制 escalate、失败不落状态。"""

from uuid import uuid4

import pytest

from nicekit.kb import fact_review_ai as module
from nicekit.kb.fact_review_ai import (
    FactReviewVerdict,
    ai_review_fact_claims,
)
from nicekit.models.kb import FactClaim, FactReviewStatus

pytestmark = pytest.mark.asyncio


def _claim(predicate: str = "cost", name: str = "卢浮宫") -> FactClaim:
    return FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="source_document",
        subject_id=uuid4(),
        predicate=predicate,
        value_json={"name": name, "unit_cost": 22},
        raw_payload={"name": name, "unit_cost": 22},
        confidence=0.9,
    )


class _Span:
    def __init__(self, claim: FactClaim):
        self.fact_claim_id = claim.id
        self.quote_text = f"{claim.value_json['name']}门票 22 欧元/人"


class _Session:
    def __init__(self, claims, entity_types=None):
        self._claims = claims
        self._entity_types = entity_types or []
        self.added = []
        self.flushed = False
        self.no_evidence = False

    async def execute(self, statement):
        text = str(statement)
        if "kb_entity_types" in text:
            rows = list(self._entity_types)
        elif "evidence_spans" in text:
            rows = [] if self.no_evidence else [_Span(claim) for claim in self._claims]
        else:
            rows = list(self._claims)

        class _Result:
            def scalars(self):
                class _All:
                    def all(self):
                        return rows

                    def __iter__(self):
                        return iter(rows)

                return _All()

        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


class _VerdictLLM:
    def __init__(self, verdicts: dict[str, FactReviewVerdict]):
        self._verdicts = verdicts
        self.calls = 0

    async def generate_structured(self, *, task, messages, output_model, org_id):
        assert task == "kb.review.judge"
        self.calls += 1
        content = messages[0]["content"]
        for key, verdict in self._verdicts.items():
            if key in content:
                return verdict
        raise AssertionError("unexpected claim content")


async def test_three_way_verdicts_update_status_with_audit() -> None:
    confirm = _claim(name="卢浮宫")
    reject = _claim(name="凡尔赛宫")
    escalate = _claim(name="埃菲尔铁塔")
    llm = _VerdictLLM(
        {
            "卢浮宫": FactReviewVerdict(action="confirm", reason="票价与引文一致"),
            "凡尔赛宫": FactReviewVerdict(action="reject", reason="引文不含该实体"),
            "埃菲尔铁塔": FactReviewVerdict(action="escalate", reason="引文数字有歧义"),
        }
    )
    session = _Session([confirm, reject, escalate])

    summary = await ai_review_fact_claims(
        session,  # type: ignore[arg-type]
        org_id=uuid4(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert (summary.confirmed, summary.rejected, summary.escalated) == (1, 1, 1)
    assert confirm.review_status == FactReviewStatus.CONFIRMED
    assert reject.review_status == FactReviewStatus.REJECTED
    assert escalate.review_status == FactReviewStatus.SUGGESTED
    assert summary.escalated_ids == [str(escalate.id)]
    for claim in (confirm, reject, escalate):
        assert claim.reviewed_by == "ai:kb.review.judge"
        assert claim.review_note
    assert session.flushed


async def test_missing_evidence_escalates_without_llm_call() -> None:
    claim = _claim()
    llm = _VerdictLLM({})
    session = _Session([claim])
    session.no_evidence = True

    summary = await ai_review_fact_claims(
        session,  # type: ignore[arg-type]
        org_id=uuid4(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert llm.calls == 0
    assert summary.escalated == 1
    assert claim.review_status == FactReviewStatus.SUGGESTED
    assert "无有效证据" in (claim.review_note or "")


async def test_llm_failure_leaves_claim_untouched() -> None:
    claim = _claim()

    class _BoomLLM:
        async def generate_structured(self, **_kwargs):
            raise RuntimeError("provider down")

    session = _Session([claim])
    summary = await ai_review_fact_claims(
        session,  # type: ignore[arg-type]
        org_id=uuid4(),
        llm=_BoomLLM(),  # type: ignore[arg-type]
    )

    assert summary.failed == 1
    assert claim.review_status == FactReviewStatus.SUGGESTED
    assert claim.reviewed_by is None


async def test_limit_validation() -> None:
    with pytest.raises(ValueError):
        await ai_review_fact_claims(
            _Session([]),  # type: ignore[arg-type]
            org_id=uuid4(),
            limit=0,
        )


def test_prompt_seed_contains_review_judge() -> None:
    from nicekit.kb.prompts_seed import KB_EXTRACT_PROMPTS

    version, content = KB_EXTRACT_PROMPTS["kb.review.judge"]
    assert version >= 1
    assert "escalate" in content and "confirm" in content and "reject" in content
    assert module._REVIEWED_BY == "ai:kb.review.judge"


async def test_review_policy_auto_and_human_routing() -> None:
    """类型 review_policy 三档:auto 直接确认(不调 LLM),human 强制留人工。"""
    from nicekit.models.kb import KbEntityType

    org_id = uuid4()
    auto_claim = _claim(predicate="fleet_price", name="车队A")
    human_claim = _claim(predicate="visa_fee", name="签证费")
    types = [
        KbEntityType(
            org_id=org_id, type_key="fleet_price", display_name="车队价格",
            field_schema={"type": "object", "properties": {"name": {"type": "string"}},
                          "required": ["name"]},
            review_policy="auto",
        ),
        KbEntityType(
            org_id=org_id, type_key="visa_fee", display_name="签证费",
            field_schema={"type": "object", "properties": {"name": {"type": "string"}},
                          "required": ["name"]},
            review_policy="human",
        ),
    ]

    class _NeverLLM:
        async def generate_structured(self, **_kwargs):
            raise AssertionError("auto/human 档不应调用 LLM")

    session = _Session([auto_claim, human_claim], entity_types=types)
    summary = await ai_review_fact_claims(
        session,  # type: ignore[arg-type]
        org_id=org_id,
        llm=_NeverLLM(),  # type: ignore[arg-type]
    )

    assert summary.confirmed == 1 and summary.escalated == 1
    assert auto_claim.review_status == FactReviewStatus.CONFIRMED
    assert "自动确认" in (auto_claim.review_note or "")
    assert human_claim.review_status == FactReviewStatus.SUGGESTED
    assert "人工必审" in (human_claim.review_note or "")
