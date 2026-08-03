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


def shared_knowledge_read_sql(table_name: str, platform_org_id: UUID | str) -> str:
    """已发布知识的三层只读旁路 SQL(本 org / 平台 org / 被显式分享)。

    与 ``org_isolation`` 是 **permissive 叠加**(PostgreSQL 同命令的多条
    permissive 策略取 OR):SELECT 时两条并集 → 三层可见;INSERT/UPDATE/DELETE
    仍只受 ``org_isolation`` 约束 → 写入面一点没放宽。这样不必动既有策略。

    分享分支查 ``kb_shares``,而 **RLS 策略里的子查询同样受被查表自己的 RLS
    约束**。``kb_shares`` 原本只有 owner 可见,grantee 查出来恒为空、整条分享
    链路静默失效(实测踩过)。所以调用方必须先给 ``kb_shares`` 加一条
    ``grantee_read`` 的 SELECT 策略,让被授权方读得到分享关系。
    """
    org = UUID(str(platform_org_id))
    return f"""
        CREATE POLICY shared_knowledge_read ON {table_name} FOR SELECT
        USING (
            org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            OR org_id = '{org}'::uuid
            OR EXISTS (
                SELECT 1 FROM kb_shares s
                 WHERE s.kb_id = {table_name}.kb_id
                   AND s.grantee_org_id
                       = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            )
        )
    """


def op_shared_knowledge_read(op, table_name: str, platform_org_id: UUID | str) -> None:
    """给一张带 ``kb_id`` 的已发布知识表加三层只读旁路。"""
    op.execute(shared_knowledge_read_sql(table_name, platform_org_id))
