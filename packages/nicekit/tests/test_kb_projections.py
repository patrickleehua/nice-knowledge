"""SQL/Wiki 投影的幂等 id、active 指针过滤与 builder 注册表。

由 TF ``tests/test_kb_projections.py`` 搬运适配:``destination_row_id`` 与
hotel_pools 投影表随 5 张旅游专表一并删除(MIGRATION-PLAN B1/B6/B7),
断言改用通用实体表 ``kb_entities``;媒体 builder 属媒体波次,未装配时不在
必需清单里。
"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from nicekit.kb.projections import (
    SQL_PROJECTION_PREDICATES,
    _validate_source_document,
    active_projection_filter,
    card_source_ref,
    parse_card_ref,
    projection_row_id,
    supported_projection_predicates,
)
from nicekit.kb.snapshot import projection_builders
from nicekit.models.kb import FactClaim, KbEntity, KbEntityType


def test_projection_ids_are_idempotent_and_snapshot_scoped() -> None:
    first_snapshot = UUID("00000000-0000-0000-0000-000000000001")
    second_snapshot = UUID("00000000-0000-0000-0000-000000000002")
    claim_id = UUID("00000000-0000-0000-0000-000000000003")

    assert projection_row_id(first_snapshot, "product", claim_id) == projection_row_id(
        first_snapshot, "product", claim_id
    )
    assert projection_row_id(first_snapshot, "product", claim_id) != projection_row_id(
        second_snapshot, "product", claim_id
    )
    assert projection_row_id(first_snapshot, "product", claim_id) != projection_row_id(
        first_snapshot, "policy", claim_id
    )


def test_sql_projection_predicates_are_registry_driven() -> None:
    # B6:不再有任何硬编码业务谓词,受支持集合 = 静态谓词 + 注册类型
    assert frozenset() == SQL_PROJECTION_PREDICATES
    registry = {
        "product": KbEntityType(
            org_id=None,
            type_key="product",
            display_name="产品",
            field_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
    }
    supported = supported_projection_predicates(registry)
    assert "product" in supported
    assert "wiki_page" in supported
    assert "unregistered_kind" not in supported


def test_active_projection_filter_uses_null_safe_pointer_match() -> None:
    statement = select(KbEntity).where(active_projection_filter(KbEntity))
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    normalized_sql = " ".join(sql.split())

    assert "kb_entities.snapshot_id IS NOT DISTINCT FROM" in sql
    assert "knowledge_bases.active_snapshot_id" in sql
    assert "required_projection_builders" in sql
    assert "kb_entities.snapshot_id IS NULL" in sql
    assert (
        "WHERE (EXISTS (SELECT knowledge_bases.id FROM knowledge_bases "
        "WHERE knowledge_bases.id = kb_entities.kb_id "
        "AND knowledge_bases.org_id = kb_entities.org_id "
        "AND knowledge_bases.lifecycle_status = 'active')) "
        "AND (kb_entities.snapshot_id IS NOT DISTINCT FROM"
    ) in normalized_sql


def test_shared_claim_uses_a_surviving_manifest_evidence_document() -> None:
    original_document_id = uuid4()
    surviving_document_id = uuid4()
    original_revision_id = uuid4()
    surviving_revision_id = uuid4()
    claim = FactClaim(
        id=uuid4(),
        org_id=uuid4(),
        kb_id=uuid4(),
        subject_type="source_document",
        subject_id=original_document_id,
        predicate="product",
        value_json={"name": "Shared product", "warehouse": "east"},
    )

    selected = _validate_source_document(
        claim,
        evidence_revisions={
            claim.id: {original_revision_id, surviving_revision_id},
        },
        revision_documents={
            surviving_revision_id: surviving_document_id,
        },
    )

    assert selected == surviving_document_id


def test_card_source_ref_round_trips_any_registered_type_key() -> None:
    entity_id = uuid4()
    ref = card_source_ref("policy_document", entity_id)
    assert parse_card_ref(ref) == ("policy_document", entity_id)
    # 普通 chunk 的 "{filename}#chunkN" 形态不会被误判为卡片
    assert parse_card_ref("handbook.md#chunk3") is None
    assert parse_card_ref(None) is None


def test_default_registry_requires_sql_wiki_graph_and_retrieval_materializers() -> None:
    assert projection_builders.required_manifest() == [
        {"name": "graph", "version": "2"},
        {"name": "retrieval", "version": "2"},
        {"name": "sql", "version": "2"},
        {"name": "wiki", "version": "2"},
    ]
