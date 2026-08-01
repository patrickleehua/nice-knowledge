"""Canonical entity aliases, claim binding, and transactional merges."""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.kb import CanonicalEntity, EntityAlias, FactClaim, KnowledgeBase

_DEFAULT_LOCALE = "und"


class EntityResolutionError(ValueError):
    """Base error for canonical entity operations."""


class EntityNotFoundError(EntityResolutionError):
    """Raised when an entity, alias, or claim is not visible."""


class EntityConflictError(EntityResolutionError):
    """Raised when an operation would make resolution ambiguous."""


@dataclass(frozen=True, slots=True)
class MergeResult:
    survivor: CanonicalEntity
    aliases_moved: int
    claims_redirected: int


def _display_text(value: str, *, field: str, max_length: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    collapsed = " ".join(normalized.split())
    if not collapsed:
        raise EntityConflictError(f"{field} must be non-empty")
    if len(collapsed) > max_length:
        raise EntityConflictError(f"{field} exceeds {max_length} characters")
    return collapsed


def normalize_alias(value: str) -> str:
    """Return the deterministic KB-local alias lookup key."""

    return _display_text(value, field="alias", max_length=300).casefold()


def normalize_locale(value: str) -> str:
    return _display_text(value, field="locale", max_length=35).lower()


async def add_entity_alias(
    session: AsyncSession,
    *,
    entity: CanonicalEntity,
    alias: str,
    locale: str = _DEFAULT_LOCALE,
    source: str = "human",
) -> EntityAlias:
    display_alias = _display_text(alias, field="alias", max_length=300)
    normalized_alias = normalize_alias(display_alias)
    normalized_locale = normalize_locale(locale)
    normalized_source = _display_text(source, field="source", max_length=50)
    existing = await session.scalar(
        select(EntityAlias).where(
            EntityAlias.kb_id == entity.kb_id,
            EntityAlias.normalized_alias == normalized_alias,
            EntityAlias.locale == normalized_locale,
        )
    )
    if existing is not None:
        if existing.entity_id == entity.id:
            return existing
        raise EntityConflictError("alias already resolves to another entity")
    row = EntityAlias(
        id=uuid4(),
        org_id=entity.org_id,
        kb_id=entity.kb_id,
        entity_id=entity.id,
        alias=display_alias,
        normalized_alias=normalized_alias,
        locale=normalized_locale,
        source=normalized_source,
    )
    session.add(row)
    await session.flush()
    return row


async def create_canonical_entity(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    entity_type: str,
    canonical_name: str,
    metadata: dict | None = None,
) -> CanonicalEntity:
    row = CanonicalEntity(
        id=uuid4(),
        org_id=org_id,
        kb_id=kb_id,
        entity_type=_display_text(entity_type, field="entity_type", max_length=50),
        canonical_name=_display_text(canonical_name, field="canonical_name", max_length=300),
        metadata_=metadata or {},
    )
    session.add(row)
    await session.flush()
    await add_entity_alias(
        session,
        entity=row,
        alias=row.canonical_name,
        source="canonical",
    )
    return row


async def rename_canonical_entity(
    session: AsyncSession,
    *,
    entity: CanonicalEntity,
    canonical_name: str,
) -> CanonicalEntity:
    new_name = _display_text(canonical_name, field="canonical_name", max_length=300)
    if new_name == entity.canonical_name:
        return entity
    old_normalized = normalize_alias(entity.canonical_name)
    new_alias = await add_entity_alias(
        session,
        entity=entity,
        alias=new_name,
        source="canonical",
    )
    old_alias = await session.scalar(
        select(EntityAlias).where(
            EntityAlias.entity_id == entity.id,
            EntityAlias.normalized_alias == old_normalized,
            EntityAlias.locale == _DEFAULT_LOCALE,
        )
    )
    if old_alias is not None and old_alias.id != new_alias.id:
        old_alias.source = "former_canonical"
        session.add(old_alias)
    new_alias.source = "canonical"
    entity.canonical_name = new_name
    session.add_all([entity, new_alias])
    await session.flush()
    return entity


async def bind_claim_to_entity(
    session: AsyncSession,
    *,
    claim_id: UUID,
    entity_id: UUID,
) -> FactClaim:
    entity = await session.scalar(
        select(CanonicalEntity).where(CanonicalEntity.id == entity_id).with_for_update()
    )
    claim = await session.scalar(
        select(FactClaim).where(FactClaim.id == claim_id).with_for_update()
    )
    if entity is None or claim is None:
        raise EntityNotFoundError("entity or fact claim not found")
    validate_entity_binding(claim=claim, entity=entity)
    claim.subject_entity_id = entity.id
    session.add(claim)
    await session.flush()
    return claim


def validate_entity_binding(
    *,
    claim: FactClaim,
    entity: CanonicalEntity,
) -> None:
    if (claim.org_id, claim.kb_id) != (entity.org_id, entity.kb_id):
        raise EntityConflictError("fact claim and entity must belong to the same KB")
    if claim.predicate != entity.entity_type:
        raise EntityConflictError("fact predicate and entity type must match")


async def delete_entity_alias(
    session: AsyncSession,
    *,
    alias_id: UUID,
) -> None:
    alias = await session.scalar(
        select(EntityAlias).where(EntityAlias.id == alias_id).with_for_update()
    )
    if alias is None:
        raise EntityNotFoundError("entity alias not found")
    if alias.source == "canonical":
        raise EntityConflictError("the canonical name alias cannot be deleted")
    await session.delete(alias)


async def merge_entities(
    session: AsyncSession,
    *,
    source_id: UUID,
    target_id: UUID,
    actor_id: UUID | None = None,
    reason: str = "manual entity merge",
) -> MergeResult:
    if source_id == target_id:
        raise EntityConflictError("an entity cannot be merged into itself")
    if actor_id is None:
        raise EntityConflictError("entity merge requires an actor")
    merge_reason = _display_text(reason, field="reason", max_length=500)
    ids = sorted((source_id, target_id), key=str)
    rows = list(
        (
            await session.execute(
                select(CanonicalEntity)
                .where(CanonicalEntity.id.in_(ids))
                .order_by(CanonicalEntity.id)
                .with_for_update()
            )
        ).scalars()
    )
    by_id = {row.id: row for row in rows}
    source = by_id.get(source_id)
    target = by_id.get(target_id)
    if source is None or target is None:
        raise EntityNotFoundError("source or target entity not found")
    if (source.org_id, source.kb_id) != (target.org_id, target.kb_id):
        raise EntityConflictError("entities must belong to the same KB")
    if source.entity_type != target.entity_type:
        raise EntityConflictError("entities must have the same type")
    if source.merged_into_entity_id is not None:
        if source.merged_into_entity_id == target.id:
            return MergeResult(
                survivor=target,
                aliases_moved=0,
                claims_redirected=0,
            )
        raise EntityConflictError("source entity has already been merged")
    if target.merged_into_entity_id is not None:
        raise EntityConflictError("target entity has already been merged")

    # Preflight every redirect before mutating aliases, pins, or claims. Replacing
    # either endpoint with the survivor must never create a relationship self-loop.
    self_loop = await session.scalar(
        select(FactClaim.id)
        .where(
            FactClaim.org_id == source.org_id,
            FactClaim.kb_id == source.kb_id,
            or_(
                (FactClaim.subject_entity_id == source.id)
                & (FactClaim.object_entity_id == target.id),
                (FactClaim.subject_entity_id == target.id)
                & (FactClaim.object_entity_id == source.id),
            ),
        )
        .limit(1)
        .with_for_update()
    )
    if self_loop is not None:
        raise EntityConflictError(
            f"entity merge would create a self-loop for fact claim {self_loop}"
        )

    aliases = list(
        (
            await session.execute(
                select(EntityAlias)
                .where(EntityAlias.entity_id == source.id)
                .order_by(EntityAlias.id)
                .with_for_update()
            )
        ).scalars()
    )
    redirected_claim_ids = set(
        (
            await session.execute(
                select(FactClaim.id)
                .where(
                    FactClaim.org_id == source.org_id,
                    FactClaim.kb_id == source.kb_id,
                    or_(
                        FactClaim.subject_entity_id == source.id,
                        FactClaim.object_entity_id == source.id,
                        (
                            FactClaim.subject_type.in_(("canonical_entity", source.entity_type))
                            & (FactClaim.subject_id == source.id)
                        ),
                    ),
                )
                .order_by(FactClaim.id)
                .with_for_update()
            )
        ).scalars()
    )

    for alias in aliases:
        alias.entity_id = target.id
        if alias.source == "canonical":
            alias.source = "merged_canonical"
        session.add(alias)

    await session.execute(
        update(FactClaim)
        .where(
            FactClaim.org_id == source.org_id,
            FactClaim.kb_id == source.kb_id,
            FactClaim.subject_entity_id == source.id,
        )
        .values(subject_entity_id=target.id)
    )
    await session.execute(
        update(FactClaim)
        .where(
            FactClaim.org_id == source.org_id,
            FactClaim.kb_id == source.kb_id,
            FactClaim.object_entity_id == source.id,
        )
        .values(object_entity_id=target.id)
    )
    await session.execute(
        update(FactClaim)
        .where(
            FactClaim.org_id == source.org_id,
            FactClaim.kb_id == source.kb_id,
            FactClaim.subject_type.in_(("canonical_entity", source.entity_type)),
            FactClaim.subject_id == source.id,
        )
        .values(subject_id=target.id)
    )

    if source.is_pinned and not target.is_pinned:
        target.is_pinned = True
        target.pinned_at = source.pinned_at
        target.pinned_by = source.pinned_by
        target.pin_reason = source.pin_reason
    source.is_pinned = False
    source.pinned_at = None
    source.pinned_by = None
    source.pin_reason = None

    now = datetime.now(UTC)
    source.merged_into_entity_id = target.id
    source.merged_at = now
    source.merged_by = actor_id
    source.merge_reason = merge_reason
    source.support_status = "unsupported"
    source.support_status_reason = "merged_into_survivor"
    source.support_status_changed_at = now
    source.support_status_snapshot_id = None
    source.unsupported_at = now
    session.add_all([source, target])
    await session.execute(
        update(KnowledgeBase)
        .where(
            KnowledgeBase.id == source.kb_id,
            KnowledgeBase.org_id == source.org_id,
        )
        .values(consumption_epoch=KnowledgeBase.consumption_epoch + 1)
    )
    await session.flush()
    return MergeResult(
        survivor=target,
        aliases_moved=len(aliases),
        claims_redirected=len(redirected_claim_ids),
    )


__all__ = [
    "EntityConflictError",
    "EntityNotFoundError",
    "EntityResolutionError",
    "MergeResult",
    "add_entity_alias",
    "bind_claim_to_entity",
    "create_canonical_entity",
    "delete_entity_alias",
    "merge_entities",
    "normalize_alias",
    "normalize_locale",
    "rename_canonical_entity",
    "validate_entity_binding",
]
