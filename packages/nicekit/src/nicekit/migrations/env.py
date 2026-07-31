"""Alembic 环境(迁移自 TF backend/migrations/env.py,async engine)。

- URL 始终用 migrator 账号(owner),与应用账号分离:双角色都无 BYPASSRLS,
  FORCE ROW LEVEL SECURITY 下 owner 也受策略约束(fail-closed)。
- target_metadata 来自 nicekit.models 聚合器(仅供 autogenerate 使用,
  运行时代码一律模块级 import)。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from nicekit.core.config import get_settings
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.models import metadata as target_metadata

use_selector_event_loop_on_windows()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移始终用 migrator 账号(owner),与应用账号分离
config.set_main_option("sqlalchemy.url", get_settings().migration_database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
