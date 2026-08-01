"""实体类型注册表:类型 seed、JSON Schema 强校验、卡片渲染。

设计:类型层可注册(改配置不动迁移),校验层保留数据库能力——LLM 抽取输出与
人工编辑共用同一份 field_schema 强校验;filterable_fields 声明哪些属性参与
结构化检索过滤(JSONB 表达式条件 + GIN)。

SDK 边界(MIGRATION-PLAN §4 "实体类型 seed"):框架自身**不内置任何领域类型**,
只 seed 一个领域无关的 ``concept`` 兜底类型;行业类型(旅游/电商/法务……)由宿主
把 spec 列表传给 :func:`ensure_entity_types` 注册。全部实体数据统一落 kb_entities。
"""

from collections.abc import Sequence
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.core.config import get_settings
from nicekit.models.kb import EntityReviewPolicy, KbEntityType

#: filterable_fields 声明支持的过滤类型(检索层据此构建 JSONB 表达式)
FILTERABLE_TYPES = ("text", "number", "date")

_NAME_FIELD = "name"


class EntityTypeInvalid(Exception):
    """类型定义非法(schema/过滤字段声明不合规)。"""


class EntityValidationError(Exception):
    """实体属性未通过所属类型的 JSON Schema 校验。"""


def validate_field_schema(schema: dict) -> None:
    """类型 field_schema 必须是合法 JSON Schema 的 object 定义,且强制含
    必填字符串属性 name(kb_entities.name 提升列与实体卡标题的来源)。"""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise EntityTypeInvalid("field_schema 必须是 type=object 的 JSON Schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise EntityTypeInvalid("field_schema.properties 不能为空")
    name_def = properties.get(_NAME_FIELD)
    if not isinstance(name_def, dict) or name_def.get("type") != "string":
        raise EntityTypeInvalid('field_schema 必须定义字符串属性 "name"(实体名)')
    if _NAME_FIELD not in (schema.get("required") or []):
        raise EntityTypeInvalid('"name" 必须在 field_schema.required 中')
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema_exceptions.SchemaError as exc:
        raise EntityTypeInvalid(f"field_schema 不是合法 JSON Schema:{exc.message}") from exc


def validate_filterable_fields(filterable: list, schema: dict) -> None:
    """过滤字段声明必须引用 schema 已定义的属性,类型限 text/number/date。"""
    properties = schema.get("properties") or {}
    seen: set[str] = set()
    for item in filterable:
        if not isinstance(item, dict) or not item.get("field"):
            raise EntityTypeInvalid("filterable_fields 每项需要 field 名")
        field = str(item["field"])
        if field in seen:
            raise EntityTypeInvalid(f"过滤字段重复声明:{field}")
        seen.add(field)
        if field not in properties:
            raise EntityTypeInvalid(f"过滤字段 {field} 未在 field_schema 中定义")
        if item.get("type") not in FILTERABLE_TYPES:
            raise EntityTypeInvalid(
                f"过滤字段 {field} 的 type 必须是 {'/'.join(FILTERABLE_TYPES)}"
            )


def validate_entity_attributes(entity_type: KbEntityType, attributes: dict) -> str:
    """强校验层(LLM 抽取与人工编辑共用):不通过抛 EntityValidationError。
    返回实体名(attributes.name,已 strip)。"""
    validator = Draft202012Validator(entity_type.field_schema)
    errors = sorted(validator.iter_errors(attributes), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = ".".join(str(p) for p in first.path) or "(根)"
        raise EntityValidationError(
            f"{entity_type.type_key} 属性校验失败:{where}: {first.message}"
        )
    name = str(attributes.get(_NAME_FIELD) or "").strip()
    if not name:
        raise EntityValidationError(f"{entity_type.type_key} 实体缺少 name")
    return name


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:  # 模板引用了缺失字段 → 空串
        return ""


def render_entity_card(entity_type: KbEntityType, attributes: dict) -> str:
    """按类型卡片模板产出实体卡文本(进 chunk 检索通道);无模板时按
    "显示名:name。字段:值" 兜底拼接标量字段。"""
    values = {
        k: ("" if v is None else "、".join(map(str, v)) if isinstance(v, list) else v)
        for k, v in attributes.items()
        if not isinstance(v, dict)
    }
    if entity_type.card_template:
        return entity_type.card_template.format_map(_SafeFormat(values)).strip()
    parts = [f"{entity_type.display_name}:{values.get(_NAME_FIELD, '')}"]
    parts += [
        f"{k}:{v}" for k, v in values.items() if k != _NAME_FIELD and str(v).strip()
    ]
    return ",".join(parts) + "。"


async def get_entity_type(
    session: AsyncSession, org_id: UUID, type_key: str
) -> KbEntityType | None:
    """按 key 解析类型定义:本 org 自定义优先,其次平台内置(RLS 已保证可见性)。"""
    rows = (
        (
            await session.execute(
                select(KbEntityType).where(KbEntityType.type_key == type_key)
            )
        )
        .scalars()
        .all()
    )
    own = next((r for r in rows if r.org_id == org_id), None)
    return own or next((r for r in rows if r.is_builtin), None)


# ---------------------------------------------------------------------------
# 兜底类型 seed(SDK 只提供一个领域无关的 concept;行业类型由宿主注册)
# ---------------------------------------------------------------------------

#: 泛化兜底类型 key:实体绑定白名单与抽取提示词在无注册类型时回落到它
FALLBACK_TYPE_KEY = "concept"

#: SDK 默认 seed 的唯一类型定义。宿主把自己的行业类型 spec 追加进
#: ``ensure_entity_types(session, org_id, specs)`` 即可(见 MIGRATION-PLAN §4)。
DEFAULT_ENTITY_TYPE_SPECS: tuple[dict, ...] = (
    {
        "type_key": FALLBACK_TYPE_KEY,
        "display_name": "概念",
        "description": "领域无关的兜底实体类型:任何未注册专用类型的名词性实体都落在这里。",
        "field_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "summary": {"type": ["string", "null"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "filterable_fields": [
            {"field": "name", "type": "text", "label": "名称"},
        ],
        "card_template": None,
        "review_policy": EntityReviewPolicy.AI.value,
    },
)


async def ensure_entity_types(
    session: AsyncSession,
    org_id: UUID | None = None,
    specs: Sequence[dict] | None = None,
) -> int:
    """幂等同步一组实体类型定义,返回新建行数。

    - ``org_id`` 缺省为平台 org(内置类型的归属),宿主也可传自己的 org
      来 seed 租户私有类型。
    - ``specs`` 缺省为 :data:`DEFAULT_ENTITY_TYPE_SPECS`(只有 concept 兜底)。
      行业类型(旅游/电商/法务……)由宿主自行传入,SDK 不内置任何领域词表。

    内置 schema 属于应用契约,不是租户自定义数据;已有 ``is_builtin`` 行随传入
    定义更新,租户自建类型以及内置类型的审核策略保持不动。
    须在平台 org 上下文/超级会话中调用(与其他平台级 seed 同款约束)。
    """
    target_org = org_id or get_settings().platform_org_id
    payloads = tuple(DEFAULT_ENTITY_TYPE_SPECS if specs is None else specs)
    for spec in payloads:
        validate_field_schema(spec["field_schema"])
        validate_filterable_fields(spec.get("filterable_fields") or [], spec["field_schema"])
    # RLS FORCE 下写平台行必须带平台 org 上下文(事务级 GUC,超级用户亦无害)
    await session.execute(
        sa_text("SELECT set_config('app.current_org_id', :org, true)"),
        {"org": str(target_org)},
    )
    created = 0
    for spec in payloads:
        existing = (
            await session.execute(
                select(KbEntityType).where(
                    KbEntityType.org_id == target_org,
                    KbEntityType.type_key == spec["type_key"],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                KbEntityType(
                    org_id=target_org,
                    type_key=spec["type_key"],
                    display_name=spec["display_name"],
                    description=spec.get("description"),
                    field_schema=spec["field_schema"],
                    filterable_fields=spec.get("filterable_fields") or [],
                    card_template=spec.get("card_template"),
                    review_policy=spec.get("review_policy") or EntityReviewPolicy.AI.value,
                    is_builtin=True,
                )
            )
            created += 1
        elif existing.is_builtin:
            existing.display_name = spec["display_name"]
            existing.description = spec.get("description")
            existing.field_schema = spec["field_schema"]
            existing.filterable_fields = spec.get("filterable_fields") or []
            existing.card_template = spec.get("card_template")
            session.add(existing)
    return created
