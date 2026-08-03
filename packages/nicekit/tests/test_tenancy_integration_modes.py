"""三种租户接入模式的契约(MIGRATION-PLAN §4 扩展点)。

A 全托管(SDK 签发 JWT)/ B 桥接(宿主 resolver)/ C 单租户(分区键恒定)。
本文件全部离线,覆盖:

1. ``tenancy.mapping``:外部主键 → UUID 的确定性、类型等价、命名空间隔离;
2. ``deps.set_principal_resolver``:身份切换点的替换与还原;
3. ``deps.single_tenant_resolver``:恒定分区键与可选的主体/角色回调;
4. ``default_routers(exclude=...)``:按名字摘 router,拼错立即报错;
5. ``create_app`` 的身份接线自检:两套身份来源并存必须启动即炸。

跨租户隔离是否真的生效属于数据库行为,见 test_tenancy_bridge_live.py(live)。
"""

import logging
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi import APIRouter, HTTPException
from starlette.requests import Request

from nicekit.api import deps
from nicekit.api.v1.router import (
    AUTH_ROUTER_NAMES,
    default_routers,
    mounted_auth_router_names,
    router_names,
)
from nicekit.core.config import Settings, get_settings
from nicekit.models.tenancy import Role
from nicekit.runtime.app_factory import create_app
from nicekit.tenancy.mapping import (
    NAMESPACE_SUBJECT,
    NAMESPACE_TENANT,
    derive_uuid,
    subject_uuid,
    tenant_uuid,
)


@pytest.fixture(autouse=True)
def _restore_resolver():
    """resolver 是进程级全局:任何一个用例漏还原都会污染后面所有用例。"""
    yield
    deps.set_principal_resolver(None)


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/kb/bases",
            "query_string": b"",
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ],
        }
    )


def _settings() -> Settings:
    # 每个用例新建:create_app 会就地改写 rate_limit_exempt_paths
    return Settings(
        task_dispatch_mode="inline",
        rate_limit_exempt_paths=["/api/v1/health"],
        cors_origins="http://localhost:3000",
    )


# ---------------------------------------------------------------------------
# 1. tenancy.mapping:外部主键 → UUID
# ---------------------------------------------------------------------------



@pytest.fixture(autouse=True)
def _reset_auth_wiring_state():
    """隔离装配态全局量。

    ``deps._mounted_auth_routers`` 记录"最近一次 create_app 挂了哪些身份路由",
    用于反向拦截"装配之后才接管身份"的错误顺序。它是模块级的,一个用例装配完
    带 auth 的 app 后,下一个用例调 set_principal_resolver 就会被上一条的残留
    拦住 —— 用例之间必须各自干净。
    """
    from nicekit.api import deps as _deps

    _deps.record_mounted_auth_routers(())
    yield
    _deps.record_mounted_auth_routers(())
    _deps.set_principal_resolver(None)


def test_derivation_is_deterministic() -> None:
    """同输入同输出是整个桥接模式的地基:宿主不存映射表,每次请求现算。"""
    assert tenant_uuid("acme") == tenant_uuid("acme")
    assert subject_uuid("u_8891") == subject_uuid("u_8891")


def test_int_and_str_are_equivalent() -> None:
    """宿主换个类型传参不该换一个租户分区(自增 int 与其字符串形式同源)。"""
    assert tenant_uuid(42) == tenant_uuid("42")
    assert subject_uuid(42) == subject_uuid("42")


def test_surrounding_whitespace_is_ignored() -> None:
    assert tenant_uuid(" acme ") == tenant_uuid("acme")


def test_different_inputs_do_not_collide() -> None:
    assert tenant_uuid("acme") != tenant_uuid("globex")


def test_tenant_and_subject_namespaces_are_isolated() -> None:
    """租户 1 号与用户 1 号必须落在不同 UUID,否则分区键会撞进主体位。"""
    assert tenant_uuid("1") != subject_uuid("1")
    assert NAMESPACE_TENANT != NAMESPACE_SUBJECT


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_external_id_is_rejected(blank: str) -> None:
    """空值不能悄悄落到"空串派生"的那个分区——没有租户要显式走单租户模式。"""
    with pytest.raises(ValueError, match="不能为空"):
        tenant_uuid(blank)
    with pytest.raises(ValueError, match="不能为空"):
        subject_uuid(blank)


def test_custom_namespace_isolates_again() -> None:
    """多套外部系统接进同一个平台时,各自的 "1" 号租户不能撞在一起。"""
    crm = uuid5(NAMESPACE_URL, "https://example.test/ns/crm")
    erp = uuid5(NAMESPACE_URL, "https://example.test/ns/erp")

    assert tenant_uuid("1", namespace=crm) != tenant_uuid("1", namespace=erp)
    assert tenant_uuid("1", namespace=crm) != tenant_uuid("1")
    # 自定义命名空间内部仍然确定性
    assert tenant_uuid("1", namespace=crm) == tenant_uuid("1", namespace=crm)


def test_derive_uuid_matches_uuid5_contract() -> None:
    """派生算法是 uuid5:宿主可以在自己那侧用同一公式反算校验。"""
    assert derive_uuid("acme", namespace=NAMESPACE_TENANT) == uuid5(NAMESPACE_TENANT, "acme")
    assert tenant_uuid("acme") == uuid5(NAMESPACE_TENANT, "acme")


def test_tenancy_package_reexports_host_facing_helpers() -> None:
    """宿主一处 import:``from nicekit.tenancy import ...`` 拿全接入零件。"""
    import nicekit.tenancy as tenancy
    from nicekit.tenancy import mapping, orgs, roles

    assert tenancy.tenant_uuid is mapping.tenant_uuid
    assert tenancy.subject_uuid is mapping.subject_uuid
    assert tenancy.ensure_org is orgs.ensure_org
    assert tenancy.register_roles is roles.register_roles
    assert tenancy.register_write_roles is roles.register_write_roles
    assert tenancy.write_roles is roles.write_roles


# ---------------------------------------------------------------------------
# 2. 身份切换点:set_principal_resolver
# ---------------------------------------------------------------------------


async def test_builtin_resolver_is_the_default() -> None:
    assert deps.uses_builtin_auth() is True
    with pytest.raises(HTTPException) as exc:
        await deps.get_org_context(_request())
    assert exc.value.status_code == 401


async def test_custom_resolver_takes_over_every_protected_endpoint() -> None:
    """桥接模式:换掉 resolver 后 get_org_context 不再看 JWT。"""
    org_id, user_id = tenant_uuid("acme"), subject_uuid("u_1")

    async def _resolve(request: Request) -> deps.Principal:
        return deps.Principal(user_id=user_id, org_id=org_id, role="editor")

    deps.set_principal_resolver(_resolve)

    assert deps.uses_builtin_auth() is False
    assert deps.principal_resolver() is _resolve
    # 没有 Authorization 头也能解析出主体——身份完全由宿主说了算
    ctx = await deps.get_org_context(_request())
    assert (ctx.org_id, ctx.user_id, ctx.role) == (org_id, user_id, "editor")


async def test_passing_none_restores_builtin_jwt() -> None:
    async def _resolve(request: Request) -> deps.Principal:
        raise AssertionError("已还原,不该再被调用")

    deps.set_principal_resolver(_resolve)
    deps.set_principal_resolver(None)

    assert deps.uses_builtin_auth() is True
    with pytest.raises(HTTPException) as exc:
        await deps.get_org_context(_request())
    assert exc.value.status_code == 401


async def test_resolver_can_read_request_headers() -> None:
    """宿主常见做法:网关把已认证租户/用户放在头里。"""

    async def _resolve(request: Request) -> deps.Principal:
        return deps.Principal(
            user_id=subject_uuid(request.headers["x-user"]),
            org_id=tenant_uuid(request.headers["x-tenant"]),
            role=str(Role.MEMBER),
        )

    deps.set_principal_resolver(_resolve)
    ctx = await deps.get_org_context(_request({"x-tenant": "globex", "x-user": "77"}))
    assert ctx.org_id == tenant_uuid("globex")
    assert ctx.user_id == subject_uuid(77)


# ---------------------------------------------------------------------------
# 3. 单租户模式
# ---------------------------------------------------------------------------


async def test_single_tenant_defaults_to_its_own_org() -> None:
    resolve = deps.single_tenant_resolver()
    ctx = await resolve(_request())
    assert ctx.org_id == deps.SINGLE_TENANT_ORG_ID
    assert ctx.role == str(Role.ORG_ADMIN)


def test_single_tenant_org_is_not_the_platform_org() -> None:
    """锁死这个决策,防回退。

    平台 org 的语义是"这份数据对所有组织可见"(kb/search.py 的 layer 判定、
    kb/image_assets.py 的可见性过滤都认它)。单租户期间把数据放平台 org 看不出
    差别,可一旦接入第二个租户,先前积累的全部知识会立刻对新租户可见——静默的
    数据泄漏,而且事后难回溯。独立 org 只是个普通租户,升级多租户时零改动。
    """
    assert get_settings().platform_org_id != deps.SINGLE_TENANT_ORG_ID
    # 走保留命名空间:外部租户 ID 哪怕恰好叫这个名字也撞不上(见 mapping 的
    # NAMESPACE_RESERVED),所以这里断言的是"不等",不是派生算法本身
    assert tenant_uuid("__single_tenant__") != deps.SINGLE_TENANT_ORG_ID


async def test_single_tenant_subject_is_stable_across_calls() -> None:
    """固定主体必须每次一致,否则审计里同一个人会散成一堆 UUID。"""
    resolve = deps.single_tenant_resolver(org_id=tenant_uuid("solo"))
    first = await resolve(_request())
    second = await resolve(_request({"x-user": "变了也没用"}))
    assert first == second
    assert first.org_id == tenant_uuid("solo")

    # 派生自 org,换个进程重建 resolver 也是同一个主体
    again = await deps.single_tenant_resolver(org_id=tenant_uuid("solo"))(_request())
    assert again.user_id == first.user_id


async def test_single_tenant_explicit_overrides() -> None:
    org_id, subject_id = tenant_uuid("solo"), subject_uuid("robot")
    resolve = deps.single_tenant_resolver(
        org_id=org_id, subject_id=subject_id, role=str(Role.MEMBER)
    )
    ctx = await resolve(_request())
    assert (ctx.org_id, ctx.user_id, ctx.role) == (org_id, subject_id, str(Role.MEMBER))


async def test_single_tenant_callbacks_bring_the_gateway_user_in() -> None:
    """网关做完认证时,用 role_of/subject_of 把人带进来,审计与记忆才分得清。"""
    resolve = deps.single_tenant_resolver(
        role_of=lambda request: request.headers.get("x-role", str(Role.MEMBER)),
        subject_of=lambda request: subject_uuid(request.headers["x-user"]),
    )
    ctx = await resolve(_request({"x-user": "alice", "x-role": "editor"}))
    assert ctx.user_id == subject_uuid("alice")
    assert ctx.role == "editor"
    assert ctx.org_id == deps.SINGLE_TENANT_ORG_ID

    # 分区键恒定:换个用户仍是同一个 org
    other = await resolve(_request({"x-user": "bob"}))
    assert other.org_id == ctx.org_id
    assert other.user_id != ctx.user_id


# ---------------------------------------------------------------------------
# 4. default_routers(exclude=...)
# ---------------------------------------------------------------------------


def test_router_names_cover_every_router() -> None:
    names = router_names()
    assert len(names) == len(default_routers()) == 18
    assert len(set(names)) == len(names), "name 必须唯一,否则排除语义歧义"
    assert set(AUTH_ROUTER_NAMES) <= set(names)


def test_no_arg_behaviour_is_unchanged() -> None:
    from nicekit.api.v1 import auth, kb, kb_status, members

    routers = default_routers()
    assert len(routers) == 18
    assert auth.router in routers and members.router in routers
    # 顺序契约:固定段路径要先于路径参数匹配
    assert routers.index(kb_status.router) < routers.index(kb.router)


def test_exclude_drops_exactly_the_named_routers() -> None:
    from nicekit.api.v1 import auth, members

    routers = default_routers(exclude=("auth", "members"))
    assert len(routers) == 16
    assert auth.router not in routers
    assert members.router not in routers
    # 其余顺序不变(APIRouter 不可哈希,只能按身份逐个比)
    dropped = (auth.router, members.router)
    kept = tuple(r for r in default_routers() if not any(r is d for d in dropped))
    assert routers == kept


def test_exclude_accepts_any_collection() -> None:
    assert default_routers(exclude={"auth"}) == default_routers(exclude=["auth"])


def test_unknown_exclude_name_raises() -> None:
    """拼错名字必须立刻暴露:静默忽略等于宿主以为 auth 摘了、其实还开着。"""
    with pytest.raises(ValueError, match="authh"):
        default_routers(exclude=("authh",))
    with pytest.raises(ValueError, match="auth-router"):
        default_routers(exclude=("auth", "auth-router"))


def test_excluded_routers_leave_no_paths_behind() -> None:
    app = create_app(
        _settings(),
        routers=default_routers(exclude=("auth", "members")),
        background_loops=(),
    )
    paths = set(app.openapi()["paths"])
    assert not [path for path in paths if path.startswith("/api/v1/auth")]
    assert not [path for path in paths if path.startswith("/api/v1/org/members")]
    assert "/api/v1/kb/bases" in paths, "只该摘掉身份路由"


def test_mounted_auth_router_names_matches_by_identity() -> None:
    """宿主自己写的 /auth router 不是 SDK 身份路由,不该被自检误伤。"""
    from nicekit.api.v1 import auth

    host_auth = APIRouter(prefix="/auth")
    assert mounted_auth_router_names([host_auth]) == ()
    assert mounted_auth_router_names([host_auth, auth.router]) == ("auth",)
    assert mounted_auth_router_names(default_routers()) == ("auth", "members")


# ---------------------------------------------------------------------------
# 5. create_app 身份接线自检
# ---------------------------------------------------------------------------


async def _bridge_resolver(request: Request) -> deps.Principal:
    return deps.Principal(
        user_id=subject_uuid("u_1"), org_id=tenant_uuid("acme"), role=str(Role.ORG_ADMIN)
    )


def test_bridged_mode_with_auth_router_fails_at_startup() -> None:
    """两套身份来源并存 → 启动即炸(运行期只会表现为偶发串号,查不出来)。"""
    deps.set_principal_resolver(_bridge_resolver)
    with pytest.raises(RuntimeError, match="两套身份来源") as exc:
        create_app(_settings(), routers=default_routers(), background_loops=())
    assert "auth" in str(exc.value) and "members" in str(exc.value)


def test_bridged_mode_with_members_router_alone_also_fails() -> None:
    """members 也是身份来源:它能建人、能授角色。"""
    from nicekit.api.v1 import members

    deps.set_principal_resolver(_bridge_resolver)
    with pytest.raises(RuntimeError, match="members"):
        create_app(_settings(), routers=[members.router], background_loops=())


def test_bridged_mode_without_auth_routers_passes(caplog) -> None:
    deps.set_principal_resolver(_bridge_resolver)
    with caplog.at_level(logging.WARNING, logger="nicekit.runtime.app_factory"):
        app = create_app(
            _settings(),
            routers=default_routers(exclude=("auth", "members")),
            background_loops=(),
        )
    assert "/api/v1/kb/bases" in set(app.openapi()["paths"])
    assert caplog.records == [], "接线正确时不该有任何抱怨"


def test_builtin_auth_without_auth_router_only_warns(caplog) -> None:
    """可能是宿主自己签发 SDK 格式 token,记 warning 但不阻断。"""
    with caplog.at_level(logging.WARNING, logger="nicekit.runtime.app_factory"):
        app = create_app(
            _settings(), routers=default_routers(exclude=("auth",)), background_loops=()
        )
    assert app is not None
    assert any("登录端点缺失" in record.getMessage() for record in caplog.records)


def test_builtin_auth_with_auth_router_is_silent(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="nicekit.runtime.app_factory"):
        create_app(_settings(), routers=default_routers(), background_loops=())
    assert caplog.records == []


def test_empty_routers_do_not_warn(caplog) -> None:
    """没挂任何 router 时谈不上"少了登录端点"(测试与自定义装配的常见形态)。"""
    with caplog.at_level(logging.WARNING, logger="nicekit.runtime.app_factory"):
        create_app(_settings(), background_loops=())
    assert caplog.records == []


def test_check_can_be_disabled() -> None:
    deps.set_principal_resolver(_bridge_resolver)
    app = create_app(
        _settings(),
        routers=default_routers(),
        background_loops=(),
        check_auth_wiring=False,
    )
    assert "/api/v1/auth/login" in set(app.openapi()["paths"])


def test_check_runs_before_tool_registry_side_effects() -> None:
    """接线错就别把全局 registry 改脏了——自检必须在任何副作用之前。"""
    from nicekit.agent.tools import ToolRegistry, default_registry

    deps.set_principal_resolver(_bridge_resolver)
    registry = ToolRegistry()
    before = set(default_registry.names())
    with pytest.raises(RuntimeError):
        create_app(
            _settings(),
            routers=default_routers(),
            background_loops=(),
            tool_registry=registry,
        )
    assert set(default_registry.names()) == before


def test_generator_routers_are_still_mounted() -> None:
    """routers 可能是生成器:自检遍历过一遍后不能把它耗干。"""
    app = create_app(
        _settings(), routers=(r for r in default_routers()), background_loops=()
    )
    assert "/api/v1/auth/login" in set(app.openapi()["paths"])


def test_principal_is_org_context_alias() -> None:
    """桥接宿主按 Principal 编程,SDK 内部按 OrgContext——必须是同一个类型。"""
    assert deps.Principal is deps.OrgContext
    principal = deps.Principal(
        user_id=subject_uuid("u"), org_id=tenant_uuid("t"), role=str(Role.MEMBER)
    )
    assert isinstance(principal.org_id, UUID)


# --- 主控 review 补充:两处被 T1 指出/裁决的边界 -------------------------------


def test_tenant_uuid_rejects_none_not_just_empty_string() -> None:
    """None 必须报错,不能靠 str() 变成 "None" 混过去。

    None 恰恰是"这个请求没带租户"的信号。放过去不会有任何报错,只会让所有
    缺租户的请求静默共用同一个分区键——一个看着完全正常、却把不同客户数据
    混在一起的分区。
    """
    from nicekit.tenancy.mapping import subject_uuid, tenant_uuid

    for factory in (tenant_uuid, subject_uuid):
        for bad in (None, "", "   "):
            with pytest.raises(ValueError):
                factory(bad)


def test_excluding_admin_drops_the_whole_admin_surface() -> None:
    """排 admin 要连 admin_mcp 一起摘掉。

    宿主的心智是"我不要平台管理面",不该要求他知道内部拆成了两个 router
    对象;留下 /admin/mcp-servers 这类 platform_admin 专属端点属于
    "以为关了其实开着",是这里最危险的失败模式。
    """
    from nicekit.api.v1.router import default_routers

    full = default_routers()
    without_admin = default_routers(exclude={"admin"})
    assert len(full) - len(without_admin) == 2, "admin 应展开为 admin + admin_mcp"

    # 细粒度仍然可用:只摘 MCP 配置面、保留其余 admin 端点
    without_mcp = default_routers(exclude={"admin_mcp"})
    assert len(full) - len(without_mcp) == 1


def test_taking_over_identity_after_app_assembly_is_rejected(monkeypatch) -> None:
    """先 create_app 挂着 auth、之后才接管身份 —— 这个顺序必须报错。

    create_app 的自检在装配那一刻读全局状态,此后再 set_principal_resolver
    它已经跑完、什么都拦不住;而这恰恰是最容易写出来的顺序(先照抄全托管
    的装配代码跑通,再加自己的身份接管)。所以反向再拦一次。
    """
    from nicekit.api import deps

    monkeypatch.setattr(deps, "_mounted_auth_routers", ("auth", "members"))
    with pytest.raises(RuntimeError, match="身份接线顺序错误"):
        deps.set_principal_resolver(deps.single_tenant_resolver())

    # 排除了身份路由的装配则放行
    monkeypatch.setattr(deps, "_mounted_auth_routers", ())
    deps.set_principal_resolver(deps.single_tenant_resolver())
    assert not deps.uses_builtin_auth()
    deps.set_principal_resolver(None)


def test_single_tenant_org_uses_a_reserved_namespace() -> None:
    """保留分区键不能与"外部租户恰好叫这个名字"撞上。"""
    from nicekit.api.deps import SINGLE_TENANT_ORG_ID
    from nicekit.tenancy.mapping import tenant_uuid

    assert tenant_uuid("single_tenant") != SINGLE_TENANT_ORG_ID
    assert tenant_uuid("__single_tenant__") != SINGLE_TENANT_ORG_ID
