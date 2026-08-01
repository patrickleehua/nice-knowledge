"""Typed permission vocabulary shared by Agent metadata and policy evaluation.

迁移自 TF backend/app/domain/agent_permission.py(MIGRATION-PLAN §5.4)。
词表原样保留;唯一新增 ``ToolPermissionSpec.creates_scope_root``——它替代
TF permissions/actions.py:327 的 ``creates_project = tool.name == "project_create"``
硬编码:哪个工具会新建一个宿主业务作用域根对象,由工具自己声明。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    TRANSITION = "transition"


class ToolRisk(StrEnum):
    ROUTINE = "routine"
    SENSITIVE = "sensitive"
    CRITICAL = "critical"


class ToolCategory(StrEnum):
    LOCAL_DATA = "local_data"
    NETWORK = "network"
    EXTERNAL_COST = "external_cost"
    FINANCIAL = "financial"
    DESTRUCTIVE = "destructive"
    WORKFLOW = "workflow"
    EXPORT = "export"


class ToolReversibility(StrEnum):
    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


class ToolDelegation(StrEnum):
    AUTOMATIC = "automatic"
    REVIEWABLE = "reviewable"
    USER_REQUIRED = "user_required"
    AGENT_FORBIDDEN = "agent_forbidden"


class PermissionScope(StrEnum):
    """授权范围三档,由窄到宽。

    RESOURCE 指宿主业务作用域这一档:chat_sessions.scope_type/scope_id 指向的
    那个作用域根(宿主可以是工单、案件、项目……由 ResourceResolver 解析)。
    """

    SESSION = "session"
    RESOURCE = "resource"
    ORGANIZATION = "organization"


class PermissionProfile(StrEnum):
    REQUEST_APPROVAL = "request_approval"
    AUTO_REVIEW = "auto_review"
    FULL_ACCESS = "full_access"
    CUSTOM = "custom"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    AUTO_REVIEW = "auto_review"
    ASK_USER = "ask_user"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolPermissionSpec:
    """Immutable, explicit permission metadata for one registered Agent tool."""

    effect: ToolEffect
    risk: ToolRisk
    categories: frozenset[ToolCategory]
    reversibility: ToolReversibility
    delegation: ToolDelegation
    scope: PermissionScope
    material_arguments: frozenset[str] = frozenset()
    secret_arguments: frozenset[str] = frozenset()
    # 该工具会创建一个新的宿主业务作用域根(如"新建工单/项目/档案")。
    # 权限判定据此区分"在通用会话里新开一个作用域"(允许)与"从通用会话改动
    # 一个已存在的作用域"(需用户批准),不再按工具名硬编码。
    creates_scope_root: bool = False

    def __post_init__(self) -> None:
        enum_fields = (
            ("effect", self.effect, ToolEffect),
            ("risk", self.risk, ToolRisk),
            ("reversibility", self.reversibility, ToolReversibility),
            ("delegation", self.delegation, ToolDelegation),
            ("scope", self.scope, PermissionScope),
        )
        for name, value, enum_type in enum_fields:
            if not isinstance(value, enum_type):
                raise TypeError(f"{name} must be a {enum_type.__name__}")
        categories = frozenset(self.categories)
        material_arguments = frozenset(self.material_arguments)
        secret_arguments = frozenset(self.secret_arguments)
        if not categories:
            raise ValueError("tool permission categories cannot be empty")
        if not all(isinstance(value, ToolCategory) for value in categories):
            raise TypeError("tool permission categories must contain ToolCategory values")
        if not all(isinstance(value, str) and value for value in material_arguments):
            raise TypeError("material argument names must be non-empty strings")
        if not all(isinstance(value, str) and value for value in secret_arguments):
            raise TypeError("secret argument names must be non-empty strings")
        if not isinstance(self.creates_scope_root, bool):
            raise TypeError("creates_scope_root must be a bool")
        object.__setattr__(self, "categories", categories)
        object.__setattr__(self, "material_arguments", material_arguments)
        object.__setattr__(self, "secret_arguments", secret_arguments)

    @property
    def side_effect(self) -> Literal["read", "write"]:
        return "read" if self.effect is ToolEffect.READ else "write"
