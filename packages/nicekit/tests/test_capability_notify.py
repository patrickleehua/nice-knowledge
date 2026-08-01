"""通知服务单测(prod-readiness-3 D2/AC-6):邮件 best-effort 全 mock,不碰 DB/网络。"""

from types import SimpleNamespace
from uuid import uuid4

import nicekit.capabilities.notify as notify_module
from nicekit.capabilities.notify import notify, send_email


def _settings(**overrides):
    base = {
        "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
        "smtp_from": "noreply@test.local", "smtp_starttls": True,
    }
    return SimpleNamespace(**{**base, **overrides})


class FakeSmtp:
    """记录调用序列的 smtplib.SMTP 替身。"""

    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[tuple] = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.calls.append(("sendmail", from_addr, to_addrs, msg))


async def test_send_email_skips_when_no_smtp_host(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "get_settings", lambda: _settings())
    FakeSmtp.instances = []
    monkeypatch.setattr(notify_module.smtplib, "SMTP", FakeSmtp)
    sent = await send_email(["a@b.c"], "标题", "正文")
    assert sent is False
    assert FakeSmtp.instances == []  # 未配置连 SMTP 都不碰


async def test_send_email_sends_with_starttls_and_login(monkeypatch) -> None:
    monkeypatch.setattr(
        notify_module, "get_settings",
        lambda: _settings(smtp_host="mail.test", smtp_user="u", smtp_password="p"),
    )
    FakeSmtp.instances = []
    monkeypatch.setattr(notify_module.smtplib, "SMTP", FakeSmtp)
    sent = await send_email(["a@b.c", "d@e.f"], "报价被退回:巴黎团", "价格偏低,请复核")
    assert sent is True
    smtp = FakeSmtp.instances[0]
    assert smtp.host == "mail.test"
    kinds = [c[0] for c in smtp.calls]
    assert kinds == ["starttls", "login", "sendmail"]
    _, from_addr, to_addrs, msg = smtp.calls[-1]
    assert from_addr == "noreply@test.local"
    assert to_addrs == ["a@b.c", "d@e.f"]
    assert "Subject:" in msg


async def test_send_email_swallows_smtp_error(monkeypatch) -> None:
    monkeypatch.setattr(
        notify_module, "get_settings", lambda: _settings(smtp_host="mail.test")
    )

    class BoomSmtp:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(notify_module.smtplib, "SMTP", BoomSmtp)
    sent = await send_email(["a@b.c"], "s", "b")
    assert sent is False  # best-effort:不上抛


class FakeSession:
    def __init__(self):
        self.added: list = []

    def add_all(self, rows):
        self.added.extend(rows)


async def test_notify_empty_recipients_noop(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "get_settings", lambda: _settings())
    session = FakeSession()
    rows = await notify(
        session, org_id=uuid4(), user_ids=[], kind="k", title="t", body="b"
    )
    assert rows == []
    assert session.added == []


async def test_notify_dedupes_recipients(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "get_settings", lambda: _settings())
    session = FakeSession()
    u1, u2 = uuid4(), uuid4()
    rows = await notify(
        session, org_id=uuid4(), user_ids=[u1, u2, u1], kind="quote.rejected",
        title="t", body="留言", link="/app/projects/x?tab=quote",
    )
    assert [r.user_id for r in rows] == [u1, u2]  # 保序去重
    assert len(session.added) == 2
    assert all(r.kind == "quote.rejected" and r.body == "留言" for r in rows)
