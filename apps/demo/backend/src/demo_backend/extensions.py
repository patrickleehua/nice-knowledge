"""SDK 扩展点示例(MIGRATION-PLAN §4)。

这个文件是 demo 的重点:它演示宿主**在不改 SDK 一行代码**的前提下,怎么把
自己的业务接进平台。四个扩展点各给一个可运行的最小实现:

1. **自定义工具** ``echo_note`` —— 宿主自建 ``ToolRegistry``,经
   ``create_app(tool_registry=...)`` 并入 SDK 默认表,agent 卡勾上即可调用;
2. **自定义实体类型** ``product`` —— 一份 entity type spec,交给
   ``bootstrap_platform(entity_type_specs=...)`` seed;它自带
   ``filterable_fields``,于是 KB 的声明式结构化召回立刻认得 ``category``/
   ``price`` 这些字段;
3. **ContextProvider** ``demo_workspace`` —— 每轮对话往 system prompt 注入一段
   "此刻工作区状态";严格遵守"超预算整段丢弃 / 出错就不注入"的三铁律;
4. **ResourceResolver** ``product_scope`` —— 把工具参数里的 ``product_id``
   解析成作用域根,让权限层能判断"这次调用动的是哪个业务对象"。

注册入口是 :func:`install_demo_extensions`,幂等,供 main 与 seed 共用。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import UUID

from nicekit.agent.permissions.actions import register_resource_resolver
from nicekit.agent.service import (
    ContextBlock,
    ContextRequest,
    register_context_provider,
)
from nicekit.agent.tools import ToolContext, ToolRegistry, tool_permission
from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolReversibility,
    ToolRisk,
)
from nicekit.models.kb import KnowledgeBase
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 扩展点 1:自定义工具
# ---------------------------------------------------------------------------

#: 宿主自己的工具表。刻意不直接往 ``nicekit.agent.tools.default_registry`` 上挂:
#: 独立一份便于宿主自测(工具全集 = SDK 内置 + 本表),装配期再合并。
demo_registry = ToolRegistry("demo")

#: 进程内的便签本(demo 用;真实宿主这里会是自己的表)
_notes: list[str] = []


#: 工具入参 schema 的写法约束(与 SDK 内置工具同口径):所有字段进 required,
#: additionalProperties=False —— 可选参数用 ``["string", "null"]` 表达,而不是
#: 从 required 里拿掉(严格 schema 让模型的参数幻觉当场失败,而不是静默丢参)。
_ECHO_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "tag": {"type": ["string", "null"]},
    },
    "required": ["text", "tag"],
    "additionalProperties": False,
}


@demo_registry.register(
    "echo_note",
    "把一段文字记到本次部署的演示便签本里并原样回显,用于验证宿主自定义工具已接通。",
    _ECHO_NOTE_SCHEMA,
    permission=tool_permission(
        # 七轴权限元数据:登记时就必须给全,坏元数据在 import 期就炸
        ToolEffect.WRITE,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("text",),  # material_arguments:进审批卡片展示的实质参数
    ),
    label="演示便签",
    category="演示",
)
async def echo_note_tool(ctx: ToolContext, args: dict) -> dict:
    text = str(args.get("text") or "").strip()
    if not text:
        return {"error": "text 不能为空"}
    tag = str(args.get("tag") or "note")
    entry = f"[{tag}] {text}"
    _notes.append(entry)
    return {"echo": text, "tag": tag, "total_notes": len(_notes)}


def demo_notes() -> list[str]:
    """便签快照(ContextProvider 与自测读它)。"""
    return list(_notes)


# ---------------------------------------------------------------------------
# 扩展点 2:自定义实体类型(非旅游:商品)
# ---------------------------------------------------------------------------

#: 一份行业实体类型 spec。``filterable_fields`` 是关键:KB 的泛化结构化召回
#: (kb/search.py 的 StructuredFilter)按它校验并打分,声明了才可被过滤。
PRODUCT_ENTITY_TYPE: dict = {
    "type_key": "product",
    "display_name": "商品",
    "description": "宿主自定义的示例实体类型:目录里的一件可售商品。",
    "field_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "sku": {"type": ["string", "null"]},
            "category": {"type": ["string", "null"]},
            "price": {"type": ["number", "null"]},
            "summary": {"type": ["string", "null"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "filterable_fields": [
        {"field": "name", "type": "text", "label": "名称"},
        {"field": "category", "type": "text", "label": "类目"},
        {"field": "price", "type": "number", "label": "价格"},
    ],
    "card_template": "商品:{name}(类目 {category},参考价 {price})。{summary}",
    "review_policy": "ai",
}


def demo_entity_type_specs() -> list[dict]:
    """交给 ``bootstrap_platform(entity_type_specs=...)`` 的类型清单。

    必须把 SDK 的 ``concept`` 兜底类型一并带上 —— 传了 specs 就是全量覆盖,
    漏掉兜底类型会让实体绑定白名单缺一块。
    """
    from nicekit.kb.entity_types import DEFAULT_ENTITY_TYPE_SPECS

    return [*DEFAULT_ENTITY_TYPE_SPECS, PRODUCT_ENTITY_TYPE]


# ---------------------------------------------------------------------------
# 扩展点 3:ContextProvider
# ---------------------------------------------------------------------------


class DemoWorkspaceContextProvider:
    """每轮往 system prompt 注入"此刻工作区状态"。

    三铁律(照抄 TF working_context 的裁决):只读不写、超预算整段丢弃、
    任何异常降级为"这轮不注入"。SDK 侧已按 ``timeout_seconds`` 兜超时,
    provider 自身只要保证不抛出脏数据即可。
    """

    name = "demo_workspace"
    budget_chars = 800
    timeout_seconds = 2.0

    async def derive(self, session: AsyncSession, ctx: ContextRequest) -> ContextBlock | None:
        kb_count = (
            await session.execute(
                select(func.count()).select_from(KnowledgeBase).where(
                    KnowledgeBase.org_id == ctx.org_id
                )
            )
        ).scalar_one()
        notes = demo_notes()
        lines = [
            "【演示工作区】",
            f"- 本组织可见知识库:{kb_count} 个",
            f"- 演示便签条数:{len(notes)}",
        ]
        if notes:
            lines.append(f"- 最近一条便签:{notes[-1][:120]}")
        text = "\n".join(lines)
        if len(text) > ctx.budget_chars:
            # 截断一半的上下文比没有更糟 —— 直接放弃本轮注入
            return None
        return ContextBlock(block_id=self.name, text=text)


# ---------------------------------------------------------------------------
# 扩展点 4:ResourceResolver
# ---------------------------------------------------------------------------


class DemoProductResourceResolver:
    """把工具参数里的 ``product_id`` 解析成宿主作用域根。

    真实宿主这里会查自己的业务表确认归属;demo 只做格式解析:参数缺失或不归
    自己管一律返回 None(交给下一个 resolver / 回落会话绑定的作用域),
    绝不猜。
    """

    async def resolve_scope(
        self,
        session: AsyncSession,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> UUID | None:
        raw = arguments.get("product_id")
        if not raw:
            return None
        try:
            return UUID(str(raw))
        except ValueError:
            # 判定"这组参数有问题"应当抛 ResourceResolutionError 让策略层 fail-closed;
            # demo 里退一步返回 None,保持示例简单。
            logger.debug("product_id 不是合法 UUID,交给下一个 resolver:%r", raw)
            return None


# ---------------------------------------------------------------------------
# 统一注册入口
# ---------------------------------------------------------------------------

_installed = False


def install_demo_extensions() -> ToolRegistry:
    """注册全部示例扩展点(幂等),返回宿主工具表供 ``create_app`` 合并。"""
    global _installed
    if _installed:
        return demo_registry
    register_context_provider(DemoWorkspaceContextProvider())
    register_resource_resolver(DemoProductResourceResolver())
    _installed = True
    return demo_registry


__all__ = [
    "PRODUCT_ENTITY_TYPE",
    "DemoProductResourceResolver",
    "DemoWorkspaceContextProvider",
    "demo_entity_type_specs",
    "demo_notes",
    "demo_registry",
    "install_demo_extensions",
]
