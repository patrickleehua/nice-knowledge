"""默认 agent 卡 seed(§5.4)与 admin agent 域接口的专测。

seed 的 DB 落库路径要真库才能验(留给装配期 e2e),这里钉死它的**内容契约**:
中性角色段、17 个通用工具、`agent.default` 任务名——旅游 SOP 一个字都不许回来。
"""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from nicekit.agent import seed as seed_module
from nicekit.agent.builtin_tools import BUILTIN_TOOL_NAMES
from nicekit.agent.seed import (
    DEFAULT_AGENT_TASK,
    DEFAULT_CARD_SEED_VERSION,
    default_card_role_prompt,
    default_card_tools,
)
from nicekit.agent.service import default_card_name
from nicekit.api import deps
from nicekit.api.v1 import admin as admin_api

ORG_ID = uuid4()
USER_ID = uuid4()


# ---------------------------------------------------------------------------
# seed 内容契约
# ---------------------------------------------------------------------------


def test_default_card_enables_every_builtin_tool() -> None:
    tools = default_card_tools()
    assert tools == list(BUILTIN_TOOL_NAMES)
    assert len(tools) == 17


def test_default_card_role_prompt_comes_from_agent_role_generic() -> None:
    prompt = default_card_role_prompt()
    assert prompt
    # 标题行由 agent_role_custom 包装时给出,卡上只放职责段(否则组装后双标题)
    assert not prompt.startswith("【")
    assert "通用助手" in prompt


@pytest.mark.parametrize(
    "word",
    ("旅游", "旅行社", "行程", "报价", "比价", "project", "customer", "workbench"),
)
def test_seed_has_no_business_residue(word: str) -> None:
    source = Path(seed_module.__file__).read_text(encoding="utf-8")
    # workbench 只允许出现在"TF 不搬什么"的说明里,不允许成为默认卡名
    assert default_card_name() != "workbench"
    if word == "workbench":
        return
    assert word not in default_card_role_prompt()
    assert word not in source.split('"""', 2)[-1]


def test_seed_task_matches_compression_and_memory_defaults() -> None:
    """默认卡的 task 与摘要/记忆抽取默认 task 同名:宿主配一条路由即可全跑通。"""
    from nicekit.agent.compression import DEFAULT_SUMMARY_TASK
    from nicekit.agent.memory import DEFAULT_MEMORY_TASK

    assert DEFAULT_AGENT_TASK == DEFAULT_SUMMARY_TASK == DEFAULT_MEMORY_TASK
    assert DEFAULT_CARD_SEED_VERSION >= 1


# ---------------------------------------------------------------------------
# admin agent 域接口
# ---------------------------------------------------------------------------


class _FakeSession:
    async def execute(self, _statement):
        raise AssertionError("本用例不该查库")

    def add(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        return None


def _client(router, *, role: str = "platform_admin"):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    ctx = deps.OrgContext(user_id=USER_ID, org_id=ORG_ID, role=role)
    app.dependency_overrides[deps.get_org_context] = lambda: ctx
    app.dependency_overrides[deps.get_org_session] = lambda: _FakeSession()
    app.dependency_overrides[deps.get_session] = lambda: _FakeSession()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_prompt_resources_listing_exposes_origin_and_variables() -> None:
    async with _client(admin_api.router) as client:
        response = await client.get("/api/v1/admin/prompt-resources")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()}
    assert set(items) >= {"global_intro", "agent_role_generic", "memory_extraction"}
    assert items["global_intro"]["variables"] == ["product_identity"]
    assert items["global_intro"]["origin"]
    assert items["global_intro"]["content_chars"] > 0
    # 列表不带全文,避免一次拉回几万字符
    assert "content" not in items["global_intro"]


async def test_prompt_resource_detail_and_404() -> None:
    async with _client(admin_api.router) as client:
        ok = await client.get("/api/v1/admin/prompt-resources/global_tool_usage")
        missing = await client.get("/api/v1/admin/prompt-resources/nope")
    assert ok.status_code == 200
    assert "【工具纪律】" in ok.json()["content"]
    assert missing.status_code == 404


async def test_prompt_resource_preview_composes_blocks() -> None:
    async with _client(admin_api.router) as client:
        response = await client.post(
            "/api/v1/admin/prompt-resources/preview",
            json={
                "blocks": [
                    {"id": "global_intro", "values": {"product_identity": "你是工单助手"}},
                    {"id": "agent_role_generic"},
                ]
            },
        )
    assert response.status_code == 200
    text = response.json()["text"]
    assert text.startswith("你是工单助手")
    assert "【当前 Agent 角色】" in text


async def test_prompt_assembly_preview_uses_the_runtime_assembler(monkeypatch) -> None:
    from nicekit.agent.service import ResolvedAgentCard

    card = ResolvedAgentCard(
        card_id=uuid4(),
        version_id=uuid4(),
        name="assistant",
        system_prompt="只回答工单问题",
        model_task=DEFAULT_AGENT_TASK,
        max_turns=8,
        timeout_seconds=300,
        tools=(),
        mcp_bindings=(),
        skills=(),
    )

    async def _resolve_card(_session, _org_id, _card_id):
        return card

    monkeypatch.setattr(admin_api, "resolve_card", _resolve_card)
    async with _client(admin_api.router) as client:
        response = await client.get(
            "/api/v1/admin/prompt-assembly", params={"card_id": str(card.card_id)}
        )
    assert response.status_code == 200
    body = response.json()
    assert sum(block["chars"] for block in body["blocks"]) == body["system_prompt_chars"]
    assert body["blocks"][0]["id"] == "global_intro"
    assert body["blocks"][-1]["id"] == "agent_role_custom"
    assert "只回答工单问题" in body["text"]
    # 运行时才有的动态段必须明示,免得预览被误当成完整 prompt
    runtime_ids = {item["id"] for item in body["runtime_only_blocks"]}
    assert {"context_providers", "memory_recall", "tool_trace"} <= runtime_ids


async def test_agent_tools_endpoint_lists_registry() -> None:
    import nicekit.agent.builtin_tools  # noqa: F401 - A2:显式 import 才注册

    async with _client(admin_api.router) as client:
        response = await client.get("/api/v1/admin/agent-tools")
    assert response.status_code == 200
    items = {item["name"]: item for item in response.json()}
    assert "memory_write" in items
    assert items["memory_write"]["side_effect"] == "write"
    assert items["memory_write"]["label"]


async def test_admin_router_rejects_non_platform_admin() -> None:
    async with _client(admin_api.router, role="org_admin") as client:
        response = await client.get("/api/v1/admin/agent-tools")
    assert response.status_code == 403


async def test_mcp_router_allows_org_admin() -> None:
    """MCP 子路由刻意放宽到 org_admin(与 TF 同口径);stdio 传输另有平台闸。"""
    ctx = deps.OrgContext(user_id=USER_ID, org_id=ORG_ID, role="org_admin")
    assert admin_api.mcp_router.dependencies  # 角色守卫挂在路由级
    with pytest.raises(HTTPException):
        admin_api._require_platform_for_stdio(ctx, "stdio")
    # 非 stdio 传输 org_admin 可配
    admin_api._require_platform_for_stdio(ctx, "streamable_http")
