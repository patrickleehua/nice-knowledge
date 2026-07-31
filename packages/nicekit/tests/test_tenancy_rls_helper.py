"""RLS helper 单测(离线):SQL 生成与登记/一致性检查。

真实 PG 上的执行与隔离行为由 test_tenancy_rls_isolation.py(live)覆盖。
"""

import pytest
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from nicekit.migrations import rls


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(str(sql))


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(rls, "_rls_enabled_tables", set())


def test_org_isolation_sql_matches_tf_baseline_shape():
    op = _RecordingOp()
    rls.op_enable_org_rls(op, "audit_logs")
    assert op.statements[0] == "ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY"
    assert op.statements[1] == "ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY"
    policy = op.statements[2]
    assert "CREATE POLICY org_isolation ON audit_logs" in policy
    # NULLIF fail-closed 写法在 USING 与 WITH CHECK 各出现一次
    nullif = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
    assert policy.count(nullif) == 2
    assert "USING" in policy
    assert "WITH CHECK" in policy


def test_platform_read_sql_parameterized_org_id():
    sql = rls.platform_read_sql("usage_daily", "00000000-0000-0000-0000-000000000001")
    assert "CREATE POLICY platform_read ON usage_daily FOR SELECT" in sql
    assert "'00000000-0000-0000-0000-000000000001'::uuid" in sql
    assert "WITH CHECK" not in sql  # 只放开 SELECT,写入面不变


def test_platform_read_sql_rejects_non_uuid():
    with pytest.raises(ValueError):
        rls.platform_read_sql("usage_daily", "1; DROP TABLE usage_daily")


def test_rls_tables_check_flags_unregistered_org_tables():
    md = MetaData()
    Table("guarded", md, Column("org_id", PGUUID(as_uuid=True)))
    Table("forgotten", md, Column("org_id", PGUUID(as_uuid=True)))
    Table("no_org_col", md, Column("id", PGUUID(as_uuid=True)))
    rls.op_enable_org_rls(_RecordingOp(), "guarded")
    assert rls.rls_tables_check(md) == ["forgotten"]
