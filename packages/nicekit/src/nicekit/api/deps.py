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
from nicekit.tenancy.mapping import NAMESPACE_RESERVED, derive_uuid, subject_uuid
from nicekit.tenancy.roles import write_roles

#: 单租户模式的默认分区键。两点刻意为之:
#: 1. 独立于 platform_org_id —— 后者语义是"对所有组织可见",单租户数据放进去
#:    会在将来接入第二个租户时静默泄漏;
#: 2. 走保留命名空间而非租户命名空间 —— 外部租户 ID 哪怕恰好叫
#:    "__single_tenant__" 也撞不上这个分区键。
SINGLE_TENANT_ORG_ID = derive_uuid("single_tenant", namespace=NAMESPACE_RESERVED)


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


#: 最近一次 ``create_app`` 挂载的 SDK 身份路由名(由 runtime.app_factory 写入)
_mounted_auth_routers: tuple[str, ...] = ()


def record_mounted_auth_routers(names: tuple[str, ...]) -> None:
    """由 ``runtime.app_factory`` 在装配时登记,供本模块反向拦截接线错误。"""
    global _mounted_auth_routers
    _mounted_auth_routers = tuple(names)


def mounted_auth_routers() -> tuple[str, ...]:
    """最近一次装配挂载的 SDK 身份路由名(测试与自检用)。"""
    return _mounted_auth_routers


def set_principal_resolver(resolver: PrincipalResolver | None) -> None:
    """替换身份解析实现;传 None 恢复 SDK 自带的 JWT 解析。

    装配期调用一次即可,**必须在 ``create_app`` 之前**。注册后 SDK 的 auth
    router 就不该再挂载——两套身份来源并存只会让"当前是谁"变得不可推理。

    如果顺序反了(先 ``create_app`` 挂着 auth router、之后才接管身份),
    ``create_app`` 的自检已经跑完、什么都没拦住,所以这里反向再拦一次:
    这个顺序恰恰是最容易写出来的那个。
    """
    global _principal_resolver
    if resolver is not None and _mounted_auth_routers:
        raise RuntimeError(
            f"身份接线顺序错误:app 已装配且挂着 SDK 身份路由 "
            f"{list(_mounted_auth_routers)},此时再接管身份不会被 create_app 的"
            "自检发现。请在 create_app 之前调用 set_principal_resolver(),并从"
            "routers 里排除 auth/members:default_routers(exclude=('auth','members'))。"
        )
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


def single_tenant_subject_id(org_id: UUID | None = None) -> UUID:
    """单租户模式的默认操作者 UUID(与 :func:`single_tenant_resolver` 同源)。

    agent 权限三表有指向 ``users`` 的外键,宿主要在 seed 里预先垫这行,就必须
    知道这个 id。不导出的话只能照抄 SDK 内部的派生表达式 —— 一处会在 SDK 改
    实现时静默失配的耦合(实测踩过:demo 只好显式传 subject_id 绕开)。
    """
    return subject_uuid(f"single-tenant:{org_id or SINGLE_TENANT_ORG_ID}")


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
    默认 org_id 是 :data:`SINGLE_TENANT_ORG_ID` —— 一个独立的固定 UUID,
    **刻意不用 ``platform_org_id``**:平台 org 的语义是"这份数据对所有组织
    可见"(见 kb/search.py 的 layer 判定与 kb/image_assets.py 的可见性过滤)。
    单租户期间两者行为无差别,但哪天要接入第二个租户,原先放在平台 org 下的
    全部知识会立刻对新租户可见——那是一次静默的数据泄漏,且难以回溯。
    独立 org 则只是一个普通租户,升级多租户时什么都不用改。

    首次启动要让这个 org **与默认操作者**都在库里存在(agent 权限三表对
    ``organizations`` 与 ``users`` 都有外键):
    ``bootstrap_platform(session, single_tenant=True)`` 会一并垫好;手动做则是
    ``ensure_principal(session, SINGLE_TENANT_ORG_ID, single_tenant_subject_id())``。

    ``role_of``/``subject_of`` 可选:网关把已认证用户放在请求头里时,用它们
    把用户带进来,这样审计与长期记忆才分得清人;不传则所有请求视为同一主体。

    ⚠️ 它不做任何身份校验。**必须**确保 SDK 不直接暴露在公网,或前置一层
    完成认证的网关——否则等于把平台管理端敞开。
    """
    fixed_org = org_id or SINGLE_TENANT_ORG_ID
    # 固定主体:从 org 确定性派生,保证多次启动稳定(审计里能对得上同一个人)
    fixed_subject = subject_id or single_tenant_subject_id(fixed_org)

    async def _resolve(request: Request) -> Principal:
        return Principal(
            user_id=subject_of(request) if subject_of else fixed_subject,
            org_id=fixed_org,
            role=role_of(request) if role_of else role,
        )

    return _resolve
