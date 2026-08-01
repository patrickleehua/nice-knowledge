"""抽取事实 AI 审核(AI 确认三档之自动档,20260714 方案第五节)。

对 suggested FactClaim 逐条对照证据引文做 LLM 裁决:
- confirm:字段值在引文中有直接依据 → 置 confirmed(带 reviewed_by/review_note 审计);
- reject:与引文矛盾/引文不支撑 → 置 rejected;
- escalate:引文不完整/有歧义/LLM 拿不准 → 保持 suggested 留人工,写明原因。
无有效证据的事实不送 LLM,直接 escalate(检索侧"确认必须有证据"门禁的前置镜像)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.entity_binding import bind_claim_entities, is_bindable
from nicekit.llm.service import LLMService, get_llm_service
from nicekit.models.kb import (
    DocumentRevision,
    EntityReviewPolicy,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    KbEntityType,
    RevisionStatus,
)

logger = logging.getLogger(__name__)

_REVIEW_TASK = "kb.review.judge"
_REVIEWED_BY = f"ai:{_REVIEW_TASK}"
_MAX_QUOTE_CHARS = 1200
_CONCURRENCY = 4


class FactReviewVerdict(BaseModel):
    action: Literal["confirm", "reject", "escalate"]
    reason: str = Field(min_length=2, max_length=300)


@dataclass
class AiReviewSummary:
    reviewed: int = 0
    confirmed: int = 0
    rejected: int = 0
    escalated: int = 0
    failed: int = 0
    escalated_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "reviewed": self.reviewed,
            "confirmed": self.confirmed,
            "rejected": self.rejected,
            "escalated": self.escalated,
            "failed": self.failed,
            "escalated_ids": self.escalated_ids,
        }


async def _valid_evidence(
    session: AsyncSession, claims: list[FactClaim]
) -> dict[UUID, list[EvidenceSpan]]:
    """confirm 门禁同款口径:staged/active revision 上的证据才算有效。"""
    if not claims:
        return {}
    rows = (
        await session.execute(
            select(EvidenceSpan)
            .join(DocumentRevision, DocumentRevision.id == EvidenceSpan.revision_id)
            .where(
                EvidenceSpan.fact_claim_id.in_([claim.id for claim in claims]),
                DocumentRevision.tombstoned_at.is_(None),
                DocumentRevision.status.in_(
                    (RevisionStatus.STAGED.value, RevisionStatus.ACTIVE.value)
                ),
            )
        )
    ).scalars()
    evidence_by_claim: dict[UUID, list[EvidenceSpan]] = {}
    for span in rows:
        evidence_by_claim.setdefault(span.fact_claim_id, []).append(span)
    return evidence_by_claim


async def _review_policies(
    session: AsyncSession, org_id: UUID, claims: list[FactClaim]
) -> dict[str, str]:
    """predicate → 类型 review_policy(本 org 定义优先,缺省 ai)。"""
    predicates = {claim.predicate for claim in claims}
    if not predicates:
        return {}
    rows = (
        (
            await session.execute(
                select(KbEntityType).where(KbEntityType.type_key.in_(predicates))
            )
        )
        .scalars()
        .all()
    )
    policies: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: r.org_id == org_id):  # 本 org 排后 → 覆盖内置
        policies[row.type_key] = row.review_policy
    return policies


def _review_message(claim: FactClaim, evidence: list[EvidenceSpan]) -> str:
    payload = claim.corrected_payload or claim.value_json
    quotes = "\n---\n".join(span.quote_text[:_MAX_QUOTE_CHARS] for span in evidence[:3])
    return (
        f"事实类型: {claim.predicate}\n"
        f"抽取置信度: {claim.confidence}\n"
        "待审事实字段(UNTRUSTED_CONTENT):\n"
        f"<<<\n{json.dumps(payload, ensure_ascii=False)}\n>>>\n"
        "原文证据引文(UNTRUSTED_CONTENT):\n"
        f"<<<\n{quotes}\n>>>"
    )


async def ai_review_fact_claims(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID | None = None,
    limit: int = 200,
    llm: LLMService | None = None,
    unreviewed_only: bool = False,
) -> AiReviewSummary:
    """批量 AI 审核 suggested 事实;调用方负责 commit。

    unreviewed_only=True 只挑 reviewed_by 为空的 claim(定时 sweep 用:已
    escalate 留人工的不重复送审,幂等且不重复烧 LLM);手动端点保持默认
    False,允许人点按钮对 escalated 重审。仅收窄选取范围,裁决逻辑不变。
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    statement = (
        select(FactClaim)
        .where(FactClaim.review_status == FactReviewStatus.SUGGESTED.value)
        .order_by(FactClaim.created_at, FactClaim.id)
        .limit(limit)
    )
    if kb_id is not None:
        statement = statement.where(FactClaim.kb_id == kb_id)
    if unreviewed_only:
        statement = statement.where(FactClaim.reviewed_by.is_(None))  # type: ignore[union-attr]
    claims = list((await session.execute(statement)).scalars().all())
    summary = AiReviewSummary(reviewed=len(claims))
    if not claims:
        return summary

    evidence_by_claim = await _valid_evidence(session, claims)
    policies = await _review_policies(session, org_id, claims)
    service = llm or get_llm_service()
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def _judge(claim: FactClaim) -> tuple[FactClaim, FactReviewVerdict | None]:
        evidence = evidence_by_claim.get(claim.id, [])
        if not evidence:
            return claim, FactReviewVerdict(
                action="escalate", reason="无有效证据(revision 非 staged/active),留人工"
            )
        policy = policies.get(claim.predicate, EntityReviewPolicy.AI.value)
        if policy == EntityReviewPolicy.HUMAN.value:
            return claim, FactReviewVerdict(
                action="escalate", reason="该类型配置为人工必审(review_policy=human)"
            )
        if policy == EntityReviewPolicy.AUTO.value:
            # 自动档:摄入时已过类型 schema 强校验 + 有有效证据 → 直接确认
            return claim, FactReviewVerdict(
                action="confirm", reason="类型配置为自动确认(schema 校验通过且证据有效)"
            )
        async with semaphore:
            try:
                verdict = await service.generate_structured(
                    task=_REVIEW_TASK,
                    messages=[{"role": "user", "content": _review_message(claim, evidence)}],
                    output_model=FactReviewVerdict,
                    org_id=org_id,
                )
            except Exception as exc:
                logger.warning(
                    "fact_review_ai judge failed claim=%s error=%s", claim.id, type(exc).__name__
                )
                return claim, None
        return claim, verdict

    results = await asyncio.gather(*(_judge(claim) for claim in claims))
    for claim, verdict in results:
        if verdict is None:
            summary.failed += 1
            continue
        if verdict.action == "confirm" and is_bindable(claim):
            # 实体/关系事实通过判定后才归一绑定 canonical entity;绑不上就留人工,
            # 未绑定的关系事实会让 graph 投影直接失败,不能放进 confirmed
            failure = await bind_claim_entities(session, claim, org_id=org_id)
            if failure is not None:
                verdict = FactReviewVerdict(action="escalate", reason=failure[:300])
        if verdict.action == "confirm":
            claim.review_status = FactReviewStatus.CONFIRMED
            summary.confirmed += 1
        elif verdict.action == "reject":
            claim.review_status = FactReviewStatus.REJECTED
            summary.rejected += 1
        else:
            summary.escalated += 1
            summary.escalated_ids.append(str(claim.id))
        claim.reviewed_by = _REVIEWED_BY
        claim.review_note = verdict.reason[:1000]
        session.add(claim)
    await session.flush()
    return summary
