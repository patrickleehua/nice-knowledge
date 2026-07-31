from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nicekit.core.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _pool_kwargs() -> dict[str, Any]:
    """连接池参数:settings 为 None 的项不传,交给 SQLAlchemy 默认值。"""
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    if settings.db_pool_size is not None:
        kwargs["pool_size"] = settings.db_pool_size
    if settings.db_max_overflow is not None:
        kwargs["max_overflow"] = settings.db_max_overflow
    if settings.db_pool_recycle is not None:
        kwargs["pool_recycle"] = settings.db_pool_recycle
    if settings.db_pool_timeout is not None:
        kwargs["pool_timeout"] = settings.db_pool_timeout
    return kwargs


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url, pool_pre_ping=True, **_pool_kwargs()
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def bind_org_context(session: AsyncSession, org_id: UUID) -> None:
    """让本 session 的每个事务自动 SET LOCAL app.current_org_id。

    set_config(..., true) 是事务级 GUC,commit 后即失效;挂 after_begin
    事件保证跨 commit 的后续事务仍带 org 上下文(RLS 依赖它)。
    """
    org = str(org_id)

    def _set(sync_session, transaction, connection) -> None:
        connection.exec_driver_sql(
            "SELECT set_config('app.current_org_id', %s, true)", (org,)
        )

    event.listen(session.sync_session, "after_begin", _set)


def org_session(factory: async_sessionmaker[AsyncSession], org_id: UUID) -> AsyncSession:
    """建一个绑定 org 上下文的独立会话(worker / service 用)。调用方负责 close。"""
    session = factory()
    bind_org_context(session, org_id)
    return session
