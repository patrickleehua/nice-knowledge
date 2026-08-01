"""工具调用痕迹(prior tool trace):把本会话调过的工具压成一段导航信息注入 system prompt。

为什么要有它:模型在长会话里最典型的浪费是"忘了自己查过"——同一个 kb_search
换个说法再查一遍、同一个详情查询连查三轮,轮次与 token 全烧在重复检索上;
更糟的是对已经失败过的调用原样重试,把上一轮的死路又走一遍。

与 P0-3 双层预算(context_budget.py)的配合——这是本模块存在的直接原因:
预算的第二层会把较旧的 tool result 的模型可见文本替换成占位符,模型于是既看不到
数据,也逐渐说不清"我到底查过什么";痕迹段正是补这个缺口。两者职责严格分工:
预算管的是"结果正文能留多少",痕迹管的是"调用发生过什么",所以痕迹里只放
工具名 / 关键参数 / 结果要点(条数、成败),绝不复制结果正文——真要细节,
重新调用工具即可(完整输出始终在 chat_messages.tool_output 里可审计)。

与压缩(compression.py)的配合:摘要生效后,被覆盖的消息行不再进模型历史,
那些工具调用连痕迹都不剩。因此本模块刻意基于**全部** ChatMessage 行构建
(调用方传未经 rows_after_summary 过滤的 rows):"查过什么"是跨压缩边界依然
有效的导航信息,而它的成本只有一行文字。

三条铁律(与其他 system prompt 注入段同口径):
- 只增导航不增数据:每条只留要点 + "需要完整结果请重新调用",预算是硬上限;
- 纯函数、不查库、不写库:输入是已经查出来的消息行,输出是一段文本;
- 派生失败不阻塞对话:异常由调用方吞掉(见 service._resolve_tool_trace),
  预算装不下就截断并注明,连声明都装不下就整段不注入。
"""

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nicekit.agent.context_budget import summarize_output_shape
from nicekit.models.chat import ChatMessage

logger = logging.getLogger(__name__)

# 注入段在 system prompt 块清单里的 id(service.py 传参与快照排障共用)
TOOL_TRACE_BLOCK_ID = "tool_trace"

# 注入段总长上限 1200 字符 ≈ 350 token:痕迹是"少查一次工具"的导航,不值得
# 为它挤占对话历史。按每行 60~150 字符估算,足够列 8~15 个工具的要点。
TOOL_TRACE_BUDGET_CHARS = 1200

# 截断尾注:预算装不下全部工具时必须显式声明,否则模型会把"没列出"当成"没查过"
TOOL_TRACE_TRUNCATED_NOTE = "(更早的调用已省略)"

# 段首声明:说清这段是什么、怎么用、以及失败调用不要原样重试
_HEADER = (
    "【本会话已查过】以下是你在本会话已经调用过的工具与结果要点,只是导航"
    "(不是完整结果):同一工具+同一参数不要重复调用;需要完整结果或更多细节时,"
    "请重新调用对应工具。标注「失败」的调用说明此路不通,换参数或换工具,不要原样重试。"
)

# 单行上限:防止某个工具的病态入参(一整段文档塞进 query)独吞整段预算
_LINE_MAX_CHARS = 220
# 单个参数值的显示上限:36 字符正好容得下一个完整 UUID,便于模型据此重查
_VALUE_MAX_CHARS = 40
# 失败原因的显示上限:够看出"是权限还是不存在",不抄完整堆栈
_ERROR_MAX_CHARS = 60
# 每次调用最多展示的参数个数:标识性字段通常 1~2 个,超过就是噪音
_MAX_PARAMS_PER_CALL = 3
# 每个工具最多展示的参数组数:更多组说明模型在扫参数空间,列全没有导航价值
_MAX_GROUPS_PER_TOOL = 3
# 列表值最多展示的元素个数
_MAX_LIST_ITEMS = 3

# 不进痕迹的工具:它们不查数据,只是交互/计划声明,列进来纯属挤占预算。
# 可变集合 + 注册入口:宿主的交互类工具同样是噪音,但 SDK 不可能预知它们的名字。
_NOISE_TOOLS: set[str] = {"ask_user", "plan_update", "update_goal"}

# 优先展示的通用参数名(不硬编码业务字段:*_id / *_ids 由后缀规则通吃,
# 这里只补上"检索类工具的检索词"这一类跨业务的通用标识)。宿主可追加。
_PREFERRED_PARAM_KEYS: set[str] = {
    "query",
    "q",
    "question",
    "keyword",
    "keywords",
    "text",
    "name",
    "title",
    "topic",
    "path",
    "url",
    "prompt",
}

_BUILTIN_NOISE_TOOLS = frozenset(_NOISE_TOOLS)
_BUILTIN_PREFERRED_PARAM_KEYS = frozenset(_PREFERRED_PARAM_KEYS)


def register_noise_tools(*names: str) -> None:
    """追加"不进痕迹"的工具名(交互/计划声明类工具)。"""
    _NOISE_TOOLS.update(name for name in names if name)


def register_preferred_param_keys(*keys: str) -> None:
    """追加优先展示的参数名(宿主的跨工具通用标识字段)。"""
    _PREFERRED_PARAM_KEYS.update(key for key in keys if key)


def noise_tools() -> frozenset[str]:
    return frozenset(_NOISE_TOOLS)


def preferred_param_keys() -> frozenset[str]:
    return frozenset(_PREFERRED_PARAM_KEYS)


def reset_trace_registrations() -> None:
    """恢复内置名单(测试 / 重新装配用)。"""
    _NOISE_TOOLS.clear()
    _NOISE_TOOLS.update(_BUILTIN_NOISE_TOOLS)
    _PREFERRED_PARAM_KEYS.clear()
    _PREFERRED_PARAM_KEYS.update(_BUILTIN_PREFERRED_PARAM_KEYS)


@dataclass
class _CallGroup:
    """同工具 + 同参数 + 同成败的一组调用(重复调用只计数,不重复列出)。"""

    params: str
    result: str
    ok: bool
    count: int = 1
    last_index: int = 0  # 该组最后一次调用在消息序列里的位置(越大越新)


@dataclass
class _ToolTrace:
    """一个工具在本会话的全部调用痕迹。"""

    name: str
    total: int = 0
    last_index: int = 0
    last_at: datetime | None = None
    groups: dict[str, _CallGroup] = field(default_factory=dict)

    @property
    def has_failure(self) -> bool:
        return any(not group.ok for group in self.groups.values())


# ---------- 取值小工具(全部容忍脏数据:JSONB 里什么都可能有) ----------


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_value(value: Any) -> str | None:
    """参数值的显示形式;dict / 空值返回 None 表示"不值得展示"。"""
    if isinstance(value, bool):  # bool 是 int 的子类,必须先判
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{_clip(value, _VALUE_MAX_CHARS)}"'
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        items = [_format_value(item) for item in value[:_MAX_LIST_ITEMS]]
        shown = [item for item in items if item is not None]
        if not shown:
            return f"[{len(value)}项]"
        suffix = "…" if len(value) > len(shown) else ""
        return "[" + ",".join(shown) + suffix + "]"
    return None


def _param_rank(key: str) -> int:
    """参数重要度:标识符 > 通用检索词 > 其余标量。通用规则,不认业务字段名。"""
    if key == "id" or key.endswith(("_id", "_ids")):
        return 0
    if key in _PREFERRED_PARAM_KEYS:
        return 1
    return 2


def _key_params(tool_input: Any) -> str:
    """从入参里提取标识性字段:按重要度取前几个,长值截断。"""
    if not isinstance(tool_input, dict) or not tool_input:
        return "无参数"
    candidates: list[tuple[int, int, str]] = []
    for order, (key, value) in enumerate(tool_input.items()):
        if value is None or value == "" or value == [] or value == {}:
            continue
        text = _format_value(value)
        if text is None:
            continue
        candidates.append((_param_rank(str(key)), order, f"{key}={text}"))
    if not candidates:
        return "无参数"
    candidates.sort(key=lambda item: (item[0], item[1]))
    return " ".join(text for _, _, text in candidates[:_MAX_PARAMS_PER_CALL])


def _row_output(row: ChatMessage) -> Any:
    """取工具结果:优先完整 tool_output(JSONB),回落到模型可见文本反序列化。

    回落分支是为历史行准备的:早期消息可能只有 content;content 还可能是被
    双层预算压过的紧凑视图,那也没关系——形状概要照样能算出条数与键名。
    """
    output = row.tool_output
    if isinstance(output, dict | list):
        return output
    content = row.content
    if not content:
        return None
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def _failure(output: Any) -> str | None:
    """失败判定:与 loop.tool_result_succeeded 同口径(非空 error 即失败)。

    这里刻意复述规则而不 import loop:loop 会连带拉起整个工具注册表,
    而本模块要保持纯函数、可脱库单测。
    """
    if isinstance(output, dict):
        error = output.get("error")
        if error:
            return _clip(error, _ERROR_MAX_CHARS)
    return None


def _result_summary(output: Any, error: str | None) -> str:
    """结果要点:成败 + 条数或关键返回字段(复用双层预算的结构概要口径)。"""
    if error is not None:
        return f"失败:{error}"
    if output is None:
        return "已执行"
    shape = summarize_output_shape(output)
    if shape.get("top_level") == "array":
        return f"命中 {shape.get('item_count', 0)} 条"
    if shape.get("top_level") == "object":
        lengths = shape.get("list_lengths") or {}
        if lengths:
            # 主体通常是最大的那个列表(命中列表 / 结果列表),条数即导航信息
            largest = max(lengths, key=lengths.__getitem__)
            return f"命中 {lengths[largest]} 条"
        keys = [str(key) for key in (shape.get("keys") or [])][:3]
        if keys:
            return "返回 " + "/".join(keys)
    return "已执行"


def _relative_time(then: datetime | None, now: datetime) -> str:
    """最后一次调用距今多久:给模型一个"这条痕迹有多新"的量级,不给精确时刻。"""
    if then is None:
        return ""
    if then.tzinfo is None:  # 容忍无时区的历史行,一律按 UTC 解读
        then = then.replace(tzinfo=UTC)
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# ---------- 聚合 ----------


def _tool_rows(rows: Sequence[ChatMessage]) -> Iterator[tuple[int, ChatMessage, str]]:
    for index, row in enumerate(rows):
        if getattr(row, "role", None) != "tool":
            continue
        name = str(getattr(row, "tool_name", None) or "").strip()
        if not name or name in _NOISE_TOOLS:
            continue
        yield index, row, name


def _aggregate(rows: Sequence[ChatMessage]) -> list[_ToolTrace]:
    """按工具聚合调用;同工具 + 同入参 + 同成败的重复调用合并计数。

    成败进分组键而不被合并掉:同样的参数先失败后成功是重要导航信号
    (说明重试有效),抹平成一条就丢了这个信息。
    """
    traces: dict[str, _ToolTrace] = {}
    for index, row, name in _tool_rows(rows):
        output = _row_output(row)
        error = _failure(output)
        params = _key_params(row.tool_input)
        # 去重签名用完整入参而非展示串:展示串只留了前几个字段,不同调用可能同形
        try:
            raw = json.dumps(row.tool_input, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = str(row.tool_input)
        signature = f"{raw}|{error is None}"

        trace = traces.setdefault(name, _ToolTrace(name=name))
        trace.total += 1
        trace.last_index = index
        if row.created_at is not None:
            trace.last_at = row.created_at
        group = trace.groups.get(signature)
        if group is None:
            trace.groups[signature] = _CallGroup(
                params=params,
                result=_result_summary(output, error),
                ok=error is None,
                last_index=index,
            )
        else:
            group.count += 1
            group.last_index = index
            group.result = _result_summary(output, error)  # 以最后一次为准
    return list(traces.values())


# ---------- 渲染 ----------


def _render_line(trace: _ToolTrace, now: datetime) -> str:
    # 组内排序:失败在前(此路不通最该先看到),其余按最近调用倒序
    groups = sorted(trace.groups.values(), key=lambda g: (g.ok, -g.last_index))
    parts: list[str] = []
    for group in groups[:_MAX_GROUPS_PER_TOOL]:
        repeat = f" ×{group.count}" if group.count > 1 else ""
        parts.append(f"{group.params}{repeat} → {group.result}")
    if len(groups) > _MAX_GROUPS_PER_TOOL:
        parts.append("…")
    when = _relative_time(trace.last_at, now)
    head = f"- {trace.name} ×{trace.total}" + (f"(最近 {when})" if when else "")
    return _clip(f"{head}:{';'.join(parts)}", _LINE_MAX_CHARS)


def _pack(lines: list[str], budget_chars: int) -> str:
    """按预算装行:先试着全装下,装不下再留出尾注空间重装一次。

    截断顺序由调用方给定(失败优先 + 最近优先);装不下的行跳过而不是就此打住,
    后面更短的行还能塞进来,同样预算下能多留几条导航。
    """

    def fit(reserve: int) -> tuple[list[str], bool]:
        used = len(_HEADER)
        picked: list[str] = []
        dropped = False
        for line in lines:
            cost = len("\n") + len(line)
            if used + cost + reserve > budget_chars:
                dropped = True
                continue
            used += cost
            picked.append(line)
        return picked, dropped

    picked, dropped = fit(0)
    if not dropped:
        return "\n".join([_HEADER, *picked])
    picked, _ = fit(len("\n") + len(TOOL_TRACE_TRUNCATED_NOTE))
    if not picked:
        # 预算连一行都装不下:整段不注入,不留"只有声明"的空壳
        return ""
    return "\n".join([_HEADER, *picked, TOOL_TRACE_TRUNCATED_NOTE])


def build_tool_trace(
    rows: list[ChatMessage],
    budget_chars: int = TOOL_TRACE_BUDGET_CHARS,
    *,
    now: datetime | None = None,
) -> str:
    """从会话消息行构建【本会话已查过】注入段;没有可列的调用时返回空串。

    rows 应为该会话的**全部**消息行(含被摘要覆盖的),理由见模块注释。
    now 仅供测试注入固定时刻,生产调用不传。
    """
    traces = _aggregate(rows)
    if not traces:
        return ""
    if budget_chars <= len(_HEADER):  # 预算连声明都装不下:整段丢弃
        return ""
    reference = now or datetime.now(UTC)
    # 行序 = 截断优先级:有失败的工具在前(避免模型重蹈覆辙),其余按最近调用倒序
    ordered = sorted(traces, key=lambda t: (not t.has_failure, -t.last_index))
    return _pack([_render_line(trace, reference) for trace in ordered], budget_chars)
