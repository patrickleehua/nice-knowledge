"""图谱召回通道的端到端真库验证。

这条链路曾经**整体空转**:``graph_recall_candidates`` 唯一的结果恢复路径是
"检索卡片",而普通文档(``DocType.GENERAL``)摄入只产出 ``entity_mention`` 与
关系谓词事实,这两类事实按设计**不建卡**(见 ingestion.py 的投影分工注释)。
于是四路混合检索里的图谱这一路对绝大多数库恒为 0 命中——强制开启与关闭,
结果完全一致。第二个独立 bug 是 wiki 卡片建卡时把 kind 从 ``wiki_page`` 改名成
``page``,而恢复时拿 kind 直接比谓词,导致 wiki 卡片被 100% 静默丢弃。

所以这里全部用真库跑,并且**不允许 mock ``graph_recall_candidates`` 本身**:
仓库里唯一的旧图谱通道测试正是把整个函数 mock 掉,还恰好只构造了唯一能通过
校验的自定义实体类型,完美绕开了两个 bug。

覆盖:
- 普通文档 → entity_mention/关系事实 → canonical binding → 快照图投影 →
  图谱召回真实返回非空(1 跳与 2 跳);
- wiki 卡片(kind != predicate)能被恢复,自定义结构化实体卡片不回归;
- 安全边界:未发布快照、quarantined 切片、跨 org/kb、已过期的边一律不泄漏。
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest
from _tenancy_pg import ensure_schema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nicekit.core.config import get_settings
from nicekit.kb.graph_projection import GraphProjectionBuilder
from nicekit.kb.graph_search import (
    CARD_RESTORE,
    CHUNK_RESTORE,
    graph_recall_candidates,
)
from nicekit.kb.projections import card_source_ref, projection_row_id
from nicekit.kb.snapshot import SnapshotBuildContext

pytestmark = pytest.mark.live

# 语料刻意用可被 zhparser 稳定切出的拉丁词,避免种子 FTS 因分词差异而失稳
SEED_NAME = "Zephyrus"
NEAR_NAME = "Borealis"
FAR_NAME = "Cascadia"
LONE_NAME = "Solitaire"

# 一篇普通文档切成四段。行区间刻意拉开,使共现(shared_source,窗口 20 行)
# 只在 seed 与 near 之间成立,far / lone 必须靠真正的关系边或根本到不了。
_CHUNKS = {
    # label: (start_line, end_line, 正文)
    "seed": (1, 5, f"{SEED_NAME} 是本文档的主实体,后文围绕它展开。"),
    "near": (10, 15, f"{NEAR_NAME} 归属于 {SEED_NAME},并进一步包含 {FAR_NAME}。"),
    "far": (40, 45, f"{FAR_NAME} 的细则单独成段,只有沿关系边两跳才够得着。"),
    "lone": (99, 105, f"{LONE_NAME} 与主实体之间没有任何边。"),
}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    # 只保证 baseline 迁移在 head:本用例自建 org/kb,不清空既有租户数据
    ensure_schema()


@pytest.fixture
async def app_sessions():
    """应用账号(FORCE RLS 生效)—— 检索侧必须以生产同款身份读。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _set_org(session, org_id) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :v, true)"), {"v": str(org_id)}
    )


async def _set_build_snapshot(session, snapshot_id) -> None:
    await session.execute(
        text("SELECT set_config('app.build_snapshot_id', :v, true)"),
        {"v": str(snapshot_id) if snapshot_id else ""},
    )


def _digest(seed: str) -> str:
    return (seed * 64)[:64]


async def _seed_kb(session, *, org_id, kb_id, snapshot_id, name: str, digest: str) -> dict:
    """建一个"普通文档"知识库:文档 → revision → 两个切片 → 三个 canonical 实体
    → entity_mention + located_in 事实 → 证据 → 快照事实支撑。返回全部 id。"""

    await _set_org(session, org_id)
    await session.execute(
        text(
            "INSERT INTO knowledge_bases (id,org_id,name,kb_type,lifecycle_status,"
            "consumption_epoch) VALUES (:i,:o,:n,'mixed','active',0)"
        ),
        {"i": str(kb_id), "o": str(org_id), "n": name},
    )
    await session.execute(
        text(
            "INSERT INTO knowledge_snapshots (id,org_id,kb_id,status,revision_set_hash,"
            "embedding_fingerprint,config_fingerprint)"
            " VALUES (:i,:o,:k,'building',:h,CAST(:fp AS jsonb),:h)"
        ),
        {
            "i": str(snapshot_id),
            "o": str(org_id),
            "k": str(kb_id),
            "h": digest,
            "fp": '{"provider": "test", "model": "m", "dim": 8}',
        },
    )
    doc_id, revision_id = uuid4(), uuid4()
    await session.execute(
        text(
            "INSERT INTO source_documents (id,org_id,kb_id,filename,object_key,sha256,"
            "doc_type,status,lifecycle_status,version,progress,progress_done,progress_total)"
            " VALUES (:i,:o,:k,:f,:ok,:h,'general','ready','active',1,100,1,1)"
        ),
        {
            "i": str(doc_id),
            "o": str(org_id),
            "k": str(kb_id),
            "f": f"{name}.md",
            "ok": f"raw/{doc_id}.md",
            "h": digest,
        },
    )
    await session.execute(
        text(
            "INSERT INTO document_revisions (id,org_id,kb_id,doc_id,revision_no,sha256,"
            "original_object_key,status) VALUES (:i,:o,:k,:d,1,:h,:ok,'active')"
        ),
        {
            "i": str(revision_id),
            "o": str(org_id),
            "k": str(kb_id),
            "d": str(doc_id),
            "h": digest,
            "ok": f"raw/{doc_id}.md",
        },
    )

    await _set_build_snapshot(session, snapshot_id)
    insert_chunk = text(
        "INSERT INTO kb_chunks (id,org_id,kb_id,revision_id,snapshot_id,source_doc_id,"
        "content_kind,content,quarantined,source_ref,start_line,end_line)"
        " VALUES (:i,:o,:k,:r,:s,:d,'text',:c,:q,:ref,:sl,:el)"
    )
    common = {
        "o": str(org_id),
        "k": str(kb_id),
        "r": str(revision_id),
        "s": str(snapshot_id),
        "d": str(doc_id),
    }
    chunks: dict[str, object] = {}
    for index, (label, (start_line, end_line, body)) in enumerate(_CHUNKS.items()):
        chunk_id = uuid4()
        chunks[label] = chunk_id
        await session.execute(
            insert_chunk,
            {
                **common,
                "i": str(chunk_id),
                "c": body,
                "q": False,
                "ref": f"{name}.md#chunk{index}",
                "sl": start_line,
                "el": end_line,
            },
        )
    # 与 near 段覆盖同一行区间、但被隔离:新恢复路径绝不能把它带出来
    quarantined_chunk_id = uuid4()
    await session.execute(
        insert_chunk,
        {
            **common,
            "i": str(quarantined_chunk_id),
            "c": f"{_CHUNKS['near'][2]}(隔离副本)",
            "q": True,
            "ref": f"{name}.md#chunk1-quarantined",
            "sl": _CHUNKS["near"][0],
            "el": _CHUNKS["near"][1],
        },
    )
    entities = {}
    for label, display in (
        ("seed", SEED_NAME),
        ("near", NEAR_NAME),
        ("far", FAR_NAME),
        ("lone", LONE_NAME),
    ):
        entity_id = uuid4()
        entities[label] = entity_id
        await session.execute(
            text(
                "INSERT INTO canonical_entities (id,org_id,kb_id,entity_type,canonical_name)"
                " VALUES (:i,:o,:k,'concept',:n)"
            ),
            {"i": str(entity_id), "o": str(org_id), "k": str(kb_id), "n": display},
        )

    claim_specs = [
        # (label, predicate, subject entity, object entity, 证据行号)
        # 提及事实各自落在自己的段落里;两条关系事实都写在 near 段(第 11 行),
        # 因此 far 段只能靠"seed →(1 跳)near →(2 跳)far"的第二跳才够得着。
        ("mention_seed", "entity_mention", "seed", None, 1),
        ("mention_near", "entity_mention", "near", None, 11),
        ("mention_far", "entity_mention", "far", None, 41),
        ("mention_lone", "entity_mention", "lone", None, 100),
        ("rel_seed_near", "located_in", "seed", "near", 11),
        ("rel_near_far", "located_in", "near", "far", 11),
    ]
    claims: dict[str, dict] = {}
    for label, predicate, subject, obj, line in claim_specs:
        claim_id, evidence_id, support_id = uuid4(), uuid4(), uuid4()
        await session.execute(
            text(
                "INSERT INTO fact_claims (id,org_id,kb_id,subject_entity_id,object_entity_id,"
                "subject_type,subject_id,predicate,value_json,raw_payload,review_status)"
                " VALUES (:i,:o,:k,:se,:oe,'canonical_entity',:si,:p,'{}','{}','confirmed')"
            ),
            {
                "i": str(claim_id),
                "o": str(org_id),
                "k": str(kb_id),
                "se": str(entities[subject]),
                "oe": str(entities[obj]) if obj else None,
                "si": str(entities[subject]),
                "p": predicate,
            },
        )
        await session.execute(
            text(
                "INSERT INTO evidence_spans (id,org_id,kb_id,fact_claim_id,revision_id,"
                "start_line,end_line,quote_text) VALUES (:i,:o,:k,:c,:r,:sl,:el,:q)"
            ),
            {
                "i": str(evidence_id),
                "o": str(org_id),
                "k": str(kb_id),
                "c": str(claim_id),
                "r": str(revision_id),
                "sl": line,
                "el": line,
                "q": f"{label} 证据原文",
            },
        )
        await session.execute(
            text(
                "INSERT INTO snapshot_fact_supports (id,org_id,kb_id,snapshot_id,"
                "fact_claim_id,evidence_span_id,revision_id,doc_id)"
                " VALUES (:i,:o,:k,:s,:c,:e,:r,:d)"
            ),
            {
                "i": str(support_id),
                "o": str(org_id),
                "k": str(kb_id),
                "s": str(snapshot_id),
                "c": str(claim_id),
                "e": str(evidence_id),
                "r": str(revision_id),
                "d": str(doc_id),
            },
        )
        claims[label] = {"id": claim_id, "evidence_id": evidence_id}

    await _set_build_snapshot(session, None)
    return {
        "org_id": org_id,
        "kb_id": kb_id,
        "snapshot_id": snapshot_id,
        "doc_id": doc_id,
        "revision_id": revision_id,
        "chunks": chunks,
        "quarantined_chunk_id": quarantined_chunk_id,
        "entities": entities,
        "claims": claims,
    }


async def _build_graph_projection(session, seeded: dict) -> None:
    await _set_org(session, seeded["org_id"])
    await _set_build_snapshot(session, seeded["snapshot_id"])
    context = SnapshotBuildContext(
        snapshot_id=seeded["snapshot_id"],
        org_id=seeded["org_id"],
        kb_id=seeded["kb_id"],
        revision_manifest=(
            {"revision_id": str(seeded["revision_id"]), "doc_id": str(seeded["doc_id"])},
        ),
        fact_claim_ids=tuple(entry["id"] for entry in seeded["claims"].values()),
        embedding_fingerprint={"provider": "test", "model": "m", "dim": 8},
        config_manifest={},
    )
    stats = await GraphProjectionBuilder().build(session, context)
    assert stats["node_count"] > 0 and stats["row_count"] > 0, stats
    await _set_build_snapshot(session, None)


async def _activate(session, seeded: dict) -> None:
    await _set_org(session, seeded["org_id"])
    await session.execute(
        text(
            "UPDATE knowledge_snapshots SET status='active', ready_at=now(),"
            " activated_at=now() WHERE id=:i"
        ),
        {"i": str(seeded["snapshot_id"])},
    )
    await session.execute(
        text("UPDATE knowledge_bases SET active_snapshot_id=:s WHERE id=:k"),
        {"s": str(seeded["snapshot_id"]), "k": str(seeded["kb_id"])},
    )


async def _open_build_scope(session, seeded: dict) -> None:
    """把已发布快照临时退回 building —— 投影表的写入受 RLS 的 snapshot_*_scope
    约束(只有 building 快照 + 匹配的 app.build_snapshot_id 才允许写),用例要往
    快照里补数据就必须走同一道闸,不能绕过。"""
    await _set_org(session, seeded["org_id"])
    await _set_build_snapshot(session, seeded["snapshot_id"])
    await session.execute(
        text(
            "UPDATE knowledge_snapshots SET status='building', ready_at=NULL,"
            " activated_at=NULL WHERE id=:i"
        ),
        {"i": str(seeded["snapshot_id"])},
    )


async def _close_build_scope(session, seeded: dict) -> None:
    await session.execute(
        text(
            "UPDATE knowledge_snapshots SET status='active', ready_at=now(),"
            " activated_at=now() WHERE id=:i"
        ),
        {"i": str(seeded["snapshot_id"])},
    )
    await _set_build_snapshot(session, None)


@pytest.fixture
async def scene():
    """两个 org 各一个已发布知识库,内容同构 —— 顺带验证跨租户隔离。"""
    settings = get_settings()
    org_a, org_b = uuid4(), uuid4()
    kb_a, kb_b = uuid4(), uuid4()
    snapshot_a, snapshot_b = uuid4(), uuid4()

    engine = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        for org, slug in ((org_a, "a"), (org_b, "b")):
            await session.execute(
                text(
                    "INSERT INTO organizations (id,name,slug,is_active)"
                    " VALUES (:i,:n,:s,true) ON CONFLICT DO NOTHING"
                ),
                {"i": str(org), "n": f"graph-search-{slug}", "s": f"gs-{org.hex[:8]}"},
            )
        await session.commit()

        seeded_a = await _seed_kb(
            session,
            org_id=org_a,
            kb_id=kb_a,
            snapshot_id=snapshot_a,
            name="graph-recall-a",
            digest=_digest("a"),
        )
        await session.commit()
        seeded_b = await _seed_kb(
            session,
            org_id=org_b,
            kb_id=kb_b,
            snapshot_id=snapshot_b,
            name="graph-recall-b",
            digest=_digest("b"),
        )
        await session.commit()
        for seeded in (seeded_a, seeded_b):
            await _build_graph_projection(session, seeded)
            await session.commit()
            await _activate(session, seeded)
            await session.commit()

    yield {"a": seeded_a, "b": seeded_b, "sessions": maker}

    async with maker() as session:
        for seeded in (seeded_a, seeded_b):
            # set_config(..., true) 是**事务局部**的:整段清理必须在一个事务里
            # 走完,中途 commit 会把 org / build 上下文清空,后续 DELETE 就被 RLS
            # 静默吃掉(rowcount=0),测试数据会一路堆在库里。
            await _open_build_scope(session, seeded)
            await session.execute(
                text("UPDATE knowledge_bases SET active_snapshot_id=NULL WHERE id=:k"),
                {"k": str(seeded["kb_id"])},
            )
            for table in (
                "kb_snapshot_entity_node_supports",
                "kb_graph_edges",
                "kb_snapshot_entity_nodes",
                "kb_chunks",
                "snapshot_fact_supports",
                "evidence_spans",
                "fact_claims",
                "canonical_entities",
                "document_revisions",
                "source_documents",
                "knowledge_snapshots",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE kb_id=:k"), {"k": str(seeded["kb_id"])}
                )
            removed = await session.execute(
                text("DELETE FROM knowledge_bases WHERE id=:k"), {"k": str(seeded["kb_id"])}
            )
            assert removed.rowcount == 1, "用例库没删干净,真库会被测试数据污染"
            await session.commit()
        await session.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [str(org_a), str(org_b)]},
        )
        await session.commit()
    await engine.dispose()


async def _recall(app_sessions, seeded: dict, query: str, *, max_hops: int = 2, **kwargs):
    async with app_sessions() as session:
        await _set_org(session, seeded["org_id"])
        return await graph_recall_candidates(
            session,
            query,
            kb_ids=kwargs.get("kb_ids", [seeded["kb_id"]]),
            max_hops=max_hops,
            top_k=kwargs.get("top_k", 20),
            as_of=kwargs.get("as_of", date.today()),
        )


# ---------------------------------------------------------------------------
# 主链路:普通文档也能有图谱增益
# ---------------------------------------------------------------------------


async def test_general_document_mentions_recall_the_source_chunk(app_sessions, scene) -> None:
    """核心断言:只有 entity_mention + 关系事实、一张卡片都没有的普通文档,
    图谱通道也必须返回非空——这正是本次修复前恒为 0 的那条路径。"""
    seeded = scene["a"]
    rows = await _recall(app_sessions, seeded, SEED_NAME, max_hops=1)

    assert rows, "普通文档的图谱召回不能为空(修复前恒为 0)"
    assert all(row.restore_mode == CHUNK_RESTORE for row in rows)
    assert {row.chunk.id for row in rows} == {seeded["chunks"]["near"]}
    [row] = rows
    assert row.kind == "chunk" and row.entity_id == seeded["chunks"]["near"]
    assert row.hops == 1 and row.score > 0
    assert "located_in" in row.predicates
    assert row.anchor_entity_id == seeded["entities"]["near"]
    assert NEAR_NAME in row.chunk.content


async def test_two_hop_expansion_reaches_the_far_entity(app_sessions, scene) -> None:
    """2 跳必须真的多走一步:1 跳只够到 near 段,2 跳才带回 far 段。"""
    seeded = scene["a"]
    one_hop = {row.chunk.id for row in await _recall(app_sessions, seeded, SEED_NAME, max_hops=1)}
    rows = await _recall(app_sessions, seeded, SEED_NAME, max_hops=2)
    two_hop = {row.chunk.id for row in rows}

    assert one_hop == {seeded["chunks"]["near"]}
    assert two_hop == {seeded["chunks"]["near"], seeded["chunks"]["far"]}
    [far_row] = [row for row in rows if row.chunk.id == seeded["chunks"]["far"]]
    assert far_row.hops == 2
    assert far_row.anchor_entity_id == seeded["entities"]["far"]
    assert len(far_row.edge_ids) == 2


async def test_unlinked_entity_is_not_recalled(app_sessions, scene) -> None:
    """图谱召回是"沿边扩展"而非全库扫:没有边连过来的实体不该被带出来。"""
    seeded = scene["a"]
    rows = await _recall(app_sessions, seeded, SEED_NAME)

    assert seeded["entities"]["lone"] not in {row.anchor_entity_id for row in rows}
    assert seeded["chunks"]["lone"] not in {row.chunk.id for row in rows}


async def test_seed_without_graph_neighbours_returns_nothing(app_sessions, scene) -> None:
    """孤立实体自己被 FTS 命中,但没有可扩展的邻居 → 空结果,不是异常。"""
    assert await _recall(app_sessions, scene["a"], LONE_NAME) == []


async def test_unknown_query_returns_nothing(app_sessions, scene) -> None:
    assert await _recall(app_sessions, scene["a"], "Nonexistentia") == []


@pytest.mark.parametrize(
    "question",
    [
        f"{SEED_NAME} 是干嘛的",
        f"{SEED_NAME} 是什么",
        f"介绍一下 {SEED_NAME}",
        f"what is {SEED_NAME}",
    ],
)
async def test_natural_language_question_finds_the_same_seed(
    app_sessions, scene, question
) -> None:
    """自然问句必须与裸实体名召回一致。

    种子发现原先用 ``websearch_to_tsquery`` 的**合取**匹配实体名,问句会被编译成
    ``'zephyrus' & '是' <-> '干'``——要求实体名自身含疑问词,于是图谱一路对所有
    问句形态恒为空。这是与稀疏通道同源的一个 bad case,必须一起守住。
    """
    seeded = scene["a"]
    baseline = {row.chunk.id for row in await _recall(app_sessions, seeded, SEED_NAME)}
    asked = {row.chunk.id for row in await _recall(app_sessions, seeded, question)}

    assert baseline, "基线不能为空,否则本用例无意义"
    assert asked == baseline


async def test_question_words_alone_do_not_seed_the_graph(app_sessions, scene) -> None:
    """析取化不能退化成"问句就全库扩展":没有实体名就没有种子。"""
    assert await _recall(app_sessions, scene["a"], "是干嘛的") == []
    assert await _recall(app_sessions, scene["a"], "介绍一下这个是什么") == []


@pytest.mark.parametrize("bad_hops", [0, 3, -1])
async def test_max_hops_is_validated(app_sessions, scene, bad_hops) -> None:
    with pytest.raises(ValueError):
        await _recall(app_sessions, scene["a"], SEED_NAME, max_hops=bad_hops)


async def test_top_k_must_be_positive(app_sessions, scene) -> None:
    with pytest.raises(ValueError):
        await _recall(app_sessions, scene["a"], SEED_NAME, top_k=0)


# ---------------------------------------------------------------------------
# 卡片路径:wiki 卡片的 kind/predicate 错配回归
# ---------------------------------------------------------------------------


async def test_wiki_page_card_is_restored_despite_kind_rename(app_sessions, scene) -> None:
    """wiki 卡片建卡时 kind 被改名成 "page",与谓词 "wiki_page" 不同名。

    旧实现拿 ``card_ref[0] != claim.predicate`` 判定,于是所有 wiki 卡片
    100% 被静默丢弃。这里在图谱邻居实体上挂一条 wiki_page 事实与对应卡片,
    断言它能以 ``kind == "page"`` 的卡片候选恢复出来。
    """
    seeded = scene["a"]
    maker = scene["sessions"]
    claim_id, evidence_id, support_id = uuid4(), uuid4(), uuid4()
    card_entity_id = projection_row_id(seeded["snapshot_id"], "wiki_page", claim_id)
    card_chunk_id = uuid4()

    async with maker() as session:
        await _open_build_scope(session, seeded)
        await session.execute(
            text(
                "INSERT INTO fact_claims (id,org_id,kb_id,subject_entity_id,subject_type,"
                "subject_id,predicate,value_json,raw_payload,review_status)"
                " VALUES (:i,:o,:k,:se,'canonical_entity',:si,'wiki_page','{}','{}','confirmed')"
            ),
            {
                "i": str(claim_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "se": str(seeded["entities"]["near"]),
                "si": str(seeded["entities"]["near"]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO evidence_spans (id,org_id,kb_id,fact_claim_id,revision_id,"
                "start_line,end_line,quote_text) VALUES (:i,:o,:k,:c,:r,11,11,'wiki 证据')"
            ),
            {
                "i": str(evidence_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "c": str(claim_id),
                "r": str(seeded["revision_id"]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO snapshot_fact_supports (id,org_id,kb_id,snapshot_id,"
                "fact_claim_id,evidence_span_id,revision_id,doc_id)"
                " VALUES (:i,:o,:k,:s,:c,:e,:r,:d)"
            ),
            {
                "i": str(support_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "s": str(seeded["snapshot_id"]),
                "c": str(claim_id),
                "e": str(evidence_id),
                "r": str(seeded["revision_id"]),
                "d": str(seeded["doc_id"]),
            },
        )
        # 节点支撑:让这条 wiki 事实成为 near 实体的 fact 支撑(与 GraphProjectionBuilder 同形)
        await session.execute(
            text(
                "INSERT INTO kb_snapshot_entity_node_supports (id,org_id,kb_id,snapshot_id,"
                "entity_id,support_type,fact_support_id)"
                " VALUES (:i,:o,:k,:s,:e,'fact',:f)"
            ),
            {
                "i": str(uuid4()),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "s": str(seeded["snapshot_id"]),
                "e": str(seeded["entities"]["near"]),
                "f": str(support_id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO kb_chunks (id,org_id,kb_id,snapshot_id,content_kind,content,"
                "quarantined,source_ref,meta)"
                " VALUES (:i,:o,:k,:s,'text',:c,false,:ref,"
                " jsonb_build_object('fact_claim_id', CAST(:claim AS text)))"
            ),
            {
                "i": str(card_chunk_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "s": str(seeded["snapshot_id"]),
                "c": f"{NEAR_NAME} wiki 卡片正文",
                "ref": card_source_ref("page", card_entity_id),
                "claim": str(claim_id),
            },
        )
        await _close_build_scope(session, seeded)
        await session.commit()

    rows = await _recall(app_sessions, seeded, SEED_NAME, max_hops=1)
    cards = [row for row in rows if row.restore_mode == CARD_RESTORE]
    assert cards, "wiki 卡片被静默丢弃(kind/predicate 错配未修复)"
    [card] = cards
    assert card.kind == "page", "卡片 kind 必须是 page,才能被 _restore_card_hits 反查 KbPage"
    assert card.entity_id == card_entity_id
    assert card.chunk.id == card_chunk_id


async def test_card_ref_pointing_at_a_foreign_claim_is_rejected(app_sessions, scene) -> None:
    """卡片的 source_ref 必须与 (snapshot, predicate, claim) 严格对应:
    伪造一张 entity_id 对不上投影行 id 的卡片,不得被恢复。"""
    seeded = scene["a"]
    maker = scene["sessions"]
    claim_id, evidence_id, support_id = uuid4(), uuid4(), uuid4()
    forged_chunk_id = uuid4()

    async with maker() as session:
        await _open_build_scope(session, seeded)
        await session.execute(
            text(
                "INSERT INTO fact_claims (id,org_id,kb_id,subject_entity_id,subject_type,"
                "subject_id,predicate,value_json,raw_payload,review_status)"
                " VALUES (:i,:o,:k,:se,'canonical_entity',:si,'wiki_page','{}','{}','confirmed')"
            ),
            {
                "i": str(claim_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "se": str(seeded["entities"]["near"]),
                "si": str(seeded["entities"]["near"]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO evidence_spans (id,org_id,kb_id,fact_claim_id,revision_id,"
                "start_line,end_line,quote_text) VALUES (:i,:o,:k,:c,:r,11,11,'伪造卡片证据')"
            ),
            {
                "i": str(evidence_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "c": str(claim_id),
                "r": str(seeded["revision_id"]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO snapshot_fact_supports (id,org_id,kb_id,snapshot_id,"
                "fact_claim_id,evidence_span_id,revision_id,doc_id)"
                " VALUES (:i,:o,:k,:s,:c,:e,:r,:d)"
            ),
            {
                "i": str(support_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "s": str(seeded["snapshot_id"]),
                "c": str(claim_id),
                "e": str(evidence_id),
                "r": str(seeded["revision_id"]),
                "d": str(seeded["doc_id"]),
            },
        )
        await session.execute(
            text(
                "INSERT INTO kb_chunks (id,org_id,kb_id,snapshot_id,content_kind,content,"
                "quarantined,source_ref,meta)"
                " VALUES (:i,:o,:k,:s,'text','伪造卡片',false,:ref,"
                " jsonb_build_object('fact_claim_id', CAST(:claim AS text)))"
            ),
            {
                "i": str(forged_chunk_id),
                "o": str(seeded["org_id"]),
                "k": str(seeded["kb_id"]),
                "s": str(seeded["snapshot_id"]),
                # entity_id 随机 → 与 projection_row_id 对不上
                "ref": card_source_ref("page", uuid4()),
                "claim": str(claim_id),
            },
        )
        await _close_build_scope(session, seeded)
        await session.commit()

    rows = await _recall(app_sessions, seeded, SEED_NAME, max_hops=1)
    assert forged_chunk_id not in {row.chunk.id for row in rows}


# ---------------------------------------------------------------------------
# 安全边界:新的恢复路径不得比旧路径宽松
# ---------------------------------------------------------------------------


async def test_quarantined_chunk_never_leaks(app_sessions, scene) -> None:
    """被隔离的切片与正常切片覆盖同一行区间,只能出正常那一条。"""
    seeded = scene["a"]
    rows = await _recall(app_sessions, seeded, SEED_NAME)

    assert rows
    assert seeded["quarantined_chunk_id"] not in {row.chunk.id for row in rows}


async def test_unpublished_snapshot_is_invisible(app_sessions, scene) -> None:
    """把 active_snapshot_id 摘掉(快照未发布)后,图谱通道必须整体空。"""
    seeded = scene["a"]
    maker = scene["sessions"]
    async with maker() as session:
        await _set_org(session, seeded["org_id"])
        await session.execute(
            text("UPDATE knowledge_bases SET active_snapshot_id=NULL WHERE id=:k"),
            {"k": str(seeded["kb_id"])},
        )
        await session.commit()
    assert await _recall(app_sessions, seeded, SEED_NAME) == []


async def test_other_org_cannot_recall_across_tenants(app_sessions, scene) -> None:
    """org B 的会话即便点名 org A 的 kb_id,也一行都拿不到。"""
    a, b = scene["a"], scene["b"]
    async with app_sessions() as session:
        await _set_org(session, b["org_id"])
        rows = await graph_recall_candidates(
            session,
            SEED_NAME,
            kb_ids=[a["kb_id"]],
            max_hops=2,
            top_k=20,
            as_of=date.today(),
        )
    assert rows == [], "跨租户泄漏:org B 不该读到 org A 的图谱内容"


async def test_kb_scope_is_respected_within_one_org(app_sessions, scene) -> None:
    """同一 org 内点名别的 kb,也不该串味(这里用 org B 自己的两种范围对照)。"""
    b = scene["b"]
    scoped = await _recall(app_sessions, b, SEED_NAME)
    assert scoped, "org B 自己的库应当正常召回"
    assert {row.chunk.kb_id for row in scoped} == {b["kb_id"]}

    empty = await _recall(app_sessions, b, SEED_NAME, kb_ids=[uuid4()])
    assert empty == []


async def test_expired_edges_are_not_traversed(app_sessions, scene) -> None:
    """把边的 valid_to 推到过去,as_of 之后这条边不能再带出任何节点。"""
    seeded = scene["a"]
    maker = scene["sessions"]
    expired = date.today() - timedelta(days=1)
    async with maker() as session:
        await _open_build_scope(session, seeded)
        updated = await session.execute(
            text("UPDATE kb_graph_edges SET valid_to=:d WHERE kb_id=:k"),
            {"d": expired, "k": str(seeded["kb_id"])},
        )
        assert updated.rowcount > 0, "边没被改到,后面的断言就没有意义"
        await _close_build_scope(session, seeded)
        await session.commit()

    assert await _recall(app_sessions, seeded, SEED_NAME) == []
    # 时间旅行到边仍有效的那天,召回应恢复 —— 证明上面为空确实是时效性所致
    revived = await _recall(app_sessions, seeded, SEED_NAME, as_of=expired - timedelta(days=1))
    assert revived


async def test_withdrawn_document_stops_recall(app_sessions, scene) -> None:
    """文档退场(lifecycle_status != active)后,证据支撑立刻失效,
    新的 chunk 恢复路径同样要跟着关门(与 graph.py 的 _support_is_current 同口径)。"""
    seeded = scene["a"]
    maker = scene["sessions"]
    async with maker() as session:
        await _set_org(session, seeded["org_id"])
        await session.execute(
            text("UPDATE source_documents SET lifecycle_status='withdrawn' WHERE id=:i"),
            {"i": str(seeded["doc_id"])},
        )
        await session.commit()
    assert await _recall(app_sessions, seeded, SEED_NAME) == []
