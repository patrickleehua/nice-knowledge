"""KB 事实 AI 审核定时扫描单测:分批、per-org 隔离、通知同日幂等、
inline/celery 模式分派与 celery beat 注册。全 mock,不碰 DB / LLM / broker。
"""

from types import SimpleNamespace
from uuid import uuid4

from nicekit.kb import review_sweep as module
from nicekit.kb.fact_review_ai import AiReviewSummary

# ---------- 假 session / factory ----------


class _ListResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        rows = self._rows

        class _All:
            def all(self):
                return rows

        return _All()


class _FactorySession:
    """org 列表查询用(async with factory() as session)。"""

    def __init__(self, org_ids):
        self._org_ids = org_ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return _ListResult(self._org_ids)


def _factory(org_ids):
    return lambda: _FactorySession(org_ids)


class _OrgSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        self.closed = True


async def _no_settle(session, *, org_id, limit=500):
    return 0


def _patch_org_sessions(monkeypatch, settle=_no_settle):
    sessions: dict = {}
    monkeypatch.setattr(
        module, "org_session", lambda factory, org_id: sessions.setdefault(org_id, _OrgSession())
    )
    monkeypatch.setattr(module, "settle_org_awaiting_documents", settle)
    return sessions


async def _zero_pending(session):
    return 0


# ---------- sweep:分批与汇总 ----------


async def test_sweep_batches_per_org_with_configured_limit(monkeypatch) -> None:
    org_a, org_b = uuid4(), uuid4()
    sessions = _patch_org_sessions(monkeypatch)
    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(kb_ai_review_sweep_batch_size=123)
    )
    calls: list[tuple] = []

    async def fake_review(session, *, org_id, limit, llm, unreviewed_only):
        calls.append((org_id, limit, unreviewed_only))
        return AiReviewSummary(reviewed=2, confirmed=2)

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", _zero_pending)

    totals = await module.sweep_pending_fact_reviews(_factory([org_a, org_b]))

    assert [c[0] for c in calls] == [org_a, org_b]
    assert all(limit == 123 and unreviewed for _, limit, unreviewed in calls)
    assert totals["orgs"] == 2
    assert totals["reviewed"] == 4 and totals["confirmed"] == 4
    assert totals["notified_orgs"] == 0 and totals["failed_orgs"] == 0
    for session in sessions.values():
        assert session.commits == 1 and session.closed


async def test_sweep_explicit_batch_limit_overrides_config(monkeypatch) -> None:
    org_id = uuid4()
    _patch_org_sessions(monkeypatch)
    seen: list[int] = []

    async def fake_review(session, *, org_id, limit, llm, unreviewed_only):
        seen.append(limit)
        return AiReviewSummary()

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", _zero_pending)

    await module.sweep_pending_fact_reviews(_factory([org_id]), batch_limit=7)
    assert seen == [7]


# ---------- sweep:审核收口 ----------


async def test_sweep_settles_documents_per_org(monkeypatch) -> None:
    """每 org 审完事实后收口 awaiting_review 文档,收口数计入汇总并在同一事务提交。"""
    org_a, org_b = uuid4(), uuid4()
    seen: list = []

    async def fake_settle(session, *, org_id, limit=500):
        seen.append((org_id, session.commits))  # 收口发生在本 org commit 之前
        return 2

    sessions = _patch_org_sessions(monkeypatch, settle=fake_settle)

    async def fake_review(session, **kwargs):
        return AiReviewSummary(reviewed=1, confirmed=1)

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", _zero_pending)

    totals = await module.sweep_pending_fact_reviews(_factory([org_a, org_b]))

    assert seen == [(org_a, 0), (org_b, 0)]
    assert totals["settled_docs"] == 4
    for session in sessions.values():
        assert session.commits == 1


# ---------- sweep:per-org 隔离 ----------


async def test_sweep_isolates_org_failures(monkeypatch) -> None:
    org_bad, org_good = uuid4(), uuid4()
    sessions = _patch_org_sessions(monkeypatch)

    async def fake_review(session, *, org_id, limit, llm, unreviewed_only):
        if org_id == org_bad:
            raise RuntimeError("org down")
        return AiReviewSummary(reviewed=1, confirmed=1)

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", _zero_pending)

    totals = await module.sweep_pending_fact_reviews(
        _factory([org_bad, org_good]), batch_limit=10
    )

    # 坏 org 只记日志回滚,好 org 照常处理
    assert totals["failed_orgs"] == 1
    assert totals["reviewed"] == 1 and totals["confirmed"] == 1
    assert sessions[org_bad].rollbacks == 1 and sessions[org_bad].closed
    assert sessions[org_good].commits == 1 and sessions[org_good].closed


# ---------- sweep:例外升级触发通知 ----------


async def test_sweep_notifies_org_on_escalation(monkeypatch) -> None:
    org_id = uuid4()
    sessions = _patch_org_sessions(monkeypatch)

    async def fake_review(session, **kwargs):
        return AiReviewSummary(reviewed=3, escalated=2, rejected=1)

    async def fake_pending(session):
        return 5

    notified: list[tuple] = []

    async def fake_notify(session, org, summary, pending):
        notified.append((org, summary.escalated, summary.rejected, pending))
        return True

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", fake_pending)
    monkeypatch.setattr(module, "_notify_pending_review", fake_notify)

    totals = await module.sweep_pending_fact_reviews(_factory([org_id]), batch_limit=10)

    assert notified == [(org_id, 2, 1, 5)]
    assert totals["notified_orgs"] == 1
    assert sessions[org_id].commits == 2  # 审核落库一次 + 通知落库一次


async def test_sweep_skips_notify_when_all_clear(monkeypatch) -> None:
    org_id = uuid4()
    _patch_org_sessions(monkeypatch)

    async def fake_review(session, **kwargs):
        return AiReviewSummary(reviewed=2, confirmed=2)

    async def boom(*args, **kwargs):
        raise AssertionError("无 escalate/reject/存量待人工时不应发通知")

    monkeypatch.setattr(module, "ai_review_fact_claims", fake_review)
    monkeypatch.setattr(module, "_pending_human_count", _zero_pending)
    monkeypatch.setattr(module, "_notify_pending_review", boom)

    totals = await module.sweep_pending_fact_reviews(_factory([org_id]), batch_limit=10)
    assert totals["notified_orgs"] == 0 and totals["failed_orgs"] == 0


# ---------- expiry:per-org 隔离(同一修复口径) ----------


async def test_expiry_sweep_isolates_org_failures(monkeypatch) -> None:
    from nicekit.kb import expiry as expiry_module

    org_bad, org_good = uuid4(), uuid4()
    sessions: dict = {}
    monkeypatch.setattr(
        expiry_module,
        "org_session",
        lambda factory, org_id: sessions.setdefault(org_id, _OrgSession()),
    )

    async def fake_sweep_org(session, org_id):
        if org_id == org_bad:
            raise RuntimeError("org down")
        return 3

    monkeypatch.setattr(expiry_module, "_sweep_org", fake_sweep_org)

    total = await expiry_module.sweep_expired_kb_docs(_factory([org_bad, org_good]))

    assert total == 3  # 坏 org 跳过,好 org 照常提醒
    assert sessions[org_bad].rollbacks == 1 and sessions[org_bad].closed
    assert sessions[org_good].closed


# ---------- 通知:同日幂等与收件人 ----------


class _NotifySession:
    """_already_notified_today 的通知查询:发过(added 非空)返回已有行。"""

    def __init__(self):
        self.added: list[str] = []

    async def execute(self, statement):
        assert "notifications" in str(statement)
        value = "existing" if self.added else None

        class _R:
            def scalar_one_or_none(self):
                return value

        return _R()


def _patch_notify_channel(monkeypatch, sent, members):
    """通知走 ports.notify_org_roles(Notifier 协议);这里替身掉整条通道。"""

    async def fake_notify(
        session, *, org_id, kind, title, body, link=None, email=False, roles=None
    ):
        if not members:
            return 0
        sent.append(
            SimpleNamespace(
                org_id=org_id,
                user_ids=[member.id for member in members],
                kind=kind,
                title=title,
                body=body,
                link=link,
                email=email,
            )
        )
        session.added.append(title)
        return len(members)

    monkeypatch.setattr(module.ports, "notify_org_roles", fake_notify)


async def test_notify_pending_review_same_day_idempotent(monkeypatch) -> None:
    org_id = uuid4()
    session = _NotifySession()
    sent: list = []
    members = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    _patch_notify_channel(monkeypatch, sent, members)
    summary = AiReviewSummary(reviewed=3, escalated=2, rejected=1)

    assert await module._notify_pending_review(session, org_id, summary, 5) is True
    assert len(sent) == 1
    note = sent[0]
    assert note.kind == "kb.fact_review_pending"
    assert note.email is False  # 批量场景只站内信,防轰炸
    assert note.user_ids == [m.id for m in members]
    assert "升级 2 条" in note.body and "驳回 1 条" in note.body and "5 条" in note.body

    # 同日再触发:标题相同 → 幂等跳过,不重发
    assert await module._notify_pending_review(session, org_id, summary, 5) is False
    assert len(sent) == 1


async def test_notify_pending_review_skips_without_recipients(monkeypatch) -> None:
    sent: list = []
    _patch_notify_channel(monkeypatch, sent, members=[])

    ok = await module._notify_pending_review(
        _NotifySession(), uuid4(), AiReviewSummary(escalated=1), 1
    )
    assert ok is False and sent == []


# ---------- ai_review_fact_claims:sweep 专用选取过滤 ----------


async def test_ai_review_unreviewed_only_filters_selection() -> None:
    from nicekit.kb.fact_review_ai import ai_review_fact_claims

    captured: list[str] = []

    class _S:
        async def execute(self, statement):
            captured.append(str(statement))
            return _ListResult([])

    summary = await ai_review_fact_claims(
        _S(),  # type: ignore[arg-type]
        org_id=uuid4(),
        unreviewed_only=True,
    )
    assert summary.reviewed == 0
    assert "reviewed_by IS NULL" in captured[0]  # 已 escalate 留人工的不再选中
