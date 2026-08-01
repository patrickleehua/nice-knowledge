"""KB 宿主扩展点回归(MIGRATION-PLAN §4):ReferenceScanner / IncidentRecorder / Notifier。

三个协议的共同契约:**注册即生效、缺省即降级**——没有宿主实现时治理链路照常
推进(无外部引用 / 不登记事件 / 不通知),绝不因为可选装配件缺席而失败。
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from nicekit.kb import ports
from nicekit.models.tenancy import Role


@pytest.fixture(autouse=True)
def _clean_registries():
    ports.reset_reference_scanners()
    ports.set_incident_recorder(None)
    ports.set_kb_notify_roles(None)
    ports.set_kb_notify_link("/org/kb")
    yield
    ports.reset_reference_scanners()
    ports.set_incident_recorder(None)
    ports.set_kb_notify_roles(None)
    ports.set_kb_notify_link("/org/kb")


# ---- ReferenceScanner ----


async def test_no_scanner_means_no_external_references() -> None:
    assert await ports.scan_references(
        object(), org_id=uuid4(), kind="snapshot", ids=[uuid4()]
    ) == {}
    assert await ports.referenced_ids(
        object(), org_id=uuid4(), kind="snapshot", ids=[uuid4()]
    ) == set()


async def test_multiple_scanners_accumulate_per_id_counts() -> None:
    target = uuid4()
    other = uuid4()

    class _Scanner:
        def __init__(self, count):
            self.count = count
            self.seen = []

        async def scan_references(self, _session, *, org_id, kind, ids):
            self.seen.append((kind, tuple(ids)))
            return {target: self.count, other: 0}

    first, second = _Scanner(2), _Scanner(3)
    ports.register_reference_scanner(first)
    ports.register_reference_scanner(second)
    ports.register_reference_scanner(first)  # 重复注册幂等

    counts = await ports.scan_references(
        object(), org_id=uuid4(), kind="document", ids=[target, other]
    )
    assert counts == {target: 5}
    assert await ports.referenced_ids(
        object(), org_id=uuid4(), kind="document", ids=[target, other]
    ) == {target}
    assert len(ports.reference_scanners()) == 2
    assert first.seen[0][0] == "document"


async def test_empty_id_list_short_circuits() -> None:
    class _Boom:
        async def scan_references(self, *_args, **_kwargs):
            raise AssertionError("空 id 列表不应触达扫描器")

    ports.register_reference_scanner(_Boom())
    assert await ports.scan_references(
        object(), org_id=uuid4(), kind="entity", ids=[]
    ) == {}


# ---- IncidentRecorder ----


async def test_incident_recording_is_a_no_op_without_a_recorder() -> None:
    await ports.record_incident(
        object(), org_id=uuid4(), kb_id=uuid4(), category="c", code="x"
    )
    assert await ports.count_open_incidents(
        object(), org_id=uuid4(), category="c"
    ) == (0, 0.0)
    assert await ports.purge_incidents(
        object(), org_id=uuid4(), kb_id=uuid4(), image_asset_ids=[uuid4()]
    ) == 0


async def test_registered_recorder_receives_every_call() -> None:
    calls: list[tuple] = []

    class _Recorder:
        async def record(self, _session, *, org_id, kb_id, category, code, image_asset_id=None):
            calls.append(("record", category, code, image_asset_id))

        async def count_open(self, _session, *, org_id, category):
            calls.append(("count", category))
            return 3, 42.0

        async def purge(self, _session, *, org_id, kb_id, image_asset_ids):
            calls.append(("purge", len(image_asset_ids)))
            return len(image_asset_ids)

    ports.set_incident_recorder(_Recorder())
    asset_id = uuid4()
    await ports.record_incident(
        object(),
        org_id=uuid4(),
        kb_id=uuid4(),
        category="media_projection_failure",
        code="missing_object",
        image_asset_id=asset_id,
    )
    assert await ports.count_open_incidents(
        object(), org_id=uuid4(), category="media_projection_failure"
    ) == (3, 42.0)
    assert await ports.purge_incidents(
        object(), org_id=uuid4(), kb_id=uuid4(), image_asset_ids=[asset_id, uuid4()]
    ) == 2
    assert calls == [
        ("record", "media_projection_failure", "missing_object", asset_id),
        ("count", "media_projection_failure"),
        ("purge", 2),
    ]


# ---- Notifier ----


def test_notify_roles_and_link_are_configurable() -> None:
    assert ports.kb_notify_roles() == (Role.ORG_ADMIN,)
    ports.set_kb_notify_roles(["platform_admin", Role.MEMBER])
    assert ports.kb_notify_roles() == (Role.PLATFORM_ADMIN, Role.MEMBER)
    ports.set_kb_notify_roles(None)
    assert ports.kb_notify_roles() == (Role.ORG_ADMIN,)

    ports.set_kb_notify_link("/console/knowledge")
    assert ports.kb_notify_link() == "/console/knowledge"


async def test_notify_degrades_to_zero_when_capability_is_unavailable(
    monkeypatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("nicekit.capabilities"):
            raise ImportError("capabilities not assembled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert (
        await ports.notify_org_roles(
            object(), org_id=uuid4(), kind="kb.test", title="t", body="b"
        )
        == 0
    )


async def test_notify_uses_registered_roles_and_link(monkeypatch) -> None:
    seen: dict = {}
    recipients = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]

    async def org_members_by_role(_session, org_id, *roles):
        seen["roles"] = roles
        return recipients

    async def notify(_session, **kwargs):
        seen.update(kwargs)

    # 打包属性而不是 sys.modules:ports 里用的是 `from nicekit.capabilities import notify`,
    # 该语句优先取包属性,真实模块一旦被别的用例 import 过,sys.modules 替身就会被绕开。
    import nicekit.capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "notify",
        SimpleNamespace(org_members_by_role=org_members_by_role, notify=notify),
    )
    ports.set_kb_notify_link("/console/knowledge")

    count = await ports.notify_org_roles(
        object(), org_id=uuid4(), kind="kb.health_alert", title="t", body="b"
    )
    assert count == 2
    assert seen["roles"] == (Role.ORG_ADMIN,)
    assert seen["link"] == "/console/knowledge"
    assert seen["user_ids"] == [member.id for member in recipients]
    assert seen["email"] is False


async def test_notify_never_raises_when_the_notifier_fails(monkeypatch) -> None:
    async def boom(*_args, **_kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setitem(
        __import__("sys").modules,
        "nicekit.capabilities.notify",
        SimpleNamespace(org_members_by_role=boom, notify=boom),
    )
    assert (
        await ports.notify_org_roles(
            object(), org_id=uuid4(), kind="kb.test", title="t", body="b"
        )
        == 0
    )
