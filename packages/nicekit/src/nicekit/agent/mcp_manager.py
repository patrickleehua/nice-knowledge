"""MCP client discovery/calls without mutating the global native tool registry.

迁移自 TF backend/app/services/agent/mcp_manager.py,近乎原样。SDK 化改造:
- **凭证解密**:`mcp_servers.headers/env` 落库时经 `core.secretbox` 加密
  (MIGRATION-PLAN §5.1),连接前统一 `decrypt_mapping()` 还原;解不开的键
  fail-closed 直接丢弃并告警,绝不把密文当明文发给上游。
- **能力元数据(可选)**:MCP 工具默认 fail-closed 按 write 处理
  (见 `mcp_tool_permission()`)。两条降级为 read 的依据,按优先级:
  1. 协议原生 `Tool.annotations.read_only_hint`(MCP 2.x 起标准化);
  2. server 配置里的私有 `tool_metadata`(形如 `{"tool": {"effect": "read"}}`),
     给还没实现 annotations 的老服务端兜底。
  TODO:等生态普遍实现 annotations 后删掉第 2 条私有约定;destructive_hint /
  idempotent_hint 也应进一步映射到 reversibility / risk 轴,现在一律取最保守值。
- **SDK 版本适配**:mcp 2.x 把 `inputSchema/isError/structuredContent` 改成了
  snake_case,并要求 streamable_http 传 httpx2 的 AsyncClient。这里两种命名都读、
  两种 httpx 都能用,避免把 SDK 小版本钉死在 nicekit 的依赖表上。
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from nicekit.core.db import org_session
from nicekit.core.secretbox import SecretBoxError, get_secret_box
from nicekit.domain.agent_permission import ToolPermissionSpec
from nicekit.models.mcp import McpServer

try:  # mcp 2.x 的 streamable_http 走 httpx2;老版本仍是 httpx
    import httpx2 as _http
except ImportError:  # pragma: no cover - 取决于安装的 mcp 版本
    import httpx as _http

logger = logging.getLogger(__name__)


def _field(obj: object, *names: str, default=None):
    """按顺序读第一个存在的属性名(吃掉 SDK 的 camelCase/snake_case 改名)。"""
    for name in names:
        try:
            value = getattr(obj, name)
        except AttributeError:
            continue
        if value is not None:
            return value
    return default


class McpConnectionError(RuntimeError):
    pass


def decrypt_mapping(raw: dict | None, *, server_name: str, field_name: str) -> dict[str, str]:
    """解密 headers/env 这类"值可能是密文"的映射。

    单个键解不开只丢该键并告警(fail-closed):把密文原样当 header 发出去
    等于泄露密文且必然被上游拒绝,不如缺一个 header 让错误显式暴露。
    """
    result: dict[str, str] = {}
    box = get_secret_box()
    for key, value in (raw or {}).items():
        text = str(value)
        try:
            result[str(key)] = box.decrypt(text)
        except SecretBoxError as exc:
            logger.warning(
                "MCP 凭证解密失败,已跳过该项",
                extra={"server": server_name, "field": field_name, "key": key, "error": str(exc)},
            )
    return result


def mcp_tool_permission(
    server: McpServer, tool: "McpToolSpec | str"
) -> ToolPermissionSpec:
    """推导一个 MCP 工具的权限元数据(fail-closed:默认 write)。

    只有明确拿到"只读"证据才降级为 read:协议原生 read_only_hint 优先,
    其次是 server 配置里的私有 tool_metadata。两者都没有就按最保守处理——
    MCP 服务端是外部系统,猜错方向的代价是让 agent 无审批地改别人的数据。
    """
    from nicekit.agent.tools import legacy_tool_permission

    name = tool if isinstance(tool, str) else tool.name
    read_only = None if isinstance(tool, str) else tool.read_only_hint
    if read_only is None:
        metadata = getattr(server, "tool_metadata", None)
        entry = (metadata or {}).get(name) if isinstance(metadata, dict) else None
        if isinstance(entry, dict):
            read_only = entry.get("effect") == "read"
    return legacy_tool_permission("read" if read_only else "write")


@dataclass(frozen=True)
class McpToolSpec:
    name: str
    description: str
    schema: dict
    # 协议原生 annotations.read_only_hint;服务端没声明时为 None(按 write 处理)
    read_only_hint: bool | None = None


@dataclass
class ClientEntry:
    tools: tuple[McpToolSpec, ...] = ()
    expires_at: float = 0.0
    last_error: str | None = None
    last_checked_at: float | None = None
    latency_ms: int | None = None


@dataclass
class _LoopState:
    entries: dict = field(default_factory=dict)
    locks: dict = field(default_factory=dict)


class MCPClientManager:
    """Per-process manager with event-loop-local locked connection metadata."""

    def __init__(
        self, *, tools_ttl_seconds: int = 300, connect_timeout_seconds: float = 10
    ) -> None:
        self._ttl = tools_ttl_seconds
        self._connect_timeout = connect_timeout_seconds
        self._states: WeakKeyDictionary = WeakKeyDictionary()

    def _state(self) -> _LoopState:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = self._states[loop] = _LoopState()
        return state

    def _server_lock(self, server_id) -> asyncio.Lock:
        state = self._state()
        return state.locks.setdefault(server_id, asyncio.Lock())

    @asynccontextmanager
    async def _session(self, server: McpServer):
        # 凭证在连接边界统一解密:落库是密文,发出去必须是明文
        headers = decrypt_mapping(server.headers, server_name=server.name, field_name="headers")
        try:
            if server.transport == "streamable_http":
                async with (
                    _http.AsyncClient(headers=headers) as client,
                    streamable_http_client(server.endpoint_url, http_client=client) as (
                        read,
                        write,
                        _,
                    ),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    yield session
                return
            if server.transport == "sse":
                async with (
                    sse_client(server.endpoint_url, headers=headers) as (
                        read,
                        write,
                    ),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    yield session
                return
            if server.transport == "stdio":
                # 每次会话拉起子进程,结束即回收;env 叠加在当前进程环境之上
                # (SDK 传 env 时不继承 os.environ,PATH 都没有会起不来)
                if not server.command:
                    raise McpConnectionError(f"MCP {server.name}:stdio 未配置 command")
                params = StdioServerParameters(
                    command=server.command,
                    args=list(server.args or []),
                    env={
                        **os.environ,
                        **decrypt_mapping(
                            server.env, server_name=server.name, field_name="env"
                        ),
                    },
                )
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    yield session
                return
            raise McpConnectionError(f"MCP {server.name}:不支持传输 {server.transport}")
        except McpConnectionError:
            raise
        except Exception as exc:
            raise McpConnectionError(f"MCP {server.name} 连接失败:{exc}") from exc

    async def list_tools(
        self, server: McpServer, *, force_refresh: bool = False
    ) -> list[McpToolSpec]:
        state = self._state()
        async with self._server_lock(server.id):
            entry = state.entries.setdefault(server.id, ClientEntry())
            if not force_refresh and entry.expires_at > time.monotonic():
                return list(entry.tools)
            started = time.monotonic()
            connect_timeout = server.connect_timeout_seconds or self._connect_timeout
            try:
                async with asyncio.timeout(connect_timeout):
                    async with self._session(server) as session:
                        result = await session.list_tools()
                tools = tuple(
                    McpToolSpec(
                        name=tool.name,
                        description=tool.description or tool.title or tool.name,
                        schema=_field(tool, "input_schema", "inputSchema", default={}),
                        read_only_hint=_field(
                            getattr(tool, "annotations", None),
                            "read_only_hint",
                            "readOnlyHint",
                        ),
                    )
                    for tool in result.tools
                )
            except TimeoutError as exc:
                error = McpConnectionError(
                    f"MCP {server.name} 连接超时({connect_timeout:g}s)"
                )
                entry.last_error = str(error)
                entry.last_checked_at = time.time()
                entry.latency_ms = round((time.monotonic() - started) * 1000)
                raise error from exc
            except Exception as exc:
                entry.last_error = str(exc)
                entry.last_checked_at = time.time()
                entry.latency_ms = round((time.monotonic() - started) * 1000)
                raise
            entry.tools = tools
            entry.expires_at = time.monotonic() + self._ttl
            entry.last_error = None
            entry.last_checked_at = time.time()
            entry.latency_ms = round((time.monotonic() - started) * 1000)
            return list(tools)

    async def status(self, server: McpServer, *, refresh: bool = False) -> dict:
        if not server.enabled:
            entry = self._state().entries.get(server.id)
            return {
                "server_id": server.id,
                "status": "disabled",
                "latency_ms": entry.latency_ms if entry else None,
                "checked_at": entry.last_checked_at if entry else None,
                "error": None,
                "tools": list(entry.tools) if entry else [],
            }
        try:
            tools = await self.list_tools(server, force_refresh=refresh)
        except Exception as exc:
            state = self._state()
            entry = state.entries.get(server.id)
            return {
                "server_id": server.id,
                "status": "offline",
                "latency_ms": entry.latency_ms if entry else None,
                "checked_at": entry.last_checked_at if entry else time.time(),
                "error": str(exc),
                "tools": list(entry.tools) if entry else [],
            }
        entry = self._state().entries[server.id]
        return {
            "server_id": server.id,
            "status": "online",
            "latency_ms": entry.latency_ms,
            "checked_at": entry.last_checked_at,
            "error": None,
            "tools": tools,
        }

    def cached_status(self, server: McpServer) -> dict:
        """Read connection metadata without opening a socket or subprocess."""
        entry = self._state().entries.get(server.id)
        if not server.enabled:
            status = "disabled"
        elif entry is None or entry.last_checked_at is None:
            status = "unchecked"
        elif entry.last_error:
            status = "offline"
        else:
            status = "online"
        return {
            "server_id": server.id,
            "status": status,
            "latency_ms": entry.latency_ms if entry else None,
            "checked_at": entry.last_checked_at if entry else None,
            "error": entry.last_error if entry else None,
            "tools": list(entry.tools) if entry else [],
        }

    async def call_tool(
        self,
        server: McpServer,
        tool_name: str,
        args: dict[str, Any],
        timeout: float | None = None,
    ) -> str:
        # 优先级:显式入参 > 服务器配置 > 默认 60s
        timeout = timeout or server.call_timeout_seconds or 60
        try:
            async with asyncio.timeout(timeout):
                async with self._session(server) as session:
                    result = await session.call_tool(
                        tool_name,
                        args,
                        read_timeout_seconds=timedelta(seconds=timeout),
                    )
        except TimeoutError as exc:
            raise McpConnectionError(
                f"MCP {server.name}.{tool_name} 调用超时({timeout:g}s)"
            ) from exc
        payload = {
            "is_error": bool(_field(result, "is_error", "isError", default=False)),
            "structured_content": _field(
                result, "structured_content", "structuredContent"
            ),
            "content": [
                item.model_dump(mode="json", by_alias=True) for item in result.content
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    async def invalidate(self, server_id) -> None:
        state = self._state()
        async with self._server_lock(server_id):
            state.entries.pop(server_id, None)


mcp_client_manager = MCPClientManager()


async def warm_enabled_mcp_servers(
    factory: async_sessionmaker[AsyncSession], platform_org_id: UUID
) -> None:
    """Load persisted MCP configurations and warm their tool metadata at startup."""
    async with org_session(factory, platform_org_id) as session:
        servers = (
            await session.execute(select(McpServer).where(McpServer.enabled))
        ).scalars().all()
    if not servers:
        return
    results = await asyncio.gather(
        *(mcp_client_manager.status(server, refresh=True) for server in servers)
    )
    online = sum(item["status"] == "online" for item in results)
    logger.info("MCP 启动加载完成", extra={"total": len(results), "online": online})
