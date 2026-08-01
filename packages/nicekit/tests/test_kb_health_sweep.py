"""KB 健康巡检单测:阈值触发/不触发、0=关、同日幂等通知、per-org 隔离、
Gauge 刷新与阈值告警。全 mock,不碰 DB / broker;celery/lifespan 装配属运行时波次。
"""

from types import SimpleNamespace
from uuid import uuid4

from nicekit.kb import health_sweep as module
from nicekit.kb import metrics
from nicekit.kb.health_sweep import OrgHealthSnapshot

# ---------- 假 session / factory(与 review_sweep 单测同款) ----------


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


def _patch_org_sessions(monkeypatch):
    sessions: dict = {}
    monkeypatch.setattr(
        module, "org_session", lambda factory, org_id: sessions.setdefault(org_id, _OrgSession())
    )
    return sessions


def _settings(**overrides):
    values = {
        "kb_health_outbox_backlog_threshold": 100,
        "kb_health_vectorless_chunks_threshold": 50,
        "kb_health_failed_docs_threshold": 5,
        "kb_health_pending_claims_threshold": 200,
        "kb_health_stuck_parsing_seconds": 1800,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------- build_alerts:阈值判定 ----------


def test_build_alerts_all_healthy_returns_empty() -> None:
    snapshot = OrgHealthSnapshot(
        outbox_pending=100,  # 等于阈值不触发(> 语义)
        vectorless_chunks=50,
        failed_docs=3,
        stuck_parsing_docs=2,
        pending_claims=200,
    )
    assert module.build_alerts(snapshot, _settings()) == []


def test_build_alerts_each_threshold_triggers() -> None:
    snapshot = OrgHealthSnapshot(
        outbox_pending=101,
        outbox_oldest_pending_age_seconds=7200.0,
        outbox_dead_letter=2,
        vectorless_chunks=51,
        failed_docs=4,
        stuck_parsing_docs=2,  # failed + stuck = 6 > 5
        pending_claims=201,
    )
    alerts = module.build_alerts(snapshot, _settings())
    assert len(alerts) == 5
    joined = "\n".join(alerts)
    assert "outbox 积压 101 条" in joined and "2.0 小时" in joined
    assert "死信 2 条" in joined
    assert "51 个切片缺失向量" in joined
    assert "文档异常 6 篇" in joined and "解析失败 4" in joined and "卡在解析中 2" in joined
    assert "事实待审积压 201 条" in joined


def test_build_alerts_dead_letter_alone_triggers_outbox_item() -> None:
    alerts = module.build_alerts(
        OrgHealthSnapshot(outbox_dead_letter=1), _settings()
    )
    assert len(alerts) == 1 and "死信" in alerts[0]


def test_build_alerts_zero_threshold_disables_item() -> None:
    snapshot = OrgHealthSnapshot(
        outbox_pending=10_000,
        outbox_dead_letter=10,
        vectorless_chunks=10_000,
        failed_docs=10_000,
        pending_claims=10_000,
    )
    settings = _settings(
        kb_health_outbox_backlog_threshold=0,
        kb_health_vectorless_chunks_threshold=0,
        kb_health_failed_docs_threshold=0,
        kb_health_pending_claims_threshold=0,
    )
    assert module.build_alerts(snapshot, settings) == []


# ---------- sweep:触发通知与 Gauge 刷新 ----------


async def test_sweep_notifies_and_sets_gauges_on_breach(monkeypatch) -> None:
    org_id = uuid4()
    sessions = _patch_org_sessions(monkeypatch)
    monkeypatch.setattr(module, "get_settings", _settings)
    snapshot = OrgHealthSnapshot(
        outbox_pending=150,
        outbox_oldest_pending_age_seconds=3600.0,
        outbox_dead_letter=3,
        vectorless_chunks=80,
        failed_docs=6,
        stuck_parsing_docs=1,
        pending_claims=250,
    )

    async def fake_collect(session, org):
        return snapshot

    notified: list = []

    async def fake_notify(session, org, alerts):
        notified.append((org, list(alerts)))
        return True

    monkeypatch.setattr(module, "collect_org_health", fake_collect)
    monkeypatch.setattr(module, "_notify_health_alert", fake_notify)

    totals = await module.sweep_kb_health(_factory([org_id]))

    assert totals == {"orgs": 1, "alerting_orgs": 1, "notified_orgs": 1, "failed_orgs": 0}
    assert len(notified) == 1 and notified[0][0] == org_id
    assert sessions[org_id].commits == 1 and sessions[org_id].closed

    org = str(org_id)
    sample = metrics.registry.get_sample_value
    assert sample("kb_outbox_pending_backlog", {"org": org}) == 150
    assert sample("kb_outbox_oldest_pending_age_seconds", {"org": org}) == 3600.0
    assert sample("kb_outbox_dead_letter", {"org": org}) == 3
    assert sample("kb_chunks_missing_embedding", {"org": org}) == 80
    assert sample("kb_docs_failed", {"org": org}) == 6
    assert sample("kb_docs_stuck_parsing", {"org": org}) == 1
    assert sample("kb_fact_claims_pending_review", {"org": org}) == 250


async def test_sweep_skips_notify_when_healthy(monkeypatch) -> None:
    org_id = uuid4()
    sessions = _patch_org_sessions(monkeypatch)
    monkeypatch.setattr(module, "get_settings", _settings)

    async def fake_collect(session, org):
        return OrgHealthSnapshot()

    async def boom(*args, **kwargs):
        raise AssertionError("健康时不应发通知")

    monkeypatch.setattr(module, "collect_org_health", fake_collect)
    monkeypatch.setattr(module, "_notify_health_alert", boom)

    totals = await module.sweep_kb_health(_factory([org_id]))
    assert totals == {"orgs": 1, "alerting_orgs": 0, "notified_orgs": 0, "failed_orgs": 0}
    assert sessions[org_id].commits == 0 and sessions[org_id].closed


async def test_sweep_counts_alerting_org_even_if_already_notified(monkeypatch) -> None:
    org_id = uuid4()
    sessions = _patch_org_sessions(monkeypatch)
    monkeypatch.setattr(module, "get_settings", _settings)

    async def fake_collect(session, org):
        return OrgHealthSnapshot(pending_claims=999)

    async def fake_notify(session, org, alerts):
        return False  # 同日已发,幂等跳过

    monkeypatch.setattr(module, "collect_org_health", fake_collect)
    monkeypatch.setattr(module, "_notify_health_alert", fake_notify)

    totals = await module.sweep_kb_health(_factory([org_id]))
    assert totals["alerting_orgs"] == 1 and totals["notified_orgs"] == 0
    assert sessions[org_id].commits == 0  # 未新发不落库


# ---------- sweep:per-org 隔离 ----------


async def test_sweep_isolates_org_failures(monkeypatch) -> None:
    org_bad, org_good = uuid4(), uuid4()
    sessions = _patch_org_sessions(monkeypatch)
    monkeypatch.setattr(module, "get_settings", _settings)

    async def fake_collect(session, org):
        if org == org_bad:
            raise RuntimeError("org down")
        return OrgHealthSnapshot(outbox_pending=101)

    async def fake_notify(session, org, alerts):
        return True

    monkeypatch.setattr(module, "collect_org_health", fake_collect)
    monkeypatch.setattr(module, "_notify_health_alert", fake_notify)

    totals = await module.sweep_kb_health(_factory([org_bad, org_good]))

    assert totals["failed_orgs"] == 1
    assert totals["alerting_orgs"] == 1 and totals["notified_orgs"] == 1
    assert sessions[org_bad].rollbacks == 1 and sessions[org_bad].closed
    assert sessions[org_good].commits == 1 and sessions[org_good].closed


# ---------- 通知:同日幂等与收件人 ----------


class _NotifySession:
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


async def test_notify_health_alert_same_day_idempotent(monkeypatch) -> None:
    org_id = uuid4()
    session = _NotifySession()
    sent: list = []
    members = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    _patch_notify_channel(monkeypatch, sent, members)
    alerts = ["outbox 积压 150 条(阈值 100)", "事实待审积压 250 条(阈值 200)"]

    assert await module._notify_health_alert(session, org_id, alerts) is True
    assert len(sent) == 1
    note = sent[0]
    assert note.kind == "kb.health_alert"
    assert note.email is False  # 批量场景只站内信,防轰炸
    assert note.user_ids == [m.id for m in members]
    assert "1. outbox 积压 150 条" in note.body and "2. 事实待审积压 250 条" in note.body

    # 同日再触发:标题相同 → 幂等跳过,不重发
    assert await module._notify_health_alert(session, org_id, alerts) is False
    assert len(sent) == 1


async def test_notify_health_alert_skips_without_recipients(monkeypatch) -> None:
    sent: list = []
    _patch_notify_channel(monkeypatch, sent, members=[])

    ok = await module._notify_health_alert(_NotifySession(), uuid4(), ["超标项"])
    assert ok is False and sent == []
