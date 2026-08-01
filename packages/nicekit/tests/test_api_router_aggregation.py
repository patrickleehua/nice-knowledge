"""api/v1/router.py:聚合器契约(§5.7)。

只做静态断言(不起 lifespan、不碰数据库):路由集合、A12 补齐的 admin 端点是否
在册、以及"SDK 内不该出现业务端点"这条规约(§7 最后一条)。
"""

import re

import pytest
from fastapi import FastAPI

from nicekit.api.v1.router import api_router, default_routers


@pytest.fixture(scope="module")
def paths() -> set[str]:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    return set(app.openapi()["paths"])


def test_default_routers_are_all_mounted(paths: set[str]) -> None:
    assert len(default_routers()) == 18
    assert "/api/v1/health" in paths
    assert "/api/v1/ready" in paths


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/login",
        "/api/v1/org/members",
        "/api/v1/chat/sessions",
        "/api/v1/memory",
        "/api/v1/icron/tasks",
        "/api/v1/notifications",
        "/api/v1/kb/bases",
        "/api/v1/kb/entity-types",
        "/api/v1/genimg/{filename}",
    ],
)
def test_core_surface_present(paths: set[str], path: str) -> None:
    assert path in paths


@pytest.mark.parametrize(
    "path",
    [
        # A12 本波补齐的管理面
        "/api/v1/admin/orgs",
        "/api/v1/admin/models",
        "/api/v1/admin/models/batch",
        "/api/v1/admin/model-route-tasks",
        "/api/v1/admin/model-catalog",
        "/api/v1/admin/providers",
        "/api/v1/admin/service-configs",
        "/api/v1/admin/usage",
        "/api/v1/admin/billing",
        "/api/v1/admin/model-prices",
        "/api/v1/admin/llm-traces",
        "/api/v1/admin/operations/diagnostics",
        "/api/v1/admin/operations/probe",
        # P2c 已有,确认没被覆盖掉
        "/api/v1/admin/agent-cards",
        "/api/v1/admin/mcp-servers",
    ],
)
def test_admin_surface_complete(paths: set[str], path: str) -> None:
    assert path in paths


def test_no_business_endpoints_leaked(paths: set[str]) -> None:
    """§7:SDK 内出现 project/customer/itinerary/quote/ota/render 即视为未完成。"""
    # 只看顶层资源段:``/kb/bases/{id}/shares`` 是 KB 跨组织授权,不是旅游分享链接
    forbidden = re.compile(
        r"^/api/v1/(projects|customers|itineraries|quotes|ota|renders|render-kinds"
        r"|templates|shares|public-shares|route-assets)\b"
    )
    assert [path for path in sorted(paths) if forbidden.search(path)] == []


def test_service_config_names_drop_ota() -> None:
    from nicekit.api.v1.admin import SERVICE_CONFIG_NAMES

    assert SERVICE_CONFIG_NAMES == ("websearch", "imagegen", "weather")


def test_kb_status_router_precedes_kb_router() -> None:
    """/kb/bases/status-board 这类固定段路径必须先于 /kb/bases/{kb_id} 匹配。"""
    from nicekit.api.v1 import kb, kb_status

    routers = default_routers()
    assert routers.index(kb_status.router) < routers.index(kb.router)
