"""Snapshot materializer for revision-scoped sparse and dense retrieval rows."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb import storage
from nicekit.kb.embedding import (
    EmbeddingService,
    chunk_context_text,
    chunk_embedding_text,
    embedding_content_hash,
)
from nicekit.kb.entity_types import render_entity_card
from nicekit.kb.guardrails import suspicious_instruction_reasons
from nicekit.kb.projections import (
    ProjectionPayloadError,
    _effective_payload,
    card_source_ref,
    load_custom_entity_types,
    projection_row_id,
)
from nicekit.llm.capability_routes import provider_model_endpoint
from nicekit.models.kb import (
    EMBEDDING_DIM,
    DocType,
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    IngestRun,
    IngestRunStatus,
    KbChunk,
    KbChunkEmbedding,
    KbImageAsset,
    KnowledgeBase,
    KnowledgeSnapshot,
    SourceDocument,
)

if TYPE_CHECKING:
    from nicekit.kb.snapshot import SnapshotBuildContext

_CHUNK_FIELDS = frozenset(
    {
        "chunk_index",
        "content",
        "embedding",
        "embedding_model",
        "end_line",
        "heading_path",
        "meta",
        "page",
        "quarantined",
        "source_ref",
        "start_line",
    }
)
# 非实体类型的建卡谓词(实体类型 key 在构建时由 KbEntityType 注册表动态叠加)
_ENTITY_CARD_PREDICATES = frozenset({"wiki_page"})
# 未注册类型的兜底卡片字段(领域无关的通用字段名);已注册类型一律走
# entity_types.render_entity_card,不经过这里。
_ENTITY_CARD_FIELDS = (
    "name",
    "title",
    "summary",
    "description",
    "category",
    "notes",
    "content_markdown",
)
_CITATION_REFS_KEY = "citation_refs"
_IMAGE_CAPTION_MAX_CHARS = 2_000
_IMAGE_OCR_MAX_CHARS = 4_000
_IMAGE_SURROUNDING_TEXT_MAX_CHARS = 4_000
IMAGE_SNAPSHOT_META_KEY = "image_snapshot"


def retrieval_chunk_id(snapshot_id: UUID, revision_id: UUID, chunk_index: int) -> UUID:
    return uuid5(snapshot_id, f"retrieval_chunk:{revision_id}:{chunk_index}")


def image_retrieval_chunk_id(snapshot_id: UUID, image_asset_id: UUID) -> UUID:
    return uuid5(snapshot_id, f"retrieval_image_chunk:{image_asset_id}")


def _bounded_image_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:max_chars] or None


def image_chunk_content(asset: KbImageAsset) -> str:
    """Compose the only searchable image text, with stable field labels."""
    fields = (
        (
            "Caption",
            _bounded_image_text(asset.caption, max_chars=_IMAGE_CAPTION_MAX_CHARS),
        ),
        (
            "OCR",
            _bounded_image_text(asset.ocr_text, max_chars=_IMAGE_OCR_MAX_CHARS),
        ),
        (
            "Surrounding text",
            _bounded_image_text(
                asset.surrounding_text,
                max_chars=_IMAGE_SURROUNDING_TEXT_MAX_CHARS,
            ),
        ),
    )
    content = "\n\n".join(
        f"[{label}]\n{value}" for label, value in fields if value is not None
    )
    if not content:
        raise ProjectionPayloadError(
            f"accepted image asset {asset.id} has no searchable enrichment"
        )
    return content


def image_chunk_snapshot_meta(asset: KbImageAsset) -> dict[str, Any]:
    caption = _bounded_image_text(
        asset.caption,
        max_chars=_IMAGE_CAPTION_MAX_CHARS,
    )
    ocr_text = _bounded_image_text(
        asset.ocr_text,
        max_chars=_IMAGE_OCR_MAX_CHARS,
    )
    alt_text = (caption or ocr_text or "")[:_IMAGE_CAPTION_MAX_CHARS]
    if not alt_text:
        raise ProjectionPayloadError(
            f"accepted image asset {asset.id} has no approved alt text"
        )
    fingerprint_payload = {
        "caption": caption,
        "ocr_text": ocr_text,
        "extraction_fingerprint": asset.extraction_fingerprint,
        "ocr_fingerprint": asset.ocr_fingerprint,
        "caption_fingerprint": asset.caption_fingerprint,
        "config_fingerprint": asset.config_fingerprint,
    }
    enrichment_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "alt_text": alt_text,
        "caption": caption,
        "ocr_text": ocr_text,
        "enrichment_fingerprint": enrichment_fingerprint,
    }


def _has_required_retrieval_builder(config_manifest: object) -> bool:
    if not isinstance(config_manifest, dict):
        return False
    builders = config_manifest.get("required_projection_builders")
    return isinstance(builders, list) and any(
        isinstance(builder, dict) and builder.get("name") == "retrieval" for builder in builders
    )


async def _active_retrieval_snapshot_id(
    session: AsyncSession, *, org_id: UUID, kb_id: UUID
) -> UUID | None:
    row = (
        await session.execute(
            select(KnowledgeBase.active_snapshot_id, KnowledgeSnapshot.config_manifest)
            .outerjoin(
                KnowledgeSnapshot,
                (KnowledgeSnapshot.id == KnowledgeBase.active_snapshot_id)
                & (KnowledgeSnapshot.org_id == KnowledgeBase.org_id)
                & (KnowledgeSnapshot.kb_id == KnowledgeBase.id),
            )
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.org_id == org_id)
        )
    ).one()
    if row.active_snapshot_id is None:
        return None
    return row.active_snapshot_id if _has_required_retrieval_builder(row.config_manifest) else None


def _entity_card_content(claim: FactClaim, type_def=None) -> str:
    payload = _effective_payload(claim)
    if type_def is not None:  # 自定义类型(M3a):卡片文本由类型模板驱动
        text = render_entity_card(type_def, payload)
        if not text.strip():
            raise ProjectionPayloadError(
                f"claim {claim.id} cannot produce a searchable entity card"
            )
        return f"{claim.predicate}\n{text}"
    parts = [claim.predicate]
    for field in _ENTITY_CARD_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(f"{field}: {value.strip()}")
        elif isinstance(value, int | float) and not isinstance(value, bool):
            parts.append(f"{field}: {value}")
    if len(parts) == 1:
        raise ProjectionPayloadError(f"claim {claim.id} cannot produce a searchable entity card")
    return "\n".join(parts)


def _citation_pairs(meta: dict | None) -> tuple[tuple[UUID, UUID], ...]:
    if not meta or _CITATION_REFS_KEY not in meta:
        return ()
    raw_refs = meta[_CITATION_REFS_KEY]
    if not isinstance(raw_refs, list):
        raise ProjectionPayloadError("retrieval citation_refs must be an array")
    pairs: set[tuple[UUID, UUID]] = set()
    for raw in raw_refs:
        if not isinstance(raw, dict) or set(raw) != {
            "fact_claim_id",
            "evidence_span_id",
        }:
            raise ProjectionPayloadError("retrieval citation_ref fields are invalid")
        try:
            pairs.add(
                (
                    UUID(str(raw["fact_claim_id"])),
                    UUID(str(raw["evidence_span_id"])),
                )
            )
        except ValueError as exc:
            raise ProjectionPayloadError("retrieval citation_ref ids are invalid") from exc
    return tuple(sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1]))))


def _chunk_anchor_contains_evidence(
    item: Mapping[str, Any], evidence: EvidenceSpan
) -> bool:
    """Match only explicit line anchors; every containing overlap is eligible."""
    chunk_start = item.get("start_line")
    chunk_end = item.get("end_line")
    if (
        chunk_start is None
        or chunk_end is None
        or evidence.start_line is None
        or evidence.end_line is None
    ):
        return False
    chunk_page = item.get("page")
    if evidence.page is not None and chunk_page != evidence.page:
        return False
    return chunk_start <= evidence.start_line and evidence.end_line <= chunk_end


async def _bind_chunk_citations(
    session: AsyncSession,
    *,
    context: SnapshotBuildContext,
    revision_id: UUID,
    staged: list[dict[str, Any]],
) -> None:
    """Persist only exact citation relationships valid in this snapshot."""
    source_ids = {
        item["source_chunk_id"] for item in staged if item.get("source_chunk_id")
    }
    requested = {
        pair for item in staged for pair in _citation_pairs(item.get("meta"))
    }
    criteria = []
    if source_ids and context.fact_claim_ids:
        criteria.append(
            and_(
                FactClaim.id.in_(context.fact_claim_ids),
                EvidenceSpan.chunk_id.in_(source_ids),
            )
        )
    if requested:
        criteria.append(EvidenceSpan.id.in_({pair[1] for pair in requested}))
    has_line_anchored_chunks = any(
        item.get("start_line") is not None and item.get("end_line") is not None
        for item in staged
    )
    if has_line_anchored_chunks and context.fact_claim_ids:
        criteria.append(
            and_(
                FactClaim.id.in_(context.fact_claim_ids),
                EvidenceSpan.start_line.is_not(None),
                EvidenceSpan.end_line.is_not(None),
            )
        )
    valid_rows = []
    if criteria:
        valid_rows = (
            await session.execute(
                select(FactClaim, EvidenceSpan)
                .join(
                    EvidenceSpan,
                    and_(
                        EvidenceSpan.fact_claim_id == FactClaim.id,
                        EvidenceSpan.org_id == FactClaim.org_id,
                        EvidenceSpan.kb_id == FactClaim.kb_id,
                    ),
                )
                .where(
                    FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                    FactClaim.org_id == context.org_id,
                    FactClaim.kb_id == context.kb_id,
                    EvidenceSpan.org_id == context.org_id,
                    EvidenceSpan.kb_id == context.kb_id,
                    EvidenceSpan.revision_id == revision_id,
                    or_(*criteria),
                )
                .order_by(FactClaim.id, EvidenceSpan.id)
            )
        ).all()

    valid_pairs = {(claim.id, evidence.id) for claim, evidence in valid_rows}
    manifest_claim_ids = set(context.fact_claim_ids)
    direct_by_chunk: dict[UUID, set[tuple[UUID, UUID]]] = {}
    anchored_rows: list[tuple[FactClaim, EvidenceSpan]] = []
    for claim, evidence in valid_rows:
        if claim.id in manifest_claim_ids and evidence.chunk_id in source_ids:
            direct_by_chunk.setdefault(evidence.chunk_id, set()).add(
                (claim.id, evidence.id)
            )
        if (
            claim.id in manifest_claim_ids
            and evidence.start_line is not None
            and evidence.end_line is not None
        ):
            anchored_rows.append((claim, evidence))

    for item in staged:
        meta = dict(item.get("meta") or {})
        meta.pop(_CITATION_REFS_KEY, None)
        refs = {
            pair for pair in _citation_pairs(item.get("meta")) if pair in valid_pairs
        }
        source_id = item.get("source_chunk_id")
        if source_id:
            refs.update(direct_by_chunk.get(source_id, ()))
        refs.update(
            (claim.id, evidence.id)
            for claim, evidence in anchored_rows
            if _chunk_anchor_contains_evidence(item, evidence)
        )
        if refs:
            meta[_CITATION_REFS_KEY] = [
                {
                    "fact_claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
                for claim_id, evidence_id in sorted(
                    refs, key=lambda pair: (str(pair[0]), str(pair[1]))
                )
            ]
        item["meta"] = meta or None


def _nullable_text(value: object, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionPayloadError(f"retrieval chunk {field} must be a string or null")
    if len(value) > max_length:
        raise ProjectionPayloadError(f"retrieval chunk {field} exceeds {max_length} characters")
    return value


def _nullable_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProjectionPayloadError(f"retrieval chunk {field} must be a positive integer or null")
    return value


def _validated_vector(value: object) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ProjectionPayloadError("retrieval chunk embedding must be an array or null")
    vector: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, int | float):
            raise ProjectionPayloadError("retrieval chunk embedding components must be numbers")
        number = float(component)
        if not math.isfinite(number):
            raise ProjectionPayloadError("retrieval chunk embedding components must be finite")
        vector.append(number)
    if not vector:
        raise ProjectionPayloadError("retrieval chunk embedding must not be empty")
    return vector


def _parse_staged_chunks(raw: bytes, *, expected_count: int) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionPayloadError("retrieval chunk artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, list):
        raise ProjectionPayloadError("retrieval chunk artifact must be a JSON array")
    if len(payload) != expected_count:
        raise ProjectionPayloadError(
            "retrieval chunk artifact count does not match ingest run stats"
        )

    parsed: list[dict[str, Any]] = []
    for expected_index, item in enumerate(payload):
        if not isinstance(item, dict) or set(item) != _CHUNK_FIELDS:
            raise ProjectionPayloadError(
                f"retrieval chunk {expected_index} fields do not match staging contract"
            )
        chunk_index = item["chunk_index"]
        if (
            isinstance(chunk_index, bool)
            or not isinstance(chunk_index, int)
            or chunk_index != expected_index
        ):
            raise ProjectionPayloadError(
                "retrieval chunk indexes must be contiguous, ordered, and zero-based"
            )
        content = item["content"]
        if not isinstance(content, str) or not content.strip():
            raise ProjectionPayloadError("retrieval chunk content must be non-empty text")
        quarantined = item["quarantined"]
        if not isinstance(quarantined, bool):
            raise ProjectionPayloadError("retrieval chunk quarantined must be boolean")
        meta = item["meta"]
        if meta is not None and not isinstance(meta, dict):
            raise ProjectionPayloadError("retrieval chunk meta must be an object or null")
        start_line = _nullable_positive_int(item["start_line"], field="start_line")
        end_line = _nullable_positive_int(item["end_line"], field="end_line")
        if (start_line is None) != (end_line is None) or (
            start_line is not None and end_line is not None and end_line < start_line
        ):
            raise ProjectionPayloadError("retrieval chunk line anchors are invalid")
        embedding_model = _nullable_text(
            item["embedding_model"], field="embedding_model", max_length=200
        )
        if embedding_model is not None and not embedding_model.strip():
            raise ProjectionPayloadError(
                "retrieval chunk embedding_model must be non-empty or null"
            )
        parsed.append(
            {
                "chunk_index": chunk_index,
                "content": content,
                "embedding": _validated_vector(item["embedding"]),
                "embedding_model": embedding_model,
                "end_line": end_line,
                "heading_path": _nullable_text(
                    item["heading_path"], field="heading_path", max_length=500
                ),
                "meta": meta,
                "page": _nullable_positive_int(item["page"], field="page"),
                "quarantined": quarantined,
                "source_ref": _nullable_text(
                    item["source_ref"], field="source_ref", max_length=500
                ),
                "start_line": start_line,
            }
        )
    return parsed


async def _upsert_chunks(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    statement = pg_insert(KbChunk).values(list(rows))
    updates = {
        key: getattr(statement.excluded, key) for key in rows[0] if key not in {"id", "created_at"}
    }
    await session.execute(
        statement.on_conflict_do_update(index_elements=[KbChunk.id], set_=updates)
    )


async def _upsert_embeddings(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    statement = pg_insert(KbChunkEmbedding).values(list(rows))
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_kb_chunk_embedding_version",
            set_={
                "content_hash": statement.excluded.content_hash,
                "embedding": statement.excluded.embedding,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


def _staged_rows_from_source_chunks(
    source_chunks: Sequence[KbChunk],
    versions_by_chunk: Mapping[UUID, Sequence[KbChunkEmbedding]],
    *,
    provider: str,
    model: str,
    dim: int,
    expected_label: str,
) -> list[dict[str, Any]]:
    """把既有 chunk 行(克隆源)转换为 staging 行契约,精确指纹命中才携带向量。"""
    staged: list[dict[str, Any]] = []
    for index, row in enumerate(source_chunks):
        current_hash = embedding_content_hash(
            chunk_embedding_text(row.content, row.heading_path, chunk_context_text(row.meta))
        )
        exact = next(
            (
                version
                for version in versions_by_chunk.get(row.id, [])
                if version.provider == provider
                and version.model == model
                and version.dim == dim
                and version.content_hash == current_hash
            ),
            None,
        )
        has_other_version = bool(versions_by_chunk.get(row.id))
        staged.append(
            {
                "source_chunk_id": row.id,
                "chunk_index": index,
                "content": row.content,
                "embedding": list(exact.embedding) if exact is not None else None,
                "embedding_model": (
                    expected_label
                    if exact is not None
                    else "legacy-version-mismatch"
                    if has_other_version
                    else None
                ),
                "end_line": row.end_line,
                "heading_path": row.heading_path,
                "meta": row.meta,
                "page": row.page,
                "quarantined": row.quarantined,
                "source_ref": row.source_ref,
                "start_line": row.start_line,
            }
        )
    return staged


async def _chunk_embedding_versions(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    chunk_ids: Sequence[UUID],
) -> dict[UUID, list[KbChunkEmbedding]]:
    versions_by_chunk: dict[UUID, list[KbChunkEmbedding]] = {}
    if not chunk_ids:
        return versions_by_chunk
    versions = (
        await session.execute(
            select(KbChunkEmbedding).where(
                KbChunkEmbedding.org_id == org_id,
                KbChunkEmbedding.kb_id == kb_id,
                KbChunkEmbedding.chunk_id.in_(list(chunk_ids)),
            )
        )
    ).scalars()
    for version in versions:
        versions_by_chunk.setdefault(version.chunk_id, []).append(version)
    return versions_by_chunk


class SnapshotEmbedder(Protocol):
    """快照构建期批量嵌入的最小接口(与 EmbeddingService.embed 对齐)。"""

    async def embed(
        self, texts: list[str], *, org_id: UUID, task: str | None = None
    ) -> list[list[float]]: ...


EmbeddingServiceFactory = Callable[[Mapping[str, Any]], SnapshotEmbedder]

_SNAPSHOT_EMBED_TASK = "kb.snapshot.embedding"


def _default_embedding_service_factory(fingerprint: Mapping[str, Any]) -> SnapshotEmbedder:
    """按快照指纹构建嵌入服务;无凭证时构建立即失败,不允许缺向量的快照 READY。"""
    endpoint = provider_model_endpoint(
        str(fingerprint["provider"]),
        str(fingerprint["model"]),
        capability="embedding",
    )
    if endpoint is None:
        raise ProjectionPayloadError(
            "retrieval projection requires a configured embedding provider model"
        )
    return EmbeddingService(
        provider=str(fingerprint["provider"]),
        model=str(fingerprint["model"]),
        dim=int(fingerprint["dim"]),
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
    )


async def _close_embedder(embedder: SnapshotEmbedder) -> None:
    close = getattr(embedder, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


async def _ensure_chunk_embeddings(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    chunk_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    provider: str,
    model: str,
    dim: int,
    expected_label: str,
    counters: Counter[str],
    embedding_service_factory: EmbeddingServiceFactory | None,
) -> None:
    """物化前补齐缺失向量:内容寻址复用优先,复用不到的增量嵌入,失败即构建失败。"""
    pending = [
        row for row in chunk_rows if not row["quarantined"] and row["embedding"] is None
    ]
    if not pending:
        return

    pending_hashes: list[str] = []
    text_by_hash: dict[str, str] = {}
    for row in pending:
        text = chunk_embedding_text(
            row["content"], row["heading_path"], chunk_context_text(row.get("meta"))
        )
        content_hash = embedding_content_hash(text)
        pending_hashes.append(content_hash)
        text_by_hash.setdefault(content_hash, text)

    # 复用优先(1):本次构建内已收集的同指纹版本行(同内容 chunk 只嵌入一次)。
    vector_by_hash: dict[str, list[float]] = {
        row["content_hash"]: list(row["embedding"]) for row in embedding_rows
    }
    # 复用优先(2):库内既有版本行按 content_hash 寻址(上一代 chunk / 历史回填共享)。
    unresolved = sorted(set(pending_hashes) - set(vector_by_hash))
    if unresolved:
        for content_hash, vector in (
            await session.execute(
                select(KbChunkEmbedding.content_hash, KbChunkEmbedding.embedding).where(
                    KbChunkEmbedding.org_id == org_id,
                    KbChunkEmbedding.kb_id == kb_id,
                    KbChunkEmbedding.provider == provider,
                    KbChunkEmbedding.model == model,
                    KbChunkEmbedding.dim == dim,
                    KbChunkEmbedding.content_hash.in_(unresolved),
                )
            )
        ).all():
            vector_by_hash.setdefault(str(content_hash), list(vector))

    to_embed = sorted(set(pending_hashes) - set(vector_by_hash))
    counters["reused_embeddings"] += sum(
        1 for content_hash in pending_hashes if content_hash in vector_by_hash
    )
    counters["generated_embeddings"] += sum(
        1 for content_hash in pending_hashes if content_hash not in vector_by_hash
    )

    if to_embed:
        factory = embedding_service_factory or _default_embedding_service_factory
        embedder = factory({"provider": provider, "model": model, "dim": dim})
        try:
            vectors = await embedder.embed(
                [text_by_hash[content_hash] for content_hash in to_embed],
                org_id=org_id,
                task=_SNAPSHOT_EMBED_TASK,
            )
        finally:
            await _close_embedder(embedder)
        if len(vectors) != len(to_embed):
            raise ProjectionPayloadError(
                "snapshot embedding service returned a mismatched batch"
            )
        for content_hash, vector in zip(to_embed, vectors, strict=True):
            normalized = [float(component) for component in vector]
            if len(normalized) != dim:
                raise ProjectionPayloadError(
                    f"snapshot embedding dimension {len(normalized)} does not match {dim}"
                )
            vector_by_hash[content_hash] = normalized

    for row, content_hash in zip(pending, pending_hashes, strict=True):
        vector = vector_by_hash[content_hash]
        row["embedding"] = vector
        row["embedding_model"] = expected_label
        embedding_rows.append(
            {
                "id": uuid5(row["id"], f"embedding:{provider}:{model}:{dim}"),
                "org_id": org_id,
                "kb_id": kb_id,
                "chunk_id": row["id"],
                "provider": provider,
                "model": model,
                "dim": dim,
                "content_hash": content_hash,
                "embedding": vector,
            }
        )


class RetrievalProjectionBuilder:
    name = "retrieval"
    version = "2"

    def __init__(
        self, embedding_service_factory: EmbeddingServiceFactory | None = None
    ) -> None:
        self._embedding_service_factory = embedding_service_factory

    async def build(
        self, session: AsyncSession, context: SnapshotBuildContext
    ) -> Mapping[str, Any]:
        manifest_ids = [UUID(item["revision_id"]) for item in context.revision_manifest]
        if not manifest_ids:
            return {
                "row_count": 0,
                "artifact_chunk_count": 0,
                "image_chunk_count": 0,
                "entity_card_count": 0,
                "embedding_count": 0,
                "revision_count": 0,
                "generation_clone_revisions": 0,
                "generation_cloned_chunks": 0,
                "staging_artifact_fallbacks": 0,
                "legacy_clone_revisions": 0,
                "legacy_cloned_chunks": 0,
                "quarantined_chunks": 0,
                "missing_embeddings": 0,
                "fingerprint_mismatches": 0,
                "reused_embeddings": 0,
                "generated_embeddings": 0,
                "vector_coverage": 1.0,
            }

        revision_rows = (
            await session.execute(
                select(DocumentRevision, SourceDocument)
                .join(SourceDocument, SourceDocument.id == DocumentRevision.doc_id)
                .where(
                    DocumentRevision.id.in_(manifest_ids),
                    DocumentRevision.org_id == context.org_id,
                    DocumentRevision.kb_id == context.kb_id,
                    SourceDocument.org_id == context.org_id,
                    SourceDocument.kb_id == context.kb_id,
                )
            )
        ).all()
        by_revision = {revision.id: (revision, document) for revision, document in revision_rows}
        if set(by_revision) != set(manifest_ids):
            raise ProjectionPayloadError(
                "snapshot revision set changed during retrieval projection build"
            )
        document_revision_ids: dict[UUID, set[UUID]] = {}
        document_ids = {document.id for _revision, document in revision_rows}
        for doc_id, revision_id in (
            await session.execute(
                select(DocumentRevision.doc_id, DocumentRevision.id).where(
                    DocumentRevision.doc_id.in_(document_ids),
                    DocumentRevision.org_id == context.org_id,
                    DocumentRevision.kb_id == context.kb_id,
                )
            )
        ).all():
            document_revision_ids.setdefault(doc_id, set()).add(revision_id)

        fingerprint = context.embedding_fingerprint
        provider = str(fingerprint["provider"])
        model = str(fingerprint["model"])
        dim = int(fingerprint["dim"])
        if dim != EMBEDDING_DIM:
            raise ProjectionPayloadError(
                f"retrieval projection dimension {dim} does not match vector({EMBEDDING_DIM})"
            )
        expected_label = f"{provider}:{model}"
        counters: Counter[str] = Counter()
        chunk_rows: list[dict[str, Any]] = []
        embedding_rows: list[dict[str, Any]] = []
        active_retrieval_snapshot_id = await _active_retrieval_snapshot_id(
            session,
            org_id=context.org_id,
            kb_id=context.kb_id,
        )
        if active_retrieval_snapshot_id == context.snapshot_id:
            raise ProjectionPayloadError(
                "retrieval projection cannot clone from its building snapshot"
            )

        for revision_id in manifest_ids:
            revision, document = by_revision[revision_id]
            run = (
                await session.execute(
                    select(IngestRun).where(
                        IngestRun.revision_id == revision_id,
                        IngestRun.org_id == context.org_id,
                        IngestRun.kb_id == context.kb_id,
                        IngestRun.stage == "chunk",
                        IngestRun.segment_no == 0,
                    )
                )
            ).scalar_one_or_none()

            staged: list[dict[str, Any]]
            if run is None:
                if str(document.doc_type) != DocType.GENERAL.value:
                    counters["structured_revisions_without_chunks"] += 1
                    continue
                clone_from_active = active_retrieval_snapshot_id is not None
                if not clone_from_active and (
                    document_revision_ids.get(document.id) != {revision_id}
                    or revision.sha256 != document.sha256
                ):
                    raise ProjectionPayloadError(
                        f"general revision {revision_id} legacy chunks have ambiguous "
                        "revision provenance; stage or reingest the revision"
                    )
                source_chunks = list(
                    (
                        await session.execute(
                            select(KbChunk)
                            .where(
                                KbChunk.org_id == context.org_id,
                                KbChunk.kb_id == context.kb_id,
                                KbChunk.source_doc_id == document.id,
                                *(
                                    (
                                        KbChunk.revision_id == revision_id,
                                        KbChunk.snapshot_id == active_retrieval_snapshot_id,
                                        KbChunk.content_kind == "text",
                                    )
                                    if clone_from_active
                                    else (
                                        KbChunk.snapshot_id.is_(None),
                                        KbChunk.content_kind == "text",
                                    )
                                ),
                            )
                            .order_by(KbChunk.chunk_index.asc().nullslast(), KbChunk.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not source_chunks:
                    source = (
                        f"active retrieval snapshot {active_retrieval_snapshot_id}"
                        if clone_from_active
                        else "legacy rows"
                    )
                    raise ProjectionPayloadError(
                        f"general revision {revision_id} has no chunk staging run or {source}"
                    )
                versions_by_chunk = await _chunk_embedding_versions(
                    session,
                    org_id=context.org_id,
                    kb_id=context.kb_id,
                    chunk_ids=[row.id for row in source_chunks],
                )
                staged = _staged_rows_from_source_chunks(
                    source_chunks,
                    versions_by_chunk,
                    provider=provider,
                    model=model,
                    dim=dim,
                    expected_label=expected_label,
                )
                counter_prefix = "generation" if clone_from_active else "legacy"
                counters[f"{counter_prefix}_clone_revisions"] += 1
                counters[f"{counter_prefix}_cloned_chunks"] += len(staged)
            else:
                if str(run.status) != IngestRunStatus.STAGED.value:
                    raise ProjectionPayloadError(f"chunk ingest run {run.id} is not staged")
                stats = run.stats
                required_stat_keys = {"artifact_key", "chunk_count"}
                allowed_stat_keys = required_stat_keys | {"consumption_epoch"}
                stat_keys = set(stats) if isinstance(stats, dict) else set()
                if (
                    not isinstance(stats, dict)
                    or not required_stat_keys.issubset(stat_keys)
                    or not stat_keys.issubset(allowed_stat_keys)
                ):
                    raise ProjectionPayloadError(
                        f"chunk ingest run {run.id} stats do not match staging contract"
                    )
                artifact_key = stats["artifact_key"]
                chunk_count = stats["chunk_count"]
                expected_prefix = (
                    f"{context.org_id}/kb/{context.kb_id}/staging/{document.id}/{revision.id}/"
                )
                relative_key = (
                    artifact_key.removeprefix(expected_prefix)
                    if isinstance(artifact_key, str)
                    else ""
                )
                key_parts = relative_key.split("/")
                if (
                    not isinstance(artifact_key, str)
                    or not artifact_key.startswith(expected_prefix)
                    or len(key_parts) != 2
                    or not key_parts[0]
                    or key_parts[1] != "chunks.json"
                ):
                    raise ProjectionPayloadError(
                        f"chunk ingest run {run.id} artifact key is outside its revision scope"
                    )
                if (
                    isinstance(chunk_count, bool)
                    or not isinstance(chunk_count, int)
                    or chunk_count < 0
                ):
                    raise ProjectionPayloadError(
                        f"chunk ingest run {run.id} chunk_count is invalid"
                    )
                try:
                    artifact = await storage.get_object(artifact_key)
                except Exception as exc:
                    # staging 产物可能已被成功构建后的 GC 回收:该 revision 若已进入
                    # active 快照,回退为 generation-clone,不因临时产物缺失卡死重建。
                    fallback_chunks: list[KbChunk] = []
                    if active_retrieval_snapshot_id is not None:
                        fallback_chunks = list(
                            (
                                await session.execute(
                                    select(KbChunk)
                                    .where(
                                        KbChunk.org_id == context.org_id,
                                        KbChunk.kb_id == context.kb_id,
                                        KbChunk.revision_id == revision_id,
                                        KbChunk.snapshot_id == active_retrieval_snapshot_id,
                                        KbChunk.content_kind == "text",
                                    )
                                    .order_by(
                                        KbChunk.chunk_index.asc().nullslast(), KbChunk.id
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    if not fallback_chunks:
                        raise ProjectionPayloadError(
                            f"chunk ingest run {run.id} artifact cannot be loaded"
                        ) from exc
                    versions_by_chunk = await _chunk_embedding_versions(
                        session,
                        org_id=context.org_id,
                        kb_id=context.kb_id,
                        chunk_ids=[row.id for row in fallback_chunks],
                    )
                    staged = _staged_rows_from_source_chunks(
                        fallback_chunks,
                        versions_by_chunk,
                        provider=provider,
                        model=model,
                        dim=dim,
                        expected_label=expected_label,
                    )
                    counters["generation_clone_revisions"] += 1
                    counters["generation_cloned_chunks"] += len(staged)
                    counters["staging_artifact_fallbacks"] += 1
                else:
                    staged = _parse_staged_chunks(artifact, expected_count=chunk_count)

            await _bind_chunk_citations(
                session,
                context=context,
                revision_id=revision_id,
                staged=staged,
            )
            for item in staged:
                chunk_id = retrieval_chunk_id(context.snapshot_id, revision_id, item["chunk_index"])
                vector = item["embedding"]
                model_matches = item["embedding_model"] == expected_label
                dimension_matches = vector is not None and len(vector) == dim
                usable_vector = not item["quarantined"] and model_matches and dimension_matches
                if item["quarantined"]:
                    counters["quarantined_chunks"] += 1
                elif vector is None or item["embedding_model"] is None:
                    counters["missing_embeddings"] += 1
                elif not model_matches or not dimension_matches:
                    counters["fingerprint_mismatches"] += 1

                chunk_rows.append(
                    {
                        "id": chunk_id,
                        "org_id": context.org_id,
                        "kb_id": context.kb_id,
                        "revision_id": revision_id,
                        "snapshot_id": context.snapshot_id,
                        "source_doc_id": document.id,
                        "content_kind": "text",
                        "image_asset_id": None,
                        "content": item["content"],
                        "embedding": vector if usable_vector else None,
                        "embedding_model": expected_label if usable_vector else None,
                        "quarantined": item["quarantined"],
                        "source_ref": item["source_ref"],
                        "heading_path": item["heading_path"],
                        "start_line": item["start_line"],
                        "end_line": item["end_line"],
                        "page": item["page"],
                        "chunk_index": item["chunk_index"],
                        "meta": item["meta"],
                    }
                )
                if usable_vector:
                    content_hash = embedding_content_hash(
                        chunk_embedding_text(
                            item["content"],
                            item["heading_path"],
                            chunk_context_text(item["meta"]),
                        )
                    )
                    embedding_rows.append(
                        {
                            "id": uuid5(chunk_id, f"embedding:{provider}:{model}:{dim}"),
                            "org_id": context.org_id,
                            "kb_id": context.kb_id,
                            "chunk_id": chunk_id,
                            "provider": provider,
                            "model": model,
                            "dim": dim,
                            "content_hash": content_hash,
                            "embedding": vector,
                        }
                    )

        artifact_chunk_count = len(chunk_rows)
        accepted_images = list(
            (
                await session.execute(
                    select(KbImageAsset)
                    .where(
                        KbImageAsset.revision_id.in_(manifest_ids),
                        KbImageAsset.org_id == context.org_id,
                        KbImageAsset.kb_id == context.kb_id,
                        KbImageAsset.enrichment_status == "succeeded",
                        KbImageAsset.review_status == "accepted",
                    )
                    .order_by(
                        KbImageAsset.revision_id,
                        KbImageAsset.reading_order.asc().nullslast(),
                        KbImageAsset.source_occurrence,
                        KbImageAsset.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for asset in accepted_images:
            revision, document = by_revision[asset.revision_id]
            if asset.doc_id != document.id or revision.doc_id != document.id:
                raise ProjectionPayloadError(
                    f"image asset {asset.id} does not match its manifest revision"
                )
            content = image_chunk_content(asset)
            snapshot_meta = image_chunk_snapshot_meta(asset)
            quarantine_reasons = suspicious_instruction_reasons(content)
            if quarantine_reasons:
                counters["quarantined_chunks"] += 1
            chunk_rows.append(
                {
                    "id": image_retrieval_chunk_id(context.snapshot_id, asset.id),
                    "org_id": context.org_id,
                    "kb_id": context.kb_id,
                    "revision_id": asset.revision_id,
                    "snapshot_id": context.snapshot_id,
                    "source_doc_id": asset.doc_id,
                    "content_kind": "image",
                    "image_asset_id": asset.id,
                    "content": content,
                    "embedding": None,
                    "embedding_model": None,
                    "quarantined": bool(quarantine_reasons),
                    "source_ref": f"kb-image/{asset.id}",
                    "heading_path": None,
                    "start_line": None,
                    "end_line": None,
                    "page": asset.page,
                    "chunk_index": None,
                    "meta": {
                        IMAGE_SNAPSHOT_META_KEY: snapshot_meta,
                        **(
                            {"quarantine_reasons": list(quarantine_reasons)}
                            if quarantine_reasons
                            else {}
                        ),
                    },
                }
            )
        image_chunk_count = len(accepted_images)
        if context.fact_claim_ids:
            custom_types = await load_custom_entity_types(session, context.org_id)
            card_predicates = _ENTITY_CARD_PREDICATES | set(custom_types)
            claims = list(
                (
                    await session.execute(
                        select(FactClaim)
                        .where(
                            FactClaim.id.in_(context.fact_claim_ids),
                            FactClaim.org_id == context.org_id,
                            FactClaim.kb_id == context.kb_id,
                            FactClaim.predicate.in_(card_predicates),
                            FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                        )
                        .order_by(FactClaim.id)
                    )
                )
                .scalars()
                .all()
            )
            entity_card_refs: set[str] = set()
            for claim in claims:
                card_kind = "page" if claim.predicate == "wiki_page" else claim.predicate
                entity_id = projection_row_id(
                    context.snapshot_id, claim.predicate, claim.id
                )
                source_ref = card_source_ref(card_kind, entity_id)
                if source_ref in entity_card_refs:
                    continue
                entity_card_refs.add(source_ref)
                chunk_rows.append(
                    {
                        "id": uuid5(
                            context.snapshot_id,
                            f"retrieval_card:{card_kind}:{claim.id}",
                        ),
                        "org_id": context.org_id,
                        "kb_id": context.kb_id,
                        "revision_id": None,
                        "snapshot_id": context.snapshot_id,
                        "source_doc_id": None,
                        "content_kind": "text",
                        "image_asset_id": None,
                        "content": _entity_card_content(
                            claim, custom_types.get(claim.predicate)
                        ),
                        "embedding": None,
                        "embedding_model": None,
                        "quarantined": False,
                        "source_ref": source_ref,
                        "heading_path": f"entity_card > {card_kind}",
                        "start_line": None,
                        "end_line": None,
                        "page": None,
                        "chunk_index": None,
                        "meta": {"fact_claim_id": str(claim.id)},
                    }
                )
        # 物化前补齐缺失向量(含实体卡片):复用优先、增量嵌入、失败即整体构建失败。
        await _ensure_chunk_embeddings(
            session,
            org_id=context.org_id,
            kb_id=context.kb_id,
            chunk_rows=chunk_rows,
            embedding_rows=embedding_rows,
            provider=provider,
            model=model,
            dim=dim,
            expected_label=expected_label,
            counters=counters,
            embedding_service_factory=self._embedding_service_factory,
        )
        eligible_rows = sum(1 for row in chunk_rows if not row["quarantined"])
        embedded_rows = sum(
            1
            for row in chunk_rows
            if not row["quarantined"] and row["embedding"] is not None
        )

        await _upsert_chunks(session, chunk_rows)
        await _upsert_embeddings(session, embedding_rows)
        return {
            "row_count": len(chunk_rows),
            "artifact_chunk_count": artifact_chunk_count,
            "image_chunk_count": image_chunk_count,
            "entity_card_count": (
                len(chunk_rows) - artifact_chunk_count - image_chunk_count
            ),
            "embedding_count": len(embedding_rows),
            "revision_count": len(manifest_ids),
            "generation_clone_revisions": counters["generation_clone_revisions"],
            "generation_cloned_chunks": counters["generation_cloned_chunks"],
            "staging_artifact_fallbacks": counters["staging_artifact_fallbacks"],
            "legacy_clone_revisions": counters["legacy_clone_revisions"],
            "legacy_cloned_chunks": counters["legacy_cloned_chunks"],
            "structured_revisions_without_chunks": counters["structured_revisions_without_chunks"],
            "quarantined_chunks": counters["quarantined_chunks"],
            "missing_embeddings": counters["missing_embeddings"],
            "fingerprint_mismatches": counters["fingerprint_mismatches"],
            "reused_embeddings": counters["reused_embeddings"],
            "generated_embeddings": counters["generated_embeddings"],
            "vector_coverage": (embedded_rows / eligible_rows) if eligible_rows else 1.0,
        }
