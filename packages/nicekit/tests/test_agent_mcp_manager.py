import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.types import CallToolResult, TextContent

from nicekit.agent.mcp_manager import MCPClientManager, McpConnectionError
from nicekit.models.mcp import McpServer


def _server() -> McpServer:
    return McpServer(
        id=uuid4(),
        name="demo",
        transport="streamable_http",
        endpoint_url="http://localhost/mcp",
    )


async def test_list_tools_is_cached(monkeypatch) -> None:
    manager = MCPClientManager(tools_ttl_seconds=300)
    calls = 0

    class FakeSession:
        async def list_tools(self):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        title=None,
                        description="Search",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    @asynccontextmanager
    async def fake_session(server):
        yield FakeSession()

    monkeypatch.setattr(manager, "_session", fake_session)
    first = await manager.list_tools(_server())
    second = await manager.list_tools(_server())
    # Different ids are intentionally not shared.
    assert calls == 2
    assert first[0].name == second[0].name == "search"

    server = _server()
    await manager.list_tools(server)
    await manager.list_tools(server)
    assert calls == 3


async def test_call_tool_serializes_sdk_result(monkeypatch) -> None:
    manager = MCPClientManager()

    class FakeSession:
        async def call_tool(self, name, arguments, read_timeout_seconds):
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    @asynccontextmanager
    async def fake_session(server):
        yield FakeSession()

    monkeypatch.setattr(manager, "_session", fake_session)
    output = await manager.call_tool(_server(), "search", {"q": "Paris"})
    assert '"text": "ok"' in output


async def test_call_tool_timeout(monkeypatch) -> None:
    manager = MCPClientManager()

    class SlowSession:
        async def call_tool(self, name, arguments, read_timeout_seconds):
            await asyncio.sleep(0.05)

    @asynccontextmanager
    async def fake_session(server):
        yield SlowSession()

    monkeypatch.setattr(manager, "_session", fake_session)
    with pytest.raises(McpConnectionError, match="调用超时"):
        await manager.call_tool(_server(), "slow", {}, timeout=0.001)


async def test_force_refresh_updates_status(monkeypatch) -> None:
    manager = MCPClientManager(tools_ttl_seconds=300)
    calls = 0

    class FakeSession:
        async def list_tools(self):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=f"tool_{calls}",
                        title=None,
                        description="Tool",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    @asynccontextmanager
    async def fake_session(server):
        yield FakeSession()

    monkeypatch.setattr(manager, "_session", fake_session)
    server = _server()
    first = await manager.status(server, refresh=True)
    second = await manager.status(server, refresh=True)

    assert first["status"] == second["status"] == "online"
    assert first["tools"][0].name == "tool_1"
    assert second["tools"][0].name == "tool_2"
    assert second["latency_ms"] is not None
    assert second["checked_at"] is not None


async def test_offline_status_keeps_last_discovered_tools(monkeypatch) -> None:
    manager = MCPClientManager()
    available = True

    class FakeSession:
        async def list_tools(self):
            if not available:
                raise RuntimeError("unreachable")
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        title=None,
                        description="Search",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    @asynccontextmanager
    async def fake_session(server):
        yield FakeSession()

    monkeypatch.setattr(manager, "_session", fake_session)
    server = _server()
    assert (await manager.status(server, refresh=True))["status"] == "online"
    available = False
    result = await manager.status(server, refresh=True)

    assert result["status"] == "offline"
    assert result["tools"][0].name == "search"
    assert "unreachable" in result["error"]
