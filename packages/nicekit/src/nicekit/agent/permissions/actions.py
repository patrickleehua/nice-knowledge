"""Canonical action normalization, redaction, hashing, and scope resolution.

与 TF(backend/app/services/agent/permissions/actions.py)的差异
(MIGRATION-PLAN §4 ResourceResolver / §5.4"permissions/ 改造"):

- `RESOURCE_ARGUMENTS`(TF :58)与 `_resource_project` 七分支 SQL(TF :223)删除,
  换成宿主注册的 `ResourceResolver`:"这组参数指向的资源属于哪个作用域根"
  只有宿主知道,SDK 不 import 任何业务表;
- `creates_project = tool.name == "project_create"`(TF :327)换成
  `ToolPermissionSpec.creates_scope_root`,由工具自己声明;
- `MATERIAL_ARGUMENT_NAMES` 去掉业务专属项(offer_index/unit_cost),
  宿主经 `register_material_arguments()` 追加。

canonicalize / 脱敏 / 指纹逻辑原样保留——它们是审批与授权匹配的稳定口径,
改动会让已签发的 grant 全部失配。
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.agent.tools import ToolDef
from nicekit.models.chat import ChatSession, ChatSessionOriginMode

REDACTED = "[REDACTED]"
SECRET_ARGUMENT_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)
# 进入"实质参数指纹"的通用参数名。指纹决定一次授权能覆盖多大范围的重复调用,
# 因此这里只放跨业务通用的量纲字段;业务字段由宿主追加。
_BUILTIN_MATERIAL_ARGUMENT_NAMES = frozenset(
    {
        "amount",
        "currency",
        "kind",
        "n",
        "price",
        "qty",
        "size",
        "status",
    }
)
_MATERIAL_ARGUMENT_NAMES: set[str] = set(_BUILTIN_MATERIAL_ARGUMENT_NAMES)


def register_material_arguments(*names: str) -> None:
    """追加进指纹的实质参数名(宿主的量纲/状态字段)。

    只增不减:去掉一个已生效的实质参数会让旧授权覆盖到更宽的动作面。
    """
    _MATERIAL_ARGUMENT_NAMES.update(name.casefold() for name in names if name)


def material_argument_names() -> frozenset[str]:
    return frozenset(_MATERIAL_ARGUMENT_NAMES)


def reset_material_arguments() -> None:
    """恢复内置名单(测试 / 重新装配用)。"""
    _MATERIAL_ARGUMENT_NAMES.clear()
    _MATERIAL_ARGUMENT_NAMES.update(_BUILTIN_MATERIAL_ARGUMENT_NAMES)


class ResourceResolutionError(ValueError):
    """resolver 明确判定这组参数不可用;code 进 ResolvedActionScope.resolution_error。

    约定的 code(policy.py 直接当 deny 的 reason_code 用):
    - ``invalid_resource_id``:参数里的资源标识格式非法;
    - ``resource_not_visible``:资源不存在或当前调用者看不见;
    - ``conflicting_scope``:同一次调用的参数指向了不同的作用域根。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ResourceResolver(Protocol):
    """宿主注册的作用域解析器。

    返回该次调用真正作用到的宿主作用域根 id;拿不准/不归自己管返回 None,
    交给下一个 resolver 或回落到会话绑定的作用域。判定"这组参数有问题"时
    抛 ResourceResolutionError,让策略层 fail-closed。
    """

    async def resolve_scope(
        self,
        session: AsyncSession,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    """resolver 可选返回的富信息(便于填 resource_type/resource_id)。"""

    scope_id: UUID | None
    resource_type: str | None = None
    resource_id: UUID | None = None


_resolvers: list[ResourceResolver] = []


def register_resource_resolver(resolver: ResourceResolver) -> ResourceResolver:
    """登记一个作用域解析器(按注册顺序询问,第一个给出非 None 的胜出)。"""
    _resolvers.append(resolver)
    return resolver


def registered_resource_resolvers() -> tuple[ResourceResolver, ...]:
    return tuple(_resolvers)


def reset_resource_resolvers() -> None:
    """清空已注册的解析器(测试 / 重新装配用)。"""
    _resolvers.clear()


@dataclass(frozen=True, slots=True)
class ResolvedActionScope:
    org_id: UUID
    session_id: UUID
    mode: Literal["general", "scoped"]
    # 会话创建时绑定的作用域根(chat_sessions.scope_id)
    bound_scope_id: UUID | None
    # 本次调用真正作用到的作用域根(resolver 结果,回落到 bound_scope_id)
    target_scope_id: UUID | None
    resource_type: str | None
    resource_id: UUID | None
    creates_scope_root: bool
    resolution_error: str | None = None

    @property
    def cross_scope(self) -> bool:
        return bool(
            self.bound_scope_id
            and self.target_scope_id
            and self.bound_scope_id != self.target_scope_id
        )


@dataclass(frozen=True, slots=True)
class CanonicalToolAction:
    tool_name: str
    sanitized_arguments: dict
    argument_hash: str
    action_hash: str
    material_arguments: dict
    material_fingerprint: str
    scope: ResolvedActionScope


def canonical_action_payload(action: CanonicalToolAction) -> dict:
    """Bounded, redacted action metadata suitable for events and approval storage."""

    scope = action.scope
    return {
        "sanitized_arguments": action.sanitized_arguments,
        "argument_hash": action.argument_hash,
        "action_hash": action.action_hash,
        "material_arguments": action.material_arguments,
        "material_fingerprint": action.material_fingerprint,
        "scope": {
            "mode": scope.mode,
            "org_id": str(scope.org_id),
            "session_id": str(scope.session_id),
            "bound_scope_id": (
                str(scope.bound_scope_id) if scope.bound_scope_id else None
            ),
            "target_scope_id": (
                str(scope.target_scope_id) if scope.target_scope_id else None
            ),
            "resource_type": scope.resource_type,
            "resource_id": str(scope.resource_id) if scope.resource_id else None,
            "creates_scope_root": scope.creates_scope_root,
            "resolution_error": scope.resolution_error,
        },
    }


def _normalize_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool arguments cannot contain non-finite numbers")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize_json(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise TypeError(f"unsupported tool argument type: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_secret_name(name: str, explicit: frozenset[str]) -> bool:
    normalized = name.casefold().replace("-", "_")
    return (
        normalized in explicit
        or normalized in SECRET_ARGUMENT_NAMES
        or normalized.endswith(("_password", "_secret", "_token", "_api_key"))
    )


def _redact(value: object, explicit: frozenset[str]) -> object:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret_name(key, explicit) else _redact(item, explicit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, explicit) for item in value]
    return value


def _is_material_name(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized in _MATERIAL_ARGUMENT_NAMES
        or normalized.endswith("_id")
        or normalized.endswith("_ids")
    )


def _material_projection(tool: ToolDef, normalized_arguments: dict) -> dict:
    declared = tool.permission.material_arguments
    keys = {
        name
        for name in normalized_arguments
        if name in declared or _is_material_name(name)
    }
    return {name: normalized_arguments[name] for name in sorted(keys)}


def parse_uuid(value: object) -> UUID | None:
    """宽松 UUID 解析,供宿主 resolver 复用(非法值返回 None,不抛)。"""
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def resolve_action_scope(
    session: AsyncSession,
    *,
    org_id: UUID,
    chat_session: ChatSession,
    tool: ToolDef,
    arguments: Mapping[str, object],
    resolved_scope: ResolvedResource | None = None,
) -> ResolvedActionScope:
    """确定这次调用作用到哪个宿主作用域根。

    顺序:预解析结果(批量场景由调用方先算好)→ 注册的 resolver → 会话绑定作用域。
    """

    resolution_error: str | None = None
    resolution = resolved_scope
    if resolution is None:
        for resolver in _resolvers:
            try:
                scope_id = await resolver.resolve_scope(session, tool.name, arguments)
            except ResourceResolutionError as exc:
                resolution_error = exc.code
                break
            if scope_id is not None:
                resolution = ResolvedResource(scope_id=scope_id)
                break

    target_scope_id = (
        resolution.scope_id
        if resolution is not None and resolution.scope_id is not None
        else chat_session.scope_id
    )
    return ResolvedActionScope(
        org_id=org_id,
        session_id=chat_session.id,
        mode=(
            "scoped"
            if chat_session.origin_mode == ChatSessionOriginMode.SCOPED
            else "general"
        ),
        bound_scope_id=chat_session.scope_id,
        target_scope_id=None if resolution_error else target_scope_id,
        resource_type=resolution.resource_type if resolution is not None else None,
        resource_id=resolution.resource_id if resolution is not None else None,
        creates_scope_root=bool(tool.permission.creates_scope_root),
        resolution_error=resolution_error,
    )


async def canonicalize_tool_action(
    session: AsyncSession,
    *,
    org_id: UUID,
    chat_session: ChatSession,
    tool: ToolDef,
    arguments: Mapping[str, object],
    resolved_scope: ResolvedResource | None = None,
) -> CanonicalToolAction:
    normalized = _normalize_json(arguments)
    if not isinstance(normalized, dict):
        raise TypeError("tool arguments must be an object")
    scope = await resolve_action_scope(
        session,
        org_id=org_id,
        chat_session=chat_session,
        tool=tool,
        arguments=normalized,
        resolved_scope=resolved_scope,
    )
    material = _material_projection(tool, normalized)
    scope_payload = {
        "org_id": str(scope.org_id),
        "session_id": str(scope.session_id),
        "scope_id": str(scope.target_scope_id) if scope.target_scope_id else None,
        "resource_type": scope.resource_type,
        "resource_id": str(scope.resource_id) if scope.resource_id else None,
        "creates_scope_root": scope.creates_scope_root,
    }
    sanitized = _redact(normalized, tool.permission.secret_arguments)
    assert isinstance(sanitized, dict)
    return CanonicalToolAction(
        tool_name=tool.name,
        sanitized_arguments=sanitized,
        argument_hash=_sha256(normalized),
        action_hash=_sha256(
            {"tool": tool.name, "arguments": normalized, "scope": scope_payload}
        ),
        material_arguments=_redact(material, tool.permission.secret_arguments),
        material_fingerprint=_sha256(
            {"tool": tool.name, "arguments": material, "scope": scope_payload}
        ),
        scope=scope,
    )
