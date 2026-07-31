"""自定义 Prompt 任务在线目录登记单测(TF test_prompt_catalog_entries.py)。

整文件 skip:全部用例走 PUT/DELETE /admin/prompt-catalog 与 GET /admin/prompts
端点(假身份 + 假 DB 会话),依赖 admin API 路由与 app 装配——蓝图 §5.7 的
P4 阶段才搬运 api/ 与 app_factory。届时把 TF 原用例连同 admin 路由一起迁入,
仅需适配 import(app.* → nicekit.*)与 Role 词表(SALES → member)。
"""

import pytest

pytest.skip(
    "依赖 admin API(/admin/prompt-catalog、/admin/prompts)与 app 装配,"
    "P4 runtime/api 阶段随路由搬运后恢复",
    allow_module_level=True,
)
