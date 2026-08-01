"""工具结果双层预算(纯函数,不碰 DB)。

KB 检索 / 外部数据拉取等工具的输出很重,长会话必然撞上下文墙。这里提供两层防线:

第一层(单条):工具结果超过单条阈值时,不再简单砍尾巴,而是生成"紧凑视图"=
    结构概要(顶层键、列表长度、示例首条)+ 截断正文 + 审计说明。模型仍能
    看清数据形状与首部内容,需要全量时可用更精确的参数重查。
第二层(总量):每次模型请求前统计所有 tool 消息的 content 总字符;超总预算时
    从最旧的 tool result 开始把 content 替换为占位符(原工具名 + 输出形状概要 +
    字符数),保留最近 keep_recent 条完整。消息本身与 tool_call_id 一律不删——
    provider 协议要求 tool_call 与 result 一一对应,删消息会产生非法对话。

两层都只影响"模型可见文本";完整工具输出始终保留在 chat_messages.tool_output
(JSONB)供审计与前端展示,压缩不可逆但可审计。
"""

import json
from typing import Any

# 单条阈值 4000 字符:与 loop.TOOL_OUTPUT_MAX_CHARS 沿用同一经验值——KB 检索
# 单条命中约 300~800 字符,4000 字符能装下 top 5~10 条,足够模型做下一步决策;
# 再大就该靠"重查更精确参数"而不是塞满上下文。
SINGLE_TOOL_OUTPUT_MAX_CHARS = 4000

# 总量预算 60000 字符:按混合中英文 JSON 估算约 15k(英文 ~4 字符/token)到
# 60k(纯中文 ~1 字符/token)token。取 60000 字符可保证即便最坏情况(纯中文),
# 工具结果层也不超过主流 128k 上下文模型的一半,给系统提示、对话文本与模型
# 输出留足余量;正常 JSON 输出则只占 ~15k token,余量更宽。
TOOL_RESULT_TOTAL_BUDGET_CHARS = 60000

# 超预算时保留最近 5 条完整 tool result:近期结果是模型当下决策的直接依据,
# 更旧的结果通常已被后续对话消化,只留形状概要即可。
TOOL_RESULT_KEEP_RECENT = 5

# 占位符标记:apply_tool_result_budget 幂等的依据(见 _is_budget_placeholder)。
_PLACEHOLDER_NOTE = "[旧工具结果已按总预算清出模型上下文]"


def _brief(item: Any, max_chars: int = 200) -> str:
    """列表示例首条的极简投影:让模型知道条目长什么样,不为省字符牺牲结构感。"""
    text = json.dumps(item, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def summarize_output_shape(value: Any) -> dict:
    """结构概要:顶层键、列表长度、示例首条。压缩后模型据此判断是否值得重查。"""
    if isinstance(value, list):
        shape: dict[str, Any] = {"top_level": "array", "item_count": len(value)}
        if value:
            shape["first_item"] = _brief(value[0])
        return shape
    if isinstance(value, dict):
        shape = {"top_level": "object", "keys": list(value)[:20]}
        list_lengths = {
            key: len(item) for key, item in value.items() if isinstance(item, list)
        }
        if list_lengths:
            shape["list_lengths"] = list_lengths
            # 示例首条取最大的列表:重型工具输出的主体通常是它(命中列表/结果列表)
            largest = max(list_lengths, key=list_lengths.__getitem__)
            if value[largest]:
                shape["first_item"] = _brief(value[largest][0])
        return shape
    return {"top_level": type(value).__name__}


def _summarize_text_shape(content: str) -> dict:
    """对已序列化文本做形状概要:能解析回 JSON 则复用结构概要,否则按纯文本计行。"""
    try:
        return summarize_output_shape(json.loads(content))
    except (TypeError, ValueError):
        return {"top_level": "text", "line_count": content.count("\n") + 1}


def compact_tool_output(
    name: str, output: dict, max_chars: int = SINGLE_TOOL_OUTPUT_MAX_CHARS
) -> str:
    """第一层:单条工具结果的模型可见文本。

    未超阈值走现有序列化口径(与 loop.truncate_output 的正常分支一致);
    超阈值生成紧凑视图 JSON,而不是裸截断——裸截断产生非法 JSON 片段,
    模型容易把断口当成数据本身。
    """
    text = json.dumps(output, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    payload = {
        "tool_name": name,
        "result_compacted": True,
        "original_char_count": len(text),
        "shape": summarize_output_shape(output),
        "content_prefix": text[:max_chars],
        "note": (
            "结果过长已压缩为紧凑视图;完整结果已存审计"
            "(chat_messages.tool_output),可用更精确的参数重新查询。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _is_budget_placeholder(content: str | None) -> bool:
    """识别第二层占位符,保证 apply_tool_result_budget 幂等(不二次包装)。"""
    if not content or not content.lstrip().startswith("{"):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("tool_result_compacted") is True
        and _PLACEHOLDER_NOTE in str(payload.get("note") or "")
    )


def _budget_placeholder(msg: dict) -> str:
    content = str(msg.get("content") or "")
    payload = {
        "tool_result_compacted": True,
        "reason": "较旧的工具结果超出模型上下文总预算",
        "tool_name": msg.get("name"),
        "original_char_count": len(content),
        "shape": _summarize_text_shape(content),
        "note": (
            f"{_PLACEHOLDER_NOTE} 完整结果仍在审计存储"
            "(chat_messages.tool_output),需要时可重新调用工具查询。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def apply_tool_result_budget(
    messages: list[dict],
    total_budget_chars: int = TOOL_RESULT_TOTAL_BUDGET_CHARS,
    keep_recent: int = TOOL_RESULT_KEEP_RECENT,
) -> list[dict]:
    """第二层:每次模型请求前对 role=tool 消息的 content 做总量预算。

    超预算时从最旧开始替换为占位符,保留最近 keep_recent 条完整;不删消息、
    不动 tool_call_id。被替换的消息用浅拷贝承接新 content——loop 里同一个
    dict 同时挂在 messages 与 outcome.new_messages(落库路径)上,原地改写
    会污染持久化内容。
    """
    if total_budget_chars <= 0:
        return messages
    tool_indexes = [
        index
        for index, msg in enumerate(messages)
        if msg.get("role") == "tool" and msg.get("content")
    ]
    total_chars = sum(len(str(messages[index]["content"])) for index in tool_indexes)
    if total_chars <= total_budget_chars or len(tool_indexes) <= keep_recent:
        return messages

    out = list(messages)
    changed = False
    for index in tool_indexes[: len(tool_indexes) - keep_recent]:
        msg = out[index]
        if _is_budget_placeholder(str(msg.get("content"))):
            continue  # 已是占位符:幂等,不二次处理
        out[index] = {**msg, "content": _budget_placeholder(msg)}
        changed = True
    return out if changed else messages
