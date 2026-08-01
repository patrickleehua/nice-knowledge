"""Agent 工具注册表:节点即工具,全部薄包装服务层,不重写业务。

schema 遵守双协议 strict 交集(additionalProperties:false、全字段 required,
可缺省语义用 ["string","null"]);执行错误抛 ToolError,loop 转错误文本回给模型,
不炸 loop。

与 TF(backend/app/services/agent/tools.py)的差异(MIGRATION-PLAN §4/§5.4):
- 模块级 `TOOLS` dict 换成 `ToolRegistry` 实例。SDK 内置工具注册到
  `default_registry`,宿主可自建 registry 再 `merge()`;重名一律报错,
  不再做"catalog/metadata/registry 三方完全相等"的全局断言
  (TF :947 那套断言假设只有一个来源,SDK 必须允许多来源)。
- `TOOL_META`(中文显示名 + 分组)并入 `ToolDef.label` / `ToolDef.category`。
- `ToolContext.pipeline_run_ids` 泛化为 `cancellable_resources` + `on_cancel`
  终结回调:工具自己登记"我起了什么需要收敛的东西、怎么收敛它"。
- 角色检查(TF 的 PRICE_ROLES/_require_pricer/_require_writer/_require_org_admin)
  不进 SDK,改为 `RoleChecker` 注入点。
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolPermissionSpec,
    ToolReversibility,
    ToolRisk,
)
from nicekit.models.chat import ChatSession

DEFAULT_TOOL_CATEGORY = "其他"


class ToolError(Exception):
    """工具业务失败(参数错/守门拒绝/对象不存在),内容会原样反馈给模型。"""


class ToolRegistrationError(ValueError):
    """工具登记违反注册表契约(重名 / 缺元数据 / schema 不合法)。"""


@dataclass
class CitationCounter:
    """一次 agent run 内的引用编号游标。

    联网工具可能在同一轮里被调用多次。若每次都从 1 开始编号,模型写 [1] 时
    指代不明,前端按 ref 生成的锚点 id 也会撞车、跳到第一处。因此编号在 run 内
    累加,由 ToolContext 携带同一个实例跨调用共享。
    """

    value: int = 0

    def take(self, count: int) -> int:
        """领取 count 个编号,返回本批的偏移量(本批第 i 条的 ref = 偏移量 + i)。"""
        offset = self.value
        self.value += max(count, 0)
        return offset


# 单次工具调用被取消/超时后的终结回调。执行边界(loop.execute_native_tool)在
# 判定终态后逐个 await,异常被吞掉——回调是尽力而为的收尾,不能反过来炸掉终结路径。
CancelHook = Callable[["ToolContext"], Awaitable[None]]


@dataclass
class ToolContext:
    session: AsyncSession
    org_id: UUID
    user_id: UUID
    # 调用者在本 org 内的角色名(tenancy.roles 的注册值)。SDK 自身不做角色判定,
    # 需要角色门槛的宿主工具走 check_role()/require_role()。
    role: str
    # 会话本体:绑定的宿主作用域(scope_type/scope_id)是工具参数的缺省回退来源
    chat_session: ChatSession
    skills: tuple[str, ...] = ()
    # Bound by the loop to the current tool_call_id. Tools may report only real
    # provider/storage boundaries; this is not a reasoning or timer channel.
    progress: Callable[[str], Awaitable[None]] | None = None
    # 跨工具调用共享(见 CitationCounter);隔离执行器必须传同一个实例进来。
    citations: CitationCounter = dataclass_field(default_factory=CitationCounter)
    # 本轮用户选定的搜索服务商;None = 跟随管理端配置(auto 故障转移链)
    websearch_provider: str | None = None
    # 当前单次工具调用已创建、且在超时/取消时必须收敛的外部资源标识。
    # 执行边界用这些稳定 ID 立即收敛,不按时间窗口猜测关联任务。
    # (TF 里这是写死的 pipeline_run_ids;SDK 不认识宿主的任务表。)
    cancellable_resources: set[str] = dataclass_field(default_factory=set)
    # 与 cancellable_resources 配套的终结回调:登记资源的工具同时登记怎么收敛它。
    on_cancel: list[CancelHook] = dataclass_field(default_factory=list)


ToolFn = Callable[[ToolContext, dict], Awaitable[dict]]

# 角色检查注入点:宿主实现"这个调用者是否具备该角色"(可含角色继承/层级)。
# SDK 默认实现只做名称相等比较。
RoleChecker = Callable[[ToolContext, str], bool]


def _default_role_checker(ctx: ToolContext, role: str) -> bool:
    return str(ctx.role) == role


_role_checker: RoleChecker = _default_role_checker


def set_role_checker(checker: RoleChecker | None) -> None:
    """注入宿主的角色判定(传 None 恢复默认的名称相等比较)。"""
    global _role_checker
    _role_checker = checker or _default_role_checker


def check_role(ctx: ToolContext, role: str) -> bool:
    return _role_checker(ctx, role)


def require_role(ctx: ToolContext, role: str, message: str) -> None:
    """角色不满足时抛 ToolError(内容原样回给模型,不炸 loop)。"""
    if not check_role(ctx, role):
        raise ToolError(message)


def legacy_tool_permission(
    side_effect: Literal["read", "write"] | None,
) -> ToolPermissionSpec:
    """Fail-closed metadata for dynamically constructed ToolDef callers.

    动态工具(MCP / 子 agent 桥接)没有声明式权限目录,只能按副作用给一份
    最保守的元数据。经 ToolRegistry.register() 登记的工具必须显式声明。
    """

    is_read = side_effect == "read"
    return ToolPermissionSpec(
        effect=ToolEffect.READ if is_read else ToolEffect.WRITE,
        risk=ToolRisk.SENSITIVE if is_read else ToolRisk.CRITICAL,
        categories=frozenset({ToolCategory.LOCAL_DATA}),
        reversibility=(
            ToolReversibility.REVERSIBLE
            if is_read
            else ToolReversibility.IRREVERSIBLE
        ),
        delegation=ToolDelegation.REVIEWABLE,
        scope=PermissionScope.SESSION,
    )


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict
    executor: ToolFn
    permission: ToolPermissionSpec | None = None
    side_effect: Literal["read", "write"] | None = None
    label: str = ""  # 显示名(admin / 工作台展示;空串时回落到 name)
    category: str = DEFAULT_TOOL_CATEGORY  # 分组(admin 工具勾选列表按此分组)
    # None 使用 Agent 服务默认总时限；只有确有更长预算的工具才显式覆盖。
    timeout_seconds: float | None = None
    # 仅供旧 loop/客户端迁移；新策略引擎读取 permission。
    confirm: bool = False
    # 写工具开始执行时是否发 tool.progress "开始执行"。自己会持续 report
    # progress 的工具(如图片生成)置 False,避免和它自己的进度打架。
    # (替代 TF loop.py:818/:1118 对 image_generate 的工具名硬编码特判。)
    emit_start_progress: bool = True
    # 工具自声明的业务阶段;宿主的事件钩子据此推导阶段事件。
    # (替代 TF service.py:1362 按工具名前缀猜阶段。)
    stage: str | None = None
    # 是否允许被"本轮临时工具"机制(runtime_tools)临时放开。
    # (替代 TF runtime_tools.py 的 NOT_RUNTIME_GRANTABLE 名单。)
    runtime_grantable: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(f"tool {self.name} timeout_seconds must be positive")
        permission = self.permission or legacy_tool_permission(self.side_effect)
        derived_side_effect = permission.side_effect
        if self.side_effect is not None and self.side_effect != derived_side_effect:
            raise ValueError(
                f"tool {self.name} side_effect conflicts with permission effect"
            )
        if self.confirm and permission.delegation not in {
            ToolDelegation.REVIEWABLE,
            ToolDelegation.USER_REQUIRED,
        }:
            raise ValueError(
                f"tool {self.name} legacy confirm requires reviewable delegation"
            )
        object.__setattr__(self, "permission", permission)
        object.__setattr__(self, "side_effect", derived_side_effect)

    @property
    def display_label(self) -> str:
        return self.label or self.name


def tool_permission(
    effect: ToolEffect,
    risk: ToolRisk,
    categories: set[ToolCategory],
    reversibility: ToolReversibility,
    delegation: ToolDelegation,
    scope: PermissionScope,
    material_arguments: tuple[str, ...] = (),
    *,
    secret_arguments: tuple[str, ...] = (),
    creates_scope_root: bool = False,
) -> ToolPermissionSpec:
    """七轴权限元数据的位置参数简写(TF tools.py:294 `_spec` 的公开版本)。"""
    return ToolPermissionSpec(
        effect=effect,
        risk=risk,
        categories=frozenset(categories),
        reversibility=reversibility,
        delegation=delegation,
        scope=scope,
        material_arguments=frozenset(material_arguments),
        secret_arguments=frozenset(secret_arguments),
        creates_scope_root=creates_scope_root,
    )


def validate_tool_permission_spec(
    name: str, schema: dict, permission: ToolPermissionSpec
) -> None:
    """Reject incomplete or unsafe tool metadata before the registry is usable."""

    if permission.effect is ToolEffect.READ and ToolCategory.DESTRUCTIVE in permission.categories:
        raise ValueError(f"tool {name}: read effect cannot be destructive")
    if permission.effect is ToolEffect.DELETE:
        if ToolCategory.DESTRUCTIVE not in permission.categories:
            raise ValueError(f"tool {name}: delete effect requires destructive category")
        if permission.risk is not ToolRisk.CRITICAL:
            raise ValueError(f"tool {name}: delete effect requires critical risk")
        if permission.delegation is ToolDelegation.AUTOMATIC:
            raise ValueError(f"tool {name}: delete effect cannot be automatic")
    if (
        permission.effect is ToolEffect.TRANSITION
        and ToolCategory.WORKFLOW not in permission.categories
    ):
        raise ValueError(f"tool {name}: transition effect requires workflow category")
    if (
        ToolCategory.EXTERNAL_COST in permission.categories
        and ToolCategory.NETWORK not in permission.categories
    ):
        raise ValueError(f"tool {name}: external cost requires network category")
    if permission.risk is ToolRisk.CRITICAL and permission.delegation is ToolDelegation.AUTOMATIC:
        raise ValueError(f"tool {name}: critical risk cannot be automatic")
    if permission.effect is ToolEffect.READ and permission.creates_scope_root:
        raise ValueError(f"tool {name}: read effect cannot create a scope root")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        raise ValueError(f"tool {name}: schema properties are required")
    declared_arguments = set(properties)
    unknown_arguments = (
        permission.material_arguments | permission.secret_arguments
    ) - declared_arguments
    if unknown_arguments:
        raise ValueError(
            f"tool {name}: permission arguments absent from schema: "
            f"{sorted(unknown_arguments)}"
        )


class ToolRegistry:
    """一组 agent 工具的登记表。

    契约(替代 TF 的模块级 TOOLS dict + validate_builtin_tool_registry 三方断言):
    - **每个登记项必须自带完整权限元数据**:register() 不接受 permission=None,
      登记时立刻跑 validate_tool_permission_spec,坏元数据在导入期就炸;
    - **允许多来源**:SDK 内置工具、宿主工具、动态工具各自登记或各建 registry
      再 merge;
    - **重名一律报错**:静默覆盖会让"哪份实现在跑"变成运行期谜题。
    """

    __slots__ = ("_name", "_tools")

    def __init__(self, name: str = "default") -> None:
        self._name = name
        self._tools: dict[str, ToolDef] = {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ToolRegistry(name={self._name!r}, tools={len(self._tools)})"

    def __contains__(self, name: object) -> bool:
        return str(name) in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    @property
    def name(self) -> str:
        return self._name

    # ---- 登记 ----------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        schema: dict,
        *,
        permission: ToolPermissionSpec,
        side_effect: Literal["read", "write"] | None = None,
        confirm: bool | None = None,
        label: str = "",
        category: str = DEFAULT_TOOL_CATEGORY,
        timeout_seconds: float | None = None,
        emit_start_progress: bool = True,
        stage: str | None = None,
        runtime_grantable: bool = True,
    ) -> Callable[[ToolFn], ToolFn]:
        """装饰器:把一个 async 执行体登记为工具。

        confirm 不传时按 delegation 推导(reviewable/user_required → True),
        与 TF register_tool 同口径。
        """

        if not isinstance(permission, ToolPermissionSpec):
            raise ToolRegistrationError(f"tool {name} is missing permission metadata")
        validate_tool_permission_spec(name, schema, permission)
        if side_effect is not None and side_effect != permission.side_effect:
            raise ToolRegistrationError(
                f"tool {name} side_effect conflicts with permission metadata"
            )
        legacy_confirm = (
            bool(confirm)
            if confirm is not None
            else permission.delegation
            in {ToolDelegation.REVIEWABLE, ToolDelegation.USER_REQUIRED}
        )

        def _wrap(fn: ToolFn) -> ToolFn:
            self.add(
                ToolDef(
                    name=name,
                    description=description,
                    schema=schema,
                    executor=fn,
                    permission=permission,
                    label=label,
                    category=category,
                    timeout_seconds=timeout_seconds,
                    confirm=legacy_confirm,
                    emit_start_progress=emit_start_progress,
                    stage=stage,
                    runtime_grantable=runtime_grantable,
                )
            )
            return fn

        return _wrap

    def add(self, tool: ToolDef) -> ToolDef:
        """登记一个已构造好的 ToolDef(动态工具:MCP / 子 agent 桥接)。"""
        if not tool.name:
            raise ToolRegistrationError("tool name cannot be empty")
        if tool.permission is None:  # pragma: no cover - ToolDef.__post_init__ 已兜底
            raise ToolRegistrationError(f"tool {tool.name} is missing permission metadata")
        if tool.name in self._tools:
            raise ToolRegistrationError(
                f"tool {tool.name} is already registered in registry {self._name!r}"
            )
        validate_tool_permission_spec(tool.name, tool.schema, tool.permission)
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> ToolDef | None:
        """摘掉一个工具(测试与重新装配用;生产路径不该用)。"""
        return self._tools.pop(name, None)

    def clear(self) -> None:
        self._tools.clear()

    # ---- 读取 ----------------------------------------------------------

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolDef:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"tool {name} is not registered in registry {self._name!r}")
        return tool

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def confirm_required_names(self) -> frozenset[str]:
        """需要用户确认的工具名(旧 confirm 位;组织策略回滚时用)。"""
        return frozenset(name for name, tool in self._tools.items() if tool.confirm)

    def runtime_grantable_names(self) -> frozenset[str]:
        return frozenset(name for name, tool in self._tools.items() if tool.runtime_grantable)

    def as_dict(self) -> dict[str, ToolDef]:
        """给 run_loop 用的 {name: ToolDef} 视图(浅拷贝,调用方改不动注册表)。"""
        return dict(self._tools)

    def schemas(self, allowed: Iterable[str]) -> list[dict]:
        """按白名单导出 provider 工具定义(未知名字忽略,由 seed/配置保证有效)。"""
        return [
            {"name": tool.name, "description": tool.description, "schema": tool.schema}
            for name in allowed
            if (tool := self._tools.get(name)) is not None
        ]

    # ---- 合并 ----------------------------------------------------------

    def merge(self, other: "ToolRegistry | Mapping[str, ToolDef]") -> "ToolRegistry":
        """把另一份登记并入本表(重名报错),返回自身便于链式调用。"""
        items = other.as_dict() if isinstance(other, ToolRegistry) else dict(other)
        conflicts = sorted(set(items) & set(self._tools))
        if conflicts:
            raise ToolRegistrationError(
                f"duplicate tool names on merge into {self._name!r}: {conflicts}"
            )
        for tool in items.values():
            self.add(tool)
        return self

    def copy(self, *, name: str | None = None) -> "ToolRegistry":
        clone = ToolRegistry(name or self._name)
        clone._tools.update(self._tools)
        return clone

    @classmethod
    def combined(
        cls, *registries: "ToolRegistry", name: str = "combined"
    ) -> "ToolRegistry":
        """把多份登记合成一份新表(源表不变;重名报错)。"""
        merged = cls(name)
        for registry in registries:
            merged.merge(registry)
        return merged


# SDK 内置工具自注册的默认表。宿主要么直接往里注册,要么自建 registry 再
# `ToolRegistry.combined(default_registry, host_registry)`。
default_registry = ToolRegistry("nicekit")


def _schema(props: dict[str, dict]) -> dict:
    """strict 交集 schema:全字段 required + additionalProperties false。"""
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_S = {"type": "string"}
_S_NULL = {"type": ["string", "null"]}
_I_NULL = {"type": ["integer", "null"]}
_B = {"type": "boolean"}
_O = {"type": "object", "additionalProperties": True}
