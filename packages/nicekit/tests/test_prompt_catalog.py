"""内置 Prompt 任务目录单测(SDK 化适配版)。

TF 原文件的两块内容:
1. seed 全覆盖校验——对照 kb/pipeline/quality/render 四个业务 seed 文件,
   这些不进 SDK(kb.* 条目 P3 由 kb 子包注册,业务条目由宿主注册),
   适配为"SDK 内置目录只含 agent.approval_review 一条"的口径锁定;
2. GET /admin/prompts 附加元信息——依赖 admin API(P4 装配),届时随
   api 测试搬运,这里不保留。

另补 register_prompt_catalog_entries 注册机制用例(SDK 化新增扩展点)。
"""

import nicekit.llm.prompt_catalog as catalog_module
from nicekit.llm.prompt_catalog import (
    BUILTIN_PROMPT_CATALOG,
    category_of,
    lookup,
    register_prompt_catalog_entries,
)

# ---------- 目录完整性 ----------


def test_sdk_builtin_catalog_contains_only_approval_review() -> None:
    # SDK 自带的内置任务只有 agent 审批复核一条;其余任务条目由子包/宿主
    # 经 register_prompt_catalog_entries 注册,不允许悄悄回填业务条目
    assert set(BUILTIN_PROMPT_CATALOG) == {"agent.approval_review"}


def test_catalog_entries_complete_and_category_matches_prefix() -> None:
    for task, entry in BUILTIN_PROMPT_CATALOG.items():
        assert entry["name_zh"] and entry["name_zh"] != task, f"{task} 缺中文名"
        assert entry["description"], f"{task} 缺作用描述"
        assert entry["category"] == task.split(".", 1)[0], f"{task} 分类与前缀不一致"


def test_lookup_and_category_for_unknown_tasks() -> None:
    assert lookup("agent.approval_review") is not None
    assert lookup("no.such.task") is None
    # 未登记任务:有点号按前缀归类,无点号归 other
    assert category_of("billing.reconcile") == "billing"
    assert category_of("demand_parse") == "other"
    assert category_of("agent.approval_review") == "agent"


# ---------- 注册机制(SDK 化新增)----------


def test_register_prompt_catalog_entries_adds_builtin_entries() -> None:
    try:
        register_prompt_catalog_entries(
            {
                "kb.extract.generic": {
                    "name_zh": "通用类型抽取",
                    "description": "KB 通用化链路:按实体类型 Schema 逐条抽取。",
                    "category": "kb",
                }
            }
        )
        entry = lookup("kb.extract.generic")
        assert entry is not None
        assert entry["name_zh"] == "通用类型抽取"
        # 注册条目与自带条目同待遇:category_of 取目录值
        assert category_of("kb.extract.generic") == "kb"
    finally:
        catalog_module.BUILTIN_PROMPT_CATALOG.pop("kb.extract.generic", None)


def test_register_same_task_overrides_previous_entry() -> None:
    entries = {
        "host.task": {"name_zh": "旧名", "description": "旧描述", "category": "host"}
    }
    try:
        register_prompt_catalog_entries(entries)
        register_prompt_catalog_entries(
            {
                "host.task": {
                    "name_zh": "新名",
                    "description": "新描述",
                    "category": "host",
                }
            }
        )
        entry = lookup("host.task")
        assert entry is not None and entry["name_zh"] == "新名"
    finally:
        catalog_module.BUILTIN_PROMPT_CATALOG.pop("host.task", None)
