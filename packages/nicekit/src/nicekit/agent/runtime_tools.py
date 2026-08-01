"""运行时临时工具开关(对齐 TripBoss engine/loop.go 的 RuntimeToolOverride 语义)。

用户在输入框为"本轮"临时打开某个工具(典型场景:这条问题需要联网查一下),
不写进 agent 卡的 tools 白名单。为什么不写卡:卡是长期能力契约,一次性需求
改契约会让"这个 agent 能做什么"变得不可预测,也逼业务方为一句话发新版本;
临时开关只作用于本次 run(或本条排队输入),下一轮不带就自动回到卡白名单。

迁移自 TF backend/app/services/agent/runtime_tools.py。SDK 化改造(§5.4):
- TF 的 `NOT_RUNTIME_GRANTABLE` 工具名硬编码名单改读 `ToolDef.runtime_grantable`
  标志位——SDK 不该知道宿主注册了哪些工具叫什么名字;
- 显示名不再查模块级 `TOOL_META`,改用 `ToolDef.label`(注册时已并入本体)。

安全底线(不可放宽):临时开关**只能放开只读能力**。写工具、需要人确认的工具、
非 AUTOMATIC 委派档的工具一律拒绝——否则用户(或被诱导的模型)可以绕过卡片
授权与审批闸直接拿到写权限,临时开关就变成了权限提升通道。判定结果(含被拒项
与原因)必须进运行时上下文快照与事件流:事后要能回答"这一轮为什么多了一个工具"。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nicekit.agent.tools import ToolDef
from nicekit.domain.agent_permission import ToolDelegation

# 事件类型常量:service.py emit 与前端事件投影共用,避免字符串漂移
RUNTIME_TOOLS_EVENT_TYPE = "runtime.tools"

# 请求来源:本轮直接发送的消息 / 运行中排队的那条输入
SOURCE_USER_INPUT = "user_input"
SOURCE_QUEUED_INPUT = "queued_input"

# 判定码(机器可读;中文说明由 _OUTCOME_HINTS 派生,前端与审计都读 hint)
OUTCOME_GRANTED = "runtime_grant"
OUTCOME_ALREADY_ALLOWED = "already_allowed"
REJECT_UNKNOWN_TOOL = "unknown_tool"
REJECT_WRITE_NOT_ALLOWED = "write_not_allowed"
REJECT_NOT_GRANTABLE = "not_runtime_grantable"

_OUTCOME_HINTS: Mapping[str, str] = {
    OUTCOME_GRANTED: "已临时开启,仅本轮生效",
    OUTCOME_ALREADY_ALLOWED: "该工具已在 agent 配置中,无需临时开启",
    REJECT_UNKNOWN_TOOL: "工具不存在",
    REJECT_WRITE_NOT_ALLOWED: "写操作/需确认的工具不能临时开启",
    REJECT_NOT_GRANTABLE: "该工具由 agent 配置决定,不支持临时开启",
}

# 请求条数上限:一次请求几十个工具没有真实用途,只会把 schema 撑大。
# 必须 >= available_runtime_tools 的候选全集(test_candidates_match_grantability_rule
# 守着这条不变量:列出来的每一个都要真能开出来),新增只读工具把候选推过上限时
# 要同步抬高这里。
MAX_RUNTIME_TOOLS = 32

# 不接受临时开启的只读工具由 `ToolDef.runtime_grantable=False` 自声明。
# SDK 内置里有三个:
# - skills_list / skill_view 的可用性取决于卡片绑定了哪些技能包,临时放开只会
#   拿到一个空技能表或直接报错,真正的开关在卡片的 skills 绑定上;
# - ask_user 是循环的交互协议(会让 run 停下来等人),卡片把它排除在外通常是
#   刻意的自治设计,不该被一次性开关改掉。


@dataclass(frozen=True)
class RuntimeToolOverride:
    """一条临时工具请求的判定结果(审计单位,accepted 与否都要保留)。"""

    tool_name: str
    source: str  # SOURCE_USER_INPUT | SOURCE_QUEUED_INPUT
    reason: str  # 判定码:OUTCOME_* 或 REJECT_*
    accepted: bool
    reject_reason: str | None = None  # 仅拒绝时有值,与 reason 同码便于直接读
    # 显示名:构造时从 ToolDef.label 带入(未知工具回落工具名)
    label: str = ""

    def to_payload(self) -> dict:
        """事件流 / 快照载荷:带中文标签与说明,前端不再各自维护一份文案表。"""
        return {
            "name": self.tool_name,
            "label": self.label or self.tool_name,
            "source": self.source,
            "reason": self.reason,
            "accepted": self.accepted,
            "reject_reason": self.reject_reason,
            "hint": _OUTCOME_HINTS.get(self.reason, self.reason),
        }


def is_runtime_grantable(tool: ToolDef) -> bool:
    """只读判定:临时开关能放开的能力面。

工具还必须自声明 `runtime_grantable=True`(替代 TF 的硬编码排除名单)。

    三条同时成立才算只读:
    - side_effect == "read"(ToolDef.__post_init__ 已由 permission.effect 归一);
    - confirm 为假(confirm 代表 delegation ∈ {reviewable, user_required},即"必须有人点头");
    - delegation 是 AUTOMATIC(排除 agent_forbidden 这类 confirm=False 但本就不许 agent 自主调的档)。
    """
    if not tool.runtime_grantable:
        return False
    if tool.side_effect != "read" or tool.confirm:
        return False
    permission = tool.permission
    return permission is not None and permission.delegation is ToolDelegation.AUTOMATIC


def resolve_overrides(
    requested: Sequence[str],
    card_tools: Sequence[str],
    all_tools: Mapping[str, ToolDef],
    *,
    source: str = SOURCE_USER_INPUT,
) -> tuple[list[str], list[RuntimeToolOverride]]:
    """逐个判定本轮临时工具请求,返回 (本轮额外放行的工具名, 全部判定结果)。

    第一个返回值只含"卡白名单之外、本轮新放开"的工具:已在白名单内的请求记一条
    already_allowed 审计但不算覆盖(它本来就能用,重复并入只会让快照口径失真)。
    纯函数,不碰 DB,便于把安全底线用单测钉死。
    """
    card_allowed = set(card_tools)
    granted: list[str] = []
    decisions: list[RuntimeToolOverride] = []
    seen: set[str] = set()
    for raw in requested[:MAX_RUNTIME_TOOLS]:
        name = str(raw or "").strip()
        # 空串与重复项直接忽略:它们不是"用户的一次判断",记进审计只是噪音
        if not name or name in seen:
            continue
        seen.add(name)
        tool = all_tools.get(name)
        if tool is None:
            decisions.append(_reject(name, source, REJECT_UNKNOWN_TOOL))
            continue
        label = tool.display_label
        if name in card_allowed:
            decisions.append(
                RuntimeToolOverride(
                    tool_name=name,
                    source=source,
                    reason=OUTCOME_ALREADY_ALLOWED,
                    accepted=True,
                    label=label,
                )
            )
            continue
        if not tool.runtime_grantable:
            decisions.append(_reject(name, source, REJECT_NOT_GRANTABLE, label=label))
            continue
        if not is_runtime_grantable(tool):
            # 安全底线:临时开关拿不到写权限,也跳不过 confirm
            decisions.append(_reject(name, source, REJECT_WRITE_NOT_ALLOWED, label=label))
            continue
        granted.append(name)
        decisions.append(
            RuntimeToolOverride(
                tool_name=name,
                source=source,
                reason=OUTCOME_GRANTED,
                accepted=True,
                label=label,
            )
        )
    return granted, decisions


def _reject(name: str, source: str, code: str, *, label: str = "") -> RuntimeToolOverride:
    return RuntimeToolOverride(
        tool_name=name,
        source=source,
        reason=code,
        accepted=False,
        reject_reason=code,
        label=label,
    )


def available_runtime_tools(
    card_tools: Sequence[str], all_tools: Mapping[str, ToolDef]
) -> list[dict]:
    """给前端列"本轮可临时开启"的候选:只读、且不在卡白名单里的工具。

    标签/分组取 ToolDef.label / ToolDef.category(注册时已并入本体),前端不硬编码
    工具名到中文的映射——新增工具只改一处。
    """
    card_allowed = set(card_tools)
    items = [
        {
            "name": name,
            "label": tool.display_label,
            "category": tool.category,
            "hint": _describe(tool.description),
            "side_effect": tool.side_effect,
        }
        for name, tool in all_tools.items()
        if name not in card_allowed and is_runtime_grantable(tool)
    ]
    items.sort(key=lambda item: (item["category"], item["name"]))
    return items


def request_payloads(decisions: Sequence[RuntimeToolOverride]) -> list[dict]:
    """判定结果 → 可 json.dumps 的列表(快照与事件流共用同一口径)。"""
    return [decision.to_payload() for decision in decisions]


_HINT_MAX_CHARS = 60


def _describe(description: str) -> str:
    """工具描述压成一句短说明:候选列表是下拉项,放整段说明只会挤爆面板。"""
    text = (description or "").strip().splitlines()[0] if description else ""
    for separator in ("。", ";", ";"):
        head, found, _tail = text.partition(separator)
        if found:
            text = head + separator
            break
    return text if len(text) <= _HINT_MAX_CHARS else text[:_HINT_MAX_CHARS] + "…"
