"""v1 router 聚合器(MIGRATION-PLAN §5.7)。

``api_router`` 是 SDK 全量 v1 路由的单一挂载点;宿主既可以直接用它,也可以
调 :func:`default_routers` 取到逐个 router 后自行取舍/追加业务 router,再交给
``runtime.app_factory.create_app(routers=...)``。

顺序有意义:``kb_status`` 必须在 ``kb`` 之前挂载(``/kb/status`` 这类固定段
路径要先于 ``/kb/{kb_id}`` 的路径参数匹配)。
"""

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

#: (router, OpenAPI tag) —— 挂载顺序即元组顺序
_DEFAULT_ROUTERS: tuple[tuple[APIRouter, str], ...] = (
    (health.router, "health"),
    (auth.router, "auth"),
    (kb_status.router, "kb"),
    (kb.router, "kb"),
    (kb_feedback.router, "kb"),
    (kb_integrity.router, "kb"),
    (kb_lifecycle_metrics.router, "kb"),
    (kb_entity_types.router, "kb-entity-types"),
    (kb_media.router, "kb-media"),
    (members.router, "members"),
    (admin.router, "admin"),
    (admin.mcp_router, "admin"),
    (agent_permissions.router, "agent-permissions"),
    (chat.router, "chat"),
    (memory.router, "memory"),
    (icron.router, "icron"),
    (notifications.router, "notifications"),
    (media.router, "media"),
)


def default_routers() -> tuple[APIRouter, ...]:
    """SDK 自带的全部 v1 routers(挂载顺序即返回顺序)。"""
    return tuple(router for router, _ in _DEFAULT_ROUTERS)


api_router = APIRouter()
for _router, _tag in _DEFAULT_ROUTERS:
    api_router.include_router(_router, tags=[_tag])


__all__ = ["api_router", "default_routers"]
