import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql

from nicekit.kb.effective_scope import (
    active_knowledge_base_membership_filter,
    current_snapshot_filter,
    effective_chunk_filter,
    live_projection_source_filter,
    live_snapshot_projection_filter,
)
from nicekit.models.kb import KbChunk, KbEntity, KbPage, KnowledgeBaseLifecycleStatus


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _normalized_sql(statement) -> str:
    return " ".join(_sql(statement).split())


def test_effective_chunk_filter_requires_snapshot_and_live_source() -> None:
    sql = _sql(select(KbChunk.id).where(effective_chunk_filter()))

    assert "knowledge_bases.active_snapshot_id = kb_chunks.snapshot_id" in sql
    assert "document_revisions.id = kb_chunks.revision_id" in sql
    assert "document_revisions.tombstoned_at IS NULL" in sql
    assert "source_documents.lifecycle_status =" in sql
    assert "kb_chunks.revision_id IS NULL" in sql
    assert "kb_chunks.source_doc_id IS NULL" in sql


def test_projection_source_filter_allows_only_manual_or_active_sources() -> None:
    sql = _sql(
        select(KbPage.id).where(
            current_snapshot_filter(KbPage),
            live_projection_source_filter(KbPage),
        )
    )

    assert "knowledge_bases.active_snapshot_id = kb_pages.snapshot_id" in sql
    assert "kb_pages.source_doc_id IS NULL" in sql
    assert "source_documents.id = kb_pages.source_doc_id" in sql
    assert "source_documents.lifecycle_status =" in sql


def test_effective_scope_rejects_unscoped_models() -> None:
    class Unscoped:
        pass

    with pytest.raises(TypeError, match="snapshot-scoped"):
        current_snapshot_filter(Unscoped)
    with pytest.raises(TypeError, match="knowledge-base-scoped"):
        active_knowledge_base_membership_filter(Unscoped)
    with pytest.raises(TypeError, match="source-backed"):
        live_projection_source_filter(Unscoped)
    with pytest.raises(TypeError, match="fact-derived"):
        live_snapshot_projection_filter(Unscoped, "kb_entity")
    with pytest.raises(ValueError, match="must not be blank"):
        live_snapshot_projection_filter(KbEntity, " ")


def test_fact_derived_projection_requires_live_candidate_support() -> None:
    sql = _sql(
        select(KbEntity.id).where(
            live_snapshot_projection_filter(
                KbEntity,
                "kb_entity",
            )
        )
    )

    assert (
        "knowledge_bases.active_snapshot_id = kb_entities.snapshot_id"
        in sql
    )
    assert (
        "snapshot_projection_supports.projection_row_id = kb_entities.id"
        in sql
    )
    assert (
        "snapshot_fact_supports.id = "
        "snapshot_projection_supports.fact_support_id"
        in sql
    )
    assert "fact_claims.review_status =" in sql
    assert "document_revisions.tombstoned_at IS NULL" in sql
    assert "source_documents.lifecycle_status =" in sql


def test_wiki_legacy_branch_is_wrapped_by_active_kb_membership() -> None:
    sql = _normalized_sql(
        select(KbPage.id).where(
            live_snapshot_projection_filter(KbPage, "wiki_page")
        )
    )

    outer_membership = (
        "WHERE (EXISTS (SELECT knowledge_bases.id FROM knowledge_bases "
        "WHERE knowledge_bases.id = kb_pages.kb_id "
        "AND knowledge_bases.org_id = kb_pages.org_id "
        "AND knowledge_bases.lifecycle_status = "
    )
    assert outer_membership in sql
    assert ")) AND (kb_pages.snapshot_id IS NOT NULL" in sql
    assert "OR kb_pages.snapshot_id IS NULL" in sql


@pytest.mark.parametrize(
    ("lifecycle_status", "is_visible"),
    [
        (KnowledgeBaseLifecycleStatus.ACTIVE.value, True),
        (KnowledgeBaseLifecycleStatus.ARCHIVED.value, False),
        (KnowledgeBaseLifecycleStatus.PURGE_PENDING.value, False),
        (KnowledgeBaseLifecycleStatus.PURGED.value, False),
    ],
)
@pytest.mark.parametrize(
    ("model", "table_name"),
    [
        (KbEntity, "kb_entities"),
        (KbPage, "kb_pages"),
    ],
)
def test_active_kb_membership_fences_structured_and_wiki_legacy_rows(
    lifecycle_status: str,
    is_visible: bool,
    model,
    table_name: str,
) -> None:
    engine = create_engine("sqlite://")
    org_id = "22222222222222222222222222222222"
    kb_id = "11111111111111111111111111111111"
    row_id = "33333333333333333333333333333333"

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE knowledge_bases "
            "(id CHAR(32), org_id CHAR(32), lifecycle_status VARCHAR(32))"
        )
        connection.exec_driver_sql(
            f"CREATE TABLE {table_name} "
            "(id CHAR(32), org_id CHAR(32), kb_id CHAR(32), "
            "snapshot_id CHAR(32))"
        )
        connection.exec_driver_sql(
            "INSERT INTO knowledge_bases (id, org_id, lifecycle_status) "
            "VALUES (?, ?, ?)",
            (kb_id, org_id, lifecycle_status),
        )
        connection.exec_driver_sql(
            f"INSERT INTO {table_name} (id, org_id, kb_id, snapshot_id) "
            "VALUES (?, ?, ?, NULL)",
            (row_id, org_id, kb_id),
        )

        visible_row = connection.execute(
            select(model.id).where(
                active_knowledge_base_membership_filter(model)
            )
        ).scalar_one_or_none()

    assert (visible_row is not None) is is_visible
