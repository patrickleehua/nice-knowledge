"""demo 数据准备:平台 bootstrap + 当前租户接入模式所需的身份行。

运行:``uv run --package nicekit-demo-backend python -m demo_backend.seed``
(或 ``uv run python -m demo_backend.seed``,在 apps/demo/backend 目录下)

模式取自 ``DEMO_AUTH_MODE``,也可用 ``--mode`` 覆盖。三种模式 seed 的差异:

- ``managed``:平台基线 + demo 组织 + 管理员账号 + 两条 membership(SDK 自带账号体系);
- ``bridged``:平台基线 + 为预置租户垫影子 org/user 行(INTEGRATION §3.3 的推荐
  做法:在宿主的"创建租户/邀请成员"流程里垫,而不是等 resolver 惰性补);
- ``single_tenant``:``bootstrap_platform(single_tenant=True)``(把 seed 归属改到
  ``SINGLE_TENANT_ORG_ID`` 并垫出这个 org)+ 垫固定操作者的影子 user 行。

幂等:重复执行只会补齐缺的部分,已存在的组织/用户/角色不会被改写。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from uuid import UUID

from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.core.logging import setup_logging
from nicekit.core.security import hash_password
from nicekit.models.tenancy import Membership, Organization, Role, User
from nicekit.runtime.bootstrap import bootstrap_platform
from nicekit.tenancy import ensure_principal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from demo_backend.extensions import demo_entity_type_specs, install_demo_extensions
from demo_backend.identity import (
    AUTH_MODES,
    DEMO_BRIDGED_TENANTS,
    DEMO_SINGLE_TENANT_SUBJECT_ID,
    ROLE_HEADER,
    SINGLE_TENANT_ORG_ID,
    TENANT_HEADER,
    USER_HEADER,
    AuthMode,
    current_auth_mode,
)

use_selector_event_loop_on_windows()

logger = logging.getLogger(__name__)

DEMO_ORG_SLUG = "demo"
DEMO_ORG_NAME = "Demo 组织"
DEMO_ADMIN_EMAIL = "admin@demo.example.com"
DEMO_ADMIN_PASSWORD = "demo-admin-2026"
DEMO_ADMIN_NAME = "Demo 管理员"


async def _ensure_user(session: AsyncSession, *, email: str, password: str, name: str) -> User:
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(email=email, password_hash=hash_password(password), full_name=name)
    session.add(user)
    await session.flush()
    logger.info("创建用户 %s", email)
    return user


async def _ensure_membership(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, role: str
) -> None:
    exists = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == org_id, Membership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        return
    session.add(Membership(org_id=org_id, user_id=user_id, role=role))
    logger.info("授予角色 org=%s role=%s", org_id, role)


async def _ensure_org(session: AsyncSession, *, slug: str, name: str) -> Organization:
    org = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if org is not None:
        return org
    org = Organization(name=name, slug=slug)
    session.add(org)
    await session.flush()
    logger.info("创建组织 %s", slug)
    return org


async def _seed_managed(
    session: AsyncSession, *, admin_email: str, admin_password: str
) -> dict:
    """全托管:SDK 自带账号体系,seed 出可登录的组织管理员 + 平台管理员。"""
    settings = get_settings()
    report = await bootstrap_platform(session, entity_type_specs=demo_entity_type_specs())

    org = await _ensure_org(session, slug=DEMO_ORG_SLUG, name=DEMO_ORG_NAME)
    org_admin = await _ensure_user(
        session, email=admin_email, password=admin_password, name=DEMO_ADMIN_NAME
    )
    await _ensure_membership(
        session, org_id=org.id, user_id=org_admin.id, role=Role.ORG_ADMIN.value
    )
    # 平台管理员:同一个账号在平台 org 里再挂一层 platform_admin,
    # 这样一套凭据既能演示租户视角(org_slug=demo),也能进 /admin 管理面。
    await _ensure_membership(
        session,
        org_id=settings.platform_org_id,
        user_id=org_admin.id,
        role=Role.PLATFORM_ADMIN.value,
    )
    await session.commit()

    return {
        "bootstrap": report.as_dict(),
        "org": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "platform_org_id": str(settings.platform_org_id),
        "admin": {"email": admin_email, "password": admin_password},
        "login_hint": (
            f"POST /api/v1/auth/login  "
            f'{{"email": "{admin_email}", "password": "***", "org_slug": "platform"}}'
            "  → 平台管理员;org_slug=demo → 组织管理员"
        ),
    }


async def _seed_bridged(session: AsyncSession) -> dict:
    """桥接:平台基线不变,额外为预置租户垫影子 org/user 行。

    影子行只为满足 agent 权限三表的外键而存在(``password_hash`` 是个不可能
    匹配任何密码的占位值,而且 auth router 根本没挂)。这里在 seed 里垫,
    走的是 INTEGRATION §3.3 的做法 1 —— 请求路径零开销,失败点落在部署动作里。
    """
    report = await bootstrap_platform(session, entity_type_specs=demo_entity_type_specs())

    tenants = []
    for tenant in DEMO_BRIDGED_TENANTS:
        await ensure_principal(
            session,
            tenant.org_id,
            tenant.user_id,
            org_name=tenant.org_name,
            org_slug=tenant.tenant,
            user_email=tenant.email,
            user_full_name=tenant.full_name,
        )
        tenants.append(
            {
                "tenant_header": tenant.tenant,
                "user_header": tenant.user,
                "org_id": str(tenant.org_id),
                "user_id": str(tenant.user_id),
            }
        )
    await session.commit()

    return {
        "bootstrap": report.as_dict(),
        "tenants": tenants,
        "call_hint": (
            f"curl -H '{TENANT_HEADER}: acme' -H '{USER_HEADER}: u-alice' "
            f"-H '{ROLE_HEADER}: OWNER' localhost:8020/api/v1/kb/bases"
            "  → 200;不带头 → 401"
        ),
    }


async def _seed_single_tenant(session: AsyncSession) -> dict:
    """单租户:seed 归属改到 SINGLE_TENANT_ORG_ID,并垫固定操作者的 user 行。

    ``single_tenant=True`` 会顺手 ``ensure_org(SINGLE_TENANT_ORG_ID)``,但
    **不管 users** —— 固定操作者那行得宿主自己垫,否则第一次动 agent 权限
    偏好就 ForeignKeyViolation。
    """
    report = await bootstrap_platform(
        session, entity_type_specs=demo_entity_type_specs(), single_tenant=True
    )
    await ensure_principal(
        session,
        SINGLE_TENANT_ORG_ID,
        DEMO_SINGLE_TENANT_SUBJECT_ID,
        org_name="Single Tenant",
        org_slug="single-tenant",
        user_email="operator@single-tenant.demo.invalid",
        user_full_name="单租户操作者",
    )
    await session.commit()

    settings = get_settings()
    return {
        "bootstrap": report.as_dict(),
        "single_tenant_org_id": str(SINGLE_TENANT_ORG_ID),
        "single_tenant_user_id": str(DEMO_SINGLE_TENANT_SUBJECT_ID),
        # 刻意并排打出来:两者不同才是对的(平台 org 语义是"对所有组织可见")
        "platform_org_id": str(settings.platform_org_id),
        "call_hint": "curl localhost:8020/api/v1/kb/bases  → 200(不需要任何头)",
    }


async def seed_demo(
    *,
    mode: AuthMode | None = None,
    admin_email: str = DEMO_ADMIN_EMAIL,
    admin_password: str = DEMO_ADMIN_PASSWORD,
) -> dict:
    """按接入模式 seed。返回可直接打印的摘要。"""
    resolved_mode = mode or current_auth_mode()
    factory = get_session_factory()
    # entity_type_specs 里的 product 类型来自扩展点,三种模式都要先装上
    install_demo_extensions()

    async with factory() as session:
        if resolved_mode == "managed":
            summary = await _seed_managed(
                session, admin_email=admin_email, admin_password=admin_password
            )
        elif resolved_mode == "bridged":
            summary = await _seed_bridged(session)
        else:
            summary = await _seed_single_tenant(session)
    return {"auth_mode": resolved_mode, **summary}


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="nicekit demo seed")
    parser.add_argument(
        "--mode",
        choices=AUTH_MODES,
        default=None,
        help=f"租户接入模式(缺省读 DEMO_AUTH_MODE,再缺省 {AUTH_MODES[0]})",
    )
    parser.add_argument("--admin-email", default=DEMO_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEMO_ADMIN_PASSWORD)
    args = parser.parse_args()
    summary = asyncio.run(
        seed_demo(
            mode=args.mode,
            admin_email=args.admin_email,
            admin_password=args.admin_password,
        )
    )
    import json

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
