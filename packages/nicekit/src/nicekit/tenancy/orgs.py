"""身份行的幂等保障(桥接 / 单租户模式接入用)。

SDK 里 ``org_id`` 绝大多数时候只是**数据分区键**——64 张表里带 ``org_id`` 的
有 52 张,其中只有 6 条外键指向 ``organizations``(3 张 agent 权限表 + 3 张
租户自有表)。KB、chat、检索、计量这些主链路完全不需要 ``organizations``
里有对应行。

但 agent 权限子系统要写入时外键必须成立,而且**不只是 org**:那 3 张表另有
5 条外键指向 ``users``(``user_id`` / ``created_by`` / ``revoked_by``)。所以
桥接模式请用 :func:`ensure_principal` 一次把 org 与 user 都垫上:

```python
from nicekit.tenancy import ensure_principal

await ensure_principal(session, org_id, user_id, org_name=..., user_email=...)
```

**调用时机**:推荐放在宿主自己的"创建租户 / 创建用户"流程里(那里本来就有
事务),而不是 resolver 里 —— resolver 每个请求都跑,契约要求它快且不 commit。
若宿主的用户是外部系统同步过来的、没有本地创建流程,则在首次进入平台功能
(如第一次打开 chat)时调一次。

**失败时机很晚,值得警惕**:不垫这两行,KB、检索、chat 全程正常,直到用户
第一次改 agent 权限偏好才 500 —— 所以宁可多调(幂等,命中即一次主键查询)。

为什么不干脆去掉这些外键:级联删除语义有价值(删租户/用户时权限策略与授权
跟着走),而代价只是宿主一次幂等调用。
"""

import logging
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.tenancy import Organization, User

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


async def ensure_user(
    session: AsyncSession,
    user_id: UUID,
    *,
    email: str | None = None,
    full_name: str | None = None,
) -> UUID:
    """确保 ``users`` 里有这一行(幂等),返回 ``user_id``。

    桥接模式下宿主的用户不在 SDK 这边注册,但 agent 权限子系统的
    ``user_id`` / ``created_by`` / ``revoked_by`` 是真外键,写入前这行必须存在。

    ``password_hash`` 写入一个**不可能匹配任何密码**的占位值:这些行只为满足
    外键而存在,绝不该能通过 SDK 自带的 auth 登录 —— 桥接模式下认证在宿主那边。
    ``email`` 有唯一约束,缺省用 ``user_id`` 派生一个稳定的占位地址。
    """
    stmt = (
        pg_insert(User)
        .values(
            id=user_id,
            email=email or f"{user_id}@bridged.invalid",
            # 占位:argon2 哈希不可能是这个形状,任何密码都验不过
            password_hash="!bridged-principal-no-login",
            full_name=full_name or f"user-{user_id.hex[:8]}",
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=[User.id])
    )
    await session.execute(stmt)
    return user_id


async def ensure_principal(
    session: AsyncSession,
    org_id: UUID,
    user_id: UUID,
    *,
    org_name: str | None = None,
    org_slug: str | None = None,
    user_email: str | None = None,
    user_full_name: str | None = None,
) -> tuple[UUID, UUID]:
    """一次垫齐 org 与 user 两行(幂等),返回 ``(org_id, user_id)``。

    桥接接入的推荐入口:agent 权限子系统同时需要这两边的外键成立,分开调
    容易只补了一半——而缺哪一半的报错都要等到用户第一次动权限设置才出现。
    """
    await ensure_org(session, org_id, name=org_name, slug=org_slug)
    await ensure_user(session, user_id, email=user_email, full_name=user_full_name)
    return org_id, user_id


__all__ = ["ensure_org", "ensure_principal", "ensure_user"]
