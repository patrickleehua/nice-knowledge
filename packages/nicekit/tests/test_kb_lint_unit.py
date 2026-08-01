"""KB-5B 结构 lint 单测(build_lint_issues 纯函数 + run_structural_lint
白名单查询,fake session,零 DB 零 LLM)。

页间入/出链信号只来自页面正文 [[wikilink]] 解析(legacy kb_links 边已移除)。"""

from uuid import uuid4

from nicekit.kb.lint import build_lint_issues, run_structural_lint
from nicekit.models.kb import KbEntity, KbPage


def _by_type(issues, t):
    return [i for i in issues if i.type == t]


def test_broken_link_detected_with_suggestion():
    # 服务条款 链到不存在的「计费口径总览」;库内有近似标题「计费口径」→ difflib 推荐
    pages = [
        ("p1", "服务条款", "详见 [[计费口径总览]] 与 [[结算中心]]。"),
        ("p2", "计费口径", "按月结算。"),
        ("p3", "结算中心", "结算中心说明。"),
    ]
    issues = build_lint_issues(pages, entity_names=set())
    broken = _by_type(issues, "broken_link")
    assert len(broken) == 1
    assert "计费口径总览" in broken[0].message
    assert broken[0].severity == "warning"
    assert "计费口径" in broken[0].suggestions


def test_wikilink_to_entity_name_not_broken():
    # [[结算中心]] 不是页,但是结构化实体名 → 不算 broken
    pages = [("p1", "接入指南", "参见 [[结算中心]]。")]
    issues = build_lint_issues(pages, entity_names={"结算中心"})
    assert _by_type(issues, "broken_link") == []


def test_orphan_and_no_outlinks():
    # p2 无人链入(orphan)且自身无出链(no_outlinks)
    pages = [
        ("p1", "服务条款", "参见 [[计费口径]]。"),
        ("p2", "计费口径", "纯文本无链接。"),
    ]
    issues = build_lint_issues(pages, entity_names=set())
    # p1 有出链但无入链 → orphan;p2 有入链(被 p1 链)但无出链 → no_outlinks
    orphans = {i.page_id for i in _by_type(issues, "orphan")}
    no_out = {i.page_id for i in _by_type(issues, "no_outlinks")}
    assert "p1" in orphans and "p1" not in no_out
    assert "p2" in no_out and "p2" not in orphans


def test_inbound_only_counted_from_wikilink_text():
    # 入链只认正文 [[title]]:p2 没被任何正文链接 → orphan(不再有 kb_links 补边)
    pages = [
        ("p1", "服务条款", "正文。"),
        ("p2", "计费口径", "正文。"),
    ]
    issues = build_lint_issues(pages, entity_names=set())
    orphans = {i.page_id for i in _by_type(issues, "orphan")}
    assert orphans == {"p1", "p2"}


def test_empty_kb_no_issues():
    assert build_lint_issues([], entity_names=set()) == []


# ---- run_structural_lint 白名单查询(fake session)----------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return iter(self._rows)


class _FakeLintSession:
    """按 select 的 entity 分发结果:kb_pages / 各实体表 name。"""

    def __init__(self, pages, names_by_model):
        self._pages = pages
        self._names_by_model = names_by_model
        self.queried_models = []

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is KbPage:
            return _Result(self._pages)
        self.queried_models.append(entity)
        return _Result(self._names_by_model.get(entity, []))


async def test_generic_kb_entity_names_join_broken_link_whitelist():
    # [[设备甲]] 是 kb_entities 通用实体名(租户自建类型)→ 不算 broken;
    # [[不存在页]] 仍应报 broken_link
    kb_id = uuid4()
    page_id = uuid4()
    pages = [(page_id, "设备台账", "使用 [[设备甲]],另见 [[不存在页]]。")]
    session = _FakeLintSession(
        pages, names_by_model={KbEntity: ["设备甲"]}
    )
    issues, stats = await run_structural_lint(session, kb_id)

    broken = [i for i in issues if i.type == "broken_link"]
    assert len(broken) == 1
    assert "不存在页" in broken[0].message
    assert stats["broken_links"] == 1
    # MIGRATION-PLAN B21:白名单只查通用实体表(TF 的 5 张行业专表已不存在)
    assert session.queried_models == [KbEntity]
