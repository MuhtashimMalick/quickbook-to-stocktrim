"""sales_order_line and purchase_order entities for push_item / pushed_item (CHECK constraints widened; downgrade drops those rows)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

OLD = "entity IN ('supplier', 'product', 'customer')"
NEW = "entity IN ('supplier', 'product', 'customer', 'sales_order_line', 'purchase_order')"
OLD_ITEM = "entity IN ('supplier', 'product', 'bom_edge', 'customer')"
NEW_ITEM = "entity IN ('supplier', 'product', 'bom_edge', 'customer', 'sales_order_line', 'purchase_order')"
NEW_ENTITIES = "('sales_order_line', 'purchase_order')"


def _swap(table: str, name: str, check: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, check)


def upgrade() -> None:
    _swap("pushed_item", "ck_pushed_item_entity", NEW)
    _swap("push_item", "ck_push_item_entity", NEW_ITEM)


def downgrade() -> None:
    op.execute(f"DELETE FROM pushed_item WHERE entity IN {NEW_ENTITIES}")
    op.execute(f"DELETE FROM push_item WHERE entity IN {NEW_ENTITIES}")
    _swap("push_item", "ck_push_item_entity", OLD_ITEM)
    _swap("pushed_item", "ck_pushed_item_entity", OLD)
