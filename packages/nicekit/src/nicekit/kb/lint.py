"""知识库结构 lint(KB-5B,借鉴 llm_wiki 的纯本地结构体检,零 LLM)。

只从库内 kb_pages 正文的 [[wikilink]] 解析出链构建入/出链图,
检出三类结构问题并给出可执行建议:
- broken_link:[[x]] 目标既不是库内页标题、也不是结构化实体名 → difflib 推荐最近标题;
- orphan:无任何入链的页(没有其他页指向它)→ 标题 token 重叠推荐可关联页;
- no_outlinks:正文无任何 [[wikilink]] 出链 → 同样按 token 重叠推荐。

纯读、零 LLM,可随时调用。severity:broken_link=warning(链接坏了),
orphan/no_outlinks=info(可优化但不影响正确性)。
"""

import difflib
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.kb.effective_scope import live_snapshot_projection_filter
from nicekit.kb.projections import active_projection_filter
from nicekit.kb.wiki_gen import extract_wikilinks
from nicekit.models.kb import KbEntity, KbPage

# 建议数量上限
_MAX_SUGGESTIONS = 3
# 标题分词(中英混合):按非字母数字/非 CJK 切,单字符 token 丢弃
_TOKEN_RE = re.compile(r"[0-9a-zA-Z一-鿿]+")


@dataclass
class LintIssue:
    type: str  # broken_link | orphan | no_outlinks
    severity: str  # warning | info
    page_id: str
    page_title: str
    message: str
    suggestions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "severity": self.severity,
            "page_id": self.page_id,
            "page_title": self.page_title,
            "message": self.message,
            "suggestions": self.suggestions,
        }


def _title_tokens(title: str) -> set[str]:
    """标题分词(去单字):中文按连续块、英文按词,用于 orphan 的重叠推荐。"""
    return {t.lower() for t in _TOKEN_RE.findall(title or "") if len(t) >= 2}


def _overlap_suggestions(
    title: str, others: list[str], limit: int = _MAX_SUGGESTIONS
) -> list[str]:
    """按标题 token 重叠数推荐可关联页(重叠越多越靠前,0 重叠不推荐)。"""
    toks = _title_tokens(title)
    if not toks:
        return []
    scored = [
        (len(toks & _title_tokens(o)), o) for o in others if o != title
    ]
    scored = [(n, o) for n, o in scored if n > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [o for _, o in scored[:limit]]


def build_lint_issues(
    pages: list[tuple[str, str, str | None]],
    entity_names: set[str],
) -> list[LintIssue]:
    """纯函数(便于单测):
    - pages: [(page_id, title, content)];
    - entity_names: 库内结构化实体名(broken_link 白名单的一部分)。
    页间入/出链信号只来自正文 [[wikilink]] 解析。
    """
    titles = {t for _, t, _ in pages}
    all_titles = sorted(titles)
    # 出链:content 的 [[wikilink]]
    outlinks: dict[str, list[str]] = {
        pid: extract_wikilinks(content or "") for pid, _, content in pages
    }
    # 入链计数:被其他页 [[title]] 指向
    inbound: dict[str, int] = {t: 0 for t in titles}
    for _pid, links in outlinks.items():
        for t in links:
            if t in inbound:
                inbound[t] += 1

    issues: list[LintIssue] = []
    for pid, title, _content in pages:
        outs = outlinks[pid]
        # broken_link:目标既非库内页、也非实体名
        for target in outs:
            if target in titles or target in entity_names:
                continue
            near = difflib.get_close_matches(target, all_titles, n=_MAX_SUGGESTIONS, cutoff=0.5)
            issues.append(
                LintIssue(
                    type="broken_link", severity="warning", page_id=pid, page_title=title,
                    message=f"链接 [[{target}]] 指向的页面/实体不存在",
                    suggestions=near,
                )
            )
        # no_outlinks:正文无任何 wikilink 出链
        if not outs:
            issues.append(
                LintIssue(
                    type="no_outlinks", severity="info", page_id=pid, page_title=title,
                    message="本页没有任何 [[wikilink]] 出链,建议关联相关页面",
                    suggestions=_overlap_suggestions(title, all_titles),
                )
            )
        # orphan:无任何入链(没有其他页指向它)
        if inbound.get(title, 0) == 0:
            issues.append(
                LintIssue(
                    type="orphan", severity="info", page_id=pid, page_title=title,
                    message="没有其他页面链接到本页(孤儿页),建议从相关页建立 [[链接]]",
                    suggestions=_overlap_suggestions(title, all_titles),
                )
            )
    # 排序:warning 在前,同级按类型稳定
    issues.sort(key=lambda i: (0 if i.severity == "warning" else 1, i.type))
    return issues


async def run_structural_lint(
    session: AsyncSession, kb_id: UUID
) -> tuple[list[LintIssue], dict]:
    """读库内 kb_pages/实体 → 结构 lint issues + 统计。"""
    pages_rows = list(
        (
            await session.execute(
                select(KbPage.id, KbPage.title, KbPage.content).where(
                    KbPage.kb_id == kb_id,
                    active_projection_filter(KbPage),
                    live_snapshot_projection_filter(KbPage, "wiki_page"),
                )
            )
        ).all()
    )
    pages = [(str(pid), title, content) for pid, title, content in pages_rows]

    # broken_link 白名单 = 库内通用实体名(MIGRATION-PLAN B21)。TF 这里遍历
    # 五张行业专表 + kb_entities;SDK 只有 KbEntity 一条通用路径,所有实体类型
    # (含宿主注册的自定义类型)都落在这张表上。同 KB 范围,RLS 定界下直接查。
    entity_names: set[str] = {
        name
        for name in (
            await session.execute(
                select(KbEntity.name).where(
                    KbEntity.kb_id == kb_id,
                    active_projection_filter(KbEntity),
                    live_snapshot_projection_filter(KbEntity, "kb_entity"),
                )
            )
        ).scalars()
        if name
    }

    issues = build_lint_issues(pages, entity_names)
    stats = {
        "pages": len(pages),
        "issues": len(issues),
        "broken_links": sum(1 for i in issues if i.type == "broken_link"),
        "orphans": sum(1 for i in issues if i.type == "orphan"),
        "no_outlinks": sum(1 for i in issues if i.type == "no_outlinks"),
    }
    return issues, stats
