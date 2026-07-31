"""角色注册表(MIGRATION-PLAN §5.2 改造点 1:Role 泛化)。

内置角色只有三个:
- platform_admin:平台管理员,只能由平台侧 seed / admin API 产生,永不可在租户内授予;
- org_admin / member:租户内可授予。

宿主在启动期通过 register_roles() 注册业务角色(幂等,重复注册同名为 no-op)。
角色值落库为 varchar(32)(models/tenancy.py):加角色不动 DDL、不 ALTER TYPE。
api/v1/members.py 的"租户内可授予角色"集合由 assignable_roles() 派生。
"""

import re

PLATFORM_ADMIN = "platform_admin"
ORG_ADMIN = "org_admin"
MEMBER = "member"

# 落库 varchar(32):小写字母开头,字母/数字/下划线,总长 <= 32
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_BUILTIN_ASSIGNABLE: tuple[str, ...] = (ORG_ADMIN, MEMBER)

# 宿主注册的角色(dict 保序、天然去重);值 = 是否可在租户内授予
_registered: dict[str, bool] = {}


def register_roles(*names: str, assignable: bool = True) -> None:
    """启动期注册宿主角色(幂等,可多次调用)。

    - 注册内置可授予角色(org_admin/member)是 no-op;
    - platform_admin 是平台保留角色,注册它一律报错(防止被放进可授予集合)。
    """
    for name in names:
        if name == PLATFORM_ADMIN:
            raise ValueError("platform_admin 是平台保留角色,不可注册/授予")
        if name in _BUILTIN_ASSIGNABLE:
            continue
        if not _NAME_RE.match(name):
            raise ValueError(f"非法角色名 {name!r}(需匹配 {_NAME_RE.pattern})")
        _registered[name] = assignable


def all_roles() -> tuple[str, ...]:
    """全部已知角色(内置在前,宿主注册的按注册顺序在后)。"""
    return (PLATFORM_ADMIN, *_BUILTIN_ASSIGNABLE, *_registered)


def assignable_roles() -> frozenset[str]:
    """租户内可授予的角色:内置 org_admin/member + 宿主注册且 assignable 的角色。"""
    return frozenset(_BUILTIN_ASSIGNABLE) | {
        name for name, assignable in _registered.items() if assignable
    }


def reset_registered_roles() -> None:
    """清空宿主注册的角色(测试 / 重新装配用;不影响内置角色)。"""
    _registered.clear()
