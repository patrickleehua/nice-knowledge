"""demo 宿主的身份装配:一份代码支持 nicekit 的三种租户接入模式。

模式由环境变量 ``DEMO_AUTH_MODE`` 选择(缺省 ``managed``,即 demo 的历史形态):

| 模式 | 谁认证 | resolver | auth/members router |
|---|---|---|---|
| ``managed`` | SDK 自己签发 JWT | 不接管(内置) | 挂 |
| ``bridged`` | 宿主(这里用请求头模拟) | :func:`bridged_principal_resolver` | **不挂** |
| ``single_tenant`` | 网关 / 无 | ``single_tenant_resolver()`` | **不挂** |

刻意不为桥接模式单开一个 ``backend-bridged`` 目录:三种模式的差异只有
"身份从哪来"与"挂不挂身份路由"两件事,复制一整份宿主代码只会让两边随时间
漂移 —— 而"接入模式换一下、其余一行不动"恰恰是这个 SDK 想证明的事。

**装配顺序是硬约束**::

    install_identity(mode)      # 内部调 set_principal_resolver()
    create_app(routers=default_routers(exclude=...))

写反了会被拦下来,两个方向都拦(实测过):

- 先接管身份、却仍挂着 auth/members → ``create_app`` 抛 RuntimeError
  ("身份接线自相矛盾");
- 先 ``create_app``(挂着 auth/members)、之后才接管 → ``set_principal_resolver``
  抛 RuntimeError("身份接线顺序错误")。后者是自检本身的盲区:自检在
  ``create_app`` 执行那一刻读全局状态,那时看到的还是内置 JWT,所以由 deps
  侧反向补一道(见 INTEGRATION.md §6.2)。
"""

from __future__ import annotations

import logging
from typing import Literal, NamedTuple, get_args
from uuid import UUID

from fastapi import HTTPException, Request, status
from nicekit.api.deps import (
    SINGLE_TENANT_ORG_ID,
    Principal,
    set_principal_resolver,
    single_tenant_resolver,
)
from nicekit.api.v1.router import AUTH_ROUTER_NAMES
from nicekit.core.db import get_session_factory
from nicekit.tenancy import (
    ensure_principal,
    register_roles,
    register_write_roles,
    subject_uuid,
    tenant_uuid,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

AuthMode = Literal["managed", "bridged", "single_tenant"]

#: ``DEMO_AUTH_MODE`` 的合法取值(顺序即文档顺序)
AUTH_MODES: tuple[str, ...] = get_args(AuthMode)

#: 缺省模式 = demo 的历史形态,不配环境变量的人拿到的还是原来那套
DEFAULT_AUTH_MODE: AuthMode = "managed"

AUTH_MODE_ENV = "DEMO_AUTH_MODE"


# ---------------------------------------------------------------------------
# 模式选择
# ---------------------------------------------------------------------------


class _AuthModeSettings(BaseSettings):
    """只为读一个变量而存在的 settings。

    刻意不用 ``os.getenv``:``.env`` 是 pydantic-settings 直接读进模型的,
    并不会被导出到 ``os.environ`` —— 写在 ``.env`` 里的 ``DEMO_AUTH_MODE``
    用 ``os.getenv`` 一个字都读不到。这里复用同一套机制(真实环境变量优先于
    ``.env``),与 ``nicekit.core.config.Settings`` 的解析口径完全一致。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    demo_auth_mode: str = DEFAULT_AUTH_MODE


def current_auth_mode() -> AuthMode:
    """读 ``DEMO_AUTH_MODE``(环境变量优先,其次 ``.env``),非法值直接报错。

    不认识的模式**不能**静默回落到 managed:那会让人以为已经切到桥接、
    实际 auth router 还挂着、身份还是 SDK 自己签的 —— 是最难发现的那类错。
    """
    raw = _AuthModeSettings().demo_auth_mode.strip().lower()
    if raw not in AUTH_MODES:
        raise ValueError(f"{AUTH_MODE_ENV}={raw!r} 不是合法模式;可选:{list(AUTH_MODES)}")
    return raw  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 桥接模式:宿主已有认证与租户
# ---------------------------------------------------------------------------

#: demo 用明文请求头模拟"宿主已经认过的身份"。
#: 真实宿主这里应当是验自己的 session cookie / JWT / 内网 mTLS ——
#: 明文头**任何人都能自称任何租户**,只适合本地演示。
TENANT_HEADER = "X-Demo-Tenant"
USER_HEADER = "X-Demo-User"
ROLE_HEADER = "X-Demo-Role"

#: 宿主角色名 → SDK 角色名。SDK 内置 platform_admin / org_admin / member,
#: 其余(editor / auditor)必须先 register_roles 登记,见 :func:`install_demo_roles`。
#: ``platform_admin`` 不在 register_roles 的合法域内,只能硬写这个字面量。
ROLE_MAP: dict[str, str] = {
    "OWNER": "org_admin",
    "ADMIN": "org_admin",
    "EDITOR": "editor",
    "VIEWER": "member",
    "AUDITOR": "auditor",
    "SUPERADMIN": "platform_admin",
}

#: 不带 ``X-Demo-Role`` 时的默认宿主角色(能读能写,省掉演示时的一个头)
DEFAULT_HOST_ROLE = "OWNER"


class BridgedTenant(NamedTuple):
    """demo 预置的桥接租户:seed 用它预先垫影子行,实测用它拼请求头。"""

    tenant: str  # 宿主侧租户主键(这里用 slug;真实宿主常是 int / 雪花号)
    org_name: str
    user: str  # 宿主侧用户主键
    email: str
    full_name: str

    @property
    def org_id(self) -> UUID:
        return tenant_uuid(self.tenant)

    @property
    def user_id(self) -> UUID:
        return subject_uuid(self.user)


#: 两个租户是刻意的:跨租户隔离要能被真的跑出来,一个租户证明不了任何事。
DEMO_BRIDGED_TENANTS: tuple[BridgedTenant, ...] = (
    BridgedTenant("acme", "Acme 商贸", "u-alice", "alice@acme.example.com", "Alice(Acme)"),
    BridgedTenant("globex", "Globex 工业", "u-bob", "bob@globex.example.com", "Bob(Globex)"),
)


def install_demo_roles() -> None:
    """登记 demo 的业务角色(幂等)。必须在 create_app 之前。

    ``editor`` 还要进写角色注册表,否则它在 KB 写端点上一律 403 ——
    ``require_write_role()`` 在请求期读这张表,漏注册不会有任何装配期报错。
    """
    register_roles("editor", "auditor")
    register_write_roles("editor")


#: 进程内已垫过影子行的 (org, user)。resolver 跑在每个请求上,
#: 没有这层短路就等于给全站每个请求加一次 DB 往返。
#: 多副本各自垫一次无所谓:ensure_principal 走 ON CONFLICT DO NOTHING。
_provisioned: set[tuple[UUID, UUID]] = set()


async def provision(
    org_id: UUID,
    user_id: UUID,
    *,
    org_name: str | None = None,
    org_slug: str | None = None,
    user_email: str | None = None,
    user_full_name: str | None = None,
) -> None:
    """幂等垫齐 organizations / users 两行(agent 权限三表有这两侧外键)。

    刻意开一个**独立短事务**:resolver 早于 ``get_org_session`` 执行,此时
    请求事务还没开始,在这里 commit 等于 commit 别人的会话。
    """
    if (org_id, user_id) in _provisioned:
        return
    async with get_session_factory()() as session:
        await ensure_principal(
            session,
            org_id,
            user_id,
            org_name=org_name,
            org_slug=org_slug,
            user_email=user_email,
            user_full_name=user_full_name,
        )
        await session.commit()
    _provisioned.add((org_id, user_id))
    logger.info("桥接身份已垫影子行 org=%s user=%s", org_id, user_id)


async def bridged_principal_resolver(request: Request) -> Principal:
    """把 demo 请求头翻译成 :class:`Principal`;缺头 401,角色没映射 403。"""
    tenant = (request.headers.get(TENANT_HEADER) or "").strip()
    subject = (request.headers.get(USER_HEADER) or "").strip()
    if not tenant or not subject:
        # 必须先判空再进 tenant_uuid:后者对 None / 空串抛 ValueError,
        # 不拦就冒到最外层变成 500,而语义明明是"没登录"。
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"缺少宿主身份头({TENANT_HEADER} / {USER_HEADER})",
        )

    host_role = (request.headers.get(ROLE_HEADER) or DEFAULT_HOST_ROLE).strip().upper()
    role = ROLE_MAP.get(host_role)
    if role is None:
        # 没映射过的角色一律 403,不回落到 member:静默降权比报错更难查。
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"未映射的宿主角色 {host_role!r}")

    org_id = tenant_uuid(tenant)
    user_id = subject_uuid(subject)
    # 惰性垫影子行 + 进程内缓存。更好的做法是在宿主"创建公司 / 邀请成员"
    # 的流程里调 provision(),seed 演示的就是那条路径。
    await provision(
        org_id,
        user_id,
        org_name=tenant,
        # 传真实 slug:KB 跨组织分享按 organizations.slug 找人,
        # 占位 slug(org-<hex12>)会让运营认不出分享给了谁。
        org_slug=tenant,
        user_email=f"{subject}@{tenant}.demo.invalid",
        user_full_name=subject,
    )
    return Principal(org_id=org_id, user_id=user_id, role=role)


# ---------------------------------------------------------------------------
# 单租户模式
# ---------------------------------------------------------------------------

#: 单租户模式下所有请求的操作者。
#: 刻意显式传给 ``single_tenant_resolver(subject_id=...)`` 而不是用它内部
#: 默认派生的那个:默认值 SDK 没有导出,seed 就无从预先垫这行影子 user
#: (agent 权限三表有 user_id 外键),只能照抄 SDK 内部的派生表达式 ——
#: 那是一条会随 SDK 改动静默失配的耦合。宿主自己拿住这个值最省事。
DEMO_SINGLE_TENANT_SUBJECT_ID = subject_uuid("demo:single-tenant-operator")


# ---------------------------------------------------------------------------
# 统一装配入口
# ---------------------------------------------------------------------------


def install_identity(mode: AuthMode) -> tuple[str, ...]:
    """按模式接管身份,返回要从 ``default_routers`` 排除的 router 名。

    **必须在 ``create_app()`` 之前调用**(理由见模块 docstring)。
    """
    if mode == "managed":
        # 全托管:什么都不做。这里刻意不调 set_principal_resolver(None) ——
        # "不接管"和"接管成内置的"在语义上是两件事,前者才是这个模式的本意。
        return ()

    install_demo_roles()
    if mode == "bridged":
        set_principal_resolver(bridged_principal_resolver)
    else:
        set_principal_resolver(single_tenant_resolver(subject_id=DEMO_SINGLE_TENANT_SUBJECT_ID))
    # 桥接与单租户都必须摘掉 SDK 的身份路由(auth 发 token、members 建人),
    # 否则 create_app 的接线自检直接 RuntimeError。
    return AUTH_ROUTER_NAMES


__all__ = [
    "AUTH_MODES",
    "AUTH_MODE_ENV",
    "DEFAULT_AUTH_MODE",
    "DEMO_BRIDGED_TENANTS",
    "DEMO_SINGLE_TENANT_SUBJECT_ID",
    "ROLE_HEADER",
    "ROLE_MAP",
    "SINGLE_TENANT_ORG_ID",
    "TENANT_HEADER",
    "USER_HEADER",
    "AuthMode",
    "BridgedTenant",
    "bridged_principal_resolver",
    "current_auth_mode",
    "install_demo_roles",
    "install_identity",
    "provision",
]
