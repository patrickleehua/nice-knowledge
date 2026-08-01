"""卡片氛围底图的确定性生成入口。

用途边界:本模块只为展示卡片生成"场景氛围背景",产物必须在展示侧明确标注
为 AI 生成的氛围背景,与系统提示中"不得将生成图冒充实拍/实景素材"的禁令
保持一致。prompt 由纯函数确定性拼装(非 LLM 拟稿),品牌硬约束(无文字/
无标志/无二维码/无价格、不可辨识真实地标、禁紫粉渐变与 AI 味浮夸风、
低对比度多留白)逐条写入 prompt。

任何"服务未配置/生成失败/结果为空"情形一律返回 None,由调用方回退 CSS
渐变底图;本模块不抛异常、不额外记账(usage 计量走 generate_images 既有
路径)。
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.capabilities.imagegen.base import ImageGenProvider, ImageGenQuery
from nicekit.capabilities.imagegen.service import generate_images, genimg_object_key
from nicekit.kb.storage import get_object

logger = logging.getLogger(__name__)

AMBIANCE_SIZE = "1024x1536"  # 竖版,匹配卡片画幅
_FALLBACK_PLACE = "自然风景"


def build_ambiance_prompt(subject: str) -> str:
    """确定性拼装氛围底图 prompt:同 subject 恒同输出,品牌硬约束逐条写入。"""
    place = subject.strip() or _FALLBACK_PLACE
    return (
        f"一张{place}的氛围背景图,竖版构图,用作展示卡片的底图。"
        "画面只作氛围背景:柔和的自然光,低对比度、低饱和度,色调温和克制,"
        "大面积留白,视觉重心靠边,适合在画面上层叠加白色文字仍保持清晰可读。"
        "硬性约束(必须全部遵守):"
        "禁止出现任何文字、字母、数字、标语、招牌文案;"
        "禁止出现任何标志、logo、品牌元素、水印;"
        "禁止出现二维码、条形码;"
        "禁止出现价格、金额、货币符号;"
        "禁止刻画可辨识的真实地标细节,远景地标只能做虚化模糊处理;"
        "禁止使用紫粉渐变配色;"
        "禁止 AI 味的浮夸特效、炫光、高饱和霓虹风格;"
        "不要高对比度画面,不要拥挤构图,不要人物特写。"
    )


async def generate_ambiance_base(
    session: AsyncSession,
    org_id: UUID,
    *,
    subject: str,
    provider: ImageGenProvider | None = None,
) -> bytes | None:
    """生成一张场景氛围底图并返回图片字节;任何失败均返回 None,不抛异常。"""
    query = ImageGenQuery(prompt=build_ambiance_prompt(subject), n=1, size=AMBIANCE_SIZE)
    try:
        result = await generate_images(session, org_id=org_id, query=query, provider=provider)
    except Exception:  # generate_images 已自兜底,这里保证入口绝不外抛
        logger.exception("ambiance base generation escaped subject=%s", subject.strip())
        return None
    if result.get("status") != "ok" or not result.get("images"):
        return None
    filename = result["images"][0].get("filename")
    if not filename:
        return None
    try:
        data = await get_object(genimg_object_key(org_id, filename))
    except Exception:
        logger.exception("ambiance base fetch failed key suffix=%s", filename)
        return None
    return data or None
