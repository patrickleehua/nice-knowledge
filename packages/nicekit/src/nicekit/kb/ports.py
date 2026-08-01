"""KB 子系统的宿主扩展点(MIGRATION-PLAN §4)。

SDK 内部禁止 import 任何宿主/业务代码,但 KB 的两条治理链路天然需要知道
"框架之外还有谁在引用这份知识":

- **ReferenceScanner**:purge / 投影 GC / 生命周期删除前的外部引用计数。
  TF 里这一步直接 import 了业务表(``kb/reference_registry.py:13-25``、
  ``kb/projection_gc.py:11-19``),SDK 改为宿主注册实现;默认无注册 = 无外部
  引用,治理链路照常工作(只是不会被业务对象挡住)。
- **IncidentRecorder**:运维事件登记(媒体投影失败等)。事件表属 operations
  子系统,不在 KB 的表集合里;默认实现为空操作,登记失败永远不改变业务结果。

两者都是"注册即生效、缺省即降级"的模块级注册表,注册顺序无关,SDK 自身不
依赖任何一个存在。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.tenancy import Role

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 外部引用扫描
# ---------------------------------------------------------------------------

#: scan_references 的 kind 词表(KB 侧可被外部引用的对象种类)
REFERENCE_KINDS = frozenset(
    {
        "knowledge_base",
        "snapshot",
        "document",
        "revision",
        "fact",
        "evidence",
        "entity",
        "media",
    }
)


@runtime_checkable
class ReferenceScanner(Protocol):
    """宿主实现:统计业务侧对一批 KB 对象的引用数。

    必须只读、可重入、不抛异常优先(异常会被调用方按"引用清单不完整"处理)。
    返回值只需包含"有引用"的 id;缺失即视作 0。
    """

    async def scan_references(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        kind: str,
        ids: Sequence[UUID],
    ) -> dict[UUID, int]: ...


_reference_scanners: list[ReferenceScanner] = []


def register_reference_scanner(scanner: ReferenceScanner) -> None:
    """注册一个外部引用扫描器(可多次注册,计数按 id 累加)。"""
    if scanner not in _reference_scanners:
        _reference_scanners.append(scanner)


def reset_reference_scanners() -> None:
    """清空注册表(测试与宿主重装配使用)。"""
    _reference_scanners.clear()


def reference_scanners() -> tuple[ReferenceScanner, ...]:
    return tuple(_reference_scanners)


async def scan_references(
    session: AsyncSession,
    *,
    org_id: UUID,
    kind: str,
    ids: Sequence[UUID],
) -> dict[UUID, int]:
    """聚合全部已注册扫描器的引用计数;无注册实现时返回空(= 无外部引用)。"""
    if not ids or not _reference_scanners:
        return {}
    totals: dict[UUID, int] = {}
    for scanner in _reference_scanners:
        counts = await scanner.scan_references(
            session, org_id=org_id, kind=kind, ids=list(ids)
        )
        for target_id, count in (counts or {}).items():
            if count:
                totals[target_id] = totals.get(target_id, 0) + int(count)
    return totals


async def referenced_ids(
    session: AsyncSession,
    *,
    org_id: UUID,
    kind: str,
    ids: Sequence[UUID],
) -> set[UUID]:
    """scan_references 的布尔视图:被外部引用的 id 集合。"""
    return {target_id for target_id, count in
            (await scan_references(session, org_id=org_id, kind=kind, ids=ids)).items()
            if count}


# ---------------------------------------------------------------------------
# 运维事件登记
# ---------------------------------------------------------------------------


@runtime_checkable
class IncidentRecorder(Protocol):
    """宿主实现:KB 运维事件的登记/统计/清理(事件表不属于 KB schema)。"""

    async def record(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        kb_id: UUID,
        category: str,
        code: str,
        image_asset_id: UUID | None = None,
    ) -> None: ...

    async def count_open(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        category: str,
    ) -> tuple[int, float]:
        """返回 (未解决事件数, 最老事件年龄秒)。"""
        ...

    async def purge(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        kb_id: UUID,
        image_asset_ids: Sequence[UUID],
    ) -> int: ...


_incident_recorder: IncidentRecorder | None = None


def set_incident_recorder(recorder: IncidentRecorder | None) -> None:
    global _incident_recorder
    _incident_recorder = recorder


def get_incident_recorder() -> IncidentRecorder | None:
    return _incident_recorder


async def record_incident(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    category: str,
    code: str,
    image_asset_id: UUID | None = None,
) -> None:
    """登记运维事件;无注册实现即静默跳过(登记是诊断,不改变业务结果)。"""
    if _incident_recorder is None:
        return
    await _incident_recorder.record(
        session,
        org_id=org_id,
        kb_id=kb_id,
        category=category,
        code=code,
        image_asset_id=image_asset_id,
    )


async def count_open_incidents(
    session: AsyncSession, *, org_id: UUID, category: str
) -> tuple[int, float]:
    if _incident_recorder is None:
        return 0, 0.0
    return await _incident_recorder.count_open(
        session, org_id=org_id, category=category
    )


async def purge_incidents(
    session: AsyncSession,
    *,
    org_id: UUID,
    kb_id: UUID,
    image_asset_ids: Sequence[UUID],
) -> int:
    if _incident_recorder is None or not image_asset_ids:
        return 0
    return await _incident_recorder.purge(
        session, org_id=org_id, kb_id=kb_id, image_asset_ids=list(image_asset_ids)
    )


# ---------------------------------------------------------------------------
# 站内通知(Notifier 协议的 KB 侧适配)
# ---------------------------------------------------------------------------

#: KB 治理通知的默认收件角色。TF 用 org_admin/operator,SDK 只有三个内置角色,
#: 宿主注册了业务角色后可调 :func:`set_kb_notify_roles` 追加(存字符串:自定义
#: 角色不进 Role 枚举,Role 是 StrEnum 所以两者可直接比较)。
_notify_roles: tuple[str, ...] = (Role.ORG_ADMIN.value,)
#: 通知里的落地链接(前端路由由宿主决定)
_notify_link: str | None = "/org/kb"


def set_kb_notify_roles(roles: Sequence[Role | str] | None) -> None:
    global _notify_roles
    _notify_roles = tuple(
        role.value if isinstance(role, Role) else str(role) for role in (roles or ())
    ) or (Role.ORG_ADMIN.value,)


def kb_notify_roles() -> tuple[str, ...]:
    return _notify_roles


def set_kb_notify_link(link: str | None) -> None:
    global _notify_link
    _notify_link = link


def kb_notify_link() -> str | None:
    return _notify_link


async def notify_org_roles(
    session: AsyncSession,
    *,
    org_id: UUID,
    kind: str,
    title: str,
    body: str,
    link: str | None = None,
    email: bool = False,
    roles: Sequence[Role | str] | None = None,
) -> int:
    """给 org 内指定角色发站内信;通知能力不可用时降级为不通知,返回收件人数。

    Notifier 走延迟 import(``nicekit.capabilities.notify``,MIGRATION-PLAN §4):
    KB 治理链路不因通知子系统缺席而失败——扫描/清理是主业,通知是附带效果。
    """
    try:  # 延迟 import:capabilities 是可选装配件
        from nicekit.capabilities import notify as notifier
    except ImportError:
        logger.debug("Notifier 不可用,KB 通知降级为不发送(kind=%s)", kind)
        return 0
    try:
        members = await notifier.org_members_by_role(
            session, org_id, *(roles or _notify_roles)
        )
        recipients = [member.id for member in members]
        await notifier.notify(
            session,
            org_id=org_id,
            user_ids=recipients,
            kind=kind,
            title=title,
            body=body,
            link=_notify_link if link is None else link,
            email=email,
        )
    except Exception:  # noqa: BLE001 - 通知永远不阻断治理链路
        logger.warning("KB 通知发送失败(kind=%s),已跳过", kind, exc_info=True)
        return 0
    return len(recipients)


__all__ = [
    "REFERENCE_KINDS",
    "IncidentRecorder",
    "ReferenceScanner",
    "kb_notify_link",
    "kb_notify_roles",
    "notify_org_roles",
    "set_kb_notify_link",
    "set_kb_notify_roles",
    "count_open_incidents",
    "get_incident_recorder",
    "purge_incidents",
    "record_incident",
    "reference_scanners",
    "referenced_ids",
    "register_reference_scanner",
    "reset_reference_scanners",
    "scan_references",
    "set_incident_recorder",
]
