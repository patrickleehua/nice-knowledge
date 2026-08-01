"""知识库完整性巡检 API:孤儿数据扫描(report-only)与管理员显式触发的清理执行。

对标 OWASP RAG Security 的孤儿数据审计建议;扫描与清理口径见
services/kb/orphan_audit.py。RLS 按会话 org 上下文过滤,返回的是
本 org 视角的报告;权限收紧到 org_admin / platform_admin(巡检结果
含内部表名与残留计数,清理更是破坏性操作,不对普通角色暴露)。

清理的安全模型:只删"已获批删除却残留"的数据(挂在已 purged 文档 / 知识库
名下的行),执行前重扫比对前端展示的期望数量,超出即拒绝(orphan_report_stale)。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.api.deps import OrgContext, get_org_session, require_role
from nicekit.kb.orphan_audit import (
    OrphanCleanupError,
    OrphanCleanupExpectation,
    cleanup_orphans,
    scan_orphans,
)
from nicekit.models.tenancy import Role

router = APIRouter(prefix="/kb")


@router.get("/integrity/orphans")
async def get_kb_orphan_report(
    ctx: Annotated[
        OrgContext, Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> dict[str, Any]:
    """现场跑一轮孤儿扫描并返回 JSON 报告(只读;定期落审计走后台巡检)。"""
    report = await scan_orphans(session)
    return report.to_dict()


class OrphanCleanupBody(BaseModel):
    """清理请求体:必须显式确认,并携带前端展示报告中的每类期望数量。"""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(description="必须显式为 true 才执行清理")
    expected_counts: dict[str, int] = Field(
        description="每类孤儿的期望数量,键为巡检报告 categories 的类别名;缺失按 0 处理"
    )


@router.post("/integrity/orphans/cleanup")
async def cleanup_kb_orphans(
    body: OrphanCleanupBody,
    ctx: Annotated[
        OrgContext, Depends(require_role(Role.ORG_ADMIN, Role.PLATFORM_ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_org_session)],
) -> dict[str, Any]:
    """管理员显式触发的孤儿清理:单事务删除 a/b 类残留,c 类走 operation 重试通道。"""
    if body.confirm is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "code": "confirmation_required",
                "message": "孤儿清理为破坏性操作,必须显式确认(confirm=true)",
            },
        )
    # 期望数缺失按 0 处理:实际数 > 0 时会触发 orphan_report_stale,天然 fail-safe
    expectation = OrphanCleanupExpectation(
        purged_document_chunks=body.expected_counts.get("purged_document_chunks", 0),
        purged_kb_residual_rows=body.expected_counts.get("purged_kb_residual_rows", 0),
        purge_registry_stale_objects=body.expected_counts.get(
            "purge_registry_stale_objects", 0
        ),
    )
    try:
        result = await cleanup_orphans(
            session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            expected=expectation,
        )
    except OrphanCleanupError as exc:
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.code == "orphan_report_stale"
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise HTTPException(
            status_code,
            {"code": exc.code, "message": exc.message, "detail": exc.detail},
        ) from exc
    return result.to_dict()
