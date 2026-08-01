"""通知能力:站内信 + 邮件 best-effort,并抽出 `Notifier` 扩展点。

迁移自 TF backend/app/services/notify.py。SDK 化改造(MIGRATION-PLAN §4/§5.6):
- 抽 `Notifier` 协议 + 默认 SQL 实现(`SqlNotifier`,落 Notification 表);
  `set_notifier()` 让宿主换成飞书/企微/webhook 等实现,SDK 内部
  (agent/icron.py、kb/expiry.py)一律经 `notify()` 门面调用,不直连表。
- 邮件主题前缀 `[TravelFlow]` 改用 `settings.app_name`。

保留的原始设计裁决(勿回退):
- notify():批量落 notifications 行,**不 commit**(随调用方事务,同 usage 先例);
  email=True 且 SMTP 已配置时同步发信(一次连接批量收件)。
  不用 create_task 派发邮件——celery 的 asyncio.run 结束即关 loop,
  pending 任务会被静默丢弃(可靠性①踩过的坑);asyncio.to_thread 阻塞点
  在线程池,await 开销即 SMTP 时延,触发点均为低频人工动作,可接受。
- 邮件 best-effort:异常吞掉记 warning;邮件发出后调用方事务回滚会出现
  "有信无站内记录"的边缘,通知类非关键一致性,接受。
- org_members_by_role():Membership⋈User 收件人解析(排除停用用户)。
"""

import asyncio
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.models.tenancy import Membership, Notification, Role, User

logger = logging.getLogger(__name__)


async def org_members_by_role(
    session: AsyncSession, org_id: UUID, *roles: Role | str
) -> list[User]:
    """按角色解析组织内的收件人(排除停用用户)。

    角色可传内置 Role 枚举,也可传 tenancy.roles 注册的自定义角色名字符串。
    """
    role_values = [r.value if isinstance(r, Role) else str(r) for r in roles]
    rows = (
        await session.execute(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.org_id == org_id,
                Membership.role.in_(role_values),
                User.is_active == True,  # noqa: E712  (SQLAlchemy 表达式,非 Python 布尔比较)
            )
        )
    ).scalars().all()
    return list(rows)


def _send_email_sync(recipients: list[str], subject: str, body: str) -> None:
    """线程池内跑(smtplib 阻塞);调用方保证 smtp_host 非空。"""
    settings = get_settings()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.sendmail(settings.smtp_from, recipients, msg.as_string())


async def send_email(recipients: list[str], subject: str, body: str) -> bool:
    """best-effort:未配置 SMTP 或发送失败都不上抛,返回是否发出。"""
    settings = get_settings()
    if not settings.smtp_host or not recipients:
        return False
    try:
        await asyncio.to_thread(_send_email_sync, recipients, subject, body)
        return True
    except Exception as exc:
        logger.warning(
            "邮件发送失败(通知已落站内信)",
            extra={"recipients": recipients, "subject": subject, "error": str(exc)},
        )
        return False


@runtime_checkable
class Notifier(Protocol):
    """通知投递扩展点(MIGRATION-PLAN §4)。

    实现方负责"把一条通知送到这些用户手上",不负责事务边界:SDK 内部调用方
    (agent/icron.py、kb/expiry.py)在自己的事务里调用,实现不得 commit。
    """

    async def notify(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_ids: list[UUID],
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
        email: bool = True,
    ) -> list[Notification]: ...


class SqlNotifier:
    """默认实现:站内信落 notifications 表(去重、不 commit)+ 可选邮件。"""

    async def notify(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        user_ids: list[UUID],
        kind: str,
        title: str,
        body: str,
        link: str | None = None,
        email: bool = True,
    ) -> list[Notification]:
        unique_ids = list(dict.fromkeys(user_ids))  # 保序去重
        if not unique_ids:
            return []
        rows = [
            Notification(
                org_id=org_id, user_id=uid, kind=kind, title=title, body=body, link=link
            )
            for uid in unique_ids
        ]
        session.add_all(rows)

        settings = get_settings()
        if email and settings.smtp_host:
            emails = (
                await session.execute(
                    select(User.email).where(
                        User.id.in_(unique_ids), User.is_active == True  # noqa: E712
                    )
                )
            ).scalars().all()
            if emails:
                await send_email(list(emails), f"[{settings.app_name}] {title}", body)
        return rows


_notifier: Notifier = SqlNotifier()


def set_notifier(notifier: Notifier | None) -> None:
    """注入宿主的通知实现(传 None 恢复默认 SqlNotifier)。"""
    global _notifier
    _notifier = notifier or SqlNotifier()


def get_notifier() -> Notifier:
    return _notifier


async def notify(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_ids: list[UUID],
    kind: str,
    title: str,
    body: str,
    link: str | None = None,
    email: bool = True,
) -> list[Notification]:
    """门面:转发给当前注册的 Notifier。SDK 内部一律经此调用。"""
    return await _notifier.notify(
        session,
        org_id=org_id,
        user_ids=user_ids,
        kind=kind,
        title=title,
        body=body,
        link=link,
        email=email,
    )
