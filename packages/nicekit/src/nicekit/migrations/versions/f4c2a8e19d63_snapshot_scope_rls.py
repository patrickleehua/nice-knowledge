"""补建快照作用域的 RESTRICTIVE 策略(投影表的第二道防线)

Revision ID: f4c2a8e19d63
Revises: e3f5a71c8b42
Create Date: 2026-08-03

**又一处 SDK 化时漏搬的保护。** 源项目给带 ``snapshot_id`` 的投影表加了四条
RESTRICTIVE 策略,把"这一行属于哪个快照"变成数据库级约束:

- ``snapshot_visibility`` (SELECT):只看得到 KB 的 **active 快照**,或本次
  正在构建的快照(``app.build_snapshot_id``);
- ``snapshot_insert_scope`` / ``snapshot_update_scope``:只能写正在构建
  (``status='building'``)的那个快照;
- ``snapshot_delete_scope``:只能删非 active 快照的行。

漏搬的后果不是立刻出错,而是**第二道防线消失**:应用层的
``effective_scope`` 谓词(``current_snapshot_filter`` 等)仍在过滤,可一旦哪条
查询忘了带,就会静默读到已退休快照的旧知识——而这正是 RLS 该兜住的那类错误。
更糟的是,代码里遍布 ``set_config('app.build_snapshot_id', …)``(snapshot.py
构建期、lifecycle.py purge、projection_gc.py 回收),它们在配合一套**并不存在**
的策略,读代码的人会以为投影表有快照级保护。

应用层本就是照这套策略写的,所以补回来不需要改一行业务代码:
- 构建期 ``snapshot.py`` 在 ``begin_nested()`` 里设好 GUC 才跑 ``build_all``;
- purge 与 GC 逐快照设 GUC 后分批删;
- ``api/v1/kb.py`` 的 wiki 草稿审核走 ``app.wiki_review_write`` 例外(见下)。

**与源项目的差异**:去掉了 ``legacy_*`` 兼容分支。那是给"snapshot_id 为 NULL
的历史行"准备的,新库从 baseline 起就没有这种行。少一个恒假分支,策略更好读。

**RESTRICTIVE 语义提醒**:它与 permissive 策略取 **AND**。所以这些表同时受
``org_isolation``(租户)、``shared_knowledge_read``(三层可见)与本组策略
(快照作用域)三重约束——租户对了、层次对了,快照不对照样看不见。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4c2a8e19d63"
down_revision: str | None = "e3f5a71c8b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 带 snapshot_id 的投影表。kb_pages 的 snapshot_id 是 NOT NULL,其余可空,
#: 但策略对两者写法一致(NULL 既不等于 active 也不等于 build,自然不可见)。
_PROJECTION_TABLES: tuple[str, ...] = (
    "kb_chunks",
    "kb_entities",
    "kb_graph_edges",
    "kb_pages",
    "kb_snapshot_entity_nodes",
    "kb_snapshot_entity_node_supports",
    "kb_snapshot_image_assets",
    "snapshot_fact_supports",
    "snapshot_projection_supports",
)

_BUILD = "NULLIF(current_setting('app.build_snapshot_id', true), '')::uuid"


def _active(table: str) -> str:
    return (
        f"(SELECT kb.active_snapshot_id FROM knowledge_bases kb "
        f"WHERE kb.id = {table}.kb_id)"
    )


def _build_write(table: str) -> str:
    """可写 = 这行属于本次构建的快照,且那个快照确实还在 building。"""
    return f"""
        {table}.snapshot_id = {_BUILD}
        AND EXISTS (
            SELECT 1 FROM knowledge_snapshots s
             WHERE s.id = {table}.snapshot_id
               AND s.org_id = {table}.org_id
               AND s.kb_id = {table}.kb_id
               AND s.status = 'building'
        )
    """


def _gc_delete(table: str) -> str:
    """可删 = 属于本次操作指定的快照,且那个快照不是 active(别删线上的)。"""
    return f"""
        {table}.snapshot_id = {_BUILD}
        AND EXISTS (
            SELECT 1 FROM knowledge_snapshots s
             WHERE s.id = {table}.snapshot_id
               AND s.org_id = {table}.org_id
               AND s.kb_id = {table}.kb_id
               AND s.status <> 'active'
        )
    """


def upgrade() -> None:
    for table in _PROJECTION_TABLES:
        op.execute(
            f"""
            CREATE POLICY snapshot_visibility ON {table}
            AS RESTRICTIVE FOR SELECT
            USING (
                {table}.snapshot_id IS NOT DISTINCT FROM {_active(table)}
                OR {table}.snapshot_id = {_BUILD}
            )
            """
        )
        op.execute(
            f"""
            CREATE POLICY snapshot_insert_scope ON {table}
            AS RESTRICTIVE FOR INSERT WITH CHECK ({_build_write(table)})
            """
        )

        # kb_pages 额外放行 wiki 草稿审核:审核是对 **active 快照** 里的页面做
        # 治理动作(发布/驳回),不属于任何构建过程,却必须能改。用一次性 GUC
        # app.wiki_review_write 显式开门(api/v1/kb.py 的审核端点会设),
        # 比放宽整条策略安全 —— 没设这个开关时,active 快照依然只读。
        update_clause = _build_write(table)
        if table == "kb_pages":
            update_clause = f"""
                ({update_clause})
                OR (
                    current_setting('app.wiki_review_write', true) = 'enabled'
                    AND kb_pages.snapshot_id = {_active("kb_pages")}
                )
            """
        op.execute(
            f"""
            CREATE POLICY snapshot_update_scope ON {table}
            AS RESTRICTIVE FOR UPDATE
            USING ({update_clause})
            WITH CHECK ({update_clause})
            """
        )
        op.execute(
            f"""
            CREATE POLICY snapshot_delete_scope ON {table}
            AS RESTRICTIVE FOR DELETE USING ({_gc_delete(table)})
            """
        )


def downgrade() -> None:
    for table in _PROJECTION_TABLES:
        for policy in (
            "snapshot_visibility",
            "snapshot_insert_scope",
            "snapshot_update_scope",
            "snapshot_delete_scope",
        ):
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
