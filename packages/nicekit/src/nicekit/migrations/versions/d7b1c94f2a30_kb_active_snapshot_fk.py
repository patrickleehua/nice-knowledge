"""补建 knowledge_bases.active_snapshot_id 的复合外键

Revision ID: d7b1c94f2a30
Revises: cb3e134da690
Create Date: 2026-08-03

模型里这条 FK 用 ``use_alter=True`` 声明(``models/kb.py`` 的
``fk_knowledge_bases_active_snapshot``),用来打破 knowledge_bases ⇄
knowledge_snapshots 的循环引用。**但 ``use_alter`` 只对
``metadata.create_all()`` 的拓扑排序生效** —— Alembic 的 ``op.create_table()``
会静默跳过它,也不会补发 ``ALTER TABLE``。结果是库里这条约束根本不存在,
而 autogenerate 每次都重报它、被当成噪声人工忽略(cb3e134da690 的 docstring
就据此误判"该 FK 由 baseline 建过")。

这条约束不是可有可无的:它保证"一个知识库的 active 快照必须是它自己的快照",
否则可以把别的库的快照 ID 写进 active_snapshot_id,让检索读到跨库数据。
被引用侧的 ``uq_knowledge_snapshot_kb_id_id`` 就是为它准备的。

补建前先清理不满足约束的存量行(把悬空的 active_snapshot_id 置空),
否则 ALTER 会失败;开发库通常为空,生产库若有脏数据,这里的置空是安全的
(active 指针丢失的表现是"该库暂无发布快照",可重新发布,不丢知识)。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b1c94f2a30"
down_revision: str | None = "cb3e134da690"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_knowledge_bases_active_snapshot"


def upgrade() -> None:
    # 悬空指针置空:指向不存在的快照、或指向别的库的快照
    op.execute(
        sa.text(
            """
            UPDATE knowledge_bases kb
               SET active_snapshot_id = NULL
             WHERE kb.active_snapshot_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM knowledge_snapshots s
                    WHERE s.id = kb.active_snapshot_id AND s.kb_id = kb.id
               )
            """
        )
    )
    op.create_foreign_key(
        _FK_NAME,
        "knowledge_bases",
        "knowledge_snapshots",
        ["id", "active_snapshot_id"],
        ["kb_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "knowledge_bases", type_="foreignkey")
