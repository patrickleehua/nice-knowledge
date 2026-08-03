"""API 依赖:身份入口与 org 会话。

**这是接入外部身份体系的唯一切换点。** SDK 的每个受保护端点都经
``get_org_context`` 取 :class:`Principal`(org_id + user_id + role),而它只是
委派给当前注册的 :class:`PrincipalResolver`。三种模式:

1. **全托管**(默认):SDK 自己签发/校验 JWT,配套 auth + members router;
2. **桥接**:宿主已有认证与租户 —— ``set_principal_resolver(自己的实现)``,
   不挂 auth/members router,外部主键经 ``tenancy.mapping`` 派生成 UUID;
3. **单租户**:宿主没有租户概念 —— ``set_principal_resolver(
   single_tenant_resolver(...))``,分区键恒定。RLS 仍然生效(别关它,
   那是代码漏写 where 时的最后一道闸)。

角色是自由字符串:内置三个(platform_admin/org_admin/member),宿主可经
``tenancy.roles.register_roles()`` 追加,``require_role`` 按值比较。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from pydantic import EmailStr as EmailStr  # auth/members 统一从本模块导入
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.core.db import bind_org_context, get_session
from nicekit.core.security import decode_access_token
from nicekit.models.tenancy import Role
from nicekit.tenancy.roles import write_roles


@dataclass(frozen=True)
class OrgContext:
    user_id: UUID
    org_id: UUID
    role: str  # 内置 Role 值或宿主注册的角色名(tenancy/roles.py)


#: 身份解析结果的别名。桥接模式下宿主返回的就是它:
#: ``org_id`` 是数据分区键,``user_id`` 是操作者,``role`` 决定能做什么。
Principal = OrgContext


class PrincipalResolver(Protocol):
    """把一个 HTTP 请求解析成 :class:`Principal`。

    这是接入任何外部身份体系的**唯一切换点**:SDK 的每个受保护端点都经
    ``get_org_context`` 拿上下文,而它只是调用当前注册的 resolver。宿主实现
    这个协议,就可以完全不用 SDK 的 auth/members 与 JWT。

    实现约定:
    - 认证失败抛 ``HTTPException(401)``;越权抛 403,不要返回 None;
    - ``org_id``/``user_id`` 必须是 UUID,外部主键用
      :mod:`nicekit.tenancy.mapping` 的 ``tenant_uuid``/``subject_uuid`` 派生;
    - 应当足够快(每个请求都会调),重活(查库、验签)自己做缓存。
    """

    async def __call__(self, request: Request) -> Principal: ...


async def _jwt_principal_resolver(request: Request) -> Principal:
    """默认 resolver:解析 SDK 自己签发的 JWT(全托管模式)。"""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_access_token(token)
        return Principal(
            user_id=UUID(claims["sub"]),
            org_id=UUID(claims["org_id"]),
            role=str(claims["role"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc


_principal_resolver: PrincipalResolver = _jwt_principal_resolver


def set_principal_resolver(resolver: PrincipalResolver | None) -> None:
    """替换身份解析实现;传 None 恢复 SDK 自带的 JWT 解析。

    装配期调用一次即可(``create_app`` 之前)。注册后 SDK 的 auth router
    就不该再挂载——两套身份来源并存只会让"当前是谁"变得不可推理。
    """
    global _principal_resolver
    _principal_resolver = resolver or _jwt_principal_resolver


def principal_resolver() -> PrincipalResolver:
    """当前生效的 resolver(启动自检与测试用)。"""
    return _principal_resolver


def uses_builtin_auth() -> bool:
    """是否仍在用 SDK 自带的 JWT 认证(未被宿主接管)。"""
    return _principal_resolver is _jwt_principal_resolver


async def get_org_context(request: Request) -> OrgContext:
    """所有受保护端点的身份入口:委派给当前注册的 :class:`PrincipalResolver`。"""
    return await _principal_resolver(request)


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


def single_tenant_resolver(
    *,
    org_id: UUID | None = None,
    role: str = str(Role.ORG_ADMIN),
    subject_id: UUID | None = None,
    role_of: Callable[[Request], str] | None = None,
    subject_of: Callable[[Request], UUID] | None = None,
) -> PrincipalResolver:
    """单租户模式:分区键恒定,不做认证。

    适合内部工具、单公司自用系统,或"认证由网关/反向代理做完了"的部署。
    默认 org_id 取 ``settings.platform_org_id``(迁移已 seed 该行,无需再建)。

    ``role_of``/``subject_of`` 可选:网关把已认证用户放在请求头里时,用它们
    把用户带进来,这样审计与长期记忆才分得清人;不传则所有请求视为同一主体。

    ⚠️ 它不做任何身份校验。**必须**确保 SDK 不直接暴露在公网,或前置一层
    完成认证的网关——否则等于把平台管理端敞开。
    """
    from nicekit.core.config import get_settings

    fixed_org = org_id or get_settings().platform_org_id
    # 固定主体:同一个 UUID 派生自 org,保证多次启动稳定(审计里能对得上)
    fixed_subject = subject_id or UUID(int=fixed_org.int ^ 0x5EED)

    async def _resolve(request: Request) -> Principal:
        return Principal(
            user_id=subject_of(request) if subject_of else fixed_subject,
            org_id=fixed_org,
            role=role_of(request) if role_of else role,
        )

    return _resolve
