"""外部服务配置读取:service_configs 行覆盖 .env 兜底。

空串/None 的键视为"未配置",回落 .env——管理端清空某字段即恢复环境变量值。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.models.service_config import ServiceConfig


async def load_overrides(session: AsyncSession, name: str) -> dict:
    row = (
        await session.execute(select(ServiceConfig).where(ServiceConfig.name == name))
    ).scalar_one_or_none()
    if row is None:
        return {}
    return {k: v for k, v in (row.payload or {}).items() if v not in ("", None)}
