"""外部 ID → UUID 的确定性映射(桥接模式接入用)。

宿主已有的租户/用户主键常常不是 UUID(自增 int、雪花号、邮箱、租户 slug),
而 SDK 的 ``org_id``/``user_id`` 列是 UUID。这里用 uuid5 做确定性派生:
**同样的输入永远得到同样的 UUID**,因此宿主不需要额外存一张映射表,
每次请求现算即可。

```python
from nicekit.tenancy.mapping import tenant_uuid, subject_uuid

org_id = tenant_uuid(user.company_id)        # 42 → 固定 UUID
user_id = subject_uuid(user.id)              # "u_8891" → 固定 UUID
```

命名空间隔离:租户与用户各有独立命名空间,所以 ``tenant_uuid("1")`` 与
``subject_uuid("1")`` 不会撞。宿主还可以传自己的 ``namespace`` 再隔离一层
(多套外部系统接进同一个平台时用)。

⚠️ 映射是单向的:拿到 UUID 反推不出原值。需要展示外部 ID 时,宿主自己在
业务侧保留对照关系;SDK 只关心分区键的稳定与唯一。
"""

from uuid import NAMESPACE_URL, UUID, uuid5

#: 租户命名空间:所有 org_id 的派生根
NAMESPACE_TENANT: UUID = uuid5(NAMESPACE_URL, "https://nicekit.dev/ns/tenant")

#: 主体(用户/服务账号)命名空间
NAMESPACE_SUBJECT: UUID = uuid5(NAMESPACE_URL, "https://nicekit.dev/ns/subject")

#: SDK 内部保留标识的命名空间。与租户命名空间隔开,这样外部租户 ID 哪怕恰好
#: 叫 "__single_tenant__" 也不会撞上 SDK 的保留分区键。
NAMESPACE_RESERVED: UUID = uuid5(NAMESPACE_URL, "https://nicekit.dev/ns/reserved")


def derive_uuid(external_id: object, *, namespace: UUID) -> UUID:
    """把任意外部标识确定性地派生成 UUID。

    ``external_id`` 会先 ``str()`` 再派生,所以 ``42`` 与 ``"42"`` 得到同一个
    UUID —— 这是刻意的:宿主换个类型传参不该换一个租户分区。

    ``None`` 与空白串一律拒绝:它们恰恰是"这个请求没带租户"的信号,而
    ``str(None)`` 是 ``"None"``、并非空串,放过去会让所有缺租户的请求静默
    共用同一个分区——一个看着完全正常、却把不同客户的数据混在一起的分区键。
    没有租户请显式走单租户模式(``single_tenant_resolver``)。
    """
    if external_id is None:
        raise ValueError(
            "external_id 不能为 None:没有租户请显式使用单租户模式 "
            "(nicekit.api.deps.single_tenant_resolver)"
        )
    text = str(external_id).strip()
    if not text:
        raise ValueError("external_id 不能为空:没有租户请显式使用单租户模式")
    return uuid5(namespace, text)


def tenant_uuid(external_id: object, *, namespace: UUID | None = None) -> UUID:
    """外部租户标识 → SDK 的 ``org_id``。"""
    return derive_uuid(external_id, namespace=namespace or NAMESPACE_TENANT)


def subject_uuid(external_id: object, *, namespace: UUID | None = None) -> UUID:
    """外部用户/服务账号标识 → SDK 的 ``user_id``。"""
    return derive_uuid(external_id, namespace=namespace or NAMESPACE_SUBJECT)


__all__ = [
    "NAMESPACE_RESERVED",
    "NAMESPACE_SUBJECT",
    "NAMESPACE_TENANT",
    "derive_uuid",
    "subject_uuid",
    "tenant_uuid",
]
