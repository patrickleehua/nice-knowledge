"""API 依赖(迁移自 TF backend/app/api/deps.py)。

改造点:OrgContext.role 从 Role 枚举放宽为 str——内置三角色之外,
宿主可经 nicekit.tenancy.roles.register_roles() 注册业务角色,
require_role 按字符串值比较,内置 Role 枚举成员(StrEnum)与注册角色名通用。
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import EmailStr as EmailStr  # auth/members 统一从本模块导入
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.db import bind_org_context, get_session
from nicekit.core.security import decode_access_token
from nicekit.models.tenancy import Role
from nicekit.tenancy.roles import write_roles

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class OrgContext:
    user_id: UUID
    org_id: UUID
    role: str  # 内置 Role 值或宿主注册的角色名(tenancy/roles.py)


async def get_org_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> OrgContext:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_access_token(credentials.credentials)
        return OrgContext(
            user_id=UUID(claims["sub"]),
            org_id=UUID(claims["org_id"]),
            role=str(claims["role"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc


async def get_org_session(
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncSession:
    """org 上下文会话:本 session 每个事务自动 SET LOCAL app.current_org_id,
    RLS 据此过滤(commit 后的下一个事务同样生效)。"""
    bind_org_context(session, ctx.org_id)
    return session


def require_role(*roles: Role | str):
    """角色守卫:接受内置 Role 枚举成员或注册角色名字符串,按值比较。"""
    allowed = {str(role) for role in roles}

    async def _checker(ctx: Annotated[OrgContext, Depends(get_org_context)]) -> OrgContext:
        if ctx.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return ctx

    return _checker


def require_write_role():
    """写操作守卫(A13):在**请求期**读 tenancy.roles 的写角色注册表。

    与 require_role 的区别是允许集合不在 import 期定格 —— 宿主在装配期
    ``register_write_roles("editor")`` 之后,已经定义好的 KB 写端点立刻放行,
    不需要各处再复制一份 ``_KB_WRITERS`` 常量。
    """

    async def _checker(ctx: Annotated[OrgContext, Depends(get_org_context)]) -> OrgContext:
        if ctx.role not in set(write_roles()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return ctx

    return _checker
