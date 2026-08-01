"""demo 数据准备:平台 bootstrap + 一个 demo 组织与管理员账号。

运行:``uv run --package nicekit-demo-backend python -m demo_backend.seed``
(或 ``uv run python -m demo_backend.seed``,在 apps/demo/backend 目录下)

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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from demo_backend.extensions import demo_entity_type_specs, install_demo_extensions

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


async def seed_demo(
    *,
    admin_email: str = DEMO_ADMIN_EMAIL,
    admin_password: str = DEMO_ADMIN_PASSWORD,
) -> dict:
    """平台 bootstrap + demo 组织/账号。返回可直接打印的摘要。"""
    settings = get_settings()
    factory = get_session_factory()
    install_demo_extensions()

    async with factory() as session:
        # 1) 平台基线:实体类型(含 demo 的 product)、KB prompt、系统路由、默认 agent 卡
        report = await bootstrap_platform(
            session, entity_type_specs=demo_entity_type_specs()
        )

        # 2) demo 组织 + 组织管理员
        org = await _ensure_org(session, slug=DEMO_ORG_SLUG, name=DEMO_ORG_NAME)
        org_admin = await _ensure_user(
            session, email=admin_email, password=admin_password, name=DEMO_ADMIN_NAME
        )
        await _ensure_membership(
            session, org_id=org.id, user_id=org_admin.id, role=Role.ORG_ADMIN.value
        )

        # 3) 平台管理员:同一个账号在平台 org 里再挂一层 platform_admin,
        #    这样一套凭据既能演示租户视角(org_slug=demo),也能进 /admin 管理面。
        await _ensure_membership(
            session,
            org_id=settings.platform_org_id,
            user_id=org_admin.id,
            role=Role.PLATFORM_ADMIN.value,
        )
        await session.commit()

        summary = {
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
    return summary


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="nicekit demo seed")
    parser.add_argument("--admin-email", default=DEMO_ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=DEMO_ADMIN_PASSWORD)
    args = parser.parse_args()
    summary = asyncio.run(
        seed_demo(admin_email=args.admin_email, admin_password=args.admin_password)
    )
    import json

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
