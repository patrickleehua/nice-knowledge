"""存活与就绪端点。

``/health`` 是纯存活探针(不碰任何依赖,限流默认豁免它);``/ready`` 逐依赖
有界探测(DB / 活跃快照 / redis / 对象存储 / 配置),不就绪返回 503。
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    # 延迟 import:存活探针不该被 readiness 的依赖链(KB storage 等)拖住
    from nicekit.operations import readiness

    report = await readiness.collect_readiness()
    return JSONResponse(
        status_code=200 if report.ready else 503,
        content=report.as_dict(),
    )
