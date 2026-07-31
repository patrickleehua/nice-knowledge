"""tenancy live 测试共用的 PG schema 装配(docker compose postgres,宿主端口 5433)。

不依赖 alembic(baseline 迁移在 P1 收口时由主控统一生成):
- 用 migrator 账号(owner)SQLModel create_all 建 8 张 tenancy 表,
  应用账号凭 postgres-init.sql 的默认权限获得读写(RLS 再做行级过滤);
- 再用 nicekit/migrations/rls.py 的 op_enable_org_rls / op_platform_read
  施加 RLS——这同时验证了 helper 生成的 SQL 可被真实 PG 执行;
- 测试结束逐表 DROP CASCADE 清理。
"""

from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import nicekit.models.tenancy  # noqa: F401 - 注册表定义进 SQLModel.metadata
from nicekit.core.config import get_settings
from nicekit.migrations import rls

TENANCY_TABLE_NAMES = (
    "organizations",
    "users",
    "memberships",
    "invitations",
    "refresh_tokens",
    "audit_logs",
    "notifications",
    "usage_daily",
)

# 三张 org 级 RLS 表(身份表 memberships/invitations/refresh_tokens 刻意不做 RLS:
# 登录发生在 org 上下文建立之前,由应用层过滤)
RLS_TABLE_NAMES = ("audit_logs", "notifications", "usage_daily")


def _tenancy_tables() -> list:
    return [SQLModel.metadata.tables[name] for name in TENANCY_TABLE_NAMES]


class _ConnOp:
    """给 op_enable_org_rls / op_platform_read 的最小 op 替身(只用到 execute)。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str) -> None:
        self._conn.execute(text(str(sql)))


def create_tenancy_schema() -> None:
    settings = get_settings()
    engine = create_engine(settings.migration_database_url)
    try:
        with engine.begin() as conn:
            # 残留兜底:上一轮异常退出留下的旧表
            for name in reversed(TENANCY_TABLE_NAMES):
                conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        SQLModel.metadata.create_all(engine, tables=_tenancy_tables())
        with engine.begin() as conn:
            op = _ConnOp(conn)
            for name in RLS_TABLE_NAMES:
                rls.op_enable_org_rls(op, name)
            rls.op_platform_read(op, "usage_daily", settings.platform_org_id)
    finally:
        engine.dispose()


def drop_tenancy_schema() -> None:
    engine = create_engine(get_settings().migration_database_url)
    try:
        with engine.begin() as conn:
            for name in reversed(TENANCY_TABLE_NAMES):
                conn.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
    finally:
        engine.dispose()
