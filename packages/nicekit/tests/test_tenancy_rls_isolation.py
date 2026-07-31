"""跨租户隔离证明(迁移自 TF backend/tests/test_rls_isolation.py):
用应用账号(受 FORCE RLS 约束)直接操作数据库。

覆盖路径:
1. 正常路径:org1 上下文只能看到 org1 的行;
2. "开发忘写 where"路径:不带 org 过滤的全表 select 也只返回本 org;
3. 未设置 org 上下文:什么都看不到(NULLIF fail-closed);
4. WITH CHECK:在 org1 上下文里插入 org2 的行被拒绝;
5. platform_read 旁路:平台 org 上下文可读全租户 usage_daily,但写入面不变,
   且未加旁路的表(audit_logs)不受影响。

与 TF 的差异:不依赖 alembic 已建表——fixture 用 migrator 账号 create_all 建
tenancy 表,并用 nicekit/migrations/rls.py 施加 RLS(见 _tenancy_pg.py),
因此本文件同时验证了 helper 生成的 SQL 在真实 PG 上的正确性。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from _tenancy_pg import create_tenancy_schema, drop_tenancy_schema
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from nicekit.core.config import get_settings
from nicekit.migrations import rls
from nicekit.models.tenancy import AuditLog, Organization, UsageDaily

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def _schema():
    create_tenancy_schema()
    yield
    drop_tenancy_schema()


def _factory():
    # NullPool:live 用例逐测试新建 event loop,连接不跨 loop 复用
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _set_org(session, org_id) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def _insert_log(factory, org_id, action: str) -> None:
    async with factory() as session:
        await _set_org(session, org_id)
        session.add(AuditLog(org_id=org_id, action=action, entity_type="test"))
        await session.commit()


@pytest.fixture
async def two_orgs():
    engine, factory = _factory()
    org1, org2 = uuid4(), uuid4()
    async with factory() as session:
        session.add(Organization(id=org1, name="测试租户一", slug=f"t1-{org1.hex[:8]}"))
        session.add(Organization(id=org2, name="测试租户二", slug=f"t2-{org2.hex[:8]}"))
        await session.commit()
    yield factory, org1, org2
    # 行级清理从简:schema 在模块结束时整体 DROP(见 _schema fixture)
    await engine.dispose()


async def test_org_sees_only_own_rows(two_orgs) -> None:
    factory, org1, org2 = two_orgs
    await _insert_log(factory, org1, "org1_action")
    await _insert_log(factory, org2, "org2_action")

    async with factory() as session:
        await _set_org(session, org1)
        # 故意不带 where org_id:模拟开发漏写过滤条件
        rows = (await session.execute(text("SELECT org_id, action FROM audit_logs"))).all()
    assert rows, "org1 应能看到自己的行"
    assert all(str(r.org_id) == str(org1) for r in rows), "RLS 泄漏:看到了其他租户的行"


async def test_no_org_context_sees_nothing(two_orgs) -> None:
    factory, org1, _ = two_orgs
    await _insert_log(factory, org1, "hidden")

    async with factory() as session:
        rows = (await session.execute(text("SELECT id FROM audit_logs"))).all()
    assert rows == [], "未设置 org 上下文时不应看到任何行"


async def test_with_check_blocks_cross_org_insert(two_orgs) -> None:
    factory, org1, org2 = two_orgs
    async with factory() as session:
        await _set_org(session, org1)
        session.add(AuditLog(org_id=org2, action="evil", entity_type="test"))
        with pytest.raises(ProgrammingError):
            await session.commit()


async def _insert_usage(factory, org_id) -> None:
    async with factory() as session:
        await _set_org(session, org_id)
        session.add(
            UsageDaily(
                org_id=org_id,
                usage_date=datetime.now(UTC).date(),
                task="rls-test",
                provider="internal",
                model="",
                calls=1,
            )
        )
        await session.commit()


async def test_platform_read_bypass_on_usage_daily(two_orgs) -> None:
    """op_platform_read:平台 org 上下文可 SELECT 全租户 usage_daily,写入面不变。"""
    factory, org1, org2 = two_orgs
    platform_org = get_settings().platform_org_id
    await _insert_usage(factory, org1)
    await _insert_usage(factory, org2)

    async with factory() as session:
        await _set_org(session, platform_org)
        rows = (await session.execute(text("SELECT org_id FROM usage_daily"))).all()
    seen = {str(r.org_id) for r in rows}
    assert {str(org1), str(org2)} <= seen, "平台上下文应能读到全租户用量"

    # 写入面不变:平台上下文替其他 org 写行仍被 org_isolation WITH CHECK 拒绝
    async with factory() as session:
        await _set_org(session, platform_org)
        session.add(
            UsageDaily(
                org_id=org1,
                usage_date=datetime.now(UTC).date(),
                task="rls-test-write",
                provider="internal",
                model="",
                calls=1,
            )
        )
        with pytest.raises(ProgrammingError):
            await session.commit()


async def test_platform_read_does_not_leak_to_other_tables(two_orgs) -> None:
    """audit_logs 未加 platform_read:平台上下文照常只见本 org(即空)。"""
    factory, org1, _ = two_orgs
    await _insert_log(factory, org1, "tenant_only")

    async with factory() as session:
        await _set_org(session, get_settings().platform_org_id)
        rows = (await session.execute(text("SELECT id FROM audit_logs"))).all()
    assert rows == [], "platform_read 旁路只对显式声明的表生效"


def test_rls_tables_check_after_helper_registration() -> None:
    """建表 fixture 走过 op_enable_org_rls 后,三张 RLS 表不再出现在缺失清单;
    身份表(刻意不开 RLS)如实上报,由 baseline 作者显式豁免。"""
    missing = set(rls.rls_tables_check(SQLModel.metadata))
    assert not {"audit_logs", "notifications", "usage_daily"} & missing
    assert {"memberships", "invitations", "refresh_tokens"} <= missing
