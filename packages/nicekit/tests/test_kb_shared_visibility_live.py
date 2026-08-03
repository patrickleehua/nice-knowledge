"""已发布知识的三层可见性(本 org / 平台 org / 被分享)—— 真库验证。

这条链路曾经整体失效:SDK 化时把源项目的读写分离策略(``org_read`` 放行三层、
``org_write`` 只认本 org)统一成了单一的 ``org_isolation`` (ALL),读侧两个旁路
就此消失。表现是"功能齐全但没人能用":``kb_shares`` 的建/列/删端点、purge 时的
清理、``visible_kb_filter`` 的三分支 OR、``_layer()`` 的 tenant/platform/shared
分级全在,却没有任何读路径能让被授权方真正读到一行。

所以这里的断言不是走过场:**放宽可见性是危险方向的改动**,写错就是跨租户泄漏。
每条都同时验证"该看见的看得见"与"不该写的写不了"。
"""

from uuid import uuid4

import pytest
from _tenancy_pg import create_tenancy_schema, drop_tenancy_schema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nicekit.core.config import get_settings

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def _schema():
    create_tenancy_schema()
    yield
    drop_tenancy_schema()


@pytest.fixture
async def factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def scenario(factory):
    """平台公共库 + A 私有库 + B 分享给 A 的库。用 migrator 建(绕开 RLS)。"""
    settings = get_settings()
    platform, org_a, org_b = settings.platform_org_id, uuid4(), uuid4()
    kb_platform, kb_a, kb_b = uuid4(), uuid4(), uuid4()

    engine = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    setup = async_sessionmaker(engine, expire_on_commit=False)
    async with setup() as s:
        for org, slug in ((org_a, "a"), (org_b, "b")):
            await s.execute(
                text(
                    "INSERT INTO organizations (id,name,slug,is_active) "
                    "VALUES (:i,:n,:s,true) ON CONFLICT DO NOTHING"
                ),
                {"i": str(org), "n": slug, "s": f"vis-{slug}-{org.hex[:6]}"},
            )
        # migrator 也受 FORCE RLS 约束(它没有 BYPASSRLS,这是刻意的),
        # 所以每插一行都要先把上下文切到该行所属的 org,否则 WITH CHECK 拒绝。
        for kb_id, org, name in (
            (kb_platform, platform, "platform-kb"),
            (kb_a, org_a, "a-private-kb"),
            (kb_b, org_b, "b-shared-kb"),
        ):
            await s.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"),
                {"o": str(org)},
            )
            await s.execute(
                text(
                    "INSERT INTO knowledge_bases "
                    "(id,org_id,name,kb_type,lifecycle_status,consumption_epoch) "
                    "VALUES (:i,:o,:n,'mixed','active',0)"
                ),
                {"i": str(kb_id), "o": str(org), "n": name},
            )
            await s.commit()

        # B 把自己的库分享给 A(这行归 B 所有,上下文切到 B)
        await s.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_b)}
        )
        await s.execute(
            text(
                "INSERT INTO kb_shares (id,org_id,kb_id,grantee_org_id) "
                "VALUES (:i,:o,:k,:g)"
            ),
            {"i": str(uuid4()), "o": str(org_b), "k": str(kb_b), "g": str(org_a)},
        )
        await s.commit()

    yield {"platform": platform, "a": org_a, "b": org_b}

    async with setup() as s:
        for org, ids in (
            (org_b, [str(kb_b)]),
            (org_a, [str(kb_a)]),
            (platform, [str(kb_platform)]),
        ):
            await s.execute(
                text("SELECT set_config('app.current_org_id', :o, true)"),
                {"o": str(org)},
            )
            if org == org_b:
                await s.execute(
                    text("DELETE FROM kb_shares WHERE kb_id = :k"), {"k": str(kb_b)}
                )
            await s.execute(
                text("DELETE FROM knowledge_bases WHERE id = ANY(:ids)"), {"ids": ids}
            )
            await s.commit()
        await s.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [str(org_a), str(org_b)]},
        )
        await s.commit()
    await engine.dispose()


async def _names_seen_by(factory, org_id) -> set[str]:
    """以应用账号(受 FORCE RLS)+ 指定 org 上下文,读全部可见库名。"""
    async with factory() as s:
        await s.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
        )
        rows = (await s.execute(text("SELECT name FROM knowledge_bases"))).all()
    return {r.name for r in rows}


async def test_tenant_sees_own_platform_and_shared(factory, scenario) -> None:
    seen = await _names_seen_by(factory, scenario["a"])
    assert "a-private-kb" in seen, "自己的库"
    assert "platform-kb" in seen, "平台公共库应对所有租户可读"
    assert "b-shared-kb" in seen, "被显式分享的库应可读(分享链路曾整体失效)"


async def test_sharing_is_one_way_and_private_stays_private(factory, scenario) -> None:
    """B 分享给 A,不等于 A 的东西对 B 可见。"""
    seen = await _names_seen_by(factory, scenario["b"])
    assert "b-shared-kb" in seen and "platform-kb" in seen
    assert "a-private-kb" not in seen, "跨租户泄漏:A 的私有库不该对 B 可见"


async def test_platform_and_shared_are_read_only(factory, scenario) -> None:
    """能读不等于能改:写入面只受 org_isolation 约束,一行都不该动到。"""
    async with factory() as s:
        await s.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"),
            {"o": str(scenario["a"])},
        )
        for target in ("platform-kb", "b-shared-kb"):
            updated = await s.execute(
                text("UPDATE knowledge_bases SET name = 'tampered' WHERE name = :n"),
                {"n": target},
            )
            assert updated.rowcount == 0, f"{target} 不该被租户 A 改写"
            deleted = await s.execute(
                text("DELETE FROM knowledge_bases WHERE name = :n"), {"n": target}
            )
            assert deleted.rowcount == 0, f"{target} 不该被租户 A 删除"
        await s.rollback()


async def test_grantee_can_read_the_share_row_itself(factory, scenario) -> None:
    """分享链路的前提:被授权方要读得到"谁分享给了我"。

    RLS 策略里的子查询同样受被查表自己的 RLS 约束。kb_shares 原本只有 owner
    可见,导致上面各表的分享分支子查询恒为空、整条链路静默失效。
    """
    async with factory() as s:
        await s.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"),
            {"o": str(scenario["a"])},
        )
        count = (await s.execute(text("SELECT count(*) FROM kb_shares"))).scalar_one()
    assert count == 1, "grantee 应能看到分享给自己的那条记录"
