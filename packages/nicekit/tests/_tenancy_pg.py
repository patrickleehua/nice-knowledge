"""live 测试共用的 PG schema 装配(docker compose postgres,宿主端口 5433)。

直接复用 baseline 迁移建库,不再自建表:
- live 测试因此验证的是真实生产路径(迁移里的 RLS 策略、平台 org seed、
  zhparser 检索配置),而不是一套只在测试里成立的等价物;
- 应用账号的读写权限来自 postgres-init.sql 的 ALTER DEFAULT PRIVILEGES
  (migrator 建的表自动授予 app),RLS 再做行级过滤。

用例之间靠 TRUNCATE 隔离而非重建表:baseline 有 60 张表,反复建删既慢,
DROP ... CASCADE 又会牵连 agent/kb 子系统的表。
"""

import subprocess
from pathlib import Path

from sqlalchemy import create_engine, text

from nicekit.core.config import get_settings

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# 每个 live 用例前清空的租户表;平台 org 由迁移 seed,清空后补回。
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


def _alembic(*args: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=_PACKAGE_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} 失败:\n{result.stdout}\n{result.stderr}")


def ensure_schema() -> None:
    """确保 baseline 已应用(幂等:已在 head 时 alembic 直接返回)。"""
    _alembic("upgrade", "head")


def reset_tenancy_data() -> None:
    """清空租户数据,保留表结构;补回迁移 seed 的平台 org。"""
    settings = get_settings()
    engine = create_engine(settings.migration_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(TENANCY_TABLE_NAMES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, is_active) "
                    "VALUES (CAST(:id AS uuid), 'Platform', 'platform', true) "
                    "ON CONFLICT (id) DO NOTHING"
                ).bindparams(id=str(settings.platform_org_id))
            )
    finally:
        engine.dispose()


def create_tenancy_schema() -> None:
    ensure_schema()
    reset_tenancy_data()


def drop_tenancy_schema() -> None:
    """用例收尾:只清数据,表结构归迁移所有。"""
    reset_tenancy_data()
