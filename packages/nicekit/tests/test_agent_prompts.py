"""Prompt 资源包(§5.4 prompts 模板化)专测。

覆盖三件事:
1. **中性化**:SDK 内置资源里不得残留任何行业词(§7 规约:出现即视为未完成);
2. **多 root**:宿主目录能覆盖 SDK 同 id 资源,顺序与失效逻辑正确;
3. **组装契约**:build_system_prompt / build_system_prompt_blocks 的签名与
   字符数守恒(sub_agents.py 与 runtime_snapshot.py 依赖它)。
"""

import re

import pytest

from nicekit.agent.prompts import (
    DEFAULT_GLOBAL_BLOCK_IDS,
    PromptBlock,
    PromptResourceError,
    build_system_prompt,
    build_system_prompt_blocks,
    compose,
    global_block_ids,
    list_prompts,
    load_prompt,
    prompt_roots,
    register_prompt_root,
    render,
    reset_prompt_roots,
    reset_prompt_variables,
    set_global_block_ids,
    set_prompt_variable,
    validate_prompt_resources,
)

# 迁移规约 §7:SDK 内出现这些字样即视为未完成(工具描述/prompt/注释/错误文案均算)
_DOMAIN_WORDS = (
    "旅行社",
    "旅游",
    "行程",
    "报价",
    "签证",
    "酒店",
    "航班",
    "大巴",
    "景点",
    "客户",
    "计调",
    "定价员",
    "TravelFlow",
    "OTA",
    "比价",
)


@pytest.fixture(autouse=True)
def _isolated_prompt_state():
    """每个用例都从"只有 SDK 内置 root + 默认变量 + 默认全局块"出发。"""
    reset_prompt_roots()
    reset_prompt_variables()
    set_global_block_ids(None)
    yield
    reset_prompt_roots()
    reset_prompt_variables()
    set_global_block_ids(None)


# ---------------------------------------------------------------------------
# 资源完整性与中性化
# ---------------------------------------------------------------------------


def test_builtin_resources_load_and_declare_variables_consistently() -> None:
    """启动期校验:七个资源全部可解析,变量声明与 {{var}} 占位符一致。"""
    assert validate_prompt_resources() == 7
    ids = {resource.id for resource in list_prompts()}
    assert ids == {
        "agent_role_custom",
        "agent_role_generic",
        "global_core_rules",
        "global_intro",
        "global_output_style",
        "global_tool_usage",
        "memory_extraction",
    }
    for resource in list_prompts():
        used = set(re.findall(r"\{\{(\w+)\}\}", resource.content))
        assert set(resource.variables) == used, resource.id
        # frontmatter 的 source 必须指向 SDK 内消费方(可追溯注入链路)
        assert resource.source.startswith("nicekit/"), resource.id


@pytest.mark.parametrize("word", _DOMAIN_WORDS)
def test_builtin_resources_have_no_domain_residue(word: str) -> None:
    for resource in list_prompts():
        haystack = f"{resource.title}{resource.description}{resource.content}"
        assert word not in haystack, f"{resource.id} 残留行业词 {word}"


def test_core_rules_keep_generic_honesty_clauses() -> None:
    """去业务化不得连诚实性规则一起删掉(§5.4 明确要求保留这四条)。"""
    text = load_prompt("global_core_rules").content
    assert "严禁编造事实" in text
    assert "注明来源" in text
    assert "精确引用" in text
    assert "超出你知识范围" in text


def test_product_identity_is_a_variable_with_neutral_default() -> None:
    resource = load_prompt("global_intro")
    assert resource.variables == ("product_identity",)
    assert "多租户 AI 工作台助手" in render("global_intro")

    set_prompt_variable("product_identity", "你是「工单机器人」")
    assert "工单机器人" in render("global_intro")


def test_memory_extraction_scope_guide_is_injectable() -> None:
    """记忆范围词表由变量注入:SDK 只内置 org,宿主注册后自行替换。"""
    resource = load_prompt("memory_extraction")
    assert resource.variables == ("memory_scope_guide",)
    assert "`org`" in render("memory_extraction")

    set_prompt_variable("memory_scope_guide", "- `scope` 取值:\n  - `ticket` —— 工单维度")
    assert "ticket" in render("memory_extraction")


def test_render_keeps_placeholder_when_variable_missing(caplog) -> None:
    set_prompt_variable("product_identity", None)
    text = render("global_intro")
    assert "{{product_identity}}" in text


# ---------------------------------------------------------------------------
# 多 root
# ---------------------------------------------------------------------------


def _write_resource(directory, resource_id: str, body: str, *, variables: str = "[]") -> None:
    (directory / f"{resource_id}.md").write_text(
        "---\n"
        f"id: {resource_id}\n"
        "title: 宿主覆盖段\n"
        "category: global\n"
        "source: demo/prompts.py\n"
        f"variables: {variables}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_host_root_overrides_builtin_resource(tmp_path) -> None:
    _write_resource(tmp_path, "global_intro", "你是宿主自定义身份。")
    register_prompt_root(tmp_path)

    assert len(prompt_roots()) == 2
    assert render("global_intro") == "你是宿主自定义身份。"
    # 覆盖后变量声明也随宿主文件走(这里没有占位符)
    assert load_prompt("global_intro").variables == ()
    assert load_prompt("global_intro").origin == str(tmp_path)


def test_host_root_can_add_new_resource(tmp_path) -> None:
    _write_resource(tmp_path, "global_compliance", "【合规】本域额外要求。")
    register_prompt_root(tmp_path)

    assert render("global_compliance") == "【合规】本域额外要求。"
    assert validate_prompt_resources() == 8


def test_register_missing_root_fails_loudly(tmp_path) -> None:
    with pytest.raises(PromptResourceError):
        register_prompt_root(tmp_path / "missing")


def test_bad_host_resource_fails_at_validation(tmp_path) -> None:
    """变量声明与占位符不一致必须在启动期炸,而不是等到第一次对话。"""
    _write_resource(tmp_path, "global_intro", "你好 {{who}}", variables="[]")
    register_prompt_root(tmp_path)
    with pytest.raises(PromptResourceError):
        validate_prompt_resources()


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def test_default_global_blocks_precede_role_block() -> None:
    assert global_block_ids() == DEFAULT_GLOBAL_BLOCK_IDS
    _text, blocks = build_system_prompt_blocks(None)
    assert [block["id"] for block in blocks] == [
        *DEFAULT_GLOBAL_BLOCK_IDS,
        "agent_role_generic",
    ]


def test_custom_role_prompt_is_wrapped_not_replacing_globals() -> None:
    text, blocks = build_system_prompt_blocks("你只回答工单问题")
    assert [block["id"] for block in blocks][-1] == "agent_role_custom"
    assert "你只回答工单问题" in text
    # 卡 prompt 改不掉全局纪律
    assert "【工具纪律】" in text


def test_block_chars_sum_equals_text_length() -> None:
    text, blocks = build_system_prompt_blocks(
        "角色", "联网已关闭", [("memory_recall", "【相关记忆】略")]
    )
    assert sum(block["chars"] for block in blocks) == len(text)
    assert blocks[-1]["id"] == "capability_context"
    assert [block["id"] for block in blocks][-2] == "memory_recall"


def test_build_system_prompt_accepts_extra_sections() -> None:
    """sub_agents.py 与 service.py 都按这个三参签名调用,签名不许漂。"""
    text = build_system_prompt("角色", None, [("tool_trace", "【本会话已查过】略")])
    assert "【本会话已查过】略" in text
    assert build_system_prompt(None) == build_system_prompt(None, None, None)


def test_global_block_ids_are_configurable(tmp_path) -> None:
    _write_resource(tmp_path, "global_compliance", "【合规】本域额外要求。")
    register_prompt_root(tmp_path)
    set_global_block_ids(["global_intro", "global_compliance"])

    _text, blocks = build_system_prompt_blocks(None)
    assert [block["id"] for block in blocks] == [
        "global_intro",
        "global_compliance",
        "agent_role_generic",
    ]

    set_global_block_ids(None)
    assert global_block_ids() == DEFAULT_GLOBAL_BLOCK_IDS


def test_compose_skips_empty_blocks() -> None:
    composed = compose([PromptBlock("global_intro"), PromptBlock("global_output_style")])
    assert composed.count("\n\n") >= 1
    assert "【输出风格】" in composed
