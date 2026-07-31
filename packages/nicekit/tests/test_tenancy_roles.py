"""角色注册表与 require_role 兼容性单测(离线)。"""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from nicekit.api.deps import OrgContext, require_role
from nicekit.models.tenancy import Role
from nicekit.tenancy import roles


@pytest.fixture(autouse=True)
def _clean_registry():
    roles.reset_registered_roles()
    yield
    roles.reset_registered_roles()


def test_builtin_defaults():
    assert roles.all_roles() == ("platform_admin", "org_admin", "member")
    assert roles.assignable_roles() == frozenset({"org_admin", "member"})


def test_register_custom_role_default_assignable():
    roles.register_roles("editor", "viewer")
    assert roles.all_roles() == ("platform_admin", "org_admin", "member", "editor", "viewer")
    assert {"editor", "viewer"} <= roles.assignable_roles()


def test_register_is_idempotent():
    roles.register_roles("editor")
    roles.register_roles("editor")
    roles.register_roles("org_admin", "member")  # 内置可授予角色:no-op
    assert roles.all_roles().count("editor") == 1
    assert roles.all_roles() == ("platform_admin", "org_admin", "member", "editor")


def test_register_platform_admin_rejected():
    with pytest.raises(ValueError):
        roles.register_roles("platform_admin")


def test_register_invalid_name_rejected():
    for bad in ("Editor", "1role", "含中文", "a" * 33, ""):
        with pytest.raises(ValueError):
            roles.register_roles(bad)


def test_register_non_assignable_role():
    roles.register_roles("auditor", assignable=False)
    assert "auditor" in roles.all_roles()
    assert "auditor" not in roles.assignable_roles()


# ---------- require_role 兼容内置枚举与注册角色字符串 ----------


def _ctx(role: str) -> OrgContext:
    return OrgContext(user_id=uuid4(), org_id=uuid4(), role=role)


async def test_require_role_accepts_builtin_enum():
    checker = require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN)
    ctx = _ctx("org_admin")  # token 里的 role 是字符串值
    assert await checker(ctx) is ctx


async def test_require_role_accepts_registered_role_string():
    roles.register_roles("editor")
    checker = require_role("editor")
    ctx = _ctx("editor")
    assert await checker(ctx) is ctx


async def test_require_role_rejects_other_roles():
    checker = require_role(Role.ORG_ADMIN)
    with pytest.raises(HTTPException) as excinfo:
        await checker(_ctx("member"))
    assert excinfo.value.status_code == 403
