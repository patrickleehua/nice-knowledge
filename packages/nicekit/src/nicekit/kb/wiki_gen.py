"""LLM 自动生成/维护 wiki 页(KB-5A,借鉴 llm_wiki 的两步思维链摄入)。

文档摄入成功后,基于该文档的统一 Markdown 增量更新库内 kb_pages:
- Step 1 分析(kb.wiki_analyze):文档 md + 现有页清单(title/type/摘要 200 字)
  + 总览页现文 → 主题/关联/矛盾/planned_pages(create|update);
- Step 2 生成(kb.wiki_generate):分析结论(context only)+ 文档 md
  + 目标页现文 → pages[](中文 Markdown,鼓励 [[wikilink]] 与来源署名);
- 落库:新页 create(origin=llm),正文为空且生成内容只进 draft;已有页走
  kb.wiki_merge LLM 合并,结果只写 draft,不改已发布正文与 origin;
  page_type 默认开放(B20),宿主可 set_valid_page_types() 注册白名单,注册后
  非白名单页会被丢弃并 warning(llm_wiki 的 schema 路由校验思想);

wikilink 归宿决策(2026-07-23):页面间交叉引用只保留在正文的 [[...]] 文本
中,由结构 lint(nicekit/kb/lint.py)从内容解析体检;不物化任何链接边
(legacy kb_links 表已随旧建议链接体系整体移除)。
- overview 综述页每库一篇(固定标题 OVERVIEW_TITLE),每次生成后用
  kb.wiki_overview 按页清单+变更摘要生成或更新待审核草稿。

长文档(> WIKI_DOC_BUDGET 字符)先按 heading 边界分块(复用 chunker 的
split_for_extraction),逐块 kb.wiki_analyze_chunk 滚动更新全局摘要,
最终摘要替代原文进 Step 1/2。不做磁盘 checkpoint,失败整体重跑。

成本口径(每文档 LLM 调用次数):
短文档 = 1(analyze)+ 1(generate)+ 更新页数(merge)+ 1(overview);
长文档另加 ceil(len/24000) 次 chunk 滚动摘要;planned_pages 为空时
只花 1 次 analyze 即返回。

DB 注意:kb 表无 ORM 级 FK,同事务父子行先 flush;所有读写走带 org
上下文的会话(FORCE RLS)。
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nicekit.core.db import org_session
from nicekit.domain.kb import (
    WikiAnalysis,
    WikiChunkDigest,
    WikiGeneration,
    WikiMergeResult,
    WikiOverview,
)
from nicekit.kb import storage
from nicekit.kb.chunker import split_for_extraction
from nicekit.kb.effective_scope import live_snapshot_projection_filter
from nicekit.kb.eligibility import (
    ActiveKnowledgeBaseLease,
    active_knowledge_base_lease_is_current,
    capture_active_knowledge_base_lease,
)
from nicekit.kb.evidence_locator import locate_evidence
from nicekit.kb.guardrails import fence_untrusted_document
from nicekit.llm.service import LLMService
from nicekit.models.kb import (
    DocumentRevision,
    EvidenceSpan,
    FactClaim,
    FactReviewStatus,
    KbPage,
    KnowledgeBase,
    SourceDocument,
)

logger = logging.getLogger(__name__)

# 单次 LLM 输入的文档字符预算;超过走滚动 digest
WIKI_DOC_BUDGET = 24000
# 页清单中每页摘要截断长度 / 总览页现文进分析的截断长度
_SUMMARY_CHARS = 200
_OVERVIEW_CTX_CHARS = 2000
# 目标页现文进 Step 2 的截断长度(merge 仍拿全文,只影响生成上下文)
_TARGET_CTX_CHARS = 6000

# page_type 是开放集合(MIGRATION-PLAN B20):``kb_pages.page_type`` 本就是自由
# 字符串列,SDK 不预设任何行业词表。默认不限制;宿主想收紧就调
# :func:`set_valid_page_types` 注册一份白名单(注册后 LLM 产出的非白名单页会被
# 丢弃并 warning,即 TF 的 schema 路由校验行为)。
#: 通用兜底类型,仅用于 LLM 产出缺失/空白 page_type 时的归一化
DEFAULT_PAGE_TYPE = "topic"
_valid_page_types: frozenset[str] | None = None


def set_valid_page_types(page_types: Iterable[str] | None) -> None:
    """注册 page_type 白名单;传 None(默认)= 不限制。"""
    global _valid_page_types
    if page_types is None:
        _valid_page_types = None
        return
    cleaned = frozenset(str(item).strip() for item in page_types if str(item).strip())
    _valid_page_types = cleaned or None


def valid_page_types() -> frozenset[str] | None:
    """当前生效的 page_type 白名单;None 表示开放。"""
    return _valid_page_types


def normalize_page_type(page_type: str | None) -> str | None:
    """归一化 LLM 产出的 page_type;返回 None 表示该页应被丢弃。

    空值一律回落 :data:`DEFAULT_PAGE_TYPE`;未注册白名单时任何非空值都合法。
    """
    cleaned = (page_type or "").strip()
    if not cleaned:
        cleaned = DEFAULT_PAGE_TYPE
    allowed = _valid_page_types
    if allowed is not None and cleaned not in allowed:
        return None
    return cleaned


OVERVIEW_TITLE = "知识库总览"

# wiki 层常量的真源(MIGRATION-PLAN A6):projections.py / wiki_review.py 一律从
# 这里 import,不再各自定义,避免投影与生成两侧的取值漂移。
PAGE_ORIGIN_LLM = "llm"
PAGE_DRAFT_PENDING = "pending_review"
WIKI_PUBLICATION_STATUS_KEY = "_wiki_publication_status"

# [[标题]] / [[标题|别名]](取标题部分);不跨行、不含嵌套方括号
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")


class WikiSourceUnavailableError(Exception):
    """文档不存在或尚未解析出 Markdown,无法生成 wiki。"""


class WikiSnapshotManagedError(Exception):
    """Legacy Wiki generation cannot mutate a snapshot-managed KB."""


async def _require_legacy_wiki_mode(session: AsyncSession, kb_id: UUID) -> None:
    active_snapshot_id = await session.scalar(
        select(KnowledgeBase.active_snapshot_id).where(KnowledgeBase.id == kb_id)
    )
    if active_snapshot_id is not None:
        raise WikiSnapshotManagedError(
            "该知识库已启用快照发布；Wiki 内容必须通过 FactClaim 构建新快照"
        )


async def _lock_current_wiki_lease(
    session: AsyncSession,
    lease: ActiveKnowledgeBaseLease,
) -> None:
    """Recheck a long Wiki call in a fresh transaction and hold the KB boundary."""

    await session.commit()
    if not await active_knowledge_base_lease_is_current(
        session,
        lease,
        lock=True,
    ):
        raise WikiSourceUnavailableError(
            "知识库消费边界已变化，Wiki 生成结果未写入"
        )


@dataclass
class WikiUpdateResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    overview_updated: bool = False
    llm_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "warnings": self.warnings,
            "overview_updated": self.overview_updated,
            "llm_calls": self.llm_calls,
        }


def extract_wikilinks(markdown: str) -> list[str]:
    """解析 [[标题]] / [[标题|别名]],去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(markdown or ""):
        title = m.group(1).split("|", 1)[0].strip()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


class WikiStore:
    """kb_pages 的读写封装(单测可用同接口的内存 fake 替换)。

    [[wikilink]] 交叉引用只保留在页面正文文本中,由结构 lint 从内容解析,
    不物化任何链接边。"""

    def __init__(self, session: AsyncSession, org_id: UUID, kb_id: UUID):
        self._session = session
        self._org_id = org_id
        self._kb_id = kb_id

    async def list_pages(self) -> list[KbPage]:
        return list(
            (
                await self._session.execute(
                    select(KbPage)
                    .where(
                        KbPage.kb_id == self._kb_id,
                        live_snapshot_projection_filter(
                            KbPage,
                            "wiki_page",
                        ),
                    )
                    .order_by(KbPage.created_at)
                )
            ).scalars().all()
        )

    async def get_page_by_title(self, title: str) -> KbPage | None:
        return (
            await self._session.execute(
                select(KbPage).where(
                    KbPage.kb_id == self._kb_id,
                    KbPage.title == title,
                    live_snapshot_projection_filter(
                        KbPage,
                        "wiki_page",
                    ),
                )
            )
        ).scalars().first()

    async def create_page(
        self,
        *,
        title: str,
        page_type: str,
        content: str,
        source_doc_id: UUID | None,
        evidence_quote: str | None = None,
    ) -> KbPage:
        del evidence_quote
        page = KbPage(
            org_id=self._org_id, kb_id=self._kb_id, title=title, page_type=page_type,
            content=None, draft_content=content, draft_status=PAGE_DRAFT_PENDING,
            source_doc_id=source_doc_id, origin=PAGE_ORIGIN_LLM,
        )
        self._session.add(page)
        # kb 表无 ORM 级 FK:先 flush 拿 id,便于同事务后续引用
        await self._session.flush()
        return page

    async def update_page(
        self, page: KbPage, content: str, evidence_quote: str | None = None
    ) -> None:
        """只更新待审核草稿，绝不修改已发布正文与来源。"""
        del evidence_quote
        if page.draft_content is not None:
            page.meta = {
                **(page.meta or {}),
                "prev_draft_content": page.draft_content,
                "prev_draft_updated_at": datetime.now(UTC).isoformat(),
            }
        page.draft_content = content
        page.draft_status = PAGE_DRAFT_PENDING
        self._session.add(page)
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()


class SnapshotWikiStore(WikiStore):
    """Stage automatic Wiki output as reviewable, evidenced FactClaims."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: UUID,
        kb_id: UUID,
        *,
        revision: DocumentRevision,
        markdown: str,
        pages: list[KbPage],
        source_claims: dict[UUID, UUID],
    ) -> None:
        super().__init__(session, org_id, kb_id)
        self._revision = revision
        self._markdown = markdown
        self._pages = {page.title: page for page in pages}
        self._source_claims = source_claims
        self._dirty: dict[str, KbPage] = {}
        self._evidence_quotes: dict[str, str | None] = {}

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        org_id: UUID,
        kb_id: UUID,
        *,
        revision: DocumentRevision,
        markdown: str,
    ) -> "SnapshotWikiStore":
        rows = list(
            (
                await session.execute(
                    select(KbPage)
                    .where(
                        KbPage.kb_id == kb_id,
                        live_snapshot_projection_filter(
                            KbPage,
                            "wiki_page",
                        ),
                    )
                    .order_by(KbPage.created_at)
                )
            )
            .scalars()
            .all()
        )
        pages = [
            KbPage(
                id=row.id,
                org_id=row.org_id,
                kb_id=row.kb_id,
                snapshot_id=row.snapshot_id,
                page_type=row.page_type,
                title=row.title,
                content=row.content,
                draft_content=row.draft_content,
                draft_status=row.draft_status,
                meta=dict(row.meta or {}) or None,
                origin=row.origin,
                source_doc_id=row.source_doc_id,
                created_by=row.created_by,
            )
            for row in rows
        ]
        source_claims: dict[UUID, UUID] = {}
        snapshot_ids = {page.snapshot_id for page in pages if page.snapshot_id is not None}
        if snapshot_ids:
            claims = list(
                (
                    await session.execute(
                        select(FactClaim).where(
                            FactClaim.kb_id == kb_id,
                            FactClaim.predicate == "wiki_page",
                            FactClaim.review_status == FactReviewStatus.CONFIRMED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            page_ids = {page.id for page in pages}
            for claim in claims:
                for snapshot_id in snapshot_ids:
                    projected_id = uuid5(snapshot_id, f"wiki_page:{claim.id}")
                    if projected_id in page_ids:
                        source_claims[projected_id] = claim.id
        return cls(
            session,
            org_id,
            kb_id,
            revision=revision,
            markdown=markdown,
            pages=pages,
            source_claims=source_claims,
        )

    async def list_pages(self) -> list[KbPage]:
        return list(self._pages.values())

    async def get_page_by_title(self, title: str) -> KbPage | None:
        return self._pages.get(title)

    async def create_page(
        self,
        *,
        title: str,
        page_type: str,
        content: str,
        source_doc_id: UUID | None,
        evidence_quote: str | None = None,
    ) -> KbPage:
        page = KbPage(
            id=uuid5(self._kb_id, f"wiki_page_subject:{title.casefold()}"),
            org_id=self._org_id,
            kb_id=self._kb_id,
            title=title,
            page_type=page_type,
            content=None,
            draft_content=content,
            draft_status=PAGE_DRAFT_PENDING,
            source_doc_id=source_doc_id,
            origin=PAGE_ORIGIN_LLM,
        )
        self._pages[title] = page
        self._dirty[title] = page
        self._evidence_quotes[title] = evidence_quote
        return page

    async def update_page(
        self, page: KbPage, content: str, evidence_quote: str | None = None
    ) -> None:
        if page.draft_content is not None:
            page.meta = {
                **(page.meta or {}),
                "prev_draft_content": page.draft_content,
                "prev_draft_updated_at": datetime.now(UTC).isoformat(),
            }
        page.draft_content = content
        page.draft_status = PAGE_DRAFT_PENDING
        self._dirty[page.title] = page
        self._evidence_quotes[page.title] = evidence_quote

    def _evidence_location(self, requested_quote: str | None):
        quote = (requested_quote or "").strip()
        if quote and quote in self._markdown:
            return locate_evidence(quote, self._markdown, None)
        fallback = next(
            (line.strip() for line in self._markdown.splitlines() if line.strip()), None
        )
        if fallback is None:
            raise WikiSourceUnavailableError("文档 Markdown 为空，无法为 Wiki 草稿绑定证据")
        return locate_evidence(fallback, self._markdown, None)

    async def commit(self) -> None:
        for title, page in self._dirty.items():
            content = (page.draft_content or "").strip()
            if not content:
                continue
            # 主体必须是本次生成所依据的源文档:投影层(projections.
            # _validate_source_document)要求 wiki 事实能追溯到快照 manifest 内
            # 该文档的 revision,页面标题的去重由投影层按 title 收敛
            subject_id = self._revision.doc_id
            payload = {
                "title": title,
                "page_type": page.page_type,
                "content_markdown": content,
                "meta": page.meta,
            }
            duplicate = await self._session.scalar(
                select(FactClaim.id).where(
                    FactClaim.kb_id == self._kb_id,
                    FactClaim.subject_type == "source_document",
                    FactClaim.subject_id == subject_id,
                    FactClaim.predicate == "wiki_page",
                    FactClaim.review_status == FactReviewStatus.SUGGESTED.value,
                    FactClaim.value_json == payload,
                )
            )
            if duplicate is not None:
                continue
            claim_id = uuid4()
            self._session.add(
                FactClaim(
                    id=claim_id,
                    org_id=self._org_id,
                    kb_id=self._kb_id,
                    subject_type="source_document",
                    subject_id=subject_id,
                    predicate="wiki_page",
                    value_json=payload,
                    raw_payload={
                        **payload,
                        "evidence_quote": self._evidence_quotes.get(title),
                    },
                    review_status=FactReviewStatus.SUGGESTED,
                    prompt_version="kb.wiki_generate:v3",
                )
            )
            await self._session.flush()
            location = self._evidence_location(self._evidence_quotes.get(title))
            self._session.add(
                EvidenceSpan(
                    org_id=self._org_id,
                    kb_id=self._kb_id,
                    fact_claim_id=claim_id,
                    revision_id=self._revision.id,
                    page=location.page,
                    start_line=location.start_line,
                    end_line=location.end_line,
                    cell_ref=location.cell_ref,
                    quote_text=location.quote_text,
                )
            )
            source_claim_id = self._source_claims.get(page.id)
            if source_claim_id is not None:
                prior = list(
                    (
                        await self._session.execute(
                            select(EvidenceSpan).where(
                                EvidenceSpan.fact_claim_id == source_claim_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for evidence in prior:
                    self._session.add(
                        EvidenceSpan(
                            org_id=self._org_id,
                            kb_id=self._kb_id,
                            fact_claim_id=claim_id,
                            revision_id=evidence.revision_id,
                            chunk_id=evidence.chunk_id,
                            page=evidence.page,
                            start_line=evidence.start_line,
                            end_line=evidence.end_line,
                            cell_ref=evidence.cell_ref,
                            quote_text=evidence.quote_text,
                        )
                    )
        await self._session.commit()


# ---- 输入组装(纯函数,便于单测)------------------------------------------


def _draft_or_published(page: KbPage) -> str:
    return page.draft_content if page.draft_content is not None else (page.content or "")


def _page_catalog(pages: list[KbPage]) -> str:
    lines = [
        f"- 「{p.title}」(page_type={p.page_type}):"
        f"{_draft_or_published(p)[:_SUMMARY_CHARS]}"
        for p in pages
    ]
    return "\n".join(lines) or "(库内暂无 wiki 页)"


def _analyze_input(doc_name: str, doc_input: str, pages: list[KbPage]) -> str:
    overview = next(
        (p for p in pages if p.page_type == "overview" and p.title == OVERVIEW_TITLE), None
    )
    overview_text = (
        _draft_or_published(overview)[:_OVERVIEW_CTX_CHARS]
        if overview
        else "(尚无总览页)"
    )
    return (
        f"新入库文档《{doc_name}》内容:\n"
        f"{fence_untrusted_document(doc_input, label='source document')}\n\n"
        "库内现有 wiki 页清单:\n"
        f"{fence_untrusted_document(_page_catalog(pages), label='existing wiki catalog')}\n\n"
        "知识库总览页现文:\n"
        f"{fence_untrusted_document(overview_text, label='existing overview text')}"
    )


def _generate_input(
    doc_name: str,
    doc_input: str,
    analysis: WikiAnalysis,
    targets: dict[str, KbPage],
) -> str:
    target_lines: list[str] = []
    for planned in analysis.planned_pages:
        existing = targets.get(planned.title)
        if existing is None:
            target_lines.append(f"- 「{planned.title}」(page_type={planned.page_type},新建)")
        else:
            content = _draft_or_published(existing)[:_TARGET_CTX_CHARS]
            target_lines.append(
                f"- 「{existing.title}」(page_type={existing.page_type},更新)现有内容:\n"
                f"{fence_untrusted_document(content, label='existing wiki text')}"
            )
    analysis_json = json.dumps(analysis.model_dump(), ensure_ascii=False, indent=2)
    return (
        "前置分析结论(仅作上下文参考):\n"
        f"{fence_untrusted_document(analysis_json, label='generated analysis')}\n\n"
        f"新入库文档《{doc_name}》内容:\n"
        f"{fence_untrusted_document(doc_input, label='source document')}\n\n"
        f"目标页清单:\n" + "\n".join(target_lines)
    )


# ---- 长文档滚动 digest ------------------------------------------------------


async def _rolling_digest(
    llm: LLMService, org_id: UUID, markdown: str, budget: int
) -> tuple[str, int]:
    """按 heading 边界分块,逐块滚动更新全局摘要;返回 (最终摘要, LLM 调用次数)。"""
    segments = split_for_extraction(markdown, budget) or [("", markdown)]
    summary = ""
    calls = 0
    for heading, segment in segments:
        head = f"(所属章节:{heading})\n" if heading else ""
        fenced_summary = fence_untrusted_document(
            summary or "(空,这是第一个分块)", label="generated summary"
        )
        fenced_segment = fence_untrusted_document(
            f"{head}{segment}", label="source document chunk"
        )
        content = (
            "当前全局摘要:\n"
            f"{fenced_summary}\n\n"
            "文档新分块:\n"
            f"{fenced_segment}"
        )
        digest = await llm.generate_structured(
            task="kb.wiki_analyze_chunk",
            messages=[{"role": "user", "content": content}],
            output_model=WikiChunkDigest,
            org_id=org_id,
        )
        summary = digest.global_summary
        calls += 1
    return summary, calls


# ---- 主编排 -----------------------------------------------------------------


async def run_wiki_update(
    store: WikiStore,
    llm: LLMService,
    *,
    org_id: UUID,
    doc_id: UUID | None,
    doc_name: str,
    markdown: str,
    doc_budget: int = WIKI_DOC_BUDGET,
    before_write: Callable[[], Awaitable[None]] | None = None,
) -> WikiUpdateResult:
    """两步思维链编排(store 可注入,单测用内存 fake)。"""
    result = WikiUpdateResult()

    doc_input = markdown
    if len(markdown) > doc_budget:
        summary, calls = await _rolling_digest(llm, org_id, markdown, doc_budget)
        doc_input = f"(长文档滚动摘要,非原文)\n{summary}"
        result.llm_calls += calls

    pages = await store.list_pages()

    # Step 1:分析
    analysis: WikiAnalysis = await llm.generate_structured(
        task="kb.wiki_analyze",
        messages=[{"role": "user", "content": _analyze_input(doc_name, doc_input, pages)}],
        output_model=WikiAnalysis,
        org_id=org_id,
    )
    result.llm_calls += 1

    planned = []
    for p in analysis.planned_pages:
        title = p.title.strip()
        if not title:
            result.warnings.append("丢弃空标题的 planned_page")
            continue
        page_type = normalize_page_type(p.page_type)
        if page_type is None:
            result.warnings.append(
                f"丢弃非法 page_type {p.page_type!r} 的计划页「{title}」"
            )
            continue
        planned.append(p.model_copy(update={"page_type": page_type}))
    if not planned:
        logger.info("wiki analyze 未计划任何页面,跳过生成: doc=%s", doc_name)
        return result
    analysis = analysis.model_copy(update={"planned_pages": planned})

    # 目标页现文(update 目标 + create 撞名的兜底)
    targets: dict[str, KbPage] = {}
    for p in planned:
        existing = await store.get_page_by_title(p.title)
        if existing is not None:
            targets[p.title] = existing

    # Step 2:生成
    generation: WikiGeneration = await llm.generate_structured(
        task="kb.wiki_generate",
        messages=[
            {"role": "user", "content": _generate_input(doc_name, doc_input, analysis, targets)}
        ],
        output_model=WikiGeneration,
        org_id=org_id,
    )
    result.llm_calls += 1

    # 落库:create / LLM 合并 update(schema 路由校验:非法 page_type 丢弃)
    written: list[KbPage] = []
    write_authorized = False

    async def authorize_write() -> None:
        nonlocal write_authorized
        if write_authorized:
            return
        if before_write is not None:
            await before_write()
        write_authorized = True

    for page_out in generation.pages:
        title = page_out.title.strip()
        if not title or not page_out.content_markdown.strip():
            result.warnings.append(f"丢弃空标题/空正文的生成页 {page_out.title!r}")
            continue
        page_type = normalize_page_type(page_out.page_type)
        if page_type is None:
            result.warnings.append(
                f"丢弃非法 page_type {page_out.page_type!r} 的生成页「{title}」"
            )
            continue
        existing = targets.get(title) or await store.get_page_by_title(title)
        if existing is None:
            await authorize_write()
            page = await store.create_page(
                title=title, page_type=page_type,
                content=page_out.content_markdown, source_doc_id=doc_id,
                evidence_quote=page_out.evidence_quote,
            )
            result.created.append(title)
        else:
            fenced_existing = fence_untrusted_document(
                _draft_or_published(existing) or "(空)", label="existing wiki text"
            )
            fenced_generated = fence_untrusted_document(
                page_out.content_markdown, label="generated draft"
            )
            merge: WikiMergeResult = await llm.generate_structured(
                task="kb.wiki_merge",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"页面「{title}」现有草稿/正文:\n"
                            f"{fenced_existing}\n\n"
                            "新版正文:\n"
                            f"{fenced_generated}"
                        ),
                    }
                ],
                output_model=WikiMergeResult,
                org_id=org_id,
            )
            result.llm_calls += 1
            await authorize_write()
            await store.update_page(
                existing, merge.merged_markdown, evidence_quote=page_out.evidence_quote
            )
            page = existing
            result.updated.append(title)
        written.append(page)

    # [[wikilink]] 交叉引用留在正文文本,由结构 lint 从内容解析体检,不落边。

    # overview 综述页:每次有实际变更后更新待审核草稿
    if written:
        change_note = "本次变更:" + "、".join(
            [f"新建「{t}」" for t in result.created] + [f"更新「{t}」" for t in result.updated]
        )
        result.llm_calls += await _rewrite_overview(store, llm, org_id, change_note)
        result.overview_updated = True

    await store.commit()
    return result


async def _rewrite_overview(
    store: WikiStore,
    llm: LLMService,
    org_id: UUID,
    change_note: str,
    *,
    before_write: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """kb.wiki_overview 生成综述草稿；返回 LLM 调用次数(1)。"""
    pages = await store.list_pages()
    listing = "\n".join(
        f"- 「{p.title}」(page_type={p.page_type})"
        for p in pages
        if not (p.page_type == "overview" and p.title == OVERVIEW_TITLE)
    )
    content = f"库内 wiki 页清单:\n{listing or '(空)'}\n\n{change_note}"
    out: WikiOverview = await llm.generate_structured(
        task="kb.wiki_overview",
        messages=[
            {
                "role": "user",
                "content": fence_untrusted_document(content, label="wiki catalog and change log"),
            }
        ],
        output_model=WikiOverview,
        org_id=org_id,
    )
    overview = await store.get_page_by_title(OVERVIEW_TITLE)
    if before_write is not None:
        await before_write()
    if overview is None:
        await store.create_page(
            title=OVERVIEW_TITLE, page_type="overview",
            content=out.content_markdown, source_doc_id=None,
        )
    else:
        await store.update_page(overview, out.content_markdown)
    return 1


# ---- 对外入口 ---------------------------------------------------------------


async def update_wiki_for_document(
    doc_id: UUID,
    org_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMService,
) -> WikiUpdateResult:
    """基于单文档的 markdown 增量更新库内 wiki 页(摄入后自动触发或手动重跑)。"""
    session = org_session(session_factory, org_id)
    try:
        doc = await session.get(SourceDocument, doc_id)
        if doc is None:
            raise WikiSourceUnavailableError(f"source_document {doc_id} 不存在")
        lease = await capture_active_knowledge_base_lease(
            session,
            kb_id=doc.kb_id,
            owner_org_id=org_id,
        )
        if lease is None:
            raise WikiSourceUnavailableError(
                f"source_document {doc_id} 的知识库不存在或不可用"
            )
        revision = (
            await session.execute(
                select(DocumentRevision)
                .where(
                    DocumentRevision.doc_id == doc.id,
                    DocumentRevision.sha256 == doc.sha256,
                    DocumentRevision.tombstoned_at.is_(None),
                )
                .order_by(DocumentRevision.revision_no.desc())
            )
        ).scalars().first()
        if not doc.markdown_key:
            raise WikiSourceUnavailableError("文档尚未解析出 Markdown,请先(重新)摄入")
        if revision is None:
            # Legacy writes remain valid only before snapshot activation.  Guard
            # them before object-store access so rejection has no source read.
            await _require_legacy_wiki_mode(session, doc.kb_id)
        markdown = (await storage.get_object(doc.markdown_key)).decode(
            "utf-8", errors="replace"
        )
        if revision is not None:
            store: WikiStore = await SnapshotWikiStore.load(
                session,
                org_id,
                doc.kb_id,
                revision=revision,
                markdown=markdown,
            )
        else:
            store = WikiStore(session, org_id, doc.kb_id)

        async def authorize_write() -> None:
            await _lock_current_wiki_lease(session, lease)

        return await run_wiki_update(
            store,
            llm,
            org_id=org_id,
            doc_id=doc.id,
            doc_name=doc.filename,
            markdown=markdown,
            before_write=authorize_write,
        )
    finally:
        await session.close()


async def refresh_kb_overview(
    kb_id: UUID,
    org_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm: LLMService,
) -> KbPage:
    """单独生成某库的综述草稿(手动端点用),返回 overview 页。"""
    session = org_session(session_factory, org_id)
    try:
        lease = await capture_active_knowledge_base_lease(
            session,
            kb_id=kb_id,
            owner_org_id=org_id,
        )
        if lease is None:
            raise WikiSourceUnavailableError("知识库不存在或不可用")
        await _require_legacy_wiki_mode(session, kb_id)
        store = WikiStore(session, org_id, kb_id)
        await _rewrite_overview(
            store,
            llm,
            org_id,
            change_note="本次变更:人工触发综述重写",
            before_write=lambda: _lock_current_wiki_lease(session, lease),
        )
        await store.commit()
        overview = await store.get_page_by_title(OVERVIEW_TITLE)
        assert overview is not None  # _rewrite_overview 必建/必更
        return overview
    finally:
        await session.close()


__all__ = [
    "DEFAULT_PAGE_TYPE",
    "OVERVIEW_TITLE",
    "PAGE_DRAFT_PENDING",
    "PAGE_ORIGIN_LLM",
    "WIKI_PUBLICATION_STATUS_KEY",
    "WIKI_DOC_BUDGET",
    "WikiSourceUnavailableError",
    "WikiSnapshotManagedError",
    "SnapshotWikiStore",
    "WikiStore",
    "WikiUpdateResult",
    "extract_wikilinks",
    "normalize_page_type",
    "set_valid_page_types",
    "valid_page_types",
    "refresh_kb_overview",
    "run_wiki_update",
    "update_wiki_for_document",
]
