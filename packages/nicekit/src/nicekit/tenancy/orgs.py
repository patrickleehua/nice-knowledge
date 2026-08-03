"""组织行的幂等保障(桥接 / 单租户模式接入用)。

SDK 里 ``org_id`` 绝大多数时候只是**数据分区键**——64 张表里带 ``org_id`` 的
有 45+ 张,其中只有 3 张 agent 权限表(approval_policies / permission_grants /
permission_preferences)带指向 ``organizations`` 的外键。也就是说 KB、chat、
检索、计量这些主链路完全不需要 ``organizations`` 里有对应行。

但那 3 张表要写入时外键必须成立,所以桥接/单租户模式下,宿主在**首次见到一个
新租户**时调一次本模块的 ``ensure_org()`` 垫一行即可(幂等,可以每次请求都调,
命中已存在时只有一次主键查询)。

```python
from nicekit.tenancy.orgs import ensure_org

org_id = tenant_uuid(user.company_id)
await ensure_org(session, org_id, name=user.company_name)
```

为什么不干脆去掉那 3 个外键:级联删除语义有价值(删租户时权限策略跟着走),
而代价只是宿主一行调用。
"""

import logging
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.tenancy import Organization

logger = logging.getLogger(__name__)


def _fallback_slug(org_id: UUID) -> str:
    """外部租户没有 slug 时的占位:取 UUID 前段,保证唯一且可读。"""
    return f"org-{org_id.hex[:12]}"


async def ensure_org(
    session: AsyncSession,
    org_id: UUID,
    *,
    name: str | None = None,
    slug: str | None = None,
) -> UUID:
    """确保 ``organizations`` 里有这一行(幂等),返回 ``org_id``。

    并发安全:走 ``ON CONFLICT DO NOTHING``,两个请求同时首见一个新租户时
    不会互相炸;**不 commit**,由调用方的事务决定何时落盘(桥接模式通常挂在
    请求事务里,业务回滚则这行也回滚)。

    已存在时不覆盖 name/slug —— 宿主那边的租户改名不该由 SDK 这条兜底路径
    静默同步,那是宿主自己的数据同步职责。
    """
    stmt = (
        pg_insert(Organization)
        .values(
            id=org_id,
            name=name or _fallback_slug(org_id),
            slug=slug or _fallback_slug(org_id),
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=[Organization.id])
    )
    await session.execute(stmt)
    return org_id


__all__ = ["ensure_org"]
