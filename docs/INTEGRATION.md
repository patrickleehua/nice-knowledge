# 租户接入指南

nicekit 曾经焊死自带的租户体系:必须用 SDK 的 `organizations` / `users` / `memberships`,
必须走 SDK 签发的 JWT。现在不必了 —— **认证入口收敛在 `get_org_context` 一个依赖上**,
换掉它背后的 `PrincipalResolver`,SDK 就接进你已有的身份体系。

本文给出三种接入模式的完整可复制方案。想看扩展点(工具 / 实体类型 / ContextProvider)
去 `docs/SDK-GUIDE.md`;这里只讲**"这个请求属于谁"**。

> 面向 AI 编码助手的极简版:`docs/AGENT-BRIEF.md`。

---

## 0. 我该选哪种模式

三个问题:

```
Q1. 你的系统里有"租户 / 公司 / 团队"这种数据隔离单位吗?
    ├─ 没有,全公司共用一套数据 ────────────────────────► 模式 C 单租户
    └─ 有 ↓

Q2. 这些租户和用户已经存在于你自己的库里(有自己的登录、自己的主键)吗?
    ├─ 是,我不想再维护第二套账号 ─────────────────────► 模式 B 桥接
    └─ 否,我正在做一个新产品,想直接用现成的多租户底座 ► 模式 A 全托管
```

| | A 全托管 | B 桥接 | C 单租户 |
|---|---|---|---|
| 谁签发凭据 | SDK(JWT + 旋转 refresh) | 宿主 | 网关 / 无 |
| `organizations` / `users` 表 | SDK 权威数据 | 影子行(垫外键用) | 一行 |
| 挂 `auth` / `members` router | 是 | **否** | **否** |
| `set_principal_resolver` | 不调用 | 必须 | 必须 |
| RLS | 开 | 开 | **开**(见 §5) |
| 前端登录页 | 用 SDK 的 | 用宿主自己的 | 无 / 网关 |

三种模式都能随时改主意:改的是装配代码,不是数据模型。

---

## 1. 先理解三件事

### 1.1 `org_id` 绝大多数时候只是分区键

52 张表带 `org_id` 列,其中 46 张开了 FORCE RLS。但**指向 `organizations` 的外键只有 6 条**:

| 表 | 外键列 | 谁用 |
|---|---|---|
| `agent_approval_policies` | `org_id`, `created_by`→`users` | agent 七轴权限 |
| `agent_permission_grants` | `org_id`, `user_id`/`created_by`/`revoked_by`→`users` | agent 七轴权限 |
| `agent_permission_preferences` | `org_id`, `user_id`→`users` | agent 七轴权限 |
| `invitations` / `memberships` / `refresh_tokens` | `org_id`, `user_id`→`users` | **SDK 自带认证专用**,桥接/单租户模式下永不写 |

也就是说:**KB、检索、chat、记忆、计量这些主链路完全不需要 `organizations` 里有对应行**。
只有 agent 权限那 3 张表要写入时,外键必须成立 —— 那正是 `ensure_org()`(以及本文
§3.3 的影子用户)存在的理由。

### 1.2 身份的唯一切换点

```
请求 → get_org_context(request)            ← nicekit/api/deps.py,所有受保护端点的入口
        └─ 委派给当前注册的 PrincipalResolver
             ├─ 默认:_jwt_principal_resolver(解 SDK 自己签的 JWT)
             └─ set_principal_resolver(你的实现) 之后:你的实现
                  ↓
              Principal(org_id, user_id, role)
                  ↓
       get_org_session → bind_org_context(session, org_id)
                  ↓
       每个事务 SET LOCAL app.current_org_id → RLS 过滤
```

`Principal` 就是 `OrgContext` 的别名(同一个 frozen dataclass),三个字段:

```python
@dataclass(frozen=True)
class OrgContext:
    user_id: UUID
    org_id: UUID
    role: str
```

### 1.3 角色是自由字符串

内置三个:`platform_admin`(平台保留,不可在租户内授予)、`org_admin`、`member`。
宿主用 `register_roles("editor")` 追加,用 `register_write_roles("editor")` 让它能写知识库。
`require_role` / `require_write_role` 按**字符串值**比较。

---

## 2. 模式 A:全托管

宿主什么都不用做 —— 这就是 `apps/demo/backend` 的形态,照抄它即可。

```python
# src/myhost/main.py
from nicekit.api.v1.router import default_routers
from nicekit.runtime.app_factory import create_app

app = create_app(routers=default_routers(), title="My Platform")
```

```bash
docker compose up -d
cd packages/nicekit && uv run --package nicekit alembic upgrade head && cd ../..
# seed:平台基线 + 你的第一个组织与管理员(照抄 apps/demo/backend/src/demo_backend/seed.py)
uv run --package nicekit-demo-backend python -m demo_backend.seed
uv run python run.py
```

登录拿 token:`POST /api/v1/auth/login {"email", "password", "org_slug"}`。

本文其余部分讲的是**你不用它自带那一套**的情况。

---

## 3. 模式 B:桥接(宿主已有认证与租户)

### 3.1 完整代码

目录:

```
myhost/
├── pyproject.toml
├── run.py
├── .env
└── src/myhost/
    ├── identity.py      # resolver + 影子行垫入
    ├── main.py          # create_app 装配
    └── seed.py          # 平台基线 seed
```

**`pyproject.toml`**

```toml
[project]
name = "myhost"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["nicekit"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/myhost"]

# 在 nice-knowledge 单仓内开发时改走 workspace:
# [tool.uv.sources]
# nicekit = { workspace = true }
```

**`.env`**(变量名 = `nicekit.core.config.Settings` 字段名的大写)

```dotenv
DATABASE_URL=postgresql+psycopg://niceknowledge_app:app-dev-secret@localhost:5433/niceknowledge
MIGRATION_DATABASE_URL=postgresql+psycopg://niceknowledge_migrator:migrator-dev-secret@localhost:5433/niceknowledge
REDIS_URL=redis://localhost:6380/0
MINIO_ENDPOINT=localhost:9010
MINIO_ACCESS_KEY=niceknowledge
MINIO_SECRET_KEY=niceknowledge-dev-secret
MINIO_BUCKET=niceknowledge
MINIO_SECURE=false
# 桥接模式不签 JWT,但 Settings 仍有该字段;留默认即可,别当成有效凭据用
JWT_SECRET=unused-in-bridge-mode-but-keep-32-plus-bytes-anyway
NICEKIT_SECRET_KEY=replace-with-a-long-random-string
CORS_ORIGINS=http://localhost:3000
TASK_DISPATCH_MODE=inline
```

**`src/myhost/identity.py`** —— 这是桥接模式的全部核心。

```python
"""把宿主的身份翻译成 nicekit 的 Principal。"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import Principal
from nicekit.core.db import get_session_factory
from nicekit.models.tenancy import User
from nicekit.tenancy import ensure_principal, register_roles, register_write_roles
from nicekit.tenancy.mapping import subject_uuid, tenant_uuid

# ---------------------------------------------------------------------------
# 1) 角色映射:宿主角色名 → SDK 角色名
# ---------------------------------------------------------------------------
# 建议在装配期调用一次(幂等)。SDK 内置 platform_admin / org_admin / member,
# 其余都要先 register_roles 登记;要能写知识库还得 register_write_roles。
def install_roles() -> None:
    register_roles("editor", "auditor")
    register_write_roles("editor")


ROLE_MAP: dict[str, str] = {
    "OWNER": "org_admin",
    "ADMIN": "org_admin",
    "EDITOR": "editor",       # 需 register_roles + register_write_roles
    "VIEWER": "member",
    "AUDITOR": "auditor",     # 只读业务角色,只需 register_roles
    "SUPERADMIN": "platform_admin",   # 能进 /api/v1/admin/*,慎发
}

# ---------------------------------------------------------------------------
# 2) 影子行垫入:agent 权限三表的外键需要 organizations + users 里有行
# ---------------------------------------------------------------------------
# SDK 提供 ensure_principal 一次垫齐 org 与 user 两侧(agent 权限三表对
# organizations 与 users 都有外键,只垫一半会在"第一次改权限偏好"时才 500)。
# 想分开控制也可以单独用 ensure_org / ensure_user。


# 进程内已垫过的 (org, user);避免每个请求都打一次库
_provisioned: set[tuple[UUID, UUID]] = set()


async def provision(
    org_id: UUID,
    user_id: UUID,
    *,
    org_name: str | None = None,
    org_slug: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
) -> None:
    if (org_id, user_id) in _provisioned:
        return
    async with get_session_factory()() as session:
        # ensure_principal = ensure_org + ensure_user。用它而不是分开调:
        # 两侧都有外键,只垫一半的报错要等到用户第一次改权限偏好才出现。
        await ensure_principal(
            session, org_id, user_id,
            org_name=org_name, org_slug=org_slug,
            user_email=email, user_full_name=full_name,
        )
        await session.commit()      # 这里 commit 是刻意的:独立短事务,不挂在请求事务上
    _provisioned.add((org_id, user_id))


# ---------------------------------------------------------------------------
# 3) resolver 本体
# ---------------------------------------------------------------------------
async def host_principal_resolver(request: Request) -> Principal:
    """把宿主 token 解析成 Principal。认证失败抛 401,越权抛 403,永远不要返回 None。"""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Not authenticated")

    # 换成你自己的校验:本地验签(推荐)/ 查缓存 / 调内网认证服务。
    # 这一步会跑在**每一个请求**上,别在这里查库或做网络往返。
    claims = verify_host_token(token)          # 你的实现

    role = ROLE_MAP.get(claims["role"])
    if role is None:
        raise HTTPException(403, f"未映射的宿主角色 {claims['role']!r}")

    # 先兜住租户缺失:tenant_uuid(None) 会 ValueError,不拦就冒成 500;
    # 这里挡一道,变成语义正确的 403(见 §3.2)。
    company_id = claims.get("company_id")
    if company_id in (None, ""):
        raise HTTPException(403, "token 里没有租户")

    org_id = tenant_uuid(company_id)             # int / str / 雪花号都行
    user_id = subject_uuid(claims["user_id"])

    # 首见这对 (租户, 用户) 时垫影子行;之后走进程内缓存,零开销。
    # 更好的做法见 §3.3:在宿主"创建公司 / 邀请成员"时就调 provision()。
    await provision(
        org_id,
        user_id,
        org_name=claims.get("company_name"),
        org_slug=claims.get("company_slug"),
        email=claims.get("email"),
        full_name=claims.get("name"),
    )
    return Principal(org_id=org_id, user_id=user_id, role=role)
```

**`src/myhost/main.py`**

```python
from fastapi import FastAPI
from nicekit.api.deps import set_principal_resolver
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports

from myhost.identity import host_principal_resolver, install_roles


def build_app() -> FastAPI:
    install_default_ports()          # SDK 内置工具 / KB 目录 / 通知端口(幂等)
    install_roles()                  # 必须在 create_app 之前
    set_principal_resolver(host_principal_resolver)   # 必须在 create_app 之前
    return create_app(
        # AUTH_ROUTER_NAMES == ("auth", "members");不排掉会被启动自检拦下
        routers=default_routers(exclude=AUTH_ROUTER_NAMES),
        title="My Platform",
    )


app = build_app()
```

**`src/myhost/seed.py`**(幂等,每次部署跑一次)

```python
import asyncio

from nicekit.core.db import get_session_factory
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.core.logging import setup_logging
from nicekit.runtime.bootstrap import bootstrap_platform, install_default_ports

use_selector_event_loop_on_windows()


async def seed() -> None:
    install_default_ports()
    async with get_session_factory()() as session:
        report = await bootstrap_platform(session)   # 实体类型 / KB prompt / 默认 agent 卡
        print(report.as_dict())


if __name__ == "__main__":
    setup_logging()
    asyncio.run(seed())
```

**`run.py`**

```python
"""开发启动入口:uv run python run.py —— Windows 上必须走它,原因见 §6.5。"""

import asyncio
import os
import sys

import uvicorn
from nicekit.core.logging import setup_logging


def main() -> None:
    setup_logging()
    config = uvicorn.Config(
        "myhost.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        log_config=None,
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
```

**跑起来**

```bash
docker compose up -d                                               # postgres 5433 / redis 6380 / minio 9010
cd packages/nicekit && uv run --package nicekit alembic upgrade head && cd -
uv run python -m myhost.seed
uv run python run.py
curl -s localhost:8000/api/v1/health                               # {"status":"ok"}
curl -s -H "authorization: Bearer <宿主 token>" localhost:8000/api/v1/kb/bases
```

### 3.2 外部主键怎么映射

`nicekit.tenancy.mapping` 用 uuid5 做**确定性派生**:同样的输入永远得到同样的 UUID,
所以宿主不需要额外维护一张映射表,每次请求现算即可。

```python
from nicekit.tenancy.mapping import subject_uuid, tenant_uuid

tenant_uuid(42)        # int
tenant_uuid("42")      # 与上一行结果相同 —— 刻意的:换个类型传参不该换一个租户分区
tenant_uuid("acme")    # slug
tenant_uuid(1234567890123456789)   # 雪花号
subject_uuid("u_8891")
```

- 租户和主体各有独立命名空间,`tenant_uuid("1") != subject_uuid("1")`。
- 多套外部系统接进同一个平台时再隔离一层:`tenant_uuid(cid, namespace=uuid5(NAMESPACE_URL, "https://crm.acme.com"))`。
- `None`、空串、纯空白一律 `ValueError` —— 它们恰恰是"这个请求没带租户"的信号。
  "没有租户"必须显式走单租户模式,不能悄悄落到一个派生出来的兜底分区。
  (`None` 尤其危险:`str(None) == "None"` 非空,若不显式拦截就会让所有缺租户的请求
  静默共用同一个看着完全正常、却把不同客户数据混在一起的分区键。SDK 已显式拒绝。)
- **仍然建议在 resolver 里自己先判空**:让它变成一个干净的 401/403,而不是 `ValueError`
  冒到最外层变成 500。
- **映射是单向的**:拿到 UUID 反推不出原值。要在界面上显示外部 ID,宿主自己在业务侧留对照。

### 3.3 什么时候调 `ensure_org` / `ensure_user`

**规则:任何一个 `(org_id, user_id)` 组合在第一次可能触碰 agent 权限三表之前,必须已经垫好行。**

两种做法,优先第一种:

1. **在宿主的租户 / 成员生命周期里调**(推荐)。你的系统"创建公司"、"邀请成员"的那段代码里
   顺手加一次 `provision(...)`。语义最准、请求路径零开销、失败点在管理动作里而不是用户请求里。
2. **在 resolver 里惰性垫 + 进程内缓存**(上面示例的做法)。适合租户来源不在你手上(SSO、
   上游系统同步)的场景。注意:缓存是**进程内**的,多副本各自会垫一次 —— 无所谓,
   `ensure_org` 走 `ON CONFLICT DO NOTHING`,`ensure_user` 同理,并发首见同一租户不会互相炸。

漏掉的后果是明确的:写 agent 权限表时 `ForeignKeyViolation`,接口 500。典型触发路径是
`PUT /api/v1/agent/permissions/preferences`、chat 里第一次授权一个工具、组织级审批策略保存。
KB、检索、chat 本身不会触发,所以这个坑往往在联调后期才炸出来。

`ensure_org` 的两个细节:

- **不 commit**,由调用方的事务决定何时落盘;
- 已存在时**不覆盖** `name` / `slug`。宿主那边的租户改名不该由 SDK 这条兜底路径静默同步。
- 不传 `slug` 会用 `org-<uuid前12位>` 占位。若你要用 **KB 跨组织分享**
  (`POST /api/v1/kb/bases/{kb_id}/shares` 的 body 是 `{"grantee_org_slug": ...}`),
  请传宿主真实的 slug,否则运营根本认不出分享给了谁。

### 3.4 角色映射

```python
register_roles("editor", "auditor")     # 装配期,幂等
register_write_roles("editor")          # 让 editor 能写 KB
```

- 角色名规则:小写字母开头,`[a-z][a-z0-9_]{0,31}`(落库 `varchar(32)`)。
- `register_roles("platform_admin")` 会直接报错 —— 平台保留角色不可注册/授予。
- `register_write_roles` 只接受已登记(或内置)的角色名,拼错立即 `ValueError`,
  不会静默放行。
- 写守卫 `require_write_role()` 在**请求期**读注册表,所以装配顺序无关;
  但 `require_role(...)` 在 import 期定格允许集合,那是各 router 自己写死的,你改不了。
- **`/api/v1/admin/*` 整个 router 要求 `platform_admin`。** 桥接模式下这是唯一能拿到平台
  管理面的方式:在 `ROLE_MAP` 里把你自己的超管映射成字面量 `"platform_admin"`。
  它不在 `register_roles` 的合法域内,所以只能硬写这个字符串。

### 3.5 哪些 router 不该挂

```python
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers, router_names

router_names()
# ('health', 'auth', 'kb_status', 'kb', 'kb_feedback', 'kb_integrity',
#  'kb_lifecycle_metrics', 'kb_entity_types', 'kb_media', 'members', 'admin',
#  'admin_mcp', 'agent_permissions', 'chat', 'memory', 'icron', 'notifications', 'media')

AUTH_ROUTER_NAMES        # ('auth', 'members')
default_routers(exclude=AUTH_ROUTER_NAMES)     # 18 → 16 个 router,182 → 173 条端点
```

- **必须排掉 `auth` 和 `members`**:前者发 token,后者建人与授角色,都是"身份来源"。
  留着就有两套主体,而请求走哪套取决于客户端带了什么头。`create_app` 的启动自检会直接
  `RuntimeError`(见 §6.2)。
- `exclude` 里写错名字会立刻 `ValueError` 并列出合法取值 —— 拼错若被静默忽略,你会以为
  身份路由已经摘掉、实际还开着。
- **想摘管理面就得摘两个**:`admin` 和 `admin_mcp` 是两个 router,只排 `admin` 会留下
  `/api/v1/admin/mcp-servers*` 五条端点。
- 其余 router 都能留:它们只经 `get_org_context` 拿身份,不关心身份从哪来。

### 3.6 前端怎么办

SDK 的 auth API 不用了,前端改动是**只改一层**的:

| 原来(全托管) | 桥接后 |
|---|---|
| `POST /api/v1/auth/login` 拿 access + refresh | 用宿主自己的登录页/SSO |
| localStorage 存 SDK token,401 时打 `/auth/refresh` 静默续期 | 从宿主会话拿 token;401 走宿主的续期/跳登录 |
| `org` 信息来自登录响应 | 来自宿主的用户信息接口 |
| 请求头 `Authorization: Bearer <SDK JWT>` | 请求头由你的 resolver 认识即可(可以还是 `Authorization: Bearer`,也可以是自定义头) |

`apps/demo/frontend/src/lib/api.ts` 是一个可裁剪的参考实现:把 `doRefresh()` 和
`redirectToLogin()` 换成宿主的即可,其余 API 调用一行都不用动(路径与响应结构没变)。

跨域时记得把宿主前端域名写进 `CORS_ORIGINS`,并确认你的自定义头在
`allow_headers`(`create_app` 默认 `["*"]`,一般不用管)。

---

## 4. 模式 C:单租户

宿主没有租户概念(内部工具、单公司自用),或者"认证已经由网关做完了"。

```python
# src/myhost/main.py
from fastapi import FastAPI
from nicekit.api.deps import set_principal_resolver, single_tenant_resolver
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports


def build_app() -> FastAPI:
    install_default_ports()
    set_principal_resolver(single_tenant_resolver())
    return create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES), title="Internal Tool")


app = build_app()
```

```python
# src/myhost/seed.py —— 注意 single_tenant=True
import asyncio

from nicekit.core.db import get_session_factory
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.runtime.bootstrap import bootstrap_platform, install_default_ports

use_selector_event_loop_on_windows()


async def seed() -> None:
    install_default_ports()
    async with get_session_factory()() as session:
        report = await bootstrap_platform(session, single_tenant=True)
        print(report.as_dict())
        # {"single_tenant_org": "73978095-…", "single_tenant_subject": "e81471a3-…", …}
        # 两行都垫了:agent 权限三表对 organizations 与 users 都有外键。
        # 想自己拿这个操作者 id:nicekit.api.deps.single_tenant_subject_id()


if __name__ == "__main__":
    asyncio.run(seed())
```

其余(pyproject / .env / run.py)与模式 B 相同。

### 4.1 默认分区键不是 `platform_org_id`

```python
from nicekit.api.deps import SINGLE_TENANT_ORG_ID
# 73978095-c3be-508a-88b0-c79c032526f5(= derive_uuid("single_tenant", namespace=NAMESPACE_RESERVED),固定值)
```

`platform_org_id`(`00000000-...-0001`)的语义是**"这份数据对所有组织可见"**:
`chat_events` / `llm_traces` / `usage_daily` 三张表有 `platform_read` 策略允许它读全租户行,
`kb/search.py` 会把平台 org 的 KB 判定为公共层,`kb/image_assets.py` 的可见性过滤同理。
单租户期间两者行为无差别,但**哪天要接入第二个租户,原先放在平台 org 下的全部知识会立刻
对新租户可见** —— 一次静默的数据泄漏。所以用一个独立的普通 org,升级多租户时什么都不用改。

迁移只 seed 了 `platform_org_id` 这一行,`SINGLE_TENANT_ORG_ID` 需要你自己垫:
`bootstrap_platform(session, single_tenant=True)` 会顺手垫掉(name/slug = `Single Tenant` /
`single-tenant`),也可以自己调 `ensure_org(session, SINGLE_TENANT_ORG_ID)`。
不垫的后果同 §3.3:agent 权限表写入外键失败。

### 4.2 网关已经认证过了:把人带进来

`single_tenant_resolver` 默认把所有请求视为**同一个主体**,审计与长期记忆就分不清人了。
网关把已认证用户放在请求头里时:

```python
from uuid import UUID
from fastapi import Request
from nicekit.api.deps import single_tenant_resolver
from nicekit.tenancy.mapping import subject_uuid

def _subject(request: Request) -> UUID:
    return subject_uuid(request.headers.get("x-forwarded-user") or "anonymous")

def _role(request: Request) -> str:
    return "org_admin" if request.headers.get("x-forwarded-groups") == "admins" else "member"

set_principal_resolver(single_tenant_resolver(subject_of=_subject, role_of=_role))
```

完整签名:

```python
def single_tenant_resolver(
    *,
    org_id: UUID | None = None,          # 默认 SINGLE_TENANT_ORG_ID
    role: str = "org_admin",             # 不传 role_of 时所有请求的角色
    subject_id: UUID | None = None,      # 不传 subject_of 时的固定主体
    role_of: Callable[[Request], str] | None = None,
    subject_of: Callable[[Request], UUID] | None = None,
) -> PrincipalResolver: ...
```

> ⚠️ **它不做任何身份校验。** 必须确保服务不直接暴露在公网,或前置一层完成认证的网关 ——
> 否则等于把 `/api/v1/admin/*` 平台管理端敞开(默认 role 是 `org_admin`,进不了 admin;
> 但 KB、chat、记忆全在)。

---

## 5. RLS:三种模式都别关

46 张表开了 `ENABLE` + **`FORCE`** ROW LEVEL SECURITY,策略是:

```sql
org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
```

`FORCE` 意味着连表 owner 也受约束;应用账号与迁移账号**都没有 BYPASSRLS**。
未设置上下文时策略恒假 —— fail-closed,什么都看不到、什么都写不进。

**单租户模式下也不要关**,理由三条:

1. 它是"代码漏写 `where org_id = ...`"时的最后一道闸。SDK 有约 200 个端点、数十个后台
   扫描器,谁都可能漏;RLS 是唯一不依赖 code review 的兜底。
2. 关掉 RLS 需要改迁移(`ALTER TABLE ... DISABLE ROW LEVEL SECURITY`),那是一条**不可逆的
   分叉**:以后想接入第二个租户就得逐表补回来,而此时库里已经有一批没有正确 `org_id` 的行。
3. 性能理由不成立:策略是单列等值比较,且所有 `org_id` 列都有索引。

代价只有一条:**你自己新增的业务表如果带 `org_id`,也要开 RLS**,用 SDK 的 helper:

```python
from nicekit.migrations.rls import op_enable_org_rls

def upgrade():
    op.create_table("tickets", ...)
    op_enable_org_rls(op, "tickets")   # ENABLE + FORCE + org_isolation 策略
```

不走请求链路的地方(worker、脚本、定时任务)用 `org_session` 拿绑好上下文的会话:

```python
from nicekit.core.db import get_session_factory, org_session

session = org_session(get_session_factory(), org_id)   # 调用方负责 close
```

---

## 6. 常见坑

### 6.1 忘了 `ensure_org` / `ensure_user`

症状:KB、chat 一切正常,直到某个用户改 agent 权限偏好或第一次授权工具 → 500,
日志里 `ForeignKeyViolation: violates foreign key constraint
"agent_permission_preferences_org_id_fkey"`(或 `..._user_id_fkey`)。
解法见 §3.3。**注意只垫 org 只解决一半** —— `user_id` 那半用 `ensure_user`,
照抄 §3.1 的 `ensure_user`。

### 6.2 两套身份并存

装配时注册了 resolver,却还挂着 `auth` / `members`。`create_app` 会直接抛:

```
RuntimeError: 身份接线自相矛盾:已用 set_principal_resolver() 接管身份,
却仍挂载了 SDK 的身份路由 ['auth', 'members']。...
```

**但自检有个盲区**:它在 `create_app` 执行时读全局状态。如果你先 `create_app(...)`
再 `set_principal_resolver(...)`,自检看到的还是内置 JWT,静默放行,运行期却是桥接的 ——
两套身份真的并存了。所以**永远把 `set_principal_resolver` 写在 `create_app` 之前**。
确有需要可以 `create_app(check_auth_wiring=False)` 关掉自检,但那应该是极少数情况。

### 6.3 resolver 里做重活

resolver 跑在**每一个请求**上。里面查库、调认证服务、验远端 JWKS,就是给全站每个请求
加一次往返。规则:

- 本地验签(把公钥/密钥在启动期加载好);
- 需要查库的信息(角色、租户名)放进 token 或进程内缓存;
- 影子行垫入用 §3.1 的 `_provisioned` 集合短路;
- **不要在 resolver 里 `session.commit()` 宿主的业务事务** —— resolver 早于
  `get_org_session` 执行,此时请求事务还没开始,你 commit 的是别人的会话。
  真要写(比如 `provision`)就自己开一个独立的短事务,像示例那样。

### 6.4 少写 `/v1`

LLM provider 的 `base_url` 必须带版本段(`https://api.siliconflow.cn/v1`)。
SDK 把它当完整前缀直接拼 `/models`、`/embeddings`,少写 `/v1` 会得到一个没有上下文的 404;
连通性测试会附 `hint=base_url_missing_version_path`。

同理,SDK 的 REST 前缀是 `/api/v1`(`create_app(router_prefix=...)` 可改,但前端与文档
全按默认值写)。调 `/api/kb/bases` 会 404 —— 不是权限问题。

### 6.5 Windows 必须走 `run.py`

uvicorn 在 win32 上**硬编码** `ProactorEventLoop` 作为 loop factory,会盖掉进程里已经设好的
`WindowsSelectorEventLoopPolicy`;而 psycopg 的 async 实现不支持 Proactor。后果:
HTTP 照常响应(`/health` 200),但 lifespan 里所有数据库后台任务与所有走 DB 的请求**全部静默失败**。

- 服务:`uv run python run.py`(用 `asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)`)。
- 脚本 / seed / 自检:文件顶部先 `use_selector_event_loop_on_windows()`。
- Linux 生产:`uvicorn myhost.main:app` 即可,上述都是 no-op。

### 6.6 其他

- **`build_app()` 别调两次**。宿主模块通常在导入时就 `app = build_app()`,再调一次会把
  自定义工具重复并入全局注册表,报 `ToolRegistrationError: duplicate tool names`。
  脚本里要用 app 就 `from myhost.main import app`。
- **数路由别看 `app.routes`**。FastAPI 0.141 起 `include_router` 不再展开成 `APIRoute`
  (`app.routes` 里是 `_IncludedRouter`),数出来是 18 而不是 182。用 `app.openapi()["paths"]`。
- **resolver 是进程级全局**。测试里换过就要在 teardown 里 `set_principal_resolver(None)` 还原,
  否则污染后续用例。同一进程里也没法给两个 app 配不同的 resolver。
- **`role` 拼错不会报错**,只会让该用户在所有端点上 403。角色映射表建议加一条单测:
  `set(ROLE_MAP.values()) <= set(all_roles())`。
- **`platform_org_id` 不要拿来存业务数据**(理由见 §4.1)。

---

## 7. 迁移、回滚与多环境

### 7.1 迁移

```bash
cd packages/nicekit
uv run --package nicekit alembic upgrade head     # 用 MIGRATION_DATABASE_URL(migrator 账号)
uv run --package nicekit alembic current
```

- 迁移账号与应用账号分离,**两个都不给 BYPASSRLS**。
- 你自己的业务表建议走独立 alembic 分支,或在 `env.py` 里合并两份 metadata;
  凡带 `org_id` 的表都要 `op_enable_org_rls`。
- 多会话/多分支并行开发后,合并完先查 `alembic heads` 是不是单头。

### 7.2 三种模式之间切换

| 从 → 到 | 数据要动吗 | 怎么做 |
|---|---|---|
| A → B | 不用 | 装配处加 resolver + `exclude=AUTH_ROUTER_NAMES`。老的 `users`/`memberships` 行留着无害,只是不再被读 |
| B → A | 要 | 影子 `users` 行的 `password_hash` 是 `!external`,没人能登录;需要给每个用户走一次设密码/邀请流程 |
| C → B | 看情况 | `SINGLE_TENANT_ORG_ID` 就是一个普通 org,直接变成第一个租户即可 —— 这正是 §4.1 不用 `platform_org_id` 换来的 |
| C → A | 要 | 同 B → A,再补 `memberships` |

**回滚**:模式切换本身不改 schema,回滚 = 把装配代码改回去重新部署。真正不可逆的只有两件事:
关掉 RLS,和把单租户数据放进 `platform_org_id`。两个都别做。

### 7.3 多环境

- `Settings` 全部从环境变量注入(字段名大写),`.env` 只作开发用。
- 每个环境用**独立数据库**,不要靠 `org_id` 隔离 dev/staging/prod —— `tenant_uuid` 是确定性的,
  同一个外部租户 ID 在两个环境派生出同一个 UUID,连错库时不会有任何报错。
- `NICEKIT_SECRET_KEY` 一旦配置就不能丢:已加密的第三方密钥遇到缺失 key 会 fail-closed 抛错,
  绝不当明文用。各环境用不同的 key,并且备份。
- 生产 `TASK_DISPATCH_MODE=celery` 时要另起 worker 与 beat;`inline` 模式的常驻循环
  只在 API 进程内兜底(见 `default_background_loops`)。

---

## 8. 上线前自检

`docs/AGENT-BRIEF.md` §5 有一段可直接运行的自检脚本,校验四件事:

1. resolver 是否真的接管(`uses_builtin_auth()`);
2. SDK 身份路由是否已摘干净;
3. `organizations` 里有没有你的 org 行;
4. RLS 是否 fail-closed、是否跨 org 过滤。

手工版三条命令:

```bash
curl -s localhost:8000/api/v1/health                                    # {"status":"ok"}
curl -s localhost:8000/api/v1/kb/bases                                  # 401(桥接)/ 200(单租户)
curl -s localhost:8000/openapi.json | python -c "import sys,json;p=json.load(sys.stdin)['paths'];print(len(p));print([k for k in p if '/auth/' in k or '/org/members' in k])"
# 桥接/单租户:173 与 [];全托管:182 与 9 条身份路由
```
