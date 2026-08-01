"""KB-5A wiki 自动生成单测(mock LLM + 内存 store,不碰 DB/MinIO/网络)。

覆盖:两步编排(分析→生成→草稿落库)、已有页正文/origin 保护、
page_type 非法丢弃、planned 为空早退、长文档滚动 digest、
auto_wiki 开关与失败不反噬摄入状态。
[[wikilink]] 只保留在正文文本(由 lint 解析),不再物化链接边。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.domain.kb import (
    IngestProfile,
    WikiAnalysis,
    WikiChunkDigest,
    WikiGeneratedPage,
    WikiGeneration,
    WikiMergeResult,
    WikiOverview,
    WikiPlannedPage,
)
from nicekit.kb import ingestion as kb_ingestion
from nicekit.kb.ingestion import _maybe_update_wiki
from nicekit.kb.wiki_gen import (
    DEFAULT_PAGE_TYPE,
    OVERVIEW_TITLE,
    SnapshotWikiStore,
    WikiStore,
    extract_wikilinks,
    run_wiki_update,
    set_valid_page_types,
)
from nicekit.models.kb import (
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    KbPage,
)

ORG_ID = uuid4()
KB_ID = uuid4()
DOC_ID = uuid4()


class _FakeSession:
    """只承担 WikiStore 内部 add/flush 的最小替身(id 缺省时补 uuid)。"""

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()

    async def flush(self) -> None:
        pass


class _ClaimSession:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def scalar(self, statement):
        del statement
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeStore(WikiStore):
    """内存版 store:查询走内存,update_page 等复用真实逻辑。"""

    def __init__(self, pages: list[KbPage] | None = None):
        super().__init__(_FakeSession(), ORG_ID, KB_ID)
        self.pages: list[KbPage] = list(pages or [])
        self.commits = 0

    async def list_pages(self) -> list[KbPage]:
        return list(self.pages)

    async def get_page_by_title(self, title: str) -> KbPage | None:
        return next((p for p in self.pages if p.title == title), None)

    async def create_page(self, **kwargs) -> KbPage:
        page = await super().create_page(**kwargs)
        self.pages.append(page)
        return page

    async def commit(self) -> None:
        self.commits += 1


class FakeLLM:
    """按 task 返回预置输出;list 值按序弹出(滚动 digest 用)。"""

    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    async def generate_structured(self, *, task, messages, output_model, org_id):
        assert org_id == ORG_ID
        self.calls.append((task, messages[0]["content"]))
        out = self.outputs[task]
        if isinstance(out, list):
            return out.pop(0)
        return out


def _existing_page(title: str, page_type: str = "topic", content: str = "旧文") -> KbPage:
    return KbPage(
        id=uuid4(), org_id=ORG_ID, kb_id=KB_ID, title=title,
        page_type=page_type, content=content, origin="human",
    )


def _analysis(*planned: tuple[str, str, str]) -> WikiAnalysis:
    return WikiAnalysis(
        key_topics=["计费"],
        related_pages=[],
        contradictions=[],
        planned_pages=[
            WikiPlannedPage(title=t, page_type=pt, action=a, reason=None)
            for t, pt, a in planned
        ],
    )


def _gen(*pages: tuple[str, str, str]) -> WikiGeneration:
    return WikiGeneration(
        pages=[
            WikiGeneratedPage(
                title=t,
                page_type=pt,
                content_markdown=c,
                evidence_quote="原文依据",
            )
            for t, pt, c in pages
        ]
    )


_OVERVIEW = WikiOverview(content_markdown="# 总览\n覆盖 [[计费]] 等主题。")


# ---- 两步编排:create 路径 ---------------------------------------------------


async def test_snapshot_store_stages_suggested_wiki_claim_with_revision_evidence() -> None:
    session = _ClaimSession()
    revision = DocumentRevision(
        id=uuid4(), org_id=ORG_ID, kb_id=KB_ID, doc_id=DOC_ID,
        revision_no=1, sha256="a" * 64, original_object_key="source/a.md",
    )
    store = SnapshotWikiStore(
        session, ORG_ID, KB_ID,
        revision=revision,
        markdown="# 计费\n结算口径便捷。",
        pages=[],
        source_claims={},
    )
    await store.create_page(
        title="结算口径", page_type="topic", content="结算口径总览。",
        source_doc_id=DOC_ID, evidence_quote="结算口径便捷。",
    )

    await store.commit()

    claim = next(item for item in session.added if isinstance(item, FactClaim))
    evidence = next(item for item in session.added if isinstance(item, EvidenceSpan))
    assert claim.predicate == "wiki_page"
    # 主体必须是源文档:投影层按此追溯快照 manifest,写成页面主体会让快照构建失败
    assert claim.subject_type == "source_document"
    assert claim.subject_id == DOC_ID
    assert claim.review_status == FactReviewStatus.SUGGESTED
    assert claim.value_json["content_markdown"] == "结算口径总览。"
    assert evidence.fact_claim_id == claim.id
    assert evidence.revision_id == revision.id
    assert evidence.quote_text == "结算口径便捷。"
    assert session.commits == 1


async def test_two_step_create_flow_pages_links_overview() -> None:
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(
                ("计费", "concept", "create"), ("结算口径", "topic", "create")
            ),
            "kb.wiki_generate": _gen(
                ("计费", "concept", "计费概况……\n来源:guide.md"),
                ("结算口径", "topic", "按月结算,详见 [[计费]]。\n来源:guide.md"),
            ),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore()
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="guide.md", markdown="# 计费\n短文档"
    )

    assert result.created == ["计费", "结算口径"] and result.updated == []
    assert result.warnings == []
    assert result.llm_calls == 3  # analyze + generate + overview
    assert [t for t, _ in llm.calls] == ["kb.wiki_analyze", "kb.wiki_generate", "kb.wiki_overview"]

    paris = await store.get_page_by_title("计费")
    transit = await store.get_page_by_title("结算口径")
    assert paris.origin == "llm" and paris.source_doc_id == DOC_ID
    assert paris.content is None and paris.draft_content.startswith("计费概况")
    assert paris.draft_status == "pending_review"
    assert paris.page_type == "concept"
    # [[wikilink]] 只保留在正文文本,不物化任何链接边。
    assert "[[计费]]" in transit.draft_content

    overview = await store.get_page_by_title(OVERVIEW_TITLE)
    assert overview is not None and overview.page_type == "overview"
    assert overview.origin == "llm" and overview.content is None
    assert overview.draft_content.startswith("# 总览")
    assert overview.draft_status == "pending_review"
    assert result.overview_updated is True
    assert store.commits == 1


async def test_long_call_revalidates_once_before_first_wiki_write() -> None:
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(
                ("计费", "concept", "create"),
                ("结算口径", "topic", "create"),
            ),
            "kb.wiki_generate": _gen(
                ("计费", "concept", "计费概况"),
                ("结算口径", "topic", "结算口径概况"),
            ),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore()
    checks = 0

    async def authorize_write() -> None:
        nonlocal checks
        checks += 1

    await run_wiki_update(
        store,
        llm,
        org_id=ORG_ID,
        doc_id=DOC_ID,
        doc_name="guide.md",
        markdown="计费资料",
        before_write=authorize_write,
    )

    assert checks == 1
    assert store.commits == 1


async def test_changed_kb_boundary_blocks_all_wiki_writes() -> None:
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(("计费", "concept", "create")),
            "kb.wiki_generate": _gen(("计费", "concept", "计费概况")),
        }
    )
    store = FakeStore()

    async def reject_write() -> None:
        raise RuntimeError("knowledge_boundary_changed")

    with pytest.raises(RuntimeError, match="knowledge_boundary_changed"):
        await run_wiki_update(
            store,
            llm,
            org_id=ORG_ID,
            doc_id=DOC_ID,
            doc_name="guide.md",
            markdown="计费资料",
            before_write=reject_write,
        )

    assert store.pages == []
    assert store.commits == 0


# ---- 合并路径:只写 draft，保护人工正文与 origin ------------------------------


async def test_merge_path_updates_only_draft() -> None:
    old = _existing_page("计费", page_type="concept", content="计费旧说明:参考价 100。")
    old.draft_content = "计费待审稿:参考价 110。"
    old.draft_status = "pending_review"
    human_overview = _existing_page(
        OVERVIEW_TITLE, page_type="overview", content="# 人工总览\n保持不变"
    )
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(("计费", "concept", "update")),
            "kb.wiki_generate": _gen(("计费", "concept", "计费新说明:参考价 120。")),
            "kb.wiki_merge": WikiMergeResult(
                merged_markdown="计费合并稿。矛盾:参考价 100(旧)vs 120(新)。"
            ),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore(pages=[old, human_overview])
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="update.md", markdown="新资料"
    )

    assert result.updated == ["计费"] and result.created == []
    assert result.llm_calls == 4  # analyze + generate + merge + overview
    assert old.content == "计费旧说明:参考价 100。"
    assert old.draft_content.startswith("计费合并稿")
    assert old.meta["prev_draft_content"] == "计费待审稿:参考价 110。"
    assert old.meta["prev_draft_updated_at"]
    assert old.origin == "human"
    assert human_overview.content == "# 人工总览\n保持不变"
    assert human_overview.origin == "human"
    assert human_overview.draft_content.startswith("# 总览")
    # merge 输入应同时携带已有待审稿与新文
    merge_input = next(c for t, c in llm.calls if t == "kb.wiki_merge")
    assert "计费待审稿" in merge_input and "计费新说明" in merge_input
    # step 2 输入:目标页标注为更新并携带已有待审稿
    gen_input = next(c for t, c in llm.calls if t == "kb.wiki_generate")
    assert "更新" in gen_input and "计费待审稿" in gen_input


# ---- page_type 校验(schema 路由校验思想)------------------------------------
#
# MIGRATION-PLAN B20:SDK 默认**不限制** page_type(kb_pages.page_type 本就是开放
# 列)。白名单是宿主可选项,注册后才恢复 TF 的"非法类型丢弃"行为。


@pytest.fixture
def page_type_whitelist():
    """注册一份 page_type 白名单,用例结束后复位为开放。"""
    set_valid_page_types({"concept", "topic", "overview"})
    yield
    set_valid_page_types(None)


async def test_open_page_types_accept_any_slug_by_default() -> None:
    """默认开放:LLM 报什么 page_type 就落什么,不丢页、不告警。"""
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(("自定义页", "banana", "create")),
            "kb.wiki_generate": _gen(("自定义页", "banana", "正文")),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore()
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="d.md", markdown="x"
    )
    assert result.created == ["自定义页"]
    assert result.warnings == []
    page = await store.get_page_by_title("自定义页")
    assert page is not None and page.page_type == "banana"


async def test_blank_page_type_falls_back_to_default() -> None:
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(("兜底页", "  ", "create")),
            "kb.wiki_generate": _gen(("兜底页", "  ", "正文")),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore()
    await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="d.md", markdown="x"
    )
    page = await store.get_page_by_title("兜底页")
    assert page is not None and page.page_type == DEFAULT_PAGE_TYPE


async def test_invalid_page_type_dropped_with_warning(page_type_whitelist) -> None:
    llm = FakeLLM(
        {
            "kb.wiki_analyze": _analysis(
                ("计费", "concept", "create"), ("野类型页", "banana", "create")
            ),
            "kb.wiki_generate": _gen(
                ("计费", "concept", "正文"), ("越权页", "hacker", "正文")
            ),
            "kb.wiki_overview": _OVERVIEW,
        }
    )
    store = FakeStore()
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="d.md", markdown="x"
    )

    assert result.created == ["计费"]
    assert len(result.warnings) == 2  # 计划页 + 生成页 各丢一个
    assert any("banana" in w for w in result.warnings)
    assert any("hacker" in w for w in result.warnings)
    assert await store.get_page_by_title("越权页") is None
    assert await store.get_page_by_title("野类型页") is None


async def test_no_valid_planned_pages_early_return_skips_generate(
    page_type_whitelist,
) -> None:
    # generate/overview 未预置:若被调用会 KeyError,证明早退未走后续步骤
    llm = FakeLLM({"kb.wiki_analyze": _analysis(("野类型页", "banana", "create"))})
    store = FakeStore()
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="d.md", markdown="x"
    )
    assert result.created == [] and result.updated == []
    assert result.overview_updated is False
    assert result.llm_calls == 1
    assert store.pages == [] and store.commits == 0


# ---- wikilink 解析:去重 / 别名 / 非法形态 ------------------------------------


def test_extract_wikilinks_dedupe_alias_and_invalid_forms() -> None:
    md = "见 [[计费]] 与 [[计费|花都]],还有 [[ 对账中心 ]];[[]] 与 [[跨\n行]] 无效。"
    assert extract_wikilinks(md) == ["计费", "对账中心"]
    assert extract_wikilinks("") == []


# ---- 长文档滚动 digest -------------------------------------------------------


async def test_long_document_rolling_digest() -> None:
    md = "# 第一章\n" + "计费住宿要点。" * 40 + "\n# 第二章\n" + "结算口径要点。" * 40
    digests = [
        WikiChunkDigest(key_points=["住宿"], global_summary="摘要一:住宿要点"),
        WikiChunkDigest(key_points=["交通"], global_summary="摘要二:住宿+交通要点"),
    ]
    llm = FakeLLM(
        {
            "kb.wiki_analyze_chunk": list(digests),
            "kb.wiki_analyze": _analysis(),  # 无 planned → 早退,聚焦 digest 路径
        }
    )
    store = FakeStore()
    result = await run_wiki_update(
        store, llm, org_id=ORG_ID, doc_id=DOC_ID, doc_name="long.md",
        markdown=md, doc_budget=300,
    )

    chunk_calls = [c for t, c in llm.calls if t == "kb.wiki_analyze_chunk"]
    assert len(chunk_calls) == 2
    assert "(空,这是第一个分块)" in chunk_calls[0]
    assert "摘要一:住宿要点" in chunk_calls[1]  # 滚动:上一步摘要进下一块输入
    analyze_input = next(c for t, c in llm.calls if t == "kb.wiki_analyze")
    assert "(长文档滚动摘要,非原文)" in analyze_input
    assert "摘要二:住宿+交通要点" in analyze_input
    assert md not in analyze_input  # 原文不整篇进分析
    assert result.llm_calls == 3  # 2 digest + 1 analyze


# ---- 摄入触发:auto_wiki 开关 / 失败不反噬 ------------------------------------


def _doc() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


async def test_auto_wiki_disabled_skips_trigger(monkeypatch) -> None:
    called: list = []

    async def fake_update(doc_id, org_id, *, session_factory, llm):
        called.append(doc_id)

    monkeypatch.setattr(kb_ingestion, "update_wiki_for_document", fake_update)
    await _maybe_update_wiki(
        _doc(), ORG_ID, session_factory=None, llm=None,
        profile=IngestProfile(auto_wiki=False),
    )
    assert called == []


@pytest.mark.parametrize("profile", [None, IngestProfile()])
async def test_auto_wiki_default_on_triggers(monkeypatch, profile) -> None:
    called: list = []

    async def fake_update(doc_id, org_id, *, session_factory, llm):
        called.append((doc_id, org_id))
        return SimpleNamespace(created=[], updated=[], warnings=[])

    monkeypatch.setattr(kb_ingestion, "update_wiki_for_document", fake_update)
    doc = _doc()
    await _maybe_update_wiki(doc, ORG_ID, session_factory=None, llm=None, profile=profile)
    assert called == [(doc.id, ORG_ID)]


async def test_wiki_failure_does_not_raise(monkeypatch) -> None:
    async def boom(doc_id, org_id, *, session_factory, llm):
        raise RuntimeError("LLM 网关抖动")

    monkeypatch.setattr(kb_ingestion, "update_wiki_for_document", boom)
    # 不应上抛:摄入状态已落库,wiki 失败只打日志
    await _maybe_update_wiki(_doc(), ORG_ID, session_factory=None, llm=None, profile=None)
