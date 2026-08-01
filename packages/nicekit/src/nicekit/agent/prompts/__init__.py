"""Agent 系统 Prompt 资源包:resources/*.md 资源文件 + 加载/渲染/组装。

搬自 TF backend/app/services/agent/prompts/。SDK 化改造(§5.4 prompts 模板化):
资源文案全部去业务化,产品身份走 `{{product_identity}}` 变量;资源目录支持
多 root(宿主目录覆盖 SDK 内置);全局块清单可配置。
"""

from nicekit.agent.prompts.assembly import (
    DEFAULT_GLOBAL_BLOCK_IDS,
    build_system_prompt,
    build_system_prompt_blocks,
    global_block_ids,
    set_global_block_ids,
)
from nicekit.agent.prompts.loader import (
    BUILTIN_RESOURCE_DIR,
    PromptBlock,
    PromptResource,
    PromptResourceError,
    compose,
    list_prompts,
    load_prompt,
    prompt_roots,
    prompt_variable_defaults,
    register_prompt_root,
    render,
    reset_prompt_roots,
    reset_prompt_variables,
    set_prompt_variable,
    validate_prompt_resources,
)

__all__ = [
    "BUILTIN_RESOURCE_DIR",
    "DEFAULT_GLOBAL_BLOCK_IDS",
    "PromptBlock",
    "PromptResource",
    "PromptResourceError",
    "build_system_prompt",
    "build_system_prompt_blocks",
    "compose",
    "global_block_ids",
    "list_prompts",
    "load_prompt",
    "prompt_roots",
    "prompt_variable_defaults",
    "register_prompt_root",
    "render",
    "reset_prompt_roots",
    "reset_prompt_variables",
    "set_global_block_ids",
    "set_prompt_variable",
    "validate_prompt_resources",
]
