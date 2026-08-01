"""KB prompt seed 裁剪与目录注册口径(MIGRATION-PLAN §5.5)。

锁定三件事:
1. 四条行业专用抽取 task 已删除,结构化抽取只剩 kb.extract.generic;
2. 保留的 prompt 正文领域中立(不得残留旅游/业务口吻);
3. 任务目录条目与 seed 逐 task 对齐,且**不在 import 期**污染 LLM 内置目录
   ——由宿主在装配期显式调用 register_kb_prompt_catalog()。
"""

import nicekit.llm.prompt_catalog as catalog_module
from nicekit.kb.prompts_seed import (
    KB_EXTRACT_PROMPTS,
    KB_PROMPT_CATALOG_ENTRIES,
    register_kb_prompt_catalog,
)
from nicekit.llm.prompt_catalog import lookup

_REMOVED_TASKS = {
    "kb.extract.hotel",
    "kb.extract.cost",
    "kb.extract.poi",
    "kb.extract.route_template",
}

# SDK 内不允许出现的行业词面(§7 验收口径的 KB 侧收窄版)。
# 注:不查 "quote"/"project" —— 会误伤 evidence_quote 与 projection 这类通用术语。
_FORBIDDEN_WORDS = (
    "旅行社",
    "旅游",
    "目的地",
    "酒店",
    "景点",
    "线路",
    "行程",
    "签证",
    "报价",
    "供应商",
    "customer",
    "itinerary",
)


def test_industry_specific_extraction_tasks_are_dropped() -> None:
    assert _REMOVED_TASKS.isdisjoint(KB_EXTRACT_PROMPTS)
    assert "kb.extract.generic" in KB_EXTRACT_PROMPTS
    assert "kb.extract.entities" in KB_EXTRACT_PROMPTS


def test_seeded_prompts_are_domain_neutral() -> None:
    for task, (version, content) in KB_EXTRACT_PROMPTS.items():
        assert version >= 1, task
        assert content.strip(), task
        for word in _FORBIDDEN_WORDS:
            assert word not in content, f"{task} 残留行业措辞:{word}"


def test_catalog_entries_cover_every_seeded_task() -> None:
    assert set(KB_PROMPT_CATALOG_ENTRIES) == set(KB_EXTRACT_PROMPTS)
    for task, entry in KB_PROMPT_CATALOG_ENTRIES.items():
        assert entry["category"] == "kb", task
        assert entry["name_zh"] and entry["description"], task
        for word in _FORBIDDEN_WORDS:
            assert word not in entry["description"], f"{task} 目录描述残留:{word}"


def test_catalog_registration_is_explicit_not_import_time() -> None:
    # import prompts_seed 不得污染全局目录(否则 SDK 内置目录口径测试会被带偏)
    assert lookup("kb.extract.generic") is None
    try:
        register_kb_prompt_catalog()
        entry = lookup("kb.extract.generic")
        assert entry is not None and entry["category"] == "kb"
    finally:
        for task in KB_PROMPT_CATALOG_ENTRIES:
            catalog_module.BUILTIN_PROMPT_CATALOG.pop(task, None)
