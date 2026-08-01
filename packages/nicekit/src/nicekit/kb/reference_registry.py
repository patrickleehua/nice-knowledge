"""Shared read-only reference registry for knowledge purge planning.

通用的 JSON 递归引用扫描器(:func:`reference_kinds` + :class:`ReferenceTargets`)
留在 SDK:它只认 id 键名词表,与业务无关。业务侧的引用计数(TF 里直接 import
了 itinerary/quote/export 等表)改走 :mod:`nicekit.kb.ports` 的 ReferenceScanner
协议——无宿主注册即"无外部引用",治理链路照常推进。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb import ports
from nicekit.models.kb_feedback import KnowledgeAnswerFeedback

_DOCUMENT_ID_KEYS = frozenset({"document_id", "source_doc_id", "doc_id"})
_REVISION_ID_KEYS = frozenset({"revision_id", "source_revision_id"})
_FACT_ID_KEYS = frozenset({"fact_claim_id", "claim_id"})
_EVIDENCE_ID_KEYS = frozenset({"evidence_span_id", "evidence_id"})
_ENTITY_ID_KEYS = frozenset(
    {
        "entity_id",
        "subject_entity_id",
        "object_entity_id",
        "src_entity_id",
        "dst_entity_id",
    }
)
_IMAGE_ID_KEYS = frozenset(
    {"asset_id", "image_asset_id", "selected_asset_id", "selected_asset_ids"}
)
_SNAPSHOT_ID_KEYS = frozenset(
    {"snapshot_id", "knowledge_snapshot_id", "active_snapshot_id"}
)


@dataclass(frozen=True, slots=True)
class ReferenceTargets:
    document_ids: frozenset[str]
    revision_ids: frozenset[str]
    fact_ids: frozenset[str]
    evidence_ids: frozenset[str]
    entity_ids: frozenset[str]
    image_ids: frozenset[str]
    snapshot_ids: frozenset[str]

    @classmethod
    def from_ids(
        cls,
        *,
        document_ids: Iterable[UUID | str] = (),
        revision_ids: Iterable[UUID | str] = (),
        fact_ids: Iterable[UUID | str] = (),
        evidence_ids: Iterable[UUID | str] = (),
        entity_ids: Iterable[UUID | str] = (),
        image_ids: Iterable[UUID | str] = (),
        snapshot_ids: Iterable[UUID | str] = (),
    ) -> ReferenceTargets:
        def normalized(values: Iterable[UUID | str]) -> frozenset[str]:
            return frozenset(str(value) for value in values)

        return cls(
            document_ids=normalized(document_ids),
            revision_ids=normalized(revision_ids),
            fact_ids=normalized(fact_ids),
            evidence_ids=normalized(evidence_ids),
            entity_ids=normalized(entity_ids),
            image_ids=normalized(image_ids),
            snapshot_ids=normalized(snapshot_ids),
        )


def reference_kinds(
    value: object,
    targets: ReferenceTargets,
    *,
    parent_key: str | None = None,
) -> set[str]:
    """Return target kinds referenced by a nested JSON-compatible value."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            found.update(
                reference_kinds(child, targets, parent_key=str(key).lower())
            )
        return found
    if isinstance(value, list):
        for child in value:
            found.update(reference_kinds(child, targets, parent_key=parent_key))
        return found
    if value is None or parent_key is None:
        return found
    serialized = str(value)
    if parent_key in _DOCUMENT_ID_KEYS and serialized in targets.document_ids:
        found.add("document")
    if parent_key in _REVISION_ID_KEYS and serialized in targets.revision_ids:
        found.add("revision")
    if parent_key in _FACT_ID_KEYS and serialized in targets.fact_ids:
        found.add("fact")
    if parent_key in _EVIDENCE_ID_KEYS and serialized in targets.evidence_ids:
        found.add("evidence")
    if parent_key in _ENTITY_ID_KEYS and serialized in targets.entity_ids:
        found.add("entity")
    if parent_key in _IMAGE_ID_KEYS and serialized in targets.image_ids:
        found.add("media")
    if parent_key in _SNAPSHOT_ID_KEYS and serialized in targets.snapshot_ids:
        found.add("snapshot")
    return found


async def business_reference_counts(
    session: AsyncSession,
    *,
    org_id: UUID,
    targets: ReferenceTargets,
) -> tuple[int, int, int]:
    """外部引用计数:(检索侧, 业务侧, 其中带媒体的)。

    SDK 自身没有业务表,三个数字全部来自注册的 ReferenceScanner:
    media kind 的计数同时计入 business 与 media,其余 KB 对象计入 business;
    retrieval 维度在 SDK 内无对应物(恒为 0),宿主若需要区分可自行在
    scanner 里把检索快照登记成 ``snapshot`` 引用。
    """

    async def count(kind: str, ids: frozenset[str]) -> int:
        parsed = _as_uuids(ids)
        if not parsed:
            return 0
        counts = await ports.scan_references(
            session, org_id=org_id, kind=kind, ids=parsed
        )
        return sum(int(value) for value in counts.values())

    media_count = await count("media", targets.image_ids)
    business_count = media_count
    for kind, ids in (
        ("document", targets.document_ids),
        ("revision", targets.revision_ids),
        ("fact", targets.fact_ids),
        ("evidence", targets.evidence_ids),
        ("entity", targets.entity_ids),
        ("snapshot", targets.snapshot_ids),
    ):
        business_count += await count(kind, ids)
    return 0, business_count, media_count


def _as_uuids(values: frozenset[str]) -> list[UUID]:
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(parsed, key=str)


async def feedback_reference_count(
    session: AsyncSession,
    *,
    org_id: UUID,
    targets: ReferenceTargets,
) -> int:
    """Count feedback/citation records that retain target identifiers."""

    rows = (
        await session.execute(
            select(KnowledgeAnswerFeedback.sources).where(
                KnowledgeAnswerFeedback.org_id == org_id
            )
        )
    ).all()
    return sum(bool(reference_kinds(row[0], targets)) for row in rows)


__all__ = [
    "ReferenceTargets",
    "business_reference_counts",
    "feedback_reference_count",
    "reference_kinds",
]
