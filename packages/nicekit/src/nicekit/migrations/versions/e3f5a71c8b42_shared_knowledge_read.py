"""还原已发布知识的三层可见性(本 org / 平台 org / 被分享)

Revision ID: e3f5a71c8b42
Revises: d7b1c94f2a30
Create Date: 2026-08-03

**这是一次功能还原,不是新设计。** 源项目对已发布的知识表用的是读写分离
策略:``org_read`` (FOR SELECT) 放行"本 org OR 平台 org OR 被显式分享",
``org_write`` (ALL) 只认本 org。SDK 化时统一改用单一的 ``org_isolation``
(ALL),把读侧的两个旁路弄丢了,后果是:

- 平台运营方无法提供公共知识库(平台 org 的知识对租户不可见);
- ``kb_shares`` 表、它的 CRUD 端点、purge 时的清理逻辑全都还在,却没有任何
  读路径能让被授权方真正读到——一整套跨租户分享是死的;
- ``kb/image_assets.py::visible_kb_filter`` 生成的三分支 OR、
  ``kb/search.py::_layer`` 的 tenant/platform/shared 分级与排序权重,后两支
  永远匹配不到行(``_layer`` 恒返回 ``tenant``)。

应用层**不需要任何改动**:检索链路里的 ``org_id ==`` 全是表间一致性 join
(``A.org_id == B.org_id``),租户过滤本来就交给 RLS。放宽读策略即生效。

**只放宽 SELECT。** 新策略与既有 ``org_isolation`` 是 permissive 叠加(同命令
多条 permissive 取 OR),写入面仍只受 ``org_isolation`` 约束:平台库与被分享
的库对租户是只读的,想改必须回到 owner org 的上下文。

**表清单的取舍**:只覆盖"已发布 / 只读"的知识面。草稿与中间态(待审事实
``fact_claims``、证据 ``evidence_spans``、摄入运行 ``ingest_runs``、发件箱)
一律不放宽——它们是 owner org 的内部工作台,共享出去既无意义也扩大暴露面。
``source_documents`` / ``document_revisions`` 必须在内:检索命中要 join 它们
取来源文件名与引用锚点,不放行会让平台库的命中缺少出处。
"""

from collections.abc import Sequence

from alembic import op

from nicekit.core.config import get_settings
from nicekit.migrations.rls import op_shared_knowledge_read

revision: str = "e3f5a71c8b42"
down_revision: str | None = "d7b1c94f2a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 带 kb_id、承载"已发布知识"的表。顺序无所谓,策略互不依赖。
_SHARED_READ_TABLES: tuple[str, ...] = (
    "knowledge_snapshots",
    "source_documents",
    "document_revisions",
    "kb_chunks",
    "kb_chunk_embeddings",
    "kb_entities",
    "kb_pages",
    "kb_graph_edges",
    "kb_image_assets",
    "kb_snapshot_image_assets",
    "snapshot_fact_supports",
    "snapshot_projection_supports",
    "kb_snapshot_entity_nodes",
    "kb_snapshot_entity_node_supports",
)

#: knowledge_bases 与 kb_entity_types 没有 kb_id 列(前者 kb_id 就是 id,
#: 后者是 org 级的类型注册表),单独写策略。
_KB_TABLE = "knowledge_bases"
_ENTITY_TYPES_TABLE = "kb_entity_types"

_POLICY = "shared_knowledge_read"


def upgrade() -> None:
    platform_org = get_settings().platform_org_id

    # 先让被授权方能读到"谁分享给了我"。这条是分享链路的前提:下面各表的
    # 分享分支是一个查 kb_shares 的子查询,而 RLS 策略里的子查询同样受被查表
    # 自己的 RLS 约束 —— kb_shares 原本只有 owner 可见的 org_isolation,
    # grantee 查出来恒为空,整条分享链路会静默失效(实测确认过)。
    # 只放 SELECT:能看见分享关系,但改/撤销仍然只有 owner 能做。
    op.execute(
        """
        CREATE POLICY grantee_read ON kb_shares FOR SELECT
        USING (
            grantee_org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        """
    )

    for table in _SHARED_READ_TABLES:
        op_shared_knowledge_read(op, table, platform_org)

    # knowledge_bases:分享关系挂在 id 上(它自己就是 kb)
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {_KB_TABLE} FOR SELECT
        USING (
            org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            OR org_id = '{platform_org}'::uuid
            OR EXISTS (
                SELECT 1 FROM kb_shares s
                 WHERE s.kb_id = {_KB_TABLE}.id
                   AND s.grantee_org_id
                       = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            )
        )
        """
    )

    # kb_entity_types:org 级注册表,没有 kb 归属,只放行平台层公共类型。
    # 租户能读平台定义的类型,才可能读懂平台库里那些实体的 attributes。
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {_ENTITY_TYPES_TABLE} FOR SELECT
        USING (
            org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            OR org_id = '{platform_org}'::uuid
        )
        """
    )


def downgrade() -> None:
    for table in (*_SHARED_READ_TABLES, _KB_TABLE, _ENTITY_TYPES_TABLE):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
    op.execute("DROP POLICY IF EXISTS grantee_read ON kb_shares")
