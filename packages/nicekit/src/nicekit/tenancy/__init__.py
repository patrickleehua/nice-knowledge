"""租户底座:组织/成员/角色,以及**外部身份接入所需的全部零件**。

桥接与单租户模式下宿主只关心四件事,这里统一 re-export,免得宿主到处找模块:

```python
from nicekit.tenancy import ensure_org, register_roles, subject_uuid, tenant_uuid

org_id = tenant_uuid(user.company_id)      # 外部租户主键 → 分区键(确定性)
user_id = subject_uuid(user.id)            # 外部用户主键 → 操作者
await ensure_org(session, org_id)          # 首见新租户时垫一行(幂等)
register_roles("editor")                   # 装配期登记自家角色
register_write_roles("editor")             # 让它能写知识库
```

身份解析本身**不在这里**:那是请求期的事,切换点在 ``nicekit.api.deps`` 的
``set_principal_resolver()`` / ``single_tenant_resolver()``。这条分界是有意的
——本包管"租户数据长什么样",deps 管"这个请求属于谁"。
"""

from nicekit.tenancy.mapping import subject_uuid, tenant_uuid
from nicekit.tenancy.orgs import ensure_org, ensure_principal, ensure_user
from nicekit.tenancy.roles import register_roles, register_write_roles, write_roles

__all__ = [
    "ensure_org",
    "ensure_principal",
    "ensure_user",
    "register_roles",
    "register_write_roles",
    "subject_uuid",
    "tenant_uuid",
    "write_roles",
]
