"""SDK 内置通用工具的登记汇总与元数据完整性(nicekit/agent/builtin_tools.py)。

这是 TF `validate_builtin_tool_registry()`(tools.py:947,catalog/metadata/registry
三方完全相等断言)在 SDK 里的替身。三方相等那套假设"只有一个来源",SDK 必须
允许宿主并入自己的工具,因此改成:**每个已登记工具都必须自带完整且自洽的元数据**,
并把内置全集钉成一份可读清单——新增/改名/改档位都要显式改这里。

另覆盖:去领域化(不得残留业务词)、runtime_grantable 与 emit_start_progress
两个标志位的取值、以及几个不依赖 DB 的执行体分支。
"""

import re

import pytest

import nicekit.agent.builtin_tools as bt
from nicekit.agent.tools import (
    ToolError,
    default_registry,
    validate_tool_permission_spec,
)
from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
)

# 内置工具的完整登记清单:名称 → (显示名, 分组, effect, delegation, scope)。
# 权限档位取自 TF BUILTIN_TOOL_PERMISSIONS(:315-904),PROJECT 档按 §5.4 更名
# 为 RESOURCE;label/category 取自 TF TOOL_META(:227-291)并中性化。
EXPECTED: dict[str, tuple[str, str, str, str, str]] = {
    "kb_list": ("知识库列表", "知识库", "read", "automatic", "organization"),
    "kb_search": ("知识库检索", "知识库", "read", "automatic", "organization"),
    "kb_image_inspect": ("核验知识图片", "知识库", "read", "reviewable", "resource"),
    "retrieval_get": ("检索依据", "知识库", "read", "automatic", "resource"),
    "plan_update": ("更新计划", "计划", "write", "automatic", "session"),
    "update_goal": ("更新会话目标", "计划", "write", "automatic", "session"),
    "cronjob": ("定时任务", "定时任务", "write", "user_required", "organization"),
    "memory_search": ("检索长期记忆", "记忆", "read", "automatic", "organization"),
    "memory_write": ("记录长期记忆", "记忆", "write", "automatic", "organization"),
    "memory_forget": ("标记记忆失效", "记忆", "write", "reviewable", "organization"),
    "ask_user": ("向用户确认", "交互", "read", "automatic", "session"),
    "skills_list": ("技能列表", "技能", "read", "automatic", "session"),
    "skill_view": ("读取技能", "技能", "read", "automatic", "session"),
    "web_search": ("联网搜索", "联网", "read", "automatic", "session"),
    "web_fetch": ("网页读取", "联网", "read", "automatic", "session"),
    "image_generate": ("生成图片", "联网", "write", "reviewable", "session"),
    "weather_get": ("天气预报", "联网", "read", "automatic", "session"),
}


def test_builtin_tool_roster_matches_the_declared_set() -> None:
    """内置全集必须与清单逐一对齐:漏登记、多登记、改名都要在这里显式失败。"""
    assert set(bt.BUILTIN_TOOL_NAMES) == set(EXPECTED)
    assert len(bt.BUILTIN_TOOL_NAMES) == len(EXPECTED)  # 无重复
    assert set(EXPECTED) <= default_registry.names()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_display_and_permission_metadata_match_the_roster(name: str) -> None:
    label, category, effect, delegation, scope = EXPECTED[name]
    tool = default_registry.require(name)
    assert tool.display_label == label
    assert tool.category == category
    permission = tool.permission
    assert permission is not None
    assert permission.effect.value == effect
    assert permission.delegation.value == delegation
    assert permission.scope.value == scope
    # side_effect 由 permission.effect 派生,不允许两处打架
    assert tool.side_effect == ("read" if effect == "read" else "write")
    # confirm 位与委派档同源(reviewable / user_required → 人在环)
    assert tool.confirm is (delegation in {"reviewable", "user_required"})


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_metadata_is_self_consistent_and_schema_is_strict(name: str) -> None:
    """替代 TF 的三方相等断言:登记项自身必须过全部一致性规则。"""
    tool = default_registry.require(name)
    validate_tool_permission_spec(name, tool.schema, tool.permission)
    assert tool.description.strip()
    assert tool.schema["type"] == "object"
    # strict 交集:全字段 required + additionalProperties false
    assert tool.schema["additionalProperties"] is False
    assert set(tool.schema["required"]) == set(tool.schema["properties"])


def test_material_arguments_exist_in_schema() -> None:
    for name in EXPECTED:
        tool = default_registry.require(name)
        declared = set(tool.schema["properties"])
        assert tool.permission.material_arguments <= declared, name


# ---------------------------------------------------------------------------
# 标志位:替代 TF 的两处工具名硬编码
# ---------------------------------------------------------------------------


def test_runtime_grantable_flag_replaces_the_hardcoded_denylist() -> None:
    """替代 TF runtime_tools.NOT_RUNTIME_GRANTABLE 名单。"""
    blocked = {
        name for name in EXPECTED if not default_registry.require(name).runtime_grantable
    }
    assert blocked == {"skills_list", "skill_view", "ask_user"}


def test_only_image_generate_suppresses_start_progress() -> None:
    """替代 TF loop.py:818/:1118 对 image_generate 的工具名特判。"""
    silent = {
        name
        for name in EXPECTED
        if not default_registry.require(name).emit_start_progress
    }
    assert silent == {"image_generate"}


def test_no_builtin_tool_declares_a_stage() -> None:
    """stage 是宿主事件钩子用的自声明位;SDK 通用工具一律不占。"""
    assert all(default_registry.require(name).stage is None for name in EXPECTED)


# ---------------------------------------------------------------------------
# 去领域化:工具描述里不得残留源仓库的业务词
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"project|customer|itinerary|quote|\bota\b|render"
    r"|旅行社|旅游|行程|报价|酒店|景点|签证|线路|客户",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_descriptions_carry_no_domain_wording(name: str) -> None:
    tool = default_registry.require(name)
    text = f"{tool.description} {tool.display_label} {tool.category}"
    assert not _FORBIDDEN.search(text), f"{name} 的文案仍带业务词:{text[:80]}"


# ---------------------------------------------------------------------------
# 不依赖 DB 的执行体分支
# ---------------------------------------------------------------------------


def test_validate_plan_steps_rejects_bad_input() -> None:
    with pytest.raises(ToolError, match="非空数组"):
        bt.validate_plan_steps([])
    with pytest.raises(ToolError, match="id 重复"):
        bt.validate_plan_steps(
            [
                {"id": "a", "title": "一", "status": "pending"},
                {"id": "a", "title": "二", "status": "pending"},
            ]
        )
    with pytest.raises(ToolError, match="status"):
        bt.validate_plan_steps([{"id": "a", "title": "一", "status": "flying"}])
    with pytest.raises(ToolError, match=str(bt.PLAN_MAX_STEPS)):
        bt.validate_plan_steps(
            [
                {"id": f"s{i}", "title": "x", "status": "pending"}
                for i in range(bt.PLAN_MAX_STEPS + 1)
            ]
        )
    ok = bt.validate_plan_steps([{"id": "a", "title": "一", "status": "done"}])
    assert ok == [{"id": "a", "title": "一", "status": "done", "note": None}]


class _Ctx:
    """ToolContext 的最小读取面(这些用例只走参数校验分支,不碰 session)。"""

    def __init__(self, skills=()):
        self.skills = tuple(skills)
        self.session = None
        self.org_id = None
        self.user_id = None
        self.chat_session = None


async def test_weather_get_validates_arguments() -> None:
    executor = default_registry.require("weather_get").executor
    with pytest.raises(ToolError, match="location"):
        await executor(_Ctx(), {"location": " ", "days": None, "start_date": None})
    with pytest.raises(ToolError, match="days"):
        await executor(_Ctx(), {"location": "上海", "days": 30, "start_date": None})
    with pytest.raises(ToolError, match="ISO 日期"):
        await executor(
            _Ctx(), {"location": "上海", "days": 3, "start_date": "2026/01/02"}
        )


async def test_skill_view_rejects_unbound_skill() -> None:
    executor = default_registry.require("skill_view").executor
    result = await executor(_Ctx(skills=("alpha",)), {"slug": "beta", "path": None})
    assert "未绑定" in result["error"]


async def test_ask_user_normalizes_questions() -> None:
    executor = default_registry.require("ask_user").executor
    result = await executor(
        _Ctx(),
        {
            "questions": [
                {
                    "id": "q1",
                    "header": "预算",
                    "question": "预算区间?",
                    "options": [
                        {"label": "低", "description": None},
                        {"label": "高", "description": None},
                    ],
                    "multi_select": False,
                }
            ]
        },
    )
    assert [q["id"] for q in result["questions"]] == ["q1"]


# ---------------------------------------------------------------------------
# 扩展点:retrieval_get 的检索快照由宿主提供
# ---------------------------------------------------------------------------


async def test_retrieval_get_reports_empty_without_provider() -> None:
    bt.set_retrieval_snapshot_provider(None)
    result = await default_registry.require("retrieval_get").executor(_Ctx(), {})
    assert result["empty"] is True
    assert "还没有" in result["message"]


async def test_retrieval_get_reads_host_snapshot_and_trims_groups() -> None:
    async def provider(_session, _org_id, _chat_session):
        return {
            "empty": False,
            "counts": {"chunks": 9},
            "retrieval": {
                "query": "配额政策",
                "chunks": [
                    {"source": f"doc-{i}", "media_refs": []} for i in range(9)
                ],
                "entities": [{"source": "e1", "media_refs": []}],
            },
        }

    bt.set_retrieval_snapshot_provider(provider)
    try:
        result = await default_registry.require("retrieval_get").executor(_Ctx(), {})
    finally:
        bt.set_retrieval_snapshot_provider(None)

    assert result["empty"] is False
    assert result["query"] == "配额政策"
    assert set(result["retrieval"]) == set(bt.RETRIEVAL_GROUPS)
    assert len(result["retrieval"]["chunks"]) == 5  # 每组前 5 条
    assert result["retrieval"]["pages"] == []  # 快照没这一组 → 空表而不是 KeyError


def test_retrieval_groups_are_generic() -> None:
    assert bt.RETRIEVAL_GROUPS == ("pages", "chunks", "entities")


# ---------------------------------------------------------------------------
# 权限档位的语义抽查(不是重复上面的表,是钉住"为什么是这一档")
# ---------------------------------------------------------------------------


def test_cronjob_always_requires_a_human() -> None:
    """建定时任务 = 授权未来无人值守地反复执行,USER_REQUIRED 让 full_access 也挡住。"""
    permission = default_registry.require("cronjob").permission
    assert permission.delegation is ToolDelegation.USER_REQUIRED
    assert ToolCategory.WORKFLOW in permission.categories
    assert default_registry.require("cronjob").confirm is True


def test_network_tools_declare_network_category() -> None:
    for name in ("web_search", "web_fetch", "image_generate", "weather_get"):
        assert ToolCategory.NETWORK in default_registry.require(name).permission.categories
    # 花钱的两个还要标外部成本(EXTERNAL_COST 依赖 NETWORK,由校验器守住)
    for name in ("image_generate", "kb_image_inspect"):
        assert (
            ToolCategory.EXTERNAL_COST
            in default_registry.require(name).permission.categories
        )


def test_memory_write_is_automatic_while_forget_needs_review() -> None:
    write = default_registry.require("memory_write").permission
    forget = default_registry.require("memory_forget").permission
    assert write.delegation is ToolDelegation.AUTOMATIC  # 每记一条都弹确认没法用
    assert forget.delegation is ToolDelegation.REVIEWABLE  # 删的是组织资产
    assert write.effect is ToolEffect.WRITE and forget.effect is ToolEffect.WRITE
    assert write.scope is PermissionScope.ORGANIZATION


def test_retrieval_snapshot_availability_tracks_provider_injection() -> None:
    """工具是否该暴露给模型,取决于宿主有没有把数据源接上。

    未接时 retrieval_get 必然返回 empty。实测模型会在 kb_search 之后调它去取
    全文,拿到"还没有成功的检索记录"后再重新组织回答——白白多一次模型往返,
    而且那句提示本身有误导性(它明明刚检索过)。
    """
    bt.set_retrieval_snapshot_provider(None)
    assert bt.retrieval_snapshot_available() is False

    async def provider(_session, _org_id, _chat_session):
        return {"empty": False, "counts": {}, "retrieval": {}}

    bt.set_retrieval_snapshot_provider(provider)
    try:
        assert bt.retrieval_snapshot_available() is True
    finally:
        bt.set_retrieval_snapshot_provider(None)

    assert bt.retrieval_snapshot_available() is False
