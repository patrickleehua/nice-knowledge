# AGENT-BRIEF:nicekit 租户接入(AI 编码助手专用)

面向人的完整版:`docs/INTEGRATION.md`。本文只给可直接照抄的结构化事实。
所有签名摘自工作区实际代码,已逐条 import 验证。

## 1. 选模式

| 判据 | 模式 | 关键动作 |
|---|---|---|
| 宿主无租户概念 / 认证由网关做完 | **single** | `set_principal_resolver(single_tenant_resolver())` |
| 宿主已有租户与登录 | **bridge** | `set_principal_resolver(自己的实现)` |
| 全新产品,愿用 SDK 自带账号体系 | **managed** | 什么都不做 |

bridge 与 single **必须** `default_routers(exclude=AUTH_ROUTER_NAMES)`,否则 `create_app` 抛 `RuntimeError`。

---

## 2. API 契约

### 2.1 `nicekit.api.deps`

```python
from nicekit.api.deps import (
    SINGLE_TENANT_ORG_ID,     # UUID 常量 = 68bfb6c1-70e0-56a3-a73c-13bf8d3b5695
    OrgContext,               # frozen dataclass(user_id: UUID, org_id: UUID, role: str)
    Principal,                # 就是 OrgContext(Principal is OrgContext -> True)
    PrincipalResolver,        # Protocol: async def __call__(self, request: Request) -> Principal
    get_org_context,          # async (request: Request) -> OrgContext   受保护端点的身份入口
    get_org_session,          # 依赖:返回已 bind org 上下文的 AsyncSession
    principal_resolver,       # () -> PrincipalResolver
    require_role,             # (*roles: Role | str) -> 依赖   允许集合在 import 期定格
    require_write_role,       # () -> 依赖                     请求期读 tenancy.roles 注册表
    set_principal_resolver,   # (resolver: PrincipalResolver | None) -> None;None = 还原内置
    single_tenant_resolver,
    uses_builtin_auth,        # () -> bool;True = 仍是内置 JWT
)

def single_tenant_resolver(
    *,
    org_id: UUID | None = None,          # 默认 SINGLE_TENANT_ORG_ID
    role: str = "org_admin",
    subject_id: UUID | None = None,
    role_of: Callable[[Request], str] | None = None,
    subject_of: Callable[[Request], UUID] | None = None,
) -> PrincipalResolver: ...
```

resolver 实现约定:认证失败抛 `HTTPException(401)`,越权抛 403,**不要返回 None**;
`org_id`/`user_id` 必须是 UUID;跑在每个请求上,重活自己缓存。

### 2.2 `nicekit.tenancy`

```python
from nicekit.tenancy import (                     # 一处 import,推荐
    ensure_org, register_roles, register_write_roles, subject_uuid, tenant_uuid, write_roles)
from nicekit.tenancy.mapping import NAMESPACE_SUBJECT, NAMESPACE_TENANT, derive_uuid
from nicekit.tenancy.roles import (PLATFORM_ADMIN, ORG_ADMIN, MEMBER,
    all_roles, assignable_roles, reset_registered_roles, reset_write_roles)

def tenant_uuid(external_id: object, *, namespace: UUID | None = None) -> UUID: ...
def subject_uuid(external_id: object, *, namespace: UUID | None = None) -> UUID: ...
def derive_uuid(external_id: object, *, namespace: UUID) -> UUID: ...   # namespace 必填 kw

async def ensure_org(session: AsyncSession, org_id: UUID, *,
                     name: str | None = None, slug: str | None = None) -> UUID: ...
    # ON CONFLICT DO NOTHING;**不 commit**;已存在时不覆盖 name/slug

def register_roles(*names: str, assignable: bool = True) -> None: ...
def register_write_roles(*names: str) -> None: ...
def write_roles() -> tuple[str, ...]: ...     # 初始 ('platform_admin', 'org_admin')
def all_roles() -> tuple[str, ...]: ...       # 初始 ('platform_admin', 'org_admin', 'member')
```

实测行为:

```python
tenant_uuid(42) == tenant_uuid("42")     # True  先 str() 再派生,换类型不换分区
tenant_uuid(42) == subject_uuid(42)      # False 命名空间隔离
tenant_uuid("  ")                        # ValueError: external_id 不能为空
tenant_uuid(None)                        # ValueError: external_id 不能为 None(见 §4.5)
register_roles("platform_admin")         # ValueError:平台保留角色
register_write_roles("没登记过的")        # ValueError:请先 register_roles()
```

角色名规则:`[a-z][a-z0-9_]{0,31}`(落库 `varchar(32)`)。

### 2.3 `nicekit.api.v1.router`

```python
from nicekit.api.v1.router import (
    AUTH_ROUTER_NAMES,          # ('auth', 'members')
    api_router,                 # 全量单一挂载点
    default_routers,            # (exclude: Collection[str] = ()) -> tuple[APIRouter, ...]
    mounted_auth_router_names,  # (routers: Iterable[APIRouter]) -> tuple[str, ...]
    router_names,               # () -> tuple[str, ...]
)
```

`router_names()` 的 18 个合法值(即 `exclude` 取值域,顺序即挂载顺序):

```
health, auth, kb_status, kb, kb_feedback, kb_integrity, kb_lifecycle_metrics,
kb_entity_types, kb_media, members, admin, admin_mcp, agent_permissions,
chat, memory, icron, notifications, media
```

- `default_routers()` → 18 router / 182 端点;`exclude=AUTH_ROUTER_NAMES` → 16 / 173。
- `exclude` 里有未知名字 → `ValueError` 并列出全部合法值(不会静默忽略)。

### 2.4 `nicekit.runtime`

```python
def create_app(settings: Settings | None = None, *,
    routers: Iterable[APIRouter] = (), router_prefix: str = "/api/v1",
    background_loops: Sequence[BackgroundLoop] | None = None,   # () = 一个循环都不起
    startup_hooks: Sequence[LifecycleHook] = (), shutdown_hooks: Sequence[LifecycleHook] = (),
    tool_registry: object | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    title: str | None = None, version: str | None = None,
    expose_metrics: bool = True, recover_orphans_on_startup: bool = True,
    warm_mcp_on_startup: bool = True, check_auth_wiring: bool = True) -> FastAPI: ...

async def bootstrap_platform(session: AsyncSession, *, org_id: UUID | None = None,
    entity_type_specs: list[dict] | None = None, seed_agent_card: bool = True,
    single_tenant: bool = False) -> BootstrapReport: ...
    # single_tenant=True:ensure_org(SINGLE_TENANT_ORG_ID) 并把 seed 归属改到它

def install_default_ports(*, force: bool = False) -> None: ...
```

`BootstrapReport` 字段:`entity_types, kb_prompts, kb_answer_route, agent_default_route,
agent_cards, single_tenant_org, prompt_resource_issues`;`.as_dict()` 可直接打印。

`check_auth_wiring` 实测行为:

| resolver 已接管 | 挂了 auth/members | 结果 |
|---|---|---|
| 是 | 是 | **`RuntimeError`**,进程起不来 |
| 是 | 否 | 通过 |
| 否(内置 JWT) | 否,且 routers 非空 | WARNING 日志,不阻断 |
| 否 | 是 | 通过 |

### 2.5 会话与迁移

```python
from nicekit.core.db import bind_org_context, get_session, get_session_factory, org_session
def org_session(factory: async_sessionmaker[AsyncSession], org_id: UUID) -> AsyncSession: ...
    # 绑好 org 上下文的独立会话(worker/脚本用),**调用方负责 close**

from nicekit.core.event_loop import use_selector_event_loop_on_windows   # 脚本顶部必调
from nicekit.migrations.rls import op_enable_org_rls, op_platform_read, rls_tables_check
def op_enable_org_rls(op, table_name: str) -> None: ...    # ENABLE + FORCE + org_isolation
```

---

## 3. 执行步骤

| # | 做什么 | 验证 | 失败怎么办 |
|---|---|---|---|
| 1 | `pyproject.toml` 加 `dependencies = ["nicekit"]`(单仓内加 `[tool.uv.sources] nicekit = { workspace = true }`) | `uv run --package <你的包> python -c "import nicekit"` | 检查 uv workspace `members` 是否含你的包路径 |
| 2 | `docker compose up -d`;`cd packages/nicekit && uv run --package nicekit alembic upgrade head` | `alembic current` 返回单个 head | `MIGRATION_DATABASE_URL` 未配;或用了官方 postgres 镜像(必须用 `deploy/` 定制镜像,预装 pgvector/pg_trgm/zhparser/pgcrypto) |
| 3 | 装配,顺序见下方代码块 | `assert uses_builtin_auth() is False` | `RuntimeError: 身份接线自相矛盾` → `exclude` 漏了;顺序写反不报错但是错的(§4.2) |
| 4 | seed:managed/bridge `await bootstrap_platform(session)`;single 加 `single_tenant=True` | `report.as_dict()` 无异常;single 模式 `single_tenant_org` 非 None | Windows 漏 `use_selector_event_loop_on_windows()` → psycopg `InterfaceError: cannot use the 'ProactorEventLoop'` |
| 5 | **bridge 必做**:垫影子行 `ensure_org` + `ensure_user`(§5.1)后 `commit` | `PUT /api/v1/agent/permissions/preferences` 返回 200 而非 500 | `ForeignKeyViolation ... agent_permission_preferences_org_id_fkey` / `_user_id_fkey` |
| 6 | `uv run python run.py`(Windows 必须;Linux 可直接 uvicorn) | `curl -s localhost:8000/api/v1/health` → `{"status":"ok"}` | 见 §4.11 |
| 7 | 跑 §5.2 自检脚本 | 全 PASS | 按 FAIL 项回到对应步骤 |

步骤 3 的顺序不可换:

```python
def build_app() -> FastAPI:
    install_default_ports()                                   # 1
    register_roles("editor"); register_write_roles("editor")  # 2  仅 bridge
    set_principal_resolver(my_resolver)                       # 3  必须早于 create_app
    return create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES))   # 4

app = build_app()
```

---

## 4. 不要这样做

**4.1 不要自己拼 SQL 绕过 RLS。** 走库要么用 `get_org_session` 依赖,要么用
`org_session(factory, org_id)`。裸 `factory()` 会话没有 `app.current_org_id`,RLS 恒假,
你会读到 0 行然后以为"数据没写进去"。**永远不要** `DISABLE ROW LEVEL SECURITY` 或给账号 `BYPASSRLS`。

**4.2 不要在 `create_app` 之后才 `set_principal_resolver`。** 自检在 `create_app` 执行那一刻读
全局状态;装配后再注册,自检看到的是内置 JWT → 静默放行,运行期却真的两套身份并存(已实测复现)。

**4.3 不要在 resolver 里 commit 请求事务。** resolver 早于 `get_org_session` 执行,此时请求事务
还没开始。要写库(如垫影子行)就 `async with get_session_factory()() as s: ...; await s.commit()`
开独立短事务,并用进程内集合短路,别每个请求都打库。

**4.4 不要把 `org_id` 硬编码。** 用 `tenant_uuid(外部主键)` 现算(确定性,不需要映射表)。
也不要把 `platform_org_id`(`00000000-…-0001`)当业务分区:它的语义是"对所有组织可见"
(`chat_events`/`llm_traces`/`usage_daily` 有 `platform_read` 策略,KB 搜索把它判为公共层),
单租户请用 `SINGLE_TENANT_ORG_ID`。

**4.5 不要把 `None` / 空串喂给 `tenant_uuid`/`subject_uuid`。** SDK 会 `ValueError`(刻意的:
`str(None)=="None"` 非空,放过去会让所有缺租户的请求静默共用同一个分区)。但**别指望它兜底** ——
resolver 里先判空并抛 401/403,否则 `ValueError` 冒到最外层就是 500。

**4.6 不要同时挂 auth router 又注册 resolver**(见 §2.4 表)。也不要"只排 `admin`":
`admin` 与 `admin_mcp` 是**两个** router,`exclude=("admin",)` 会留下
`/api/v1/admin/mcp-servers`、`.../status`、`.../{server_id}`、`.../{server_id}/status`、
`.../{server_id}/test` 五条端点。要摘管理面用 `exclude=(*AUTH_ROUTER_NAMES, "admin", "admin_mcp")`。

**4.7 不要用 `sys.modules` 打桩或 monkeypatch 内部符号。** 身份切换点只有一个,测试里用它并还原:

```python
@pytest.fixture(autouse=True)
def _restore_resolver():
    yield
    set_principal_resolver(None)   # resolver 是进程级全局,漏还原会污染后续用例
```

**4.8 不要用 `app.routes` 数路由。** FastAPI 0.141 起 `include_router` 不展开,
`app.routes` 里是 `_IncludedRouter`,数出来是 18 而不是 182。用 `app.openapi()["paths"]`。

**4.9 不要在脚本里重复调 `build_app()`。** 宿主模块通常导入时已 `app = build_app()`;
再调一次会把自定义工具重复并入全局注册表,报 `ToolRegistrationError: duplicate tool names`。
脚本里 `from myhost.main import app`。

**4.10 不要漏 `/v1`。** SDK REST 前缀是 `/api/v1`;LLM provider 的 `base_url` 也必须带版本段
(如 `https://api.siliconflow.cn/v1`),否则 404 且难排查。

**4.11 不要直接 `uvicorn myhost.main:app`(Windows)。** uvicorn 在 win32 硬编码
`ProactorEventLoop`,psycopg async 不支持,症状是 HTTP 200 但所有 DB 操作静默失败。
走 `run.py`(`asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)`);
脚本顶部用 `use_selector_event_loop_on_windows()`。

---

## 5. 可复制代码

### 5.1 `ensure_user`(SDK 未提供,bridge 必备)

```python
from uuid import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from nicekit.models.tenancy import User


async def ensure_user(session: AsyncSession, user_id: UUID, *,
                      email: str | None = None, full_name: str | None = None) -> UUID:
    """幂等垫一行 users(agent 权限三表的 user_id 外键需要)。不 commit。"""
    await session.execute(
        pg_insert(User)
        .values(
            id=user_id,
            email=email or f"{user_id}@users.noreply.invalid",   # email 有唯一约束
            password_hash="!external",                           # 永不用于登录
            full_name=full_name or f"user-{user_id.hex[:8]}",
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=[User.id])
    )
    return user_id
```

### 5.2 自检脚本 `selfcheck.py`

```python
"""nicekit 租户接入自检。用法:uv run python selfcheck.py <managed|bridge|single> <org_id>"""

import asyncio
import sys
from uuid import UUID

from sqlalchemy import text

from nicekit.api import deps
from nicekit.api.v1.router import AUTH_ROUTER_NAMES
from nicekit.core.db import get_session_factory, org_session
from nicekit.core.event_loop import use_selector_event_loop_on_windows

use_selector_event_loop_on_windows()

# 改成你自己的 ASGI 入口。导入已装配好的 app,**不要再调一次 build_app()**(§4.9)。
from demo_backend.main import app  # noqa: E402

PROBE_TABLE = "agent_permission_preferences"   # 任意一张开了 RLS 的表
ok = True


def check(name: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"[{'PASS' if passed else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


async def main(mode: str, org_id: UUID) -> None:
    builtin = deps.uses_builtin_auth()
    paths = set(app.openapi()["paths"])        # 必须走 openapi(),不能数 app.routes(§4.8)
    auth_paths = sorted(p for p in paths if "/auth/" in p or "/org/members" in p)

    if mode == "managed":
        check("全托管:仍用内置 JWT resolver", builtin)
        check("全托管:auth/members 已挂载", bool(auth_paths), f"{len(auth_paths)} 条身份路由")
    else:
        check("resolver 已接管(uses_builtin_auth() is False)", not builtin,
              f"当前 resolver={deps.principal_resolver()!r}")
        check("SDK 身份路由已摘", not auth_paths,
              f"残留 {auth_paths}" if auth_paths else f"共 {len(paths)} 条路由")
    print(f"       (身份 router 名字 {list(AUTH_ROUTER_NAMES)},"
          f"用 default_routers(exclude=AUTH_ROUTER_NAMES) 排除)")

    factory = get_session_factory()

    async with factory() as s:                 # org 行是否存在(agent 权限三表外键靠它)
        n = (await s.execute(
            text("SELECT count(*) FROM organizations WHERE id = :o"), {"o": org_id}
        )).scalar_one()
    check("organizations 有该 org 行", n == 1, f"org_id={org_id} count={n}")

    async with factory() as s:                 # RLS:未绑定上下文 -> 什么都看不到
        n = (await s.execute(text(f"SELECT count(*) FROM {PROBE_TABLE}"))).scalar_one()
    check("RLS fail-closed(无 org 上下文可见 0 行)", n == 0, f"可见 {n} 行")

    s = org_session(factory, org_id)           # RLS:跨 org 不可见
    try:
        n = (await s.execute(
            text(f"SELECT count(*) FROM {PROBE_TABLE} WHERE org_id <> :o"), {"o": org_id}
        )).scalar_one()
    finally:
        await s.close()
    check("RLS 跨 org 过滤(本 org 会话看不到别人的行)", n == 0, f"泄漏 {n} 行")

    print("\n结论:", "接入正确" if ok else "存在问题,勿上线")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"managed", "bridge", "single"}:
        raise SystemExit("用法:python selfcheck.py <managed|bridge|single> <org_id>")
    asyncio.run(main(sys.argv[1], UUID(sys.argv[2])))
```

实测输出(demo 宿主 managed 模式,exit 0):

```
[PASS] 全托管:仍用内置 JWT resolver
[PASS] 全托管:auth/members 已挂载 — 9 条身份路由
[PASS] organizations 有该 org 行 — org_id=00000000-0000-0000-0000-000000000001 count=1
[PASS] RLS fail-closed(无 org 上下文可见 0 行) — 可见 0 行
[PASS] RLS 跨 org 过滤(本 org 会话看不到别人的行) — 泄漏 0 行
```

### 5.3 装配骨架

```python
# ---------- bridge ----------
from fastapi import FastAPI, HTTPException, Request
from nicekit.api.deps import Principal, set_principal_resolver
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports
from nicekit.tenancy import register_roles, register_write_roles, subject_uuid, tenant_uuid

ROLE_MAP = {"OWNER": "org_admin", "EDITOR": "editor", "VIEWER": "member",
            "SUPERADMIN": "platform_admin"}   # platform_admin 只能硬写字面量(§6.4)


async def resolver(request: Request) -> Principal:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Not authenticated")
    claims = verify_host_token(token)                 # 本地验签,别做网络往返
    role = ROLE_MAP.get(claims["role"])
    if role is None:
        raise HTTPException(403, f"未映射的角色 {claims['role']!r}")
    if not claims.get("company_id"):
        raise HTTPException(403, "token 里没有租户")   # §4.5
    org_id, user_id = tenant_uuid(claims["company_id"]), subject_uuid(claims["user_id"])
    await provision(org_id, user_id)                  # ensure_org + ensure_user,见 INTEGRATION.md §3.1
    return Principal(org_id=org_id, user_id=user_id, role=role)


def build_app() -> FastAPI:
    install_default_ports()
    register_roles("editor")
    register_write_roles("editor")
    set_principal_resolver(resolver)                  # 必须早于 create_app
    return create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES), title="My Platform")


app = build_app()


# ---------- single(替换上面的 resolver 与 build_app)----------
from nicekit.api.deps import single_tenant_resolver

def build_app() -> FastAPI:
    install_default_ports()
    set_principal_resolver(single_tenant_resolver())  # 默认 org = SINGLE_TENANT_ORG_ID
    return create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES), title="Internal Tool")
# seed 时必须:await bootstrap_platform(session, single_tenant=True)
```

---

## 6. 数据事实(排障用)

| 事实 | 值 |
|---|---|
| 带 `org_id` 列的表 | 52 |
| 开 FORCE RLS 的表 | 46(49 条策略 = 46 条 `org_isolation` + 3 条 `platform_read`) |
| `platform_read` 所在表 | `chat_events` / `llm_traces` / `usage_daily`(仅 SELECT) |
| `org_id` 外键 → `organizations` | 6 张:`agent_approval_policies`、`agent_permission_grants`、`agent_permission_preferences`、`invitations`、`memberships`、`refresh_tokens` |
| 外键 → `users` | 5 条,分布在上述 3 张 agent 表 + `memberships` + `refresh_tokens` |
| RLS 策略表达式 | `org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid` |
| 未设上下文时 | 策略恒假(fail-closed) |
| `platform_org_id` | `00000000-0000-0000-0000-000000000001`(迁移已 seed) |
| `SINGLE_TENANT_ORG_ID` | `68bfb6c1-70e0-56a3-a73c-13bf8d3b5695`(**迁移未 seed**,需 `single_tenant=True`) |
| 全量端点 / 摘身份后 | 182 / 173 |
| `/api/v1/admin/*` | 整个 router 要求 `platform_admin` |
| KB 跨组织分享 | `POST /api/v1/kb/bases/{kb_id}/shares`,body `{"grantee_org_slug": …}` —— 靠 `organizations.slug` 找人;`ensure_org` 不传 slug 会得到 `org-<hex12>` 占位 |

---

## 7. 已知契约缺口(写代码时绕开)

1. **没有 `ensure_user`**:`nicekit.tenancy.orgs` 只有 `ensure_org`,但 agent 权限三表同时有
   `users` 外键。照抄 §5.1。
2. **`check_auth_wiring` 有顺序盲区**:`create_app` 之后再 `set_principal_resolver` 可绕过(§4.2)。
3. **`platform_admin` 不可注册**(`register_roles` 会 `ValueError`),bridge 模式想要平台管理面
   只能在角色映射里硬写字面量 `"platform_admin"`。
4. **resolver 是进程级全局**,无法按 app 实例隔离;测试必须 teardown 还原。
5. **角色名不校验**:`Principal(role="typo")` 不报错,只在每个端点静默 403。建议加单测
   `assert set(ROLE_MAP.values()) <= set(all_roles())`。
