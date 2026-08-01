"""system prompt 分层组装:全局段 → 角色段 → 运行时上下文段 → 运行时能力段。

card.system_prompt 不再是完整 system prompt,只承担角色段职责;全局
工具纪律/输出规则由 global_* 资源永远前置加载,业务方在 admin 改卡
prompt 只能改角色行为,改不掉平台底线。

与 TF(backend/app/services/agent/prompts/assembly.py)的差异(§4 "Prompt 多 root"):
`_GLOBAL_BLOCK_IDS` 固定四元组改为**可配置块清单**(`set_global_block_ids()`)。
宿主可以插自己的全局段(如行业合规声明),但顺序仍由清单一次性决定——
不提供"运行时按条件塞一段"的口子,否则全局段就不再是可审计的固定契约。
"""

import threading
from collections.abc import Sequence

from nicekit.agent.prompts.loader import PromptBlock, render

# 全局段固定顺序:身份 → 基础规则 → 工具纪律 → 输出风格(与 TF 链路一致)
DEFAULT_GLOBAL_BLOCK_IDS: tuple[str, ...] = (
    "global_intro",
    "global_core_rules",
    "global_tool_usage",
    "global_output_style",
)

_lock = threading.RLock()
_global_block_ids: tuple[str, ...] = DEFAULT_GLOBAL_BLOCK_IDS

# 块间分隔符(与 loader.compose 一致);块字符数把分隔符算进后一块,保证求和守恒
_SEPARATOR = "\n\n"


def set_global_block_ids(block_ids: Sequence[str] | None) -> None:
    """替换全局段清单(传 None 恢复 SDK 默认四段)。

    传入的 id 必须都已能被 loader 解析——装配期把宿主 root 注册好再调本函数,
    随后 `validate_prompt_resources()` 会一并校验。
    """
    global _global_block_ids
    with _lock:
        _global_block_ids = (
            DEFAULT_GLOBAL_BLOCK_IDS
            if block_ids is None
            else tuple(str(value) for value in block_ids)
        )


def global_block_ids() -> tuple[str, ...]:
    with _lock:
        return _global_block_ids


def build_system_prompt_blocks(
    role_prompt: str | None,
    capability_context: str | None = None,
    extra_sections: list[tuple[str, str]] | None = None,
) -> tuple[str, list[dict]]:
    """组装运行时 system prompt,同时返回块元信息 [{"id", "chars"}]。

    元信息给运行时上下文快照(runtime_snapshot)用:排障时要能回答"这一轮的
    system prompt 是哪几块拼的、各占多少字符",而不是只看到一个总长度。
    约定 sum(chars) == len(text),块顺序即拼接顺序——分隔符记在后一块头上。

    extra_sections 为运行时派生的上下文段(由注册的 ContextProvider 产出),
    插在角色段之后、能力段之前:它是"当前干什么"的事实,该在角色定义之后读到;
    而能力段是运行时降级说明,必须留在最后一段以保持既有格式不变。
    """
    blocks = [PromptBlock(block_id) for block_id in global_block_ids()]
    role = (role_prompt or "").strip()
    if role:
        blocks.append(PromptBlock("agent_role_custom", {"agent_prompt": role}))
    else:
        blocks.append(PromptBlock("agent_role_generic"))

    # 逐块渲染(等价于 loader.compose,只是额外留住每块文本以便统计字符数)
    parts: list[tuple[str, str]] = []
    for block in blocks:
        rendered = render(block.id, block.values).strip()
        if rendered:
            parts.append((block.id, rendered))
    for block_id, section in extra_sections or []:
        section_text = (section or "").strip()
        if section_text:
            parts.append((block_id, section_text))

    text = _SEPARATOR.join(part for _, part in parts)
    meta = [
        {"id": block_id, "chars": len(part) + (0 if index == 0 else len(_SEPARATOR))}
        for index, (block_id, part) in enumerate(parts)
    ]
    if capability_context:
        # 沿用【运行时能力】前缀追加在最后,保持既有格式与快照口径不变
        suffix = f"{_SEPARATOR}【运行时能力】{capability_context}"
        text = f"{text}{suffix}"
        meta.append({"id": "capability_context", "chars": len(suffix)})
    return text, meta


def build_system_prompt(
    role_prompt: str | None,
    capability_context: str | None = None,
    extra_sections: list[tuple[str, str]] | None = None,
) -> str:
    """组装运行时 system prompt(build_system_prompt_blocks 的薄包装,只取文本)。

    role_prompt 即 card.system_prompt:非空走 agent_role_custom 包裹注入
    (带"不覆盖全局规则"的边界说明),为空回落 agent_role_generic 通用角色。
    capability_context 为运行时能力说明(MCP/技能等动态快照),沿用
    【运行时能力】前缀追加在最后,保持既有格式不变。
    """
    text, _ = build_system_prompt_blocks(role_prompt, capability_context, extra_sections)
    return text
