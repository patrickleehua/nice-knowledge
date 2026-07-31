"""认证与成员/邀请 API 冒烟(live,需要 docker compose postgres)。

参照 TF backend/tests/test_auth_api.py 并适配:
- 不用 `with TestClient(app)`(会触发 lifespan 卡死,TF 经验);
- 不依赖组装好的 app:轻量 FastAPI 只挂 auth/members 两个 router;
- 可授予角色断言改为注册表口径(sales 等旧业务角色已收敛掉)。
"""

from uuid import uuid4

import pytest
from _tenancy_pg import create_tenancy_schema, drop_tenancy_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nicekit.api.v1.auth import router as auth_router
from nicekit.api.v1.members import router as members_router
from nicekit.core.config import get_settings
from nicekit.core.db import get_session
from nicekit.core.security import hash_password
from nicekit.models.tenancy import Membership, Organization, Role, User
from nicekit.tenancy import roles

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def _schema():
    create_tenancy_schema()
    yield
    drop_tenancy_schema()


@pytest.fixture(scope="module")
def client():
    """轻量 app + 覆写 get_session(NullPool:请求各自建连,不跨 loop 复用)。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(members_router, prefix="/api/v1")
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


@pytest.fixture
async def seeded_admin():
    """一个 org + 一名 org_admin;schema 随模块整体 DROP,不做行级清理。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id, user_id = uuid4(), uuid4()
    email = f"admin-{user_id.hex[:8]}@nicekit-qa.dev"
    async with factory() as session:
        session.add(Organization(id=org_id, name="认证测试组织", slug=f"auth-{org_id.hex[:8]}"))
        session.add(
            User(
                id=user_id,
                email=email,
                password_hash=hash_password("pw-12345678"),
                full_name="测试管理员",
            )
        )
        await session.flush()
        session.add(Membership(org_id=org_id, user_id=user_id, role=Role.ORG_ADMIN))
        await session.commit()
    yield email
    await engine.dispose()


def _login(client: TestClient, email: str, password: str = "pw-12345678") -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_login_refresh_logout_flow(client: TestClient, seeded_admin: str) -> None:
    data = _login(client, seeded_admin)
    assert data["org"]["role"] == "org_admin"
    old_refresh = data["refresh_token"]

    # 错误密码
    bad = client.post("/api/v1/auth/login", json={"email": seeded_admin, "password": "nope"})
    assert bad.status_code == 401

    # 刷新(旋转)
    resp2 = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp2.status_code == 200, resp2.text

    # 旧 refresh token 已作废
    resp3 = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp3.status_code == 401

    # 注销新 token
    new_refresh = resp2.json()["refresh_token"]
    resp4 = client.post("/api/v1/auth/logout", json={"refresh_token": new_refresh})
    assert resp4.status_code == 204
    resp5 = client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert resp5.status_code == 401


def test_invite_flow_with_role_registry(client: TestClient, seeded_admin: str) -> None:
    token = _login(client, seeded_admin)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    invitee = f"member-{uuid4().hex[:8]}@nicekit-qa.dev"

    # 内置可授予角色 member:创建邀请,明文 token 只返回这一次
    resp = client.post(
        "/api/v1/org/members/invites",
        json={"email": invitee, "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    invite_token = resp.json()["token"]

    # platform_admin 永不可授予;sales 等旧业务角色已收敛,未注册即拒绝
    for role in ("platform_admin", "sales"):
        denied = client.post(
            "/api/v1/org/members/invites",
            json={"email": f"x-{uuid4().hex[:8]}@nicekit-qa.dev", "role": role},
            headers=headers,
        )
        assert denied.status_code == 422, f"{role} 应不可授予"

    # 宿主注册角色后立即可授予(请求时派生,非模块级快照)
    roles.register_roles("editor")
    try:
        ok = client.post(
            "/api/v1/org/members/invites",
            json={"email": f"e-{uuid4().hex[:8]}@nicekit-qa.dev", "role": "editor"},
            headers=headers,
        )
        assert ok.status_code == 201, ok.text
    finally:
        roles.reset_registered_roles()

    # 注销注册后同名角色回到不可授予
    gone = client.post(
        "/api/v1/org/members/invites",
        json={"email": f"g-{uuid4().hex[:8]}@nicekit-qa.dev", "role": "editor"},
        headers=headers,
    )
    assert gone.status_code == 422

    # 邀请查看 → 接受(新用户设密码)→ 用新账号登录
    info = client.get(f"/api/v1/auth/invite/{invite_token}")
    assert info.status_code == 200, info.text
    assert info.json() == {
        "email": invitee,
        "role": "member",
        "org_name": "认证测试组织",
        "org_slug": info.json()["org_slug"],
        "user_exists": False,
    }

    accepted = client.post(
        "/api/v1/auth/invite/accept",
        json={"token": invite_token, "full_name": "新成员", "password": "pw-87654321"},
    )
    assert accepted.status_code == 200, accepted.text

    # 已接受的邀请不能复用
    again = client.get(f"/api/v1/auth/invite/{invite_token}")
    assert again.status_code == 404

    member_login = _login(client, invitee, "pw-87654321")
    assert member_login["org"]["role"] == "member"


def test_admin_cannot_change_or_remove_self(client: TestClient, seeded_admin: str) -> None:
    token = _login(client, seeded_admin)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    members = client.get("/api/v1/org/members", headers=headers)
    assert members.status_code == 200, members.text
    me = next(m for m in members.json() if m["email"] == seeded_admin)

    patched = client.patch(
        f"/api/v1/org/members/{me['membership_id']}",
        json={"role": "member"},
        headers=headers,
    )
    assert patched.status_code == 400

    removed = client.delete(f"/api/v1/org/members/{me['membership_id']}", headers=headers)
    assert removed.status_code == 400


def test_members_requires_admin_role(client: TestClient, seeded_admin: str) -> None:
    # 未认证
    assert client.get("/api/v1/org/members").status_code == 401
    # member 角色不足:借邀请流造一个 member,再用其 token 访问
    token = _login(client, seeded_admin)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    invitee = f"m-{uuid4().hex[:8]}@nicekit-qa.dev"
    invite = client.post(
        "/api/v1/org/members/invites",
        json={"email": invitee, "role": "member"},
        headers=headers,
    )
    assert invite.status_code == 201
    client.post(
        "/api/v1/auth/invite/accept",
        json={"token": invite.json()["token"], "password": "pw-11223344"},
    )
    member_token = _login(client, invitee, "pw-11223344")["access_token"]
    resp = client.get(
        "/api/v1/org/members", headers={"Authorization": f"Bearer {member_token}"}
    )
    assert resp.status_code == 403
