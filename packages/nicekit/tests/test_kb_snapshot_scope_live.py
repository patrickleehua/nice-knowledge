"""快照作用域的 RESTRICTIVE 策略 —— 真库验证。

这组策略把"这一行属于哪个快照"变成数据库级约束,是投影表的**第二道防线**:
应用层的 effective_scope 谓词是第一道,而 RLS 兜住的正是"哪条查询忘了带过滤"
这类错误。SDK 化时整组漏搬,代码里遍布的 set_config('app.build_snapshot_id')
因此在配合一套并不存在的策略。
"""

from uuid import uuid4

import pytest
from _tenancy_pg import create_tenancy_schema, drop_tenancy_schema
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nicekit.core.config import get_settings

pytestmark = pytest.mark.live

# 每个快照的 (kb_id, revision_set_hash, embedding_fingerprint, config_fingerprint)
# 有唯一约束,两个快照必须给不同指纹
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_INSERT_CHUNK = (
    "INSERT INTO kb_chunks (id,org_id,kb_id,snapshot_id,content,content_kind)"
    " VALUES (:i,:o,:k,:s,:c,'text')"
)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    create_tenancy_schema()
    yield
    drop_tenancy_schema()


@pytest.fixture
async def app_factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _set(session, key: str, value: str) -> None:
    await session.execute(
        text("SELECT set_config(:k, :v, true)"), {"k": key, "v": value}
    )


@pytest.fixture
async def scene():
    """一个 KB + 两个快照(active / retired),各挂一行 chunk。"""
    settings = get_settings()
    org = settings.platform_org_id
    kb_id, snap_active, snap_retired = uuid4(), uuid4(), uuid4()

    engine = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    setup = async_sessionmaker(engine, expire_on_commit=False)
    async with setup() as s:
        await _set(s, "app.current_org_id", str(org))
        await s.execute(
            text(
                "INSERT INTO knowledge_bases"
                " (id,org_id,name,kb_type,lifecycle_status,consumption_epoch)"
                " VALUES (:i,:o,'snapshot-scope-kb','mixed','active',0)"
            ),
            {"i": str(kb_id), "o": str(org)},
        )
        # 先都建成 building:写投影受 snapshot_insert_scope 约束,只有 building 能写
        for snap, digest in ((snap_active, _HASH_A), (snap_retired, _HASH_B)):
            await s.execute(
                text(
                    "INSERT INTO knowledge_snapshots"
                    " (id,org_id,kb_id,status,revision_set_hash,embedding_fingerprint,"
                    "  config_fingerprint)"
                    " VALUES (:i,:o,:k,'building',:h,'{}',:h)"
                ),
                {"i": str(snap), "o": str(org), "k": str(kb_id), "h": digest},
            )
        for snap, body in ((snap_active, "active-chunk"), (snap_retired, "old-chunk")):
            await _set(s, "app.build_snapshot_id", str(snap))
            await s.execute(
                text(_INSERT_CHUNK),
                {
                    "i": str(uuid4()), "o": str(org), "k": str(kb_id),
                    "s": str(snap), "c": body,
                },
            )
        await _set(s, "app.build_snapshot_id", "")
        await s.execute(
            # status 与时间戳有配对 CHECK:active 要 activated_at、retired 要 retired_at
            text(
                "UPDATE knowledge_snapshots SET status='active',"
                " ready_at=now(), activated_at=now() WHERE id=:i"
            ),
            {"i": str(snap_active)},
        )
        await s.execute(
            text(
                "UPDATE knowledge_snapshots SET status='retired',"
                " ready_at=now(), activated_at=now(), retired_at=now() WHERE id=:i"
            ),
            {"i": str(snap_retired)},
        )
        await s.execute(
            text("UPDATE knowledge_bases SET active_snapshot_id=:s WHERE id=:k"),
            {"s": str(snap_active), "k": str(kb_id)},
        )
        await s.commit()

    yield {"org": org, "kb": kb_id, "active": snap_active, "retired": snap_retired}

    async with setup() as s:
        await _set(s, "app.current_org_id", str(org))
        await s.execute(
            text("UPDATE knowledge_bases SET active_snapshot_id=NULL WHERE id=:k"),
            {"k": str(kb_id)},
        )
        await s.execute(
            text(
                "UPDATE knowledge_snapshots SET status='retired',"
                " ready_at=COALESCE(ready_at, now()),"
                " activated_at=COALESCE(activated_at, now()), retired_at=now()"
                " WHERE kb_id=:k AND status <> 'retired'"
            ),
            {"k": str(kb_id)},
        )
        for snap in (snap_active, snap_retired):
            await _set(s, "app.build_snapshot_id", str(snap))
            await s.execute(
                text("DELETE FROM kb_chunks WHERE kb_id=:k AND snapshot_id=:s"),
                {"k": str(kb_id), "s": str(snap)},
            )
        await _set(s, "app.build_snapshot_id", "")
        await s.execute(
            text("DELETE FROM knowledge_snapshots WHERE kb_id=:k"), {"k": str(kb_id)}
        )
        await s.execute(text("DELETE FROM knowledge_bases WHERE id=:k"), {"k": str(kb_id)})
        await s.commit()
    await engine.dispose()


async def test_only_active_snapshot_rows_are_visible(app_factory, scene) -> None:
    """退休快照的行不该出现在常规查询里——哪怕查询忘了带快照过滤。"""
    async with app_factory() as s:
        await _set(s, "app.current_org_id", str(scene["org"]))
        rows = (
            await s.execute(
                text("SELECT content FROM kb_chunks WHERE kb_id=:k"),
                {"k": str(scene["kb"])},
            )
        ).all()
    assert {r.content for r in rows} == {"active-chunk"}


async def test_build_guc_opens_the_snapshot_under_construction(app_factory, scene) -> None:
    """构建期要读得到自己正在写的快照,否则 builder 无法自查产物。"""
    async with app_factory() as s:
        await _set(s, "app.current_org_id", str(scene["org"]))
        await _set(s, "app.build_snapshot_id", str(scene["retired"]))
        rows = (
            await s.execute(
                text("SELECT content FROM kb_chunks WHERE kb_id=:k"),
                {"k": str(scene["kb"])},
            )
        ).all()
    assert {r.content for r in rows} == {"active-chunk", "old-chunk"}


async def test_active_rows_survive_a_delete_without_guc(app_factory, scene) -> None:
    """线上快照的投影不能被随手删掉:purge/GC 必须显式指定快照。"""
    async with app_factory() as s:
        await _set(s, "app.current_org_id", str(scene["org"]))
        deleted = await s.execute(
            text("DELETE FROM kb_chunks WHERE kb_id=:k"), {"k": str(scene["kb"])}
        )
        assert deleted.rowcount == 0
        await s.rollback()


async def test_cannot_insert_into_a_published_snapshot(app_factory, scene) -> None:
    """只能往 building 的快照写:防止往已发布的快照里偷加内容。"""
    async with app_factory() as s:
        await _set(s, "app.current_org_id", str(scene["org"]))
        await _set(s, "app.build_snapshot_id", str(scene["active"]))
        with pytest.raises(ProgrammingError):
            await s.execute(
                text(_INSERT_CHUNK),
                {
                    "i": str(uuid4()), "o": str(scene["org"]), "k": str(scene["kb"]),
                    "s": str(scene["active"]), "c": "sneaked",
                },
            )
        await s.rollback()
