"""桥接模式的真库回归(MIGRATION-PLAN §4):**换掉身份来源,隔离不能跟着换掉**。

桥接模式把"当前是谁"整个交给了宿主:org_id 不再来自 SDK 签发的 JWT,而是
``tenant_uuid(外部租户主键)`` 现算出来的。这里要证明的是,即便如此:

1. RLS 仍然是隔离的**唯一执行者** —— 两个派生 org 各写一行 audit_logs,
   用应用账号(受 FORCE RLS 约束)不带任何 where 全表 select,只见自己的行;
2. 未建立 org 上下文时什么都看不到(NULLIF fail-closed);
3. 跨租户写入被 WITH CHECK 拒绝 —— 宿主 resolver 就算返回了错的 org,
   也写不进别人的分区。

这是整个租户接入方案最关键的安全断言:如果它挂了,桥接模式等于把多租户隔离
交给了"宿主别写错"这种运气。所以走的是真实链路(baseline 迁移建的库 + 应用
账号 + ``deps.get_org_session``),不是等价物。

另外覆盖 ``ensure_org`` 的幂等性 —— 桥接宿主会在每个请求上调它。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from _tenancy_pg import create_tenancy_schema, drop_tenancy_schema, reset_tenancy_data
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from nicekit.api import deps
from nicekit.core.config import get_settings
from nicekit.models.tenancy import AuditLog, Role, UsageDaily
from nicekit.tenancy import ensure_org, subject_uuid, tenant_uuid

pytestmark = pytest.mark.live

#: 两个外部租户主键(宿主侧的 slug),SDK 侧只见它们派生出来的 UUID
ACME, GLOBEX = "acme", "globex"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    create_tenancy_schema()
    yield
    drop_tenancy_schema()


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例从空表起步(断言要精确到行数),并还原进程级 resolver。"""
    reset_tenancy_data()
    yield
    deps.set_principal_resolver(None)


@pytest.fixture
async def factory():
    # NullPool:live 用例逐测试新建 event loop,连接不跨 loop 复用
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _host_resolver(request: Request) -> deps.Principal:
    """宿主的身份实现:网关认证完把外部租户/用户放在头里,这里现算成 UUID。"""
    return deps.Principal(
        org_id=tenant_uuid(request.headers["x-tenant"]),
        user_id=subject_uuid(request.headers["x-user"]),
        role=str(Role.ORG_ADMIN),
    )


def _request(tenant: str, user: str = "u_1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/kb/bases",
            "query_string": b"",
            "headers": [(b"x-tenant", tenant.encode()), (b"x-user", user.encode())],
        }
    )


async def _principal(tenant: str) -> deps.Principal:
    """走真实入口 ``get_org_context``,而不是自己拼一个 Principal。"""
    deps.set_principal_resolver(_host_resolver)
    return await deps.get_org_context(_request(tenant))


async def _write_audit(factory, tenant: str, action: str) -> None:
    ctx = await _principal(tenant)
    async with factory() as raw:
        session = await deps.get_org_session(ctx, raw)
        await ensure_org(session, ctx.org_id, name=f"外部租户 {tenant}", slug=tenant)
        session.add(
            AuditLog(org_id=ctx.org_id, user_id=ctx.user_id, action=action, entity_type="test")
        )
        await session.commit()


async def _read_all_audit(factory, tenant: str) -> list:
    """故意不带 where org_id:模拟宿主/SDK 漏写过滤条件,只靠 RLS 兜底。"""
    ctx = await _principal(tenant)
    async with factory() as raw:
        session = await deps.get_org_session(ctx, raw)
        return (await session.execute(text("SELECT org_id, action FROM audit_logs"))).all()


async def test_derived_orgs_are_distinct_and_stable() -> None:
    assert tenant_uuid(ACME) != tenant_uuid(GLOBEX)
    assert tenant_uuid(ACME) == tenant_uuid(ACME)


async def test_bridged_mode_still_isolates_across_tenants(factory) -> None:
    """核心安全断言:身份来源换成宿主后,跨租户隔离依旧由 RLS 强制执行。"""
    await _write_audit(factory, ACME, "acme_action")
    await _write_audit(factory, GLOBEX, "globex_action")

    acme_rows = await _read_all_audit(factory, ACME)
    assert len(acme_rows) == 1, "acme 应恰好看到自己那一行"
    assert acme_rows[0].action == "acme_action"
    assert acme_rows[0].org_id == tenant_uuid(ACME)

    globex_rows = await _read_all_audit(factory, GLOBEX)
    assert len(globex_rows) == 1, "globex 应恰好看到自己那一行"
    assert globex_rows[0].action == "globex_action"
    assert globex_rows[0].org_id == tenant_uuid(GLOBEX)

    # 反向确认:两边看到的确实不是同一行
    assert acme_rows[0].org_id != globex_rows[0].org_id


async def test_no_org_context_sees_nothing(factory) -> None:
    """不经 get_org_session 直接查:NULLIF fail-closed,一行都不给。"""
    await _write_audit(factory, ACME, "acme_action")
    await _write_audit(factory, GLOBEX, "globex_action")

    async with factory() as raw:
        rows = (await raw.execute(text("SELECT id FROM audit_logs"))).all()
    assert rows == [], "未设置 org 上下文时不应看到任何行"


async def test_bridged_context_cannot_write_into_another_tenant(factory) -> None:
    """宿主 resolver 返回错 org 也没用:WITH CHECK 挡在数据库那一层。"""
    ctx = await _principal(ACME)
    async with factory() as raw:
        session = await deps.get_org_session(ctx, raw)
        session.add(
            AuditLog(org_id=tenant_uuid(GLOBEX), action="evil", entity_type="test")
        )
        with pytest.raises(ProgrammingError):
            await session.commit()


async def test_single_tenant_org_is_an_ordinary_tenant(factory) -> None:
    """单租户默认 org 必须是**普通租户**,不能捡到平台 org 的只读旁路。

    这是"默认 org 不用 platform_org_id"那条决策的可观测证据:平台 org 能读到
    全租户 usage_daily(op_platform_read),单租户 org 读不到。若两者混为一谈,
    将来接入第二个租户时,先前积累的数据会立刻对新租户可见。
    """
    settings = get_settings()
    assert settings.platform_org_id != deps.SINGLE_TENANT_ORG_ID

    async with factory() as session:
        await ensure_org(session, deps.SINGLE_TENANT_ORG_ID, name="单租户", slug="single")
        await session.commit()

    today = datetime.now(UTC).date()
    for org_id in (tenant_uuid(ACME), tenant_uuid(GLOBEX)):
        async with factory() as raw:
            session = await deps.get_org_session(
                deps.Principal(user_id=uuid4(), org_id=org_id, role=str(Role.ORG_ADMIN)), raw
            )
            await ensure_org(session, org_id)
            session.add(
                UsageDaily(
                    org_id=org_id,
                    usage_date=today,
                    task="bridge-test",
                    provider="internal",
                    model="",
                    calls=1,
                )
            )
            await session.commit()

    single_ctx = await deps.single_tenant_resolver()(_request(ACME))
    async with factory() as raw:
        session = await deps.get_org_session(single_ctx, raw)
        rows = (await session.execute(text("SELECT org_id FROM usage_daily"))).all()
    assert rows == [], "单租户 org 不该读到其他租户的用量"

    # 对照:平台 org 确实有只读旁路,证明上面的空结果不是因为表里没数据
    async with factory() as raw:
        session = await deps.get_org_session(
            deps.Principal(
                user_id=uuid4(), org_id=settings.platform_org_id, role=str(Role.PLATFORM_ADMIN)
            ),
            raw,
        )
        platform_rows = (await session.execute(text("SELECT org_id FROM usage_daily"))).all()
    assert len(platform_rows) == 2, "平台 org 应能读到全租户用量"


async def test_ensure_org_is_idempotent(factory) -> None:
    """桥接宿主每个请求都会调它:第二次必须是 no-op,不能报错也不能建第二行。"""
    org_id = tenant_uuid(ACME)
    async with factory() as session:
        await ensure_org(session, org_id, name="第一次", slug="acme-1")
        await session.commit()
    async with factory() as session:
        await ensure_org(session, org_id, name="第二次", slug="acme-2")
        await session.commit()

    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT name, slug FROM organizations WHERE id = CAST(:id AS uuid)"),
                {"id": str(org_id)},
            )
        ).all()
    assert len(rows) == 1, "同一个 org_id 调两次只应有一行"
    # 已存在时不覆盖:宿主那边改名由宿主自己的数据同步负责,不该被兜底路径静默改写
    assert rows[0].name == "第一次"
    assert rows[0].slug == "acme-1"


async def test_ensure_org_does_not_commit(factory) -> None:
    """不 commit 是契约的一部分:业务回滚时垫的这行也要跟着回滚。"""
    org_id = uuid4()
    async with factory() as session:
        await ensure_org(session, org_id)
        await session.rollback()

    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT id FROM organizations WHERE id = CAST(:id AS uuid)"),
                {"id": str(org_id)},
            )
        ).all()
    assert rows == [], "ensure_org 不该自己 commit"


async def test_ensure_org_fills_readable_fallback_names(factory) -> None:
    """外部租户没有 slug 时的占位要唯一且可读(否则 slug 唯一约束会撞)。"""
    async with factory() as session:
        await ensure_org(session, tenant_uuid(ACME))
        await ensure_org(session, tenant_uuid(GLOBEX))
        await session.commit()

    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT id, name, slug FROM organizations WHERE slug LIKE 'org-%'")
            )
        ).all()
    assert len(rows) == 2
    assert len({row.slug for row in rows}) == 2, "占位 slug 必须唯一"
    assert all(row.slug.startswith("org-") for row in rows)
