"""usage_daily 记账服务:非 LLM 计量统一入口(迁移自 TF backend/app/services/usage.py)。

- usage_upsert_stmt:唯一键(org, date, task, provider, model)冲突时
  tokens/calls/quantity 全累加——llm/service._record 与本模块共用,
  全库只有这一处 upsert 写法。
- record_usage:用调用方 session、不 commit——与业务操作同事务,
  业务回滚则计量不落(失败不计费);org 上下文由调用方 session 已绑定
  (org_session / get_org_session),RLS 自然放行本 org 写入。

计量口径:字节类计量(存储 / 文件上传导出)quantity 存字节数;纯次数类
quantity=0 只计 calls;LLM 行由 llm 侧落 tokens。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.tenancy import UsageDaily


def usage_upsert_stmt(
    *,
    org_id: UUID,
    task: str,
    provider: str,
    model: str,
    calls: int = 1,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    quantity: int = 0,
) -> Insert:
    stmt = pg_insert(UsageDaily).values(
        org_id=org_id,
        usage_date=datetime.now(UTC).date(),
        task=task,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        calls=calls,
        quantity=quantity,
    )
    return stmt.on_conflict_do_update(
        constraint="uq_usage_daily_key",
        set_={
            "tokens_in": UsageDaily.tokens_in + stmt.excluded.tokens_in,
            "tokens_out": UsageDaily.tokens_out + stmt.excluded.tokens_out,
            "cache_read_tokens": UsageDaily.cache_read_tokens + stmt.excluded.cache_read_tokens,
            "cache_write_tokens": UsageDaily.cache_write_tokens + stmt.excluded.cache_write_tokens,
            "calls": UsageDaily.calls + stmt.excluded.calls,
            "quantity": UsageDaily.quantity + stmt.excluded.quantity,
        },
    )


async def record_usage(
    session: AsyncSession,
    *,
    org_id: UUID,
    task: str,
    provider: str = "internal",
    model: str = "",
    calls: int = 1,
    quantity: int = 0,
) -> None:
    """非 LLM 计量:调用方 session 内执行,不 commit(随业务事务一起落/回滚)。"""
    await session.execute(
        usage_upsert_stmt(
            org_id=org_id, task=task, provider=provider, model=model,
            calls=calls, quantity=quantity,
        )
    )
