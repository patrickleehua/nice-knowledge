"""生成图片取回 API:鉴权流式转发 MinIO(不走预签名)。

key 由 ctx.org_id 拼出({org_id}/genimg/{filename}),org 隔离天然成立——
其他租户拿到 URL 也取不到本 org 的对象。
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from minio.error import S3Error

from nicekit.api.deps import OrgContext, get_org_context
from nicekit.capabilities.imagegen.service import genimg_object_key
from nicekit.kb.storage import get_object

router = APIRouter()

Ctx = Annotated[OrgContext, Depends(get_org_context)]

_FILENAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(png|jpg|webp)$"
)
_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@router.get("/genimg/{filename}")
async def get_generated_image(filename: str, ctx: Ctx) -> Response:
    match = _FILENAME.match(filename)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
    try:
        data = await get_object(genimg_object_key(ctx.org_id, filename))
    except S3Error as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在") from exc
    return Response(
        content=data,
        media_type=_MIME[match.group(1)],
        headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
