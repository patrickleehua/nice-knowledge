"""RLS helper(MIGRATION-PLAN §5.2 改造点 2):迁移里统一施加 org 级行隔离。

org_isolation 策略 SQL 严格沿用 TF baseline(ae23a2c746c9_tenancy.py)的写法:
NULLIF 防空串——set_config(..., true) 的事务结束后 GUC 在连接上残留为
''(非 NULL),直接 ::uuid 会报错;NULLIF 使未设置上下文时策略恒为假
(fail-closed:没有 org 上下文 = 什么都看不到、什么都写不进)。

platform_read 参照 TF d4a7e0b2c913_usage_platform_read.py:permissive 策略
按 OR 合并,租户读写仍走 org_isolation,本策略只放开平台 org 上下文的
SELECT,写入面不变。platform_org_id 作参数传入(不再硬编码平台 UUID)。

登记表:op_enable_org_rls 每施加一张表就登记到模块级 set;
rls_tables_check(metadata) 返回"带 org_id 列但未登记 RLS"的表名清单,
供 baseline 迁移末尾自检(设计约束:凡带 org_id 的业务表必须开 RLS;
身份表 memberships/invitations/refresh_tokens 是刻意的例外,由调用方豁免)。
"""

from uuid import UUID

# 已通过 op_enable_org_rls 施加 RLS 的表名(进程级登记)
_rls_enabled_tables: set[str] = set()


def org_isolation_sql(table_name: str) -> list[str]:
    """生成对一张表启用 org 级 RLS 的三条 SQL(op_enable_org_rls 与测试共用)。"""
    return [
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY",
        f"""
            CREATE POLICY org_isolation ON {table_name}
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """,
    ]


def platform_read_sql(table_name: str, platform_org_id: UUID | str) -> str:
    """生成平台只读旁路策略 SQL(先把 platform_org_id 规整为 UUID,杜绝拼接注入)。"""
    org = UUID(str(platform_org_id))
    return f"""
        CREATE POLICY platform_read ON {table_name} FOR SELECT
        USING (
            NULLIF(current_setting('app.current_org_id', true), '')::uuid
                = '{org}'::uuid
        )
        """


def op_enable_org_rls(op, table_name: str) -> None:
    """ENABLE + FORCE ROW LEVEL SECURITY + org_isolation 策略,并登记该表。"""
    for sql in org_isolation_sql(table_name):
        op.execute(sql)
    _rls_enabled_tables.add(table_name)


def op_platform_read(op, table_name: str, platform_org_id: UUID | str) -> None:
    """平台 org 上下文可读全租户行(SELECT 旁路);写入面不变。"""
    op.execute(platform_read_sql(table_name, platform_org_id))


def rls_tables_check(metadata) -> list[str]:
    """一致性检查:返回带 org_id 列、但尚未经 op_enable_org_rls 登记的表名。

    在 baseline 迁移末尾调用,非空即说明有表漏开 RLS(身份表等刻意例外
    由调用方在结果里豁免后再断言)。
    """
    return sorted(
        table.name
        for table in metadata.tables.values()
        if "org_id" in table.columns and table.name not in _rls_enabled_tables
    )
