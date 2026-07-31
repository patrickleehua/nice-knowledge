"""内置 Prompt 任务目录:task → 中文名/作用描述/分类。

为什么要有这份目录:DB 的 prompts 表只有 task 标识(kb.extract.generic 这类
机器名),管理端无法向运营者说明"这条 prompt 是谁在什么链路里用的"。
目录随源码维护(seed 加任务时同步登记,测试对照 seed 文件强制校验覆盖),
GET /admin/prompts 据此附加展示元信息;目录里没有的 task 视为业务方自定义
登记(builtin=False)。

分类约定:category 取 task 前缀(点号前一段)。

SDK 化改造(蓝图 §5.3):目录机制保留,SDK 内置条目只留 agent.approval_review
一条;其余任务条目由子包/宿主经 register_prompt_catalog_entries() 在 import
期注册(P3 的 kb.* 条目届时由 kb 子包注册,业务任务由宿主注册)。
"""

from collections.abc import Mapping
from typing import TypedDict


class CatalogEntry(TypedDict):
    name_zh: str
    description: str
    category: str


def _entry(name_zh: str, description: str, category: str) -> CatalogEntry:
    return {"name_zh": name_zh, "description": description, "category": category}


# 描述口径:一两句话说清「哪个链路 + 干什么」,内容提炼自 seed 正文与调用方
# 语义(agent/permissions/reviewer.py),不做臆测。
BUILTIN_PROMPT_CATALOG: dict[str, CatalogEntry] = {
    # ---- Agent 权限治理 -----------------------------------------------------
    "agent.approval_review": _entry(
        "Agent 操作审批复核",
        "Agent 权限治理链路:对确定性权限边界放行后仍需独立复核的工具动作做"
        "无工具复审,输出 approve/deny/escalate,拿不准升级人工。",
        "agent",
    ),
}


def register_prompt_catalog_entries(entries: Mapping[str, CatalogEntry]) -> None:
    """子包/宿主注册内置任务目录条目(import 期调用,同 task 后注册覆盖)。

    注册进来的条目与 SDK 自带条目同等待遇:管理端视为 builtin,不允许经
    在线目录 API 改写。条目结构:{"name_zh": ..., "description": ..., "category": ...}。
    """
    for task, entry in entries.items():
        BUILTIN_PROMPT_CATALOG[task] = _entry(
            entry["name_zh"], entry["description"], entry["category"]
        )


def lookup(task: str) -> CatalogEntry | None:
    """按 task 查内置目录;未登记(业务方自定义任务)返回 None。"""
    return BUILTIN_PROMPT_CATALOG.get(task)


def category_of(task: str) -> str:
    """任务分类:内置取目录值;未登记任务按点号前缀推导,无点号归入 other。

    推导而非写死,是为了自定义任务也能进前端分类筛选(如业务方自行登记
    billing.xxx 时自动归入 billing 组),不至于全部挤在无分类里。
    """
    entry = lookup(task)
    if entry is not None:
        return entry["category"]
    return task.split(".", 1)[0] if "." in task else "other"
