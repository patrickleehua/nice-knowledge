"""v1 router 聚合器(MIGRATION-PLAN §5.7)。

``api_router`` 是 SDK 全量 v1 路由的单一挂载点;宿主既可以直接用它,也可以
调 :func:`default_routers` 取到逐个 router 后自行取舍/追加业务 router,再交给
``runtime.app_factory.create_app(routers=...)``。

顺序有意义:``kb_status`` 必须在 ``kb`` 之前挂载(``/kb/status`` 这类固定段
路径要先于 ``/kb/{kb_id}`` 的路径参数匹配)。

**每个 router 带一个稳定 name**,宿主按名字取舍(桥接模式要排掉身份路由):

```python
create_app(routers=default_routers(exclude=("auth", "members")))
```

name 与 OpenAPI tag 分开,是因为 tag 会重复(5 个 KB 子 router 共用 ``kb``
tag),按 tag 排除等于一刀切掉整个知识库面;name 唯一,粒度才对得上"排掉某个
router"这件事。名字一经导出即视为宿主可见契约,改名等于破坏兼容。

注意管理面是**两个** router(``admin`` 与 ``admin_mcp``),排除时要一起写,
只排 ``admin`` 会留下 ``/admin/mcp-servers``;:func:`router_names` 里能看到全部。
"""

from collections.abc import Collection, Iterable

from fastapi import APIRouter

from nicekit.api.v1 import (
    admin,
    agent_permissions,
    auth,
    chat,
    health,
    icron,
    kb,
    kb_entity_types,
    kb_feedback,
    kb_integrity,
    kb_lifecycle_metrics,
    kb_media,
    kb_status,
    media,
    members,
    memory,
    notifications,
)

#: (name, router, OpenAPI tag) —— 挂载顺序即元组顺序
_DEFAULT_ROUTERS: tuple[tuple[str, APIRouter, str], ...] = (
    ("health", health.router, "health"),
    ("auth", auth.router, "auth"),
    ("kb_status", kb_status.router, "kb"),
    ("kb", kb.router, "kb"),
    ("kb_feedback", kb_feedback.router, "kb"),
    ("kb_integrity", kb_integrity.router, "kb"),
    ("kb_lifecycle_metrics", kb_lifecycle_metrics.router, "kb"),
    ("kb_entity_types", kb_entity_types.router, "kb-entity-types"),
    ("kb_media", kb_media.router, "kb-media"),
    ("members", members.router, "members"),
    ("admin", admin.router, "admin"),
    ("admin_mcp", admin.mcp_router, "admin"),
    ("agent_permissions", agent_permissions.router, "agent-permissions"),
    ("chat", chat.router, "chat"),
    ("memory", memory.router, "memory"),
    ("icron", icron.router, "icron"),
    ("notifications", notifications.router, "notifications"),
    ("media", media.router, "media"),
)

#: SDK 自带的身份路由:auth 发 token、members 建人与授角色,两者都是"身份来源"。
#: 宿主用 ``deps.set_principal_resolver()`` 接管身份后必须一并排除,
#: ``runtime.app_factory`` 的启动自检据此判定接线是否自相矛盾。
AUTH_ROUTER_NAMES: tuple[str, ...] = ("auth", "members")


def router_names() -> tuple[str, ...]:
    """全部 router 的名字(挂载顺序),即 ``exclude`` 的合法取值域。"""
    return tuple(name for name, _, _ in _DEFAULT_ROUTERS)


#: 排除时的展开组:宿主心智里的"一个面"在实现上可能是多个 router。
#: 排 ``admin`` 就该把整个平台管理面摘干净,不该留下 ``/admin/mcp-servers``
#: 这类同样是 platform_admin 专属的配置端点 —— "以为关了其实开着"是这里最
#: 危险的失败模式。要单独保留某一个,直接列举细粒度名字即可。
_ROUTER_GROUPS: dict[str, tuple[str, ...]] = {
    "admin": ("admin", "admin_mcp"),
}


def _expand_exclude(exclude: Collection[str]) -> set[str]:
    expanded: set[str] = set()
    for name in exclude:
        expanded.update(_ROUTER_GROUPS.get(name, (name,)))
    return expanded


def default_routers(exclude: Collection[str] = ()) -> tuple[APIRouter, ...]:
    """SDK 自带的 v1 routers(挂载顺序即返回顺序),可按名字排除。

    不认识的名字直接报错:排除清单最常见的用途就是摘掉 auth,而拼错名字若被
    静默忽略,宿主会以为身份路由已经摘掉、实际还开着 —— 这类"以为关了其实开着"
    必须在装配期炸,不能留到线上。

    ``exclude={"admin"}`` 会连同 ``admin_mcp`` 一起摘掉(见 ``_ROUTER_GROUPS``)。
    """
    unknown = sorted(set(exclude) - set(router_names()) - set(_ROUTER_GROUPS))
    if unknown:
        raise ValueError(f"未知的 router 名字 {unknown};可选:{list(router_names())}")
    dropped = _expand_exclude(exclude)
    return tuple(router for name, router, _ in _DEFAULT_ROUTERS if name not in dropped)


def mounted_auth_router_names(routers: Iterable[APIRouter]) -> tuple[str, ...]:
    """挑出给定 routers 里属于 SDK 身份路由的名字(启动自检用)。

    按对象身份比对而不是路径前缀:宿主完全可以自己写一个 ``/auth`` 前缀的
    router 接自家登录,那正是桥接模式的正常形态,不该被自检误伤。
    """
    mounted = list(routers)
    return tuple(
        name
        for name, router, _ in _DEFAULT_ROUTERS
        if name in AUTH_ROUTER_NAMES and any(router is candidate for candidate in mounted)
    )


api_router = APIRouter()
for _name, _router, _tag in _DEFAULT_ROUTERS:
    api_router.include_router(_router, tags=[_tag])


__all__ = [
    "AUTH_ROUTER_NAMES",
    "api_router",
    "default_routers",
    "mounted_auth_router_names",
    "router_names",
]
